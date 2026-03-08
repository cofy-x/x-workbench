from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._shared.media_core import (
    MAX_UPLOAD_BYTES,
    discover_workspace_root,
    format_command_display,
    has_executable,
    parse_multipart_upload,
    probe_video,
)


try:  # pragma: no cover - optional runtime dependency
    from faster_whisper import WhisperModel  # type: ignore

    HAS_WHISPER = True
except Exception:  # pragma: no cover - optional runtime dependency
    WhisperModel = None
    HAS_WHISPER = False


DEFAULT_LANG = "auto"
DEFAULT_MODEL = "small"
DEFAULT_STYLE = "clean"
DEFAULT_BURN_IN = True
DEFAULT_OUT_FORMAT = "mp4"

OUTPUT_FORMATS = ["mp4", "webm"]
STYLE_OPTIONS = ["clean", "boxed", "outline"]
MODEL_OPTIONS = ["tiny", "base", "small", "medium", "large-v3"]

STYLE_TO_ASS = {
    "clean": "FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=0",
    "boxed": "FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,BackColour=&H80000000,BorderStyle=3,Outline=0,Shadow=0,MarginV=24",
    "outline": "FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0",
}


WORKSPACE_ROOT = discover_workspace_root()
OUTPUT_ROOT = WORKSPACE_ROOT / "generated" / "subtitle_studio"
UPLOAD_DIR = OUTPUT_ROOT / "_uploads"
WEB_DIR = Path(__file__).with_name("web")
INDEX_FILE = WEB_DIR / "index.html"


@dataclass
class TranscribeRequest:
    input_file: str
    lang: str = DEFAULT_LANG
    model: str = DEFAULT_MODEL


@dataclass
class RenderRequest:
    input_file: str
    srt_relative_path: str
    srt_text: str = ""
    burn_in: bool = DEFAULT_BURN_IN
    style: str = DEFAULT_STYLE
    out_format: str = DEFAULT_OUT_FORMAT
    output_name: str = ""


