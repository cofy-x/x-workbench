from __future__ import annotations

import argparse
import json
import math
import mimetypes
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

from PIL import Image, ImageDraw, ImageFont, ImageOps

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._shared.media_core import (
    MAX_UPLOAD_BYTES,
    discover_workspace_root,
    has_executable,
    parse_multipart_upload,
    probe_video,
)


DEFAULT_MODE = "scene"
DEFAULT_SCENE_THRESHOLD = 0.30
DEFAULT_INTERVAL_SECONDS = 2.0
DEFAULT_COLS = 5
DEFAULT_MAX_FRAMES = 24
DEFAULT_THUMB_WIDTH = 280
FRAME_IMAGE_EXT = "png"

MODES = ["scene", "interval"]

WORKSPACE_ROOT = discover_workspace_root()
OUTPUT_ROOT = WORKSPACE_ROOT / "generated" / "storyboard_builder"
UPLOAD_DIR = OUTPUT_ROOT / "_uploads"
WEB_DIR = Path(__file__).with_name("web")
INDEX_FILE = WEB_DIR / "index.html"


@dataclass
class ExtractRequest:
    input_file: str
    mode: str = DEFAULT_MODE
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    max_frames: int = DEFAULT_MAX_FRAMES


@dataclass
class BuildRequest:
    job_id: str
    cols: int = DEFAULT_COLS
    thumb_width: int = DEFAULT_THUMB_WIDTH


def _send_json(handler: BaseHTTPRequestHandler, payload: dict, status: int = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _format_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02}:{m:02}:{s:06.3f}"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_extract_payload(payload: dict) -> ExtractRequest:
    request = ExtractRequest(
        input_file=str(payload.get("input_file", "")).strip(),
        mode=str(payload.get("mode", DEFAULT_MODE)).strip() or DEFAULT_MODE,
        scene_threshold=float(payload.get("scene_threshold", DEFAULT_SCENE_THRESHOLD)),
        interval_seconds=float(payload.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)),
        max_frames=int(payload.get("max_frames", DEFAULT_MAX_FRAMES)),
    )
    _validate_extract_request(request)
    return request


def _parse_build_payload(payload: dict) -> BuildRequest:
    request = BuildRequest(
        job_id=str(payload.get("job_id", "")).strip(),
        cols=int(payload.get("cols", DEFAULT_COLS)),
        thumb_width=int(payload.get("thumb_width", DEFAULT_THUMB_WIDTH)),
    )
    _validate_build_request(request)
    return request


def _validate_extract_request(request: ExtractRequest) -> None:
    if not request.input_file:
        raise ValueError("input_file is required")
    if request.mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    if request.scene_threshold <= 0 or request.scene_threshold >= 1:
        raise ValueError("scene_threshold must be in (0, 1)")
    if request.interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")
    if request.max_frames <= 0:
        raise ValueError("max_frames must be > 0")


def _validate_build_request(request: BuildRequest) -> None:
    if not request.job_id:
        raise ValueError("job_id is required")
    if request.cols <= 0:
        raise ValueError("cols must be > 0")
    if request.thumb_width < 120:
        raise ValueError("thumb_width must be >= 120")


def _parse_showinfo_timestamps(stderr: str) -> List[float]:
    times: List[float] = []
    pattern = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
    for line in stderr.splitlines():
        match = pattern.search(line)
        if match:
            times.append(float(match.group(1)))
    return times


def _relative_path(path: Path) -> str:
    return str(path.resolve().relative_to(WORKSPACE_ROOT))


