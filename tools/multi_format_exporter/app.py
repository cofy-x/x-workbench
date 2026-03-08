from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
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
    RangeCut,
    cuts_to_keep_ranges,
    discover_workspace_root,
    format_command_display,
    has_executable,
    parse_multipart_upload,
    parse_time,
    probe_video,
)


PRESETS: Dict[str, Dict[str, int]] = {
    "tiktok": {"width": 1080, "height": 1920},
    "youtube": {"width": 1920, "height": 1080},
    "square": {"width": 1080, "height": 1080},
}

FIT_MODES = ["crop_center", "pad"]
ENCODING_PRESETS = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
]

DEFAULT_FIT_MODE = "crop_center"
DEFAULT_SPEED = 1.0
DEFAULT_CRF = 23
DEFAULT_ENCODE_PRESET = "medium"
DEFAULT_REMOVE_AUDIO = True

WORKSPACE_ROOT = discover_workspace_root()
OUTPUT_ROOT = WORKSPACE_ROOT / "generated" / "multi_format_exporter"
UPLOAD_DIR = OUTPUT_ROOT / "_uploads"
WEB_DIR = Path(__file__).with_name("web")
INDEX_FILE = WEB_DIR / "index.html"


@dataclass
class BatchRequest:
    input_file: str
    presets: List[str] = field(default_factory=lambda: ["tiktok", "youtube", "square"])
    fit_mode: str = DEFAULT_FIT_MODE
    speed: float = DEFAULT_SPEED
    cuts: List[RangeCut] = field(default_factory=list)
    trim_start: str = ""
    trim_end: str = ""
    remove_audio: bool = DEFAULT_REMOVE_AUDIO
    crf: int = DEFAULT_CRF
    encode_preset: str = DEFAULT_ENCODE_PRESET


