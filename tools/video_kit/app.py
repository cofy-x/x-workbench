from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_SPEED = 1.0
DEFAULT_CRF = 23
DEFAULT_PRESET = "medium"
DEFAULT_OUTPUT_FORMAT = "mp4"
DEFAULT_REMOVE_AUDIO = True
DEFAULT_SCALE = "original"
DEFAULT_GIF_FPS = 15
DEFAULT_GIF_WIDTH = 640

MAX_UPLOAD_BYTES = 500 * 1024 * 1024

SPEED_PRESETS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.67, 3.0, 4.0]
SCALE_OPTIONS = ["original", "1080p", "720p", "480p", "50%", "25%"]
OUTPUT_FORMATS = ["mp4", "webm", "gif"]
ENCODING_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def discover_workspace_root() -> Path:
    env_root = os.getenv("TOOLS_WORKSPACE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    script_dir = Path(__file__).resolve().parent
    for candidate in [script_dir, *script_dir.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd().resolve()


WORKSPACE_ROOT = discover_workspace_root()
OUTPUT_ROOT = WORKSPACE_ROOT / "generated" / "video_kit"
UPLOAD_DIR = OUTPUT_ROOT / "_uploads"
WEB_DIR = Path(__file__).with_name("web")
INDEX_FILE = WEB_DIR / "index.html"


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------
def _parse_time(t: str) -> float:
    """Parse a time string to seconds.  Accepts 5.0, 0:05, 1:30, 1:05:30."""
    t = t.strip()
    if not t:
        return 0.0
    parts = t.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Invalid time format: {t}")


def _cuts_to_keep_ranges(
    cuts: List[List[float]],
    total_duration: float,
    trim_start: float = 0,
    trim_end: float = 0,
) -> List[List[float]]:
    """Convert cut ranges to keep ranges within [trim_start, trim_end]."""
    start = trim_start if trim_start > 0 else 0
    end = trim_end if trim_end > 0 else total_duration
    if end <= start:
        return []

    sorted_cuts = sorted(cuts, key=lambda c: c[0])
    merged: List[List[float]] = []
    for cs, ce in sorted_cuts:
        cs, ce = max(cs, start), min(ce, end)
        if cs >= ce:
            continue
        if merged and cs <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], ce)
        else:
            merged.append([cs, ce])

    keep: List[List[float]] = []
    pos = start
    for cs, ce in merged:
        if pos < cs:
            keep.append([pos, cs])
        pos = ce
    if pos < end:
        keep.append([pos, end])
    return keep


# ---------------------------------------------------------------------------
# External tool helpers
# ---------------------------------------------------------------------------
def _has_executable(name: str) -> bool:
    return shutil.which(name) is not None


def probe_video(file_path: Path) -> Dict[str, Any]:
    """Return video metadata via ffprobe."""
    if not _has_executable("ffprobe"):
        raise RuntimeError("ffprobe not found in PATH")

    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(file_path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    audio_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None
    )
    fmt = data.get("format", {})

    info: Dict[str, Any] = {
        "filename": file_path.name,
        "size_bytes": int(fmt.get("size", 0)),
        "duration": float(fmt.get("duration", 0)),
        "format_name": fmt.get("format_name", ""),
    }
    if video_stream:
        info["width"] = int(video_stream.get("width", 0))
        info["height"] = int(video_stream.get("height", 0))
        info["codec"] = video_stream.get("codec_name", "")
        fps_parts = video_stream.get("r_frame_rate", "0/1").split("/")
        if len(fps_parts) == 2 and int(fps_parts[1]) > 0:
            info["fps"] = round(int(fps_parts[0]) / int(fps_parts[1]), 2)
        else:
            info["fps"] = 0
    info["has_audio"] = audio_stream is not None
    return info