def extract_frames(request: ExtractRequest) -> Dict[str, Any]:
    if not has_executable("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH")

    input_path = UPLOAD_DIR / request.input_file
    if not input_path.is_file():
        raise ValueError(f"Input file not found: {request.input_file}")

    job_id = uuid.uuid4().hex[:12]
    job_dir = OUTPUT_ROOT / job_id
    frames_dir = job_dir / "frames"
    job_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Use PNG for extracted frames to avoid MJPEG strict-compliance issues
    # on some MP4 color-range combinations.
    output_pattern = frames_dir / f"frame_%04d.{FRAME_IMAGE_EXT}"
    if request.mode == "scene":
        vf = f"select='gt(scene,{request.scene_threshold})',showinfo"
    else:
        vf = f"fps=1/{request.interval_seconds},showinfo"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        vf,
        "-vsync",
        "vfr",
        "-frames:v",
        str(request.max_frames),
        str(output_pattern),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        tail = proc.stderr[-1000:] if proc.stderr else "(no output)"
        raise RuntimeError(f"ffmpeg frame extraction failed: {tail}")

    frame_files = sorted(frames_dir.glob(f"frame_*.{FRAME_IMAGE_EXT}"))
    if not frame_files:
        if request.mode == "scene":
            raise RuntimeError(
                "No frames extracted in scene mode. Try lowering scene_threshold "
                "(for example 0.2-0.35), or switch to interval mode."
            )
        raise RuntimeError("No frames extracted")

    timestamps = _parse_showinfo_timestamps(proc.stderr)
    if len(timestamps) < len(frame_files):
        info = probe_video(input_path)
        duration = max(info.duration, 0.001)
        if request.mode == "interval":
            timestamps = [min(i * request.interval_seconds, duration) for i in range(len(frame_files))]
        else:
            step = duration / max(len(frame_files), 1)
            timestamps = [i * step for i in range(len(frame_files))]
    timestamps = timestamps[: len(frame_files)]

    items: List[Dict[str, Any]] = []
    for idx, frame in enumerate(frame_files):
        ts = timestamps[idx] if idx < len(timestamps) else 0.0
        items.append(
            {
                "index": idx + 1,
                "filename": frame.name,
                "timestamp": round(ts, 3),
                "timestamp_label": _format_time(ts),
                "relative_path": _relative_path(frame),
                "url": "/" + _relative_path(frame).replace("\\", "/"),
            }
        )

    timestamps_path = job_dir / "timestamps.json"
    _write_json(
        timestamps_path,
        {
            "job_id": job_id,
            "mode": request.mode,
            "source_input_file": request.input_file,
            "frame_count": len(items),
            "frames": items,
        },
    )

    return {
        "job_id": job_id,
        "mode": request.mode,
        "frame_count": len(items),
        "frames": items,
        "timestamps_relative_path": _relative_path(timestamps_path),
    }


def _load_default_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "Arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_contact_sheet(request: BuildRequest) -> Dict[str, Any]:
    job_dir = OUTPUT_ROOT / request.job_id
    timestamps_path = job_dir / "timestamps.json"
    frames_dir = job_dir / "frames"

    if not timestamps_path.is_file():
        raise ValueError(f"timestamps.json not found for job: {request.job_id}")
    if not frames_dir.is_dir():
        raise ValueError(f"frames directory not found for job: {request.job_id}")

    payload = _read_json(timestamps_path)
    frames = payload.get("frames", [])
    if not frames:
        raise ValueError("No frames metadata found")

    thumb_width = request.thumb_width
    thumb_height = int(thumb_width * 9 / 16)
    caption_height = 26
    rows = math.ceil(len(frames) / request.cols)
    canvas_width = request.cols * thumb_width
    canvas_height = rows * (thumb_height + caption_height)

    canvas = Image.new("RGB", (canvas_width, canvas_height), (245, 247, 250))
    draw = ImageDraw.Draw(canvas)
    font = _load_default_font(14)

    for idx, item in enumerate(frames):
        frame_path = WORKSPACE_ROOT / item["relative_path"]
        if not frame_path.is_file():
            continue

        row = idx // request.cols
        col = idx % request.cols
        x = col * thumb_width
        y = row * (thumb_height + caption_height)

        with Image.open(frame_path) as source:
            thumb = ImageOps.fit(source.convert("RGB"), (thumb_width, thumb_height), Image.Resampling.LANCZOS)
            canvas.paste(thumb, (x, y))

        label = f"#{item['index']}  {item['timestamp_label']}"
        draw.rectangle([x, y + thumb_height, x + thumb_width, y + thumb_height + caption_height], fill=(15, 23, 36))
        draw.text((x + 8, y + thumb_height + 5), label, fill=(240, 244, 255), font=font)

    contact_sheet_path = job_dir / "contact_sheet.jpg"
    canvas.save(contact_sheet_path, format="JPEG", quality=90)

    summary_lines = [
        f"# Storyboard Summary ({request.job_id})",
        "",
        f"- Frame count: {len(frames)}",
        f"- Columns: {request.cols}",
        f"- Thumbnail width: {thumb_width}",
        f"- Contact sheet: `{_relative_path(contact_sheet_path)}`",
        "",
        "## Timeline",
        "",
    ]
    for item in frames:
        summary_lines.append(f"- {item['index']:02d}. {item['timestamp_label']} -> `{item['relative_path']}`")

    summary_path = job_dir / "summary.md"
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    result = {
        "job_id": request.job_id,
        "contact_sheet_relative_path": _relative_path(contact_sheet_path),
        "contact_sheet_url": _relative_path(contact_sheet_path).replace("\\", "/"),
        "summary_relative_path": _relative_path(summary_path),
        "summary_url": _relative_path(summary_path).replace("\\", "/"),
        "frame_count": len(frames),
    }

    return result