def _send_json(handler: BaseHTTPRequestHandler, payload: dict, status: int = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _parse_cuts(raw: Any) -> List[RangeCut]:
    if not raw or not isinstance(raw, list):
        return []
    cuts: List[RangeCut] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            start = parse_time(str(item[0]))
            end = parse_time(str(item[1]))
            if end > start:
                cuts.append(RangeCut(start, end))
    return cuts


def _parse_payload(payload: dict) -> BatchRequest:
    input_file = str(payload.get("input_file", "")).strip()
    presets = payload.get("presets")
    if isinstance(presets, list):
        selected = [str(item).strip() for item in presets if str(item).strip() in PRESETS]
    else:
        selected = ["tiktok", "youtube", "square"]

    request = BatchRequest(
        input_file=input_file,
        presets=selected or ["tiktok", "youtube", "square"],
        fit_mode=str(payload.get("fit_mode", DEFAULT_FIT_MODE)).strip() or DEFAULT_FIT_MODE,
        speed=float(payload.get("speed", DEFAULT_SPEED)),
        cuts=_parse_cuts(payload.get("cuts")),
        trim_start=str(payload.get("trim_start", "")).strip(),
        trim_end=str(payload.get("trim_end", "")).strip(),
        remove_audio=bool(payload.get("remove_audio", DEFAULT_REMOVE_AUDIO)),
        crf=int(payload.get("crf", DEFAULT_CRF)),
        encode_preset=str(payload.get("encode_preset", DEFAULT_ENCODE_PRESET)).strip() or DEFAULT_ENCODE_PRESET,
    )
    _validate_request(request)
    return request


def _validate_request(request: BatchRequest) -> None:
    if not request.input_file:
        raise ValueError("input_file is required")
    if request.fit_mode not in FIT_MODES:
        raise ValueError(f"fit_mode must be one of: {', '.join(FIT_MODES)}")
    if request.speed <= 0 or request.speed > 100:
        raise ValueError("speed must be between 0.01 and 100")
    if request.crf < 0 or request.crf > 51:
        raise ValueError("crf must be between 0 and 51")
    if request.encode_preset not in ENCODING_PRESETS:
        raise ValueError(f"encode_preset must be one of: {', '.join(ENCODING_PRESETS)}")
    if not request.presets:
        raise ValueError("At least one preset is required")
    for preset in request.presets:
        if preset not in PRESETS:
            raise ValueError(f"Unknown preset: {preset}")


def _fit_filter(width: int, height: int, fit_mode: str) -> str:
    aspect = width / height
    if fit_mode == "crop_center":
        return (
            f"scale='if(gt(a,{aspect:.6f}),-2,{width})':'if(gt(a,{aspect:.6f}),{height},-2)',"
            f"crop={width}:{height}"
        )
    return (
        f"scale='if(gt(a,{aspect:.6f}),{width},-2)':'if(gt(a,{aspect:.6f}),-2,{height})',"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )


def _build_atempo_chain(speed: float) -> List[str]:
    parts: List[str] = []
    s = speed
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    while s > 100.0:
        parts.append("atempo=100.0")
        s /= 100.0
    if abs(s - 1.0) > 0.001:
        parts.append(f"atempo={s:.6g}")
    return parts


def _build_command(
    request: BatchRequest,
    input_path: Path,
    output_path: Path,
    width: int,
    height: int,
    keep_ranges: List[RangeCut] | None,
) -> List[str]:
    fit = _fit_filter(width, height, request.fit_mode)

    if keep_ranges:
        cmd: List[str] = ["ffmpeg", "-y", "-i", str(input_path)]
        fc_parts: List[str] = []
        vlabels: List[str] = []
        alabels: List[str] = []
        has_audio_path = not request.remove_audio

        for idx, keep in enumerate(keep_ranges):
            vl = f"v{idx}"
            fc_parts.append(f"[0:v]trim={keep.start}:{keep.end},setpts=PTS-STARTPTS[{vl}]")
            vlabels.append(f"[{vl}]")
            if has_audio_path:
                al = f"a{idx}"
                fc_parts.append(f"[0:a]atrim={keep.start}:{keep.end},asetpts=PTS-STARTPTS[{al}]")
                alabels.append(f"[{al}]")

        n = len(keep_ranges)
        fc_parts.append(f"{''.join(vlabels)}concat=n={n}:v=1:a=0[vjoined]")
        if has_audio_path:
            fc_parts.append(f"{''.join(alabels)}concat=n={n}:v=0:a=1[ajoined]")

        post_v: List[str] = []
        if request.speed != 1.0:
            post_v.append(f"setpts=PTS/{request.speed}")
        post_v.append(fit)
        fc_parts.append(f"[vjoined]{','.join(post_v)}[vout]")
        if has_audio_path:
            if request.speed != 1.0:
                atempo = _build_atempo_chain(request.speed)
                if atempo:
                    fc_parts.append(f"[ajoined]{','.join(atempo)}[aout]")
                    amap = "[aout]"
                else:
                    amap = "[ajoined]"
            else:
                amap = "[ajoined]"
        else:
            amap = None

        cmd.extend(["-filter_complex", ";\n".join(fc_parts), "-map", "[vout]"])
        if amap:
            cmd.extend(["-map", amap])
        else:
            cmd.append("-an")

        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-crf",
                str(request.crf),
                "-preset",
                request.encode_preset,
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
            ]
        )
        if not request.remove_audio:
            cmd.extend(["-c:a", "aac", "-b:a", "128k"])

        cmd.append(str(output_path))
        return cmd

    cmd = ["ffmpeg", "-y"]
    if request.trim_start:
        cmd.extend(["-ss", request.trim_start])
    if request.trim_end:
        cmd.extend(["-to", request.trim_end])
    cmd.extend(["-i", str(input_path)])

    vfilters: List[str] = []
    if request.speed != 1.0:
        vfilters.append(f"setpts=PTS/{request.speed}")
    vfilters.append(fit)
    cmd.extend(["-filter:v", ",".join(vfilters)])

    if request.remove_audio:
        cmd.append("-an")
    elif request.speed != 1.0:
        atempo = _build_atempo_chain(request.speed)
        if atempo:
            cmd.extend(["-filter:a", ",".join(atempo)])

    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-crf",
            str(request.crf),
            "-preset",
            request.encode_preset,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
    )
    if not request.remove_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "128k"])

    cmd.append(str(output_path))
    return cmd


def _resolve_keep_ranges(request: BatchRequest, input_path: Path) -> List[RangeCut] | None:
    if not request.cuts:
        return None
    info = probe_video(input_path)
    total = info.duration
    if total <= 0:
        raise ValueError("Cannot determine duration for cut computation")
    trim_start = parse_time(request.trim_start) if request.trim_start else 0
    trim_end = parse_time(request.trim_end) if request.trim_end else total
    keep = cuts_to_keep_ranges(request.cuts, total, trim_start, trim_end)
    if not keep:
        raise ValueError("No content remaining after cuts")
    return keep