def _send_json(handler: BaseHTTPRequestHandler, payload: dict, status: int = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours = total_ms // 3_600_000
    total_ms %= 3_600_000
    minutes = total_ms // 60_000
    total_ms %= 60_000
    secs = total_ms // 1_000
    ms = total_ms % 1_000
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def _write_srt(segments: List[Dict[str, Any]], output_path: Path) -> None:
    lines: List[str] = []
    for idx, segment in enumerate(segments, start=1):
        start = _srt_timestamp(float(segment["start"]))
        end = _srt_timestamp(float(segment["end"]))
        text = str(segment["text"]).strip() or "..."
        lines.extend([str(idx), f"{start} --> {end}", text, ""])
    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _safe_generated_path(relative_path: str) -> Path:
    target = (WORKSPACE_ROOT / relative_path).resolve()
    generated_root = OUTPUT_ROOT.resolve()
    if generated_root != target and generated_root not in target.parents:
        raise ValueError("Path is outside subtitle_studio generated directory")
    return target


def _parse_transcribe_payload(payload: dict) -> TranscribeRequest:
    input_file = str(payload.get("input_file", "")).strip()
    lang = str(payload.get("lang", DEFAULT_LANG)).strip() or DEFAULT_LANG
    model = str(payload.get("model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    if not input_file:
        raise ValueError("input_file is required")
    if lang == "":
        lang = DEFAULT_LANG
    return TranscribeRequest(input_file=input_file, lang=lang, model=model)


def _parse_render_payload(payload: dict) -> RenderRequest:
    input_file = str(payload.get("input_file", "")).strip()
    srt_relative_path = str(payload.get("srt_relative_path", "")).strip()
    srt_text = str(payload.get("srt_text", "")).strip()
    burn_in = bool(payload.get("burn_in", DEFAULT_BURN_IN))
    style = str(payload.get("style", DEFAULT_STYLE)).strip() or DEFAULT_STYLE
    out_format = str(payload.get("out_format", DEFAULT_OUT_FORMAT)).strip() or DEFAULT_OUT_FORMAT
    output_name = str(payload.get("output_name", "")).strip()

    if not input_file:
        raise ValueError("input_file is required")
    if not srt_relative_path:
        raise ValueError("srt_relative_path is required")
    if style not in STYLE_OPTIONS:
        raise ValueError(f"style must be one of: {', '.join(STYLE_OPTIONS)}")
    if out_format not in OUTPUT_FORMATS:
        raise ValueError(f"out_format must be one of: {', '.join(OUTPUT_FORMATS)}")
    return RenderRequest(
        input_file=input_file,
        srt_relative_path=srt_relative_path,
        srt_text=srt_text,
        burn_in=burn_in,
        style=style,
        out_format=out_format,
        output_name=output_name,
    )


def _normalize_segments(raw_segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for segment in raw_segments:
        start = float(segment["start"])
        end = float(segment["end"])
        if end <= start:
            continue
        text = re.sub(r"\s+", " ", str(segment["text"]).strip())
        if not text:
            continue
        cleaned.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return cleaned


def _transcribe_with_whisper(input_path: Path, lang: str, model: str) -> Dict[str, Any]:
    if not HAS_WHISPER or WhisperModel is None:
        raise RuntimeError(
            "faster-whisper is not installed. Install dependencies with `uv sync` before transcription."
        )

    language = None if lang in ("", "auto") else lang
    whisper_model = WhisperModel(model, device="auto", compute_type="int8")
    stream, info = whisper_model.transcribe(str(input_path), language=language)

    raw_segments: List[Dict[str, Any]] = []
    for segment in stream:
        raw_segments.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": str(segment.text).strip(),
            }
        )

    segments = _normalize_segments(raw_segments)
    if not segments:
        raise RuntimeError("No speech segments detected in input")

    detected_language = getattr(info, "language", None) or "unknown"
    return {
        "segments": segments,
        "detected_language": detected_language,
    }


def _build_render_command(
    input_path: Path,
    srt_path: Path,
    output_path: Path,
    burn_in: bool,
    style: str,
) -> List[str]:
    cmd: List[str] = ["ffmpeg", "-y", "-i", str(input_path)]

    if burn_in:
        escaped_srt = str(srt_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        force_style = STYLE_TO_ASS.get(style, STYLE_TO_ASS[DEFAULT_STYLE])
        vf = f"subtitles='{escaped_srt}':force_style='{force_style}'"
        cmd.extend(["-vf", vf])

    suffix = output_path.suffix.lower()
    if suffix == ".mp4":
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
            ]
        )
        cmd.extend(["-c:a", "aac", "-b:a", "128k"])
    elif suffix == ".webm":
        cmd.extend(["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-c:a", "libopus", "-b:a", "96k"])
    else:
        cmd.extend(["-c", "copy"])

    cmd.append(str(output_path))
    return cmd


def _run_transcribe(request: TranscribeRequest) -> Dict[str, Any]:
    input_path = UPLOAD_DIR / request.input_file
    if not input_path.is_file():
        raise ValueError(f"Input file not found: {request.input_file}")

    result = _transcribe_with_whisper(input_path, request.lang, request.model)
    job_id = uuid.uuid4().hex[:12]
    job_dir = OUTPUT_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    segments_path = job_dir / "segments.json"
    srt_path = job_dir / "captions.srt"
    segments_path.write_text(json.dumps(result["segments"], ensure_ascii=False, indent=2), encoding="utf-8")
    _write_srt(result["segments"], srt_path)

    return {
        "job_id": job_id,
        "input_file": request.input_file,
        "detected_language": result["detected_language"],
        "segment_count": len(result["segments"]),
        "segments": result["segments"],
        "srt_relative_path": str(srt_path.resolve().relative_to(WORKSPACE_ROOT)),
        "segments_relative_path": str(segments_path.resolve().relative_to(WORKSPACE_ROOT)),
    }


def _run_render(request: RenderRequest) -> Dict[str, Any]:
    if not has_executable("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH")

    input_path = UPLOAD_DIR / request.input_file
    if not input_path.is_file():
        raise ValueError(f"Input file not found: {request.input_file}")

    srt_path = _safe_generated_path(request.srt_relative_path)
    if not srt_path.is_file():
        raise ValueError(f"SRT file not found: {request.srt_relative_path}")
    if request.srt_text:
        srt_path.write_text(request.srt_text.strip() + "\n", encoding="utf-8")

    job_id = srt_path.parent.name
    output_dir = OUTPUT_ROOT / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = Path(request.input_file).stem
    if re.match(r"^[0-9a-f]{8}_", stem):
        stem = stem[9:]
    output_name = request.output_name or f"{stem}-subtitled.{request.out_format}"
    if not output_name.endswith(f".{request.out_format}"):
        output_name = f"{Path(output_name).stem}.{request.out_format}"

    output_path = output_dir / output_name

    cmd = _build_render_command(
        input_path=input_path,
        srt_path=srt_path,
        output_path=output_path,
        burn_in=request.burn_in,
        style=request.style,
    )

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        tail = proc.stderr[-1000:] if proc.stderr else "(no output)"
        raise RuntimeError(f"ffmpeg failed: {tail}")

    relative = output_path.resolve().relative_to(WORKSPACE_ROOT)
    return {
        "command": format_command_display(cmd),
        "output_file": output_path.name,
        "output_relative_path": str(relative),
        "output_url": relative.as_posix(),
        "output_size_bytes": output_path.stat().st_size if output_path.exists() else 0,
        "srt_relative_path": request.srt_relative_path,
    }


class SubtitleStudioHandler(BaseHTTPRequestHandler):
    server_version = "SubtitleStudioHTTP/1.0"

    def _serve_file(self, file_path: Path, content_type: str | None = None) -> None:
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        raw = file_path.read_bytes()
        guessed = content_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guessed)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def _serve_under_generated(self, path: str) -> None:
        relative = path.lstrip("/")
        target = (WORKSPACE_ROOT / relative).resolve()
        root = OUTPUT_ROOT.resolve()
        if root != target and root not in target.parents:
            self.send_error(HTTPStatus.FORBIDDEN, "Path outside subtitle_studio output")
            return
        self._serve_file(target)

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/index.html"):
            self._serve_file(INDEX_FILE, "text/html; charset=utf-8")
            return

        if path == "/api/options":
            _send_json(
                self,
                {
                    "ok": True,
                    "result": {
                        "defaults": {
                            "lang": DEFAULT_LANG,
                            "model": DEFAULT_MODEL,
                            "style": DEFAULT_STYLE,
                            "burn_in": DEFAULT_BURN_IN,
                            "out_format": DEFAULT_OUT_FORMAT,
                        },
                        "models": MODEL_OPTIONS,
                        "styles": STYLE_OPTIONS,
                        "output_formats": OUTPUT_FORMATS,
                        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
                        "faster_whisper_available": HAS_WHISPER,
                        "ffmpeg_available": has_executable("ffmpeg"),
                        "ffprobe_available": has_executable("ffprobe"),
                    },
                },
            )
            return

        if path.startswith("/generated/"):
            self._serve_under_generated(path)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        handler_map = {
            "/api/upload": self._handle_upload,
            "/api/transcribe": self._handle_transcribe,
            "/api/render": self._handle_render,
        }
        handler = handler_map.get(path)
        if handler:
            handler()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")

    def _handle_upload(self) -> None:
        try:
            filename, data = parse_multipart_upload(self.headers, self.rfile)
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"
            dest = UPLOAD_DIR / unique_name
            dest.write_bytes(data)

            info = {"uploaded_name": unique_name, "original_name": filename}
            try:
                info.update(probe_video(dest).to_dict())
            except Exception:
                pass
            _send_json(self, {"ok": True, "result": info})
        except ValueError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            _send_json(self, {"ok": False, "error": f"Upload failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_transcribe(self) -> None:
        try:
            payload = self._read_json_body()
            request = _parse_transcribe_payload(payload)
            result = _run_transcribe(request)
            _send_json(self, {"ok": True, "result": result})
        except json.JSONDecodeError:
            _send_json(self, {"ok": False, "error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:
            _send_json(self, {"ok": False, "error": f"Transcribe failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_render(self) -> None:
        try:
            payload = self._read_json_body()
            request = _parse_render_payload(payload)
            result = _run_render(request)
            _send_json(self, {"ok": True, "result": result})
        except json.JSONDecodeError:
            _send_json(self, {"ok": False, "error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:
            _send_json(self, {"ok": False, "error": f"Render failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def run_server(host: str, port: int) -> None:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"Missing static page: {INDEX_FILE}")

    server = ThreadingHTTPServer((host, port), SubtitleStudioHandler)
    print(f"Subtitle Studio server is running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


def run_process_once(args: argparse.Namespace) -> None:
    if not has_executable("ffmpeg"):
        print("ERROR: ffmpeg not found in PATH.")
        raise SystemExit(1)

    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f"ERROR: Input file not found: {args.input}")
        raise SystemExit(1)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_name = f"{uuid.uuid4().hex[:8]}_{input_path.name}"
    upload_path = UPLOAD_DIR / upload_name

    try:
        upload_path.symlink_to(input_path)
    except OSError:
        shutil.copy2(input_path, upload_path)

    try:
        transcribe_result = _run_transcribe(
            TranscribeRequest(input_file=upload_name, lang=args.lang, model=args.model)
        )
        print(f"Transcribed {transcribe_result['segment_count']} segments.")

        if args.export_srt:
            print(f"SRT: {transcribe_result['srt_relative_path']}")

        render_result = _run_render(
            RenderRequest(
                input_file=upload_name,
                srt_relative_path=transcribe_result["srt_relative_path"],
                burn_in=args.burn_in,
                style=args.style,
                out_format=args.out_format,
                output_name=args.output or "",
            )
        )
        print(f"Command:\n{render_result['command']}\n")
        print(f"Output: {render_result['output_relative_path']}")
        print(f"Size:   {render_result['output_size_bytes']:,} bytes")
    finally:
        upload_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Subtitle Studio for local transcription and subtitle rendering. "
            "By default it starts a local HTTP server with a web UI."
        )
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP server host")
    parser.add_argument("--port", default=8000, type=int, help="HTTP server port")
    parser.add_argument(
        "--process",
        action="store_true",
        help="Run in CLI mode instead of starting HTTP server",
    )
    parser.add_argument("-i", "--input", help="Input video path for CLI mode")
    parser.add_argument("-o", "--output", help="Output filename")
    parser.add_argument("--lang", default=DEFAULT_LANG, help="Language code or auto")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="faster-whisper model size")
    parser.add_argument("--style", default=DEFAULT_STYLE, choices=STYLE_OPTIONS, help="Subtitle style preset")
    parser.add_argument(
        "--burn-in",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_BURN_IN,
        help="Burn subtitles into video (default: enabled)",
    )
    parser.add_argument(
        "--out-format",
        default=DEFAULT_OUT_FORMAT,
        choices=OUTPUT_FORMATS,
        help="Output video format",
    )
    parser.add_argument(
        "--export-srt",
        action="store_true",
        help="Print generated SRT path in CLI mode",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.process:
        if not args.input:
            print("ERROR: --input (-i) is required in --process mode")
            raise SystemExit(1)
        run_process_once(args)
        return
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