# ---------------------------------------------------------------------------
# Processing request / command builder
# ---------------------------------------------------------------------------
@dataclass
class ProcessingRequest:
    input_file: str = ""
    output_format: str = DEFAULT_OUTPUT_FORMAT
    speed: float = DEFAULT_SPEED
    trim_start: str = ""
    trim_end: str = ""
    remove_audio: bool = DEFAULT_REMOVE_AUDIO
    crf: int = DEFAULT_CRF
    preset: str = DEFAULT_PRESET
    scale: str = DEFAULT_SCALE
    gif_fps: int = DEFAULT_GIF_FPS
    gif_width: int = DEFAULT_GIF_WIDTH
    output_name: str = ""
    cuts: List[List[float]] = field(default_factory=list)
    target_duration: float = 0


def _resolve_scale_filter(scale: str) -> str | None:
    mapping = {
        "1080p": "scale=-2:1080",
        "720p": "scale=-2:720",
        "480p": "scale=-2:480",
        "50%": "scale=trunc(iw/4)*2:trunc(ih/4)*2",
        "25%": "scale=trunc(iw/8)*2:trunc(ih/8)*2",
    }
    return mapping.get(scale)


def _build_atempo_chain(speed: float) -> List[str]:
    """Build atempo filter parts; each value must be in [0.5, 100.0]."""
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


def _input_args(request: ProcessingRequest, input_path: Path) -> List[str]:
    args: List[str] = []
    if request.trim_start:
        args.extend(["-ss", request.trim_start])
    if request.trim_end:
        args.extend(["-to", request.trim_end])
    args.extend(["-i", str(input_path)])
    return args


def build_ffmpeg_command(
    request: ProcessingRequest,
    output_path: Path,
    keep_ranges: List[List[float]] | None = None,
) -> List[str] | List[List[str]]:
    """
    Return a flat command list for mp4/webm, or a list of two command lists
    for GIF (palette-based two-pass).  When *keep_ranges* is provided the
    filter_complex trim+concat path is used instead of simple filters.
    """
    input_path = UPLOAD_DIR / request.input_file

    if keep_ranges:
        if request.output_format == "gif":
            return _build_cuts_gif_commands(
                request, input_path, output_path, keep_ranges,
            )
        return _build_cuts_command(request, input_path, output_path, keep_ranges)

    if request.output_format == "gif":
        return _build_gif_commands(request, input_path, output_path)

    cmd: List[str] = ["ffmpeg", "-y"]
    cmd.extend(_input_args(request, input_path))

    vfilters: List[str] = []
    if request.speed != 1.0:
        vfilters.append(f"setpts=PTS/{request.speed}")
    scale = _resolve_scale_filter(request.scale)
    if scale:
        vfilters.append(scale)
    if vfilters:
        cmd.extend(["-filter:v", ",".join(vfilters)])

    if request.output_format == "mp4":
        cmd.extend([
            "-c:v", "libx264", "-crf", str(request.crf),
            "-preset", request.preset,
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        ])
    elif request.output_format == "webm":
        cmd.extend(["-c:v", "libvpx-vp9", "-crf", str(request.crf), "-b:v", "0"])

    if request.remove_audio:
        cmd.append("-an")
    elif request.speed != 1.0:
        atempo = _build_atempo_chain(request.speed)
        if atempo:
            cmd.extend(["-filter:a", ",".join(atempo)])

    cmd.append(str(output_path))
    return cmd


# -- filter_complex path for multi-segment cuts --

def _post_filters(request: ProcessingRequest) -> List[str]:
    """Filters applied after concat (speed + scale)."""
    parts: List[str] = []
    if request.speed != 1.0:
        parts.append(f"setpts=PTS/{request.speed}")
    scale = _resolve_scale_filter(request.scale)
    if scale:
        parts.append(scale)
    return parts


