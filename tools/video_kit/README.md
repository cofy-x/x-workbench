# Video Kit

FFmpeg-based video processing toolkit with a web UI and CLI interface. Designed for quick post-processing of screen recordings and demo videos.

## Prerequisites

- Python >= 3.13
- [ffmpeg](https://ffmpeg.org/) and ffprobe installed and available in `PATH`

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg
```

## Features

- **Speed adjustment** — speed up or slow down video (0.25x to 4x, presets for common values like 2x, 2.67x)
- **Cut segments** — remove arbitrary time ranges from the middle of a video (e.g. waiting-for-server pauses)
- **Target duration** — set a desired output length and auto-calculate the required speed
- **Trim / cut** — extract a segment by start and end time
- **Remove audio** — strip audio track (on by default for silent demo videos)
- **Compress** — adjust CRF quality and encoding preset
- **Scale / resize** — 1080p, 720p, 480p, 50%, 25%
- **Convert to GIF** — two-pass palette-optimized animated GIF with configurable FPS and width
- **Result preview** — click GIF outputs in the result panel to open a larger preview
- **Combinable** — multiple operations run in a single ffmpeg pass to avoid re-encoding
- **Command preview** — see the exact ffmpeg command before running it

## Quick Start

### Web UI (server mode)

```bash
make serve TOOL=video_kit
# or directly:
uv run python tools/video_kit/app.py --port 8000
```

Open <http://127.0.0.1:8000> in your browser.

### CLI mode

```bash
# Speed up 2.67x, remove audio
uv run python tools/video_kit/app.py --process -i demo.mp4 --speed 2.67

# Trim + speed up + scale to 720p
uv run python tools/video_kit/app.py --process -i demo.mp4 \
  --speed 2 --trim-start 0:05 --trim-end 1:30 --scale 720p

# Cut out waiting segments + set target duration
uv run python tools/video_kit/app.py --process -i demo.mp4 \
  --cut 8-15 --cut 25-30 --target-duration 15

# Cut segments with manual speed
uv run python tools/video_kit/app.py --process -i demo.mp4 \
  --cut 0:08-0:15 --cut 0:25-0:30 --speed 2

# Convert to GIF
uv run python tools/video_kit/app.py --process -i demo.mp4 \
  --format gif --speed 2 --gif-fps 12 --gif-width 800

# Keep audio and slow down
uv run python tools/video_kit/app.py --process -i demo.mp4 \
  --speed 0.5 --keep-audio
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--process` | — | Run in CLI mode (required for processing) |
| `-i`, `--input` | — | Input video file path |
| `-o`, `--output` | auto | Output filename |
| `--format` | mp4 | Output format: mp4, webm, gif |
| `--speed` | 1.0 | Speed multiplier |
| `--target-duration` | — | Target output duration in seconds (auto-calculates speed) |
| `--trim-start` | — | Start time (e.g. `0:05` or `5.0`) |
| `--trim-end` | — | End time (e.g. `1:30` or `90.0`) |
| `--cut` | — | Cut a time range, repeatable (e.g. `--cut 8-15 --cut 25-30`) |
| `--keep-audio` | — | Keep audio track (removed by default) |
| `--crf` | 23 | CRF quality (0-51, lower = better) |
| `--preset` | medium | Encoding preset (ultrafast to veryslow) |
| `--scale` | original | Scale option: 1080p, 720p, 480p, 50%, 25% |
| `--gif-fps` | 15 | GIF frame rate |
| `--gif-width` | 640 | GIF width in pixels |

## Typical Workflow: Demo Video

1. Record a 40s app demo
2. Upload to Video Kit
3. Add cuts for "waiting for server" pauses (e.g. 8-15s, 25-30s)
4. Set target duration to 15s
5. Tool auto-calculates speed (28s remaining / 15s target = 1.87x)
6. Process — done!

## Output

Processed files are written to `generated/video_kit/`. This directory is git-ignored.

## API Endpoints (server mode)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web UI |
| GET | `/api/options` | Defaults and available choices |
| POST | `/api/upload` | Upload a video file (multipart/form-data) |
| POST | `/api/process` | Process video (JSON body) |
| POST | `/api/preview-command` | Preview ffmpeg command without executing |
| POST | `/api/calculate-speed` | Calculate speed from cuts + target duration |
| GET | `/generated/*` | Serve processed files |