def _build_batch(request: BatchRequest) -> Dict[str, Any]:
    input_path = UPLOAD_DIR / request.input_file
    if not input_path.is_file():
        raise ValueError(f"Input file not found: {request.input_file}")
    if not has_executable("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH")

    keep_ranges = _resolve_keep_ranges(request, input_path)
    job_id = uuid.uuid4().hex[:12]
    job_dir = OUTPUT_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    stem = Path(request.input_file).stem
    if re.match(r"^[0-9a-f]{8}_", stem):
        stem = stem[9:]

    tasks: List[Dict[str, Any]] = []
    for preset in request.presets:
        spec = PRESETS[preset]
        output_name = f"{preset}.mp4"
        output_path = job_dir / output_name
        cmd = _build_command(
            request=request,
            input_path=input_path,
            output_path=output_path,
            width=spec["width"],
            height=spec["height"],
            keep_ranges=keep_ranges,
        )
        tasks.append(
            {
                "preset": preset,
                "width": spec["width"],
                "height": spec["height"],
                "output_file": output_name,
                "output_path": output_path,
                "command": cmd,
                "command_display": format_command_display(cmd),
            }
        )

    return {
        "job_id": job_id,
        "input_stem": stem,
        "job_dir": job_dir,
        "tasks": tasks,
    }


def preview_batch_commands(request: BatchRequest) -> Dict[str, Any]:
    built = _build_batch(request)
    previews = []
    for task in built["tasks"]:
        previews.append(
            {
                "preset": task["preset"],
                "output_file": task["output_file"],
                "command": task["command_display"],
            }
        )
    return {"job_id": built["job_id"], "commands": previews}