def _build_cuts_command(
    request: ProcessingRequest,
    input_path: Path,
    output_path: Path,
    keep_ranges: List[List[float]],
) -> List[str]:
    cmd: List[str] = ["ffmpeg", "-y", "-i", str(input_path)]

    fc_parts: List[str] = []
    vlabels: List[str] = []
    alabels: List[str] = []
    has_audio_path = not request.remove_audio

    for i, (ks, ke) in enumerate(keep_ranges):
        vl = f"v{i}"
        fc_parts.append(f"[0:v]trim={ks}:{ke},setpts=PTS-STARTPTS[{vl}]")
        vlabels.append(f"[{vl}]")
        if has_audio_path:
            al = f"a{i}"
            fc_parts.append(f"[0:a]atrim={ks}:{ke},asetpts=PTS-STARTPTS[{al}]")
            alabels.append(f"[{al}]")

    n = len(keep_ranges)
    if has_audio_path:
        fc_parts.append(
            f"{''.join(vlabels)}concat=n={n}:v=1:a=0[vjoined]"
        )
        fc_parts.append(
            f"{''.join(alabels)}concat=n={n}:v=0:a=1[ajoined]"
        )
    else:
        fc_parts.append(
            f"{''.join(vlabels)}concat=n={n}:v=1:a=0[vjoined]"
        )

    pf = _post_filters(request)
    if pf:
        fc_parts.append(f"[vjoined]{','.join(pf)}[vout]")
        vmap = "[vout]"
    else:
        vmap = "[vjoined]"

    if has_audio_path and request.speed != 1.0:
        atempo = _build_atempo_chain(request.speed)
        if atempo:
            fc_parts.append(f"[ajoined]{','.join(atempo)}[aout]")
            amap: str | None = "[aout]"
        else:
            amap = "[ajoined]"
    elif has_audio_path:
        amap = "[ajoined]"
    else:
        amap = None

    fc_string = ";\n".join(fc_parts)
    cmd.extend(["-filter_complex", fc_string, "-map", vmap])
    if amap:
        cmd.extend(["-map", amap])

    if request.output_format == "mp4":
        cmd.extend([
            "-c:v", "libx264", "-crf", str(request.crf),
            "-preset", request.preset,
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        ])
    elif request.output_format == "webm":
        cmd.extend(["-c:v", "libvpx-vp9", "-crf", str(request.crf), "-b:v", "0"])

    if not has_audio_path:
        cmd.append("-an")

    cmd.append(str(output_path))
    return cmd


def _build_cuts_gif_commands(
    request: ProcessingRequest,
    input_path: Path,
    output_path: Path,
    keep_ranges: List[List[float]],
) -> List[List[str]]:
    palette_path = output_path.with_name(output_path.stem + "_palette.png")
    n = len(keep_ranges)

    trim_lines: List[str] = []
    vlabels: List[str] = []
    for i, (ks, ke) in enumerate(keep_ranges):
        vl = f"v{i}"
        trim_lines.append(f"[0:v]trim={ks}:{ke},setpts=PTS-STARTPTS[{vl}]")
        vlabels.append(f"[{vl}]")
    concat_line = f"{''.join(vlabels)}concat=n={n}:v=1:a=0[joined]"

    gif_filters: List[str] = []
    if request.speed != 1.0:
        gif_filters.append(f"setpts=PTS/{request.speed}")
    gif_filters.append(f"fps={request.gif_fps}")
    gif_filters.append(f"scale={request.gif_width}:-1:flags=lanczos")
    gif_chain = ",".join(gif_filters)

    fc1_parts = trim_lines + [
        concat_line,
        f"[joined]{gif_chain},palettegen[pal]",
    ]
    cmd1 = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-filter_complex", ";\n".join(fc1_parts),
        "-map", "[pal]", "-an", str(palette_path),
    ]

    fc2_parts = trim_lines + [
        concat_line,
        f"[joined]{gif_chain}[x];[x][1:v]paletteuse[out]",
    ]
    cmd2 = [
        "ffmpeg", "-y", "-i", str(input_path), "-i", str(palette_path),
        "-filter_complex", ";\n".join(fc2_parts),
        "-map", "[out]", "-an", str(output_path),
    ]
    return [cmd1, cmd2]


