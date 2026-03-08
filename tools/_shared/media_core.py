from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence


MAX_UPLOAD_BYTES = 500 * 1024 * 1024


@dataclass(frozen=True)
class MediaProbeInfo:
    filename: str
    size_bytes: int
    duration: float
    format_name: str
    width: int = 0
    height: int = 0
    codec: str = ""
    fps: float = 0.0
    has_audio: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RangeCut:
    start: float
    end: float

    def to_list(self) -> List[float]:
        return [self.start, self.end]


@dataclass(frozen=True)
class RenderJobResult:
    output_file: str
    output_url: str
    output_size_bytes: int
    command: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def discover_workspace_root() -> Path:
    env_root = os.getenv("TOOLS_WORKSPACE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    script_dir = Path(__file__).resolve().parent
    for candidate in [script_dir, *script_dir.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd().resolve()


def has_executable(name: str) -> bool:
    return shutil.which(name) is not None


def parse_time(value: str) -> float:
    text = value.strip()
    if not text:
        return 0.0
    parts = text.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Invalid time format: {value}")


def cuts_to_keep_ranges(
    cuts: Sequence[RangeCut],
    total_duration: float,
    trim_start: float = 0,
    trim_end: float = 0,
) -> List[RangeCut]:
    start = trim_start if trim_start > 0 else 0
    end = trim_end if trim_end > 0 else total_duration
    if end <= start:
        return []

    sorted_cuts = sorted(cuts, key=lambda c: c.start)
    merged: List[RangeCut] = []
    for cut in sorted_cuts:
        cs, ce = max(cut.start, start), min(cut.end, end)
        if cs >= ce:
            continue
        if merged and cs <= merged[-1].end:
            merged[-1] = RangeCut(merged[-1].start, max(merged[-1].end, ce))
        else:
            merged.append(RangeCut(cs, ce))

    keep: List[RangeCut] = []
    pos = start
    for cut in merged:
        if pos < cut.start:
            keep.append(RangeCut(pos, cut.start))
        pos = cut.end
    if pos < end:
        keep.append(RangeCut(pos, end))
    return keep


def probe_video(file_path: Path) -> MediaProbeInfo:
    if not has_executable("ffprobe"):
        raise RuntimeError("ffprobe not found in PATH")

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"),
        None,
    )
    fmt = data.get("format", {})

    info = MediaProbeInfo(
        filename=file_path.name,
        size_bytes=int(fmt.get("size", 0)),
        duration=float(fmt.get("duration", 0)),
        format_name=fmt.get("format_name", ""),
        has_audio=audio_stream is not None,
    )
    if not video_stream:
        return info

    fps = 0.0
    fps_raw = str(video_stream.get("r_frame_rate", "0/1"))
    fps_parts = fps_raw.split("/")
    if len(fps_parts) == 2:
        den = int(fps_parts[1]) if fps_parts[1].isdigit() else 0
        num = int(fps_parts[0]) if fps_parts[0].isdigit() else 0
        if den > 0:
            fps = round(num / den, 2)

    return MediaProbeInfo(
        filename=info.filename,
        size_bytes=info.size_bytes,
        duration=info.duration,
        format_name=info.format_name,
        width=int(video_stream.get("width", 0)),
        height=int(video_stream.get("height", 0)),
        codec=str(video_stream.get("codec_name", "")),
        fps=fps,
        has_audio=info.has_audio,
    )


def shell_quote(text: str) -> str:
    if any(c in text for c in " ;()[]{}$|&<>*?'\"\\"):
        return f'"{text}"'
    return text


def format_single_command(cmd: Sequence[str]) -> str:
    if len(cmd) <= 4:
        return " ".join(shell_quote(part) for part in cmd)

    lines = [shell_quote(cmd[0])]
    i = 1
    while i < len(cmd):
        token = cmd[i]
        if token.startswith("-") and i + 1 < len(cmd) and not cmd[i + 1].startswith("-"):
            lines.append(f"  {shell_quote(token)} {shell_quote(cmd[i + 1])}")
            i += 2
        else:
            lines.append(f"  {shell_quote(token)}")
            i += 1
    return " \\\n".join(lines)


def format_command_display(cmd: Sequence[str] | Sequence[Sequence[str]]) -> str:
    if cmd and isinstance(cmd[0], (list, tuple)):  # type: ignore[index]
        parts = []
        for idx, sub_cmd in enumerate(cmd):  # type: ignore[assignment]
            parts.append(f"# Pass {idx + 1}\n{format_single_command(sub_cmd)}")
        return "\n\n".join(parts)
    return format_single_command(cmd)  # type: ignore[arg-type]


def parse_multipart_upload(
    headers: Any,
    rfile: Any,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
) -> tuple[str, bytes]:
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
    if content_length > max_upload_bytes:
        raise ValueError(f"File too large (max {max_upload_bytes // (1024 * 1024)} MB)")
    if content_length <= 0:
        raise ValueError("Empty upload")

    body = rfile.read(content_length)
    boundary_bytes = f"--{boundary}".encode()

    for part in body.split(boundary_bytes):
        if b"Content-Disposition" not in part:
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        header_text = part[:header_end].decode("utf-8", errors="replace")
        file_data = part[header_end + 4 :]
        if file_data.endswith(b"\r\n"):
            file_data = file_data[:-2]

        match = re.search(r'filename="([^"]+)"', header_text)
        if match and file_data:
            return Path(match.group(1)).name, file_data

    raise ValueError("No file found in upload")