def process_batch(request: BatchRequest) -> Dict[str, Any]:
    built = _build_batch(request)
    outputs: List[Dict[str, Any]] = []

    for task in built["tasks"]:
        cmd = task["command"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            tail = proc.stderr[-1000:] if proc.stderr else "(no output)"
            raise RuntimeError(f"ffmpeg failed for {task['preset']}: {tail}")

        output_path: Path = task["output_path"]
        relative = output_path.resolve().relative_to(WORKSPACE_ROOT)
        outputs.append(
            {
                "preset": task["preset"],
                "width": task["width"],
                "height": task["height"],
                "output_file": task["output_file"],
                "output_relative_path": str(relative),
                "output_url": relative.as_posix(),
                "output_size_bytes": output_path.stat().st_size if output_path.exists() else 0,
                "command": task["command_display"],
            }
        )

    manifest = {
        "job_id": built["job_id"],
        "input_file": request.input_file,
        "fit_mode": request.fit_mode,
        "speed": request.speed,
        "remove_audio": request.remove_audio,
        "outputs": outputs,
    }
    manifest_path = built["job_dir"] / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["manifest_relative_path"] = str(manifest_path.resolve().relative_to(WORKSPACE_ROOT))
    return manifest


class MultiFormatHandler(BaseHTTPRequestHandler):
    server_version = "MultiFormatExporterHTTP/1.0"

    def _serve_file(self, file_path: Path, content_type: str | None = None) -> None:
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        file_size = file_path.stat().st_size
        guessed = content_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

        range_hdr = self.headers.get("Range")
        if range_hdr and range_hdr.startswith("bytes="):
            spec = range_hdr[6:]
            parts = spec.split("-", 1)
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
            if start >= file_size:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            end = min(end, file_size - 1)
            length = end - start + 1

            with open(file_path, "rb") as fh:
                fh.seek(start)
                data = fh.read(length)

            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", guessed)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (ConnectionResetError, BrokenPipeError):
                pass
        else:
            raw = file_path.read_bytes()
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
            self.send_error(HTTPStatus.FORBIDDEN, "Path outside multi_format_exporter output")
            return
        self._serve_file(target)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length > 0 else b"{}"
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
                            "fit_mode": DEFAULT_FIT_MODE,
                            "speed": DEFAULT_SPEED,
                            "crf": DEFAULT_CRF,
                            "encode_preset": DEFAULT_ENCODE_PRESET,
                            "remove_audio": DEFAULT_REMOVE_AUDIO,
                            "presets": ["tiktok", "youtube", "square"],
                        },
                        "presets": PRESETS,
                        "fit_modes": FIT_MODES,
                        "encoding_presets": ENCODING_PRESETS,
                        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
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
            "/api/preview-batch-command": self._handle_preview,
            "/api/process-batch": self._handle_process,
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

            relative = dest.resolve().relative_to(WORKSPACE_ROOT)
            info: Dict[str, Any] = {
                "uploaded_name": unique_name,
                "original_name": filename,
                "upload_relative_path": str(relative),
                "upload_url": relative.as_posix(),
            }
            try:
                info.update(probe_video(dest).to_dict())
            except Exception:
                pass
            _send_json(self, {"ok": True, "result": info})
        except ValueError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            _send_json(self, {"ok": False, "error": f"Upload failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_preview(self) -> None:
        try:
            payload = self._read_json()
            request = _parse_payload(payload)
            result = preview_batch_commands(request)
            _send_json(self, {"ok": True, "result": result})
        except json.JSONDecodeError:
            _send_json(self, {"ok": False, "error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:
            _send_json(self, {"ok": False, "error": f"Preview failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_process(self) -> None:
        try:
            payload = self._read_json()
            request = _parse_payload(payload)
            result = process_batch(request)
            _send_json(self, {"ok": True, "result": result})
        except json.JSONDecodeError:
            _send_json(self, {"ok": False, "error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:
            _send_json(self, {"ok": False, "error": f"Processing failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def run_server(host: str, port: int) -> None:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"Missing static page: {INDEX_FILE}")

    server = ThreadingHTTPServer((host, port), MultiFormatHandler)
    print(f"Multi Format Exporter server is running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


def run_process_once(args: argparse.Namespace) -> None:
    if not has_executable("ffmpeg"):
        print("ERROR: ffmpeg not found in PATH")
        raise SystemExit(1)

    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f"ERROR: input file not found: {args.input}")
        raise SystemExit(1)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_name = f"{uuid.uuid4().hex[:8]}_{input_path.name}"
    upload_path = UPLOAD_DIR / upload_name

    try:
        upload_path.symlink_to(input_path)
    except OSError:
        shutil.copy2(input_path, upload_path)

    cuts: List[RangeCut] = []
    for raw in args.cut or []:
        parts = raw.split("-", 1)
        if len(parts) == 2:
            start = parse_time(parts[0])
            end = parse_time(parts[1])
            if end > start:
                cuts.append(RangeCut(start, end))

    request = BatchRequest(
        input_file=upload_name,
        presets=args.preset or ["tiktok", "youtube", "square"],
        fit_mode=args.fit_mode,
        speed=args.speed,
        cuts=cuts,
        trim_start=args.trim_start or "",
        trim_end=args.trim_end or "",
        remove_audio=not args.keep_audio,
        crf=args.crf,
        encode_preset=args.encode_preset,
    )

    try:
        result = process_batch(request)
        print(f"Job: {result['job_id']}")
        for item in result["outputs"]:
            print(f"- {item['preset']}: {item['output_relative_path']} ({item['output_size_bytes']:,} bytes)")
    finally:
        upload_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Batch export one source video to multiple social/media formats. "
            "By default it starts a local HTTP server with a web UI."
        )
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP server host")
    parser.add_argument("--port", default=8000, type=int, help="HTTP server port")
    parser.add_argument("--process", action="store_true", help="Run CLI batch export mode")
    parser.add_argument("-i", "--input", help="Input video path (CLI mode)")
    parser.add_argument("--preset", action="append", choices=sorted(PRESETS.keys()), help="Export preset, repeatable")
    parser.add_argument("--fit-mode", default=DEFAULT_FIT_MODE, choices=FIT_MODES, help="Fit strategy")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="Speed multiplier")
    parser.add_argument("--cut", action="append", metavar="START-END", help="Cut a time range, repeatable")
    parser.add_argument("--trim-start", default=None, help="Trim start time")
    parser.add_argument("--trim-end", default=None, help="Trim end time")
    parser.add_argument("--keep-audio", action="store_true", help="Keep audio track")
    parser.add_argument("--crf", type=int, default=DEFAULT_CRF, help="CRF value (0-51)")
    parser.add_argument(
        "--encode-preset",
        default=DEFAULT_ENCODE_PRESET,
        choices=ENCODING_PRESETS,
        help="x264 encoding preset",
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