# -- simple path (no cuts) --

def _build_gif_commands(
    request: ProcessingRequest, input_path: Path, output_path: Path,
) -> List[List[str]]:
    palette_path = output_path.with_name(output_path.stem + "_palette.png")
    common_in = _input_args(request, input_path)

    filters: List[str] = []
    if request.speed != 1.0:
        filters.append(f"setpts=PTS/{request.speed}")
    filters.append(f"fps={request.gif_fps}")
    filters.append(f"scale={request.gif_width}:-1:flags=lanczos")
    chain = ",".join(filters)

    cmd1 = ["ffmpeg", "-y"] + common_in + [
        "-vf", f"{chain},palettegen", "-an", str(palette_path),
    ]
    cmd2 = ["ffmpeg", "-y"] + common_in + [
        "-i", str(palette_path),
        "-filter_complex", f"[0:v]{chain}[x];[x][1:v]paletteuse",
        "-an", str(output_path),
    ]
    return [cmd1, cmd2]


# ---------------------------------------------------------------------------
# Command display helpers
# ---------------------------------------------------------------------------
def _shell_quote(s: str) -> str:
    if any(c in s for c in " ;()[]{}$|&<>*?'\"\\"):
        return f'"{s}"'
    return s


def _format_single_cmd(cmd: List[str]) -> str:
    if len(cmd) <= 4:
        return " ".join(_shell_quote(a) for a in cmd)
    lines = [_shell_quote(cmd[0])]
    i = 1
    while i < len(cmd):
        token = cmd[i]
        if (
            token.startswith("-")
            and i + 1 < len(cmd)
            and not cmd[i + 1].startswith("-")
        ):
            lines.append(f"  {_shell_quote(token)} {_shell_quote(cmd[i + 1])}")
            i += 2
        else:
            lines.append(f"  {_shell_quote(token)}")
            i += 1
    return " \\\n".join(lines)