class StoryboardHandler(BaseHTTPRequestHandler):
    server_version = "StoryboardBuilderHTTP/1.0"

    def _serve_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        raw = path.read_bytes()
        guessed = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guessed)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def _serve_generated(self, url_path: str) -> None:
        relative = url_path.lstrip("/")
        target = (WORKSPACE_ROOT / relative).resolve()
        root = OUTPUT_ROOT.resolve()
        if root != target and root not in target.parents:
            self.send_error(HTTPStatus.FORBIDDEN, "Path outside storyboard_builder output")
            return
        self._serve_file(target)

    def _read_json_body(self) -> dict:
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
                            "mode": DEFAULT_MODE,
                            "scene_threshold": DEFAULT_SCENE_THRESHOLD,
                            "interval_seconds": DEFAULT_INTERVAL_SECONDS,
                            "cols": DEFAULT_COLS,
                            "max_frames": DEFAULT_MAX_FRAMES,
                            "thumb_width": DEFAULT_THUMB_WIDTH,
                        },
                        "modes": MODES,
                        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
                        "ffmpeg_available": has_executable("ffmpeg"),
                        "ffprobe_available": has_executable("ffprobe"),
                    },
                },
            )
            return

        if path.startswith("/generated/"):
            self._serve_generated(path)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        handler_map = {
            "/api/upload": self._handle_upload,
            "/api/extract-frames": self._handle_extract,
            "/api/build-contact-sheet": self._handle_build,
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

            info: Dict[str, Any] = {"uploaded_name": unique_name, "original_name": filename}
            try:
                info.update(probe_video(dest).to_dict())
            except Exception:
                pass
            _send_json(self, {"ok": True, "result": info})
        except ValueError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            _send_json(self, {"ok": False, "error": f"Upload failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_extract(self) -> None:
        try:
            payload = self._read_json_body()
            request = _parse_extract_payload(payload)
            result = extract_frames(request)
            _send_json(self, {"ok": True, "result": result})
        except json.JSONDecodeError:
            _send_json(self, {"ok": False, "error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:
            _send_json(self, {"ok": False, "error": f"Extraction failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_build(self) -> None:
        try:
            payload = self._read_json_body()
            request = _parse_build_payload(payload)
            result = build_contact_sheet(request)
            _send_json(self, {"ok": True, "result": result})
        except json.JSONDecodeError:
            _send_json(self, {"ok": False, "error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            _send_json(self, {"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:
            _send_json(self, {"ok": False, "error": f"Build failed: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def run_server(host: str, port: int) -> None:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"Missing static page: {INDEX_FILE}")

    server = ThreadingHTTPServer((host, port), StoryboardHandler)
    print(f"Storyboard Builder server is running at http://{host}:{port}")
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

    try:
        extracted = extract_frames(
            ExtractRequest(
                input_file=upload_name,
                mode=args.mode,
                scene_threshold=args.scene_threshold,
                interval_seconds=args.interval_seconds,
                max_frames=args.max_frames,
            )
        )
        built = build_contact_sheet(
            BuildRequest(
                job_id=extracted["job_id"],
                cols=args.cols,
                thumb_width=args.thumb_width,
            )
        )

        print(f"Job: {extracted['job_id']}")
        print(f"Frames: {extracted['frame_count']}")
        print(f"Timestamps: {extracted['timestamps_relative_path']}")
        print(f"Contact sheet: {built['contact_sheet_relative_path']}")
        print(f"Summary: {built['summary_relative_path']}")
    finally:
        upload_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Storyboard Builder for extracting key frames and building contact sheets. "
            "By default it starts a local HTTP server with a web UI."
        )
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP server host")
    parser.add_argument("--port", default=8000, type=int, help="HTTP server port")
    parser.add_argument("--process", action="store_true", help="Run CLI mode")
    parser.add_argument("-i", "--input", help="Input video path in CLI mode")
    parser.add_argument("--mode", default=DEFAULT_MODE, choices=MODES, help="Frame extraction mode")
    parser.add_argument("--scene-threshold", type=float, default=DEFAULT_SCENE_THRESHOLD, help="Scene mode threshold")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS, help="Interval mode seconds")
    parser.add_argument("--cols", type=int, default=DEFAULT_COLS, help="Contact sheet columns")
    parser.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES, help="Maximum extracted frames")
    parser.add_argument("--thumb-width", type=int, default=DEFAULT_THUMB_WIDTH, help="Thumbnail width")
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