def format_command_display(cmd: List[str] | List[List[str]]) -> str:
    if cmd and isinstance(cmd[0], list):
        parts = [
            f"# Pass {i + 1}\n{_format_single_cmd(c)}"
            for i, c in enumerate(cmd)
        ]
        return "\n\n".join(parts)
    return _format_single_cmd(cmd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _generate_output_name(request: ProcessingRequest) -> str:
    if request.output_name:
        return request.output_name

    stem = Path(request.input_file).stem
    # Strip uuid prefix added during upload (8hex_)
    if re.match(r"^[0-9a-f]{8}_", stem):
        stem = stem[9:]

    tags: List[str] = []
    if request.speed != 1.0:
        tags.append(f"{request.speed}x")
    if request.cuts:
        tags.append(f"{len(request.cuts)}cuts")
    if request.trim_start or request.trim_end:
        tags.append("trimmed")
    if request.scale not in ("original", ""):
        tags.append(request.scale.replace("%", "pct"))
    if request.remove_audio:
        tags.append("noaudio")
    suffix = "-".join(tags) if tags else "processed"
    return f"{stem}-{suffix}.{request.output_format}"


def _validate_request(request: ProcessingRequest) -> None:
    if request.speed <= 0 or request.speed > 100:
        raise ValueError("Speed must be between 0.01 and 100")
    if request.crf < 0 or request.crf > 51:
        raise ValueError("CRF must be between 0 and 51")
    if request.output_format not in OUTPUT_FORMATS:
        raise ValueError(f"Output format must be one of: {', '.join(OUTPUT_FORMATS)}")
    if request.preset not in ENCODING_PRESETS:
        raise ValueError(f"Preset must be one of: {', '.join(ENCODING_PRESETS)}")
    if request.scale and request.scale not in SCALE_OPTIONS:
        raise ValueError(f"Scale must be one of: {', '.join(SCALE_OPTIONS)}")
    for i, cut in enumerate(request.cuts):
        if len(cut) != 2 or cut[0] >= cut[1]:
            raise ValueError(f"Cut {i + 1}: start must be less than end")


def _resolve_keep_ranges(request: ProcessingRequest) -> List[List[float]] | None:
    """Return keep ranges if cuts are present, else None."""
    if not request.cuts:
        return None
    input_path = UPLOAD_DIR / request.input_file
    info = probe_video(input_path)
    total = info.get("duration", 0)
    if total <= 0:
        raise ValueError("Cannot determine video duration for cut computation")
    ts = _parse_time(request.trim_start) if request.trim_start else 0
    te = _parse_time(request.trim_end) if request.trim_end else total
    keep = _cuts_to_keep_ranges(request.cuts, total, ts, te)
    if not keep:
        raise ValueError("No content remaining after cuts")
    return keep


def _apply_target_duration(request: ProcessingRequest, keep_ranges: List[List[float]] | None) -> None:
    """Mutate request.speed when target_duration is set."""
    if request.target_duration <= 0:
        return
    if keep_ranges:
        kept = sum(e - s for s, e in keep_ranges)
    else:
        input_path = UPLOAD_DIR / request.input_file
        info = probe_video(input_path)
        total = info.get("duration", 0)
        ts = _parse_time(request.trim_start) if request.trim_start else 0
        te = _parse_time(request.trim_end) if request.trim_end else total
        kept = te - ts
    if kept <= 0:
        raise ValueError("No content remaining to compute speed")
    request.speed = round(kept / request.target_duration, 6)
    request.speed = max(0.25, min(request.speed, 100))


def process_video(request: ProcessingRequest) -> Dict[str, Any]:
    input_path = UPLOAD_DIR / request.input_file
    if not input_path.is_file():
        raise ValueError(f"Input file not found: {request.input_file}")
    if not _has_executable("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH. Please install ffmpeg.")

    _validate_request(request)

    keep_ranges = _resolve_keep_ranges(request)
    _apply_target_duration(request, keep_ranges)

    output_name = _generate_output_name(request)
    output_path = OUTPUT_ROOT / output_name
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    cmd = build_ffmpeg_command(request, output_path, keep_ranges)
    command_display = format_command_display(cmd)

    commands = cmd if (cmd and isinstance(cmd[0], list)) else [cmd]
    for idx, single in enumerate(commands):
        proc = subprocess.run(single, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            tail = proc.stderr[-800:] if proc.stderr else "(no output)"
            raise RuntimeError(f"ffmpeg failed (pass {idx + 1}): {tail}")

    palette = output_path.with_name(output_path.stem + "_palette.png")
    if palette.exists():
        palette.unlink()

    relative = output_path.resolve().relative_to(WORKSPACE_ROOT)
    return {
        "command": command_display,
        "output_file": output_name,
        "output_url": "/" + relative.as_posix(),
        "output_size_bytes": output_path.stat().st_size if output_path.exists() else 0,
    }


def calculate_speed_info(request: ProcessingRequest) -> Dict[str, Any]:
    """Compute speed and keep-range metadata without processing."""
    input_path = UPLOAD_DIR / request.input_file
    if not input_path.is_file():
        raise ValueError(f"Input file not found: {request.input_file}")

    info = probe_video(input_path)
    total = info.get("duration", 0)
    ts = _parse_time(request.trim_start) if request.trim_start else 0
    te = _parse_time(request.trim_end) if request.trim_end else total

    if request.cuts:
        keep = _cuts_to_keep_ranges(request.cuts, total, ts, te)
    else:
        keep = [[ts, te]] if te > ts else []

    kept_duration = sum(e - s for s, e in keep)
    cut_duration = (te - ts) - kept_duration

    speed = 1.0
    if request.target_duration > 0 and kept_duration > 0:
        speed = round(kept_duration / request.target_duration, 6)
        speed = max(0.25, min(speed, 100))

    return {
        "total_duration": round(total, 3),
        "trim_start": round(ts, 3),
        "trim_end": round(te, 3),
        "kept_duration": round(kept_duration, 3),
        "cut_duration": round(cut_duration, 3),
        "speed": speed,
        "keep_ranges": [[round(s, 3), round(e, 3)] for s, e in keep],
    }


def preview_command(request: ProcessingRequest) -> Dict[str, Any]:
    """Return the ffmpeg command without executing it."""
    input_path = UPLOAD_DIR / request.input_file
    if not input_path.is_file():
        raise ValueError(f"Input file not found: {request.input_file}")
    _validate_request(request)
    keep_ranges = _resolve_keep_ranges(request)
    _apply_target_duration(request, keep_ranges)

    output_name = _generate_output_name(request)
    output_path = OUTPUT_ROOT / output_name
    cmd = build_ffmpeg_command(request, output_path, keep_ranges)
    return {"command": format_command_display(cmd), "output_file": output_name}


# ---------------------------------------------------------------------------
# Multipart upload parser (stdlib-only, cgi removed in 3.13)
# ---------------------------------------------------------------------------
def parse_multipart_upload(headers: Any, rfile: Any) -> tuple[str, bytes]:
    content_type = headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        raise ValueError("Expected multipart/form-data")

    boundary = None
    for segment in content_type.split(";"):
        segment = segment.strip()
        if segment.startswith("boundary="):
            boundary = segment[len("boundary="):]
            break
    if not boundary:
        raise ValueError("Missing boundary in Content-Type")

    content_length = int(headers.get("Content-Length", 0))
    if content_length > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
        )
    if content_length <= 0:
        raise ValueError("Empty upload")

    body = rfile.read(content_length)
    boundary_bytes = f"--{boundary}".encode()

    for part in body.split(boundary_bytes):
        if b"Content-Disposition" not in part:
            continue
        hdr_end = part.find(b"\r\n\r\n")
        if hdr_end < 0:
            continue
        hdr_text = part[:hdr_end].decode("utf-8", errors="replace")
        file_data = part[hdr_end + 4 :]
        if file_data.endswith(b"\r\n"):
            file_data = file_data[:-2]

        match = re.search(r'filename="([^"]+)"', hdr_text)
        if match and file_data:
            return Path(match.group(1)).name, file_data

    raise ValueError("No file found in upload")


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
def _parse_cuts(raw: Any) -> List[List[float]]:
    """Parse cuts from API payload.  Accepts [[start, end], ...] with
    numeric or time-string values."""
    if not raw or not isinstance(raw, list):
        return []
    result: List[List[float]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            result.append([_parse_time(str(item[0])), _parse_time(str(item[1]))])
    return result


def _parse_processing_payload(payload: dict) -> ProcessingRequest:
    return ProcessingRequest(
        input_file=str(payload.get("input_file", "")).strip(),
        output_format=str(payload.get("output_format", DEFAULT_OUTPUT_FORMAT)).strip(),
        speed=float(payload.get("speed", DEFAULT_SPEED)),
        trim_start=str(payload.get("trim_start", "")).strip(),
        trim_end=str(payload.get("trim_end", "")).strip(),
        remove_audio=bool(payload.get("remove_audio", DEFAULT_REMOVE_AUDIO)),
        crf=int(payload.get("crf", DEFAULT_CRF)),
        preset=str(payload.get("preset", DEFAULT_PRESET)).strip(),
        scale=str(payload.get("scale", DEFAULT_SCALE)).strip(),
        gif_fps=int(payload.get("gif_fps", DEFAULT_GIF_FPS)),
        gif_width=int(payload.get("gif_width", DEFAULT_GIF_WIDTH)),
        output_name=str(payload.get("output_name", "")).strip(),
        cuts=_parse_cuts(payload.get("cuts")),
        target_duration=float(payload.get("target_duration", 0)),
    )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class VideoKitHandler(BaseHTTPRequestHandler):
    server_version = "VideoKitHTTP/1.0"

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(
        self, file_path: Path, content_type: str | None = None,
    ) -> None:
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        file_size = file_path.stat().st_size
        ct = (
            content_type
            or mimetypes.guess_type(str(file_path))[0]
            or "application/octet-stream"
        )

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
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(data)
        else:
            raw = file_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(raw)

    def _serve_under(self, url_path: str, allowed_root: Path) -> None:
        relative = url_path.lstrip("/")
        target = (WORKSPACE_ROOT / relative).resolve()
        root = allowed_root.resolve()
        if root != target and root not in target.parents:
            self.send_error(HTTPStatus.FORBIDDEN, "Path outside allowed directory")
            return
        self._serve_file(target)

    # -- routing --

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/index.html"):
            self._serve_file(INDEX_FILE, "text/html; charset=utf-8")
            return

        if path == "/api/options":
            self._send_json({
                "defaults": {
                    "speed": DEFAULT_SPEED,
                    "crf": DEFAULT_CRF,
                    "preset": DEFAULT_PRESET,
                    "output_format": DEFAULT_OUTPUT_FORMAT,
                    "remove_audio": DEFAULT_REMOVE_AUDIO,
                    "scale": DEFAULT_SCALE,
                    "gif_fps": DEFAULT_GIF_FPS,
                    "gif_width": DEFAULT_GIF_WIDTH,
                },
                "speed_presets": SPEED_PRESETS,
                "scale_options": SCALE_OPTIONS,
                "output_formats": OUTPUT_FORMATS,
                "encoding_presets": ENCODING_PRESETS,
                "ffmpeg_available": _has_executable("ffmpeg"),
                "ffprobe_available": _has_executable("ffprobe"),
            })
            return

        if path.startswith("/generated/"):
            self._serve_under(path, OUTPUT_ROOT)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        handler_map = {
            "/api/upload": self._handle_upload,
            "/api/process": self._handle_process,
            "/api/preview-command": self._handle_preview,
            "/api/calculate-speed": self._handle_calculate_speed,
        }
        handler = handler_map.get(path)
        if handler:
            handler()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")

    # -- POST handlers --

    def _handle_upload(self) -> None:
        try:
            filename, data = parse_multipart_upload(self.headers, self.rfile)
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"
            dest = UPLOAD_DIR / unique_name
            dest.write_bytes(data)

            info: Dict[str, Any] = {"uploaded_name": unique_name, "original_name": filename}
            try:
                info.update(probe_video(dest))
            except Exception:
                pass
            self._send_json({"ok": True, "result": info})
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json(
                {"ok": False, "error": f"Upload failed: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _handle_process(self) -> None:
        try:
            payload = self._read_json_body()
            request = _parse_processing_payload(payload)
            result = process_video(request)
            self._send_json({"ok": True, "result": result})
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "Invalid JSON."}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:
            self._send_json(
                {"ok": False, "error": f"Processing error: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _handle_preview(self) -> None:
        try:
            payload = self._read_json_body()
            request = _parse_processing_payload(payload)
            result = preview_command(request)
            self._send_json({"ok": True, "result": result})
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "Invalid JSON."}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json(
                {"ok": False, "error": f"Error: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _handle_calculate_speed(self) -> None:
        try:
            payload = self._read_json_body()
            request = _parse_processing_payload(payload)
            result = calculate_speed_info(request)
            self._send_json({"ok": True, "result": result})
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "Invalid JSON."}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json(
                {"ok": False, "error": f"Error: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


# ---------------------------------------------------------------------------
# Server / CLI entry-points
# ---------------------------------------------------------------------------
def run_server(host: str, port: int) -> None:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"Missing static page: {INDEX_FILE}")
    if not _has_executable("ffmpeg"):
        print("WARNING: ffmpeg not found in PATH. Processing will not work.")
    if not _has_executable("ffprobe"):
        print("WARNING: ffprobe not found in PATH. Video probing will not work.")

    server = ThreadingHTTPServer((host, port), VideoKitHandler)
    url = f"http://{host}:{port}"
    print(f"Video Kit server is running at {url}")
    print("Open this URL in your browser to process videos.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


def run_process_once(args: argparse.Namespace) -> None:
    if not _has_executable("ffmpeg"):
        print("ERROR: ffmpeg not found in PATH. Please install ffmpeg.")
        raise SystemExit(1)

    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f"ERROR: Input file not found: {args.input}")
        raise SystemExit(1)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_name = f"{uuid.uuid4().hex[:8]}_{input_path.name}"
    dest = UPLOAD_DIR / upload_name

    try:
        dest.symlink_to(input_path)
    except OSError:
        shutil.copy2(input_path, dest)

    cuts: List[List[float]] = []
    for raw_cut in (args.cut or []):
        parts = raw_cut.split("-", 1)
        if len(parts) == 2:
            cuts.append([_parse_time(parts[0]), _parse_time(parts[1])])

    request = ProcessingRequest(
        input_file=upload_name,
        output_format=args.format,
        speed=args.speed,
        trim_start=args.trim_start or "",
        trim_end=args.trim_end or "",
        remove_audio=not args.keep_audio,
        crf=args.crf,
        preset=args.preset,
        scale=args.scale,
        gif_fps=args.gif_fps,
        gif_width=args.gif_width,
        output_name=args.output or "",
        cuts=cuts,
        target_duration=args.target_duration or 0,
    )

    try:
        result = process_video(request)
        print(f"Command:\n{result['command']}\n")
        print(f"Output: {result['output_file']}")
        print(f"Size:   {result['output_size_bytes']:,} bytes")
    finally:
        if dest.is_symlink() or dest.is_file():
            dest.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Video processing toolkit powered by ffmpeg. "
            "By default starts a local HTTP server with a web UI."
        ),
    )
    p.add_argument("--host", default="127.0.0.1", help="HTTP server host")
    p.add_argument("--port", default=8000, type=int, help="HTTP server port")
    p.add_argument(
        "--process", action="store_true",
        help="Process a video in CLI mode instead of starting the HTTP server",
    )
    p.add_argument("-i", "--input", help="Input video file (CLI mode)")
    p.add_argument("-o", "--output", help="Output filename (auto-generated if omitted)")
    p.add_argument(
        "--format", default=DEFAULT_OUTPUT_FORMAT, choices=OUTPUT_FORMATS,
        help="Output format",
    )
    p.add_argument("--speed", default=DEFAULT_SPEED, type=float, help="Speed multiplier")
    p.add_argument("--trim-start", default=None, help="Start time (e.g. 0:05 or 5.0)")
    p.add_argument("--trim-end", default=None, help="End time (e.g. 1:30 or 90.0)")
    p.add_argument(
        "--keep-audio", action="store_true",
        help="Keep audio track (default: remove)",
    )
    p.add_argument("--crf", default=DEFAULT_CRF, type=int, help="CRF quality (0-51)")
    p.add_argument(
        "--preset", default=DEFAULT_PRESET, choices=ENCODING_PRESETS,
        help="Encoding preset",
    )
    p.add_argument(
        "--scale", default=DEFAULT_SCALE, choices=SCALE_OPTIONS,
        help="Scale / resize option",
    )
    p.add_argument("--gif-fps", default=DEFAULT_GIF_FPS, type=int, help="GIF frame rate")
    p.add_argument("--gif-width", default=DEFAULT_GIF_WIDTH, type=int, help="GIF width (px)")
    p.add_argument(
        "--cut", action="append", metavar="START-END",
        help="Cut a time range (e.g. 8-15 or 0:08-0:15). Repeatable.",
    )
    p.add_argument(
        "--target-duration", type=float, default=None,
        help="Target output duration in seconds (auto-calculates speed)",
    )
    return p.parse_args()


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
