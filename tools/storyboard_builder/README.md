# Storyboard Builder

Generate review-ready storyboard artifacts from a video:

1. Extract key frames by `scene` or `interval` mode
2. Build a contact sheet image
3. Export timeline metadata and markdown summary
4. Click result images to view full-size preview in the browser

## Modes

- `scene`: Detect visual scene changes and keep key moments only. `scene_threshold` controls sensitivity (lower means more frames; higher means fewer).
- `interval`: Extract one frame every N seconds (`interval_seconds`), regardless of scene changes.

## Prerequisites

- Python >= 3.13
- `ffmpeg` and `ffprobe` in PATH
- `pillow` (already in project dependencies)

## Run Web UI

```bash
make serve TOOL=storyboard_builder
```

Open `http://127.0.0.1:8000`.

## CLI Mode

```bash
uv run python tools/storyboard_builder/app.py --process -i demo.mp4 \
  --mode scene --scene-threshold 0.30 --cols 5 --max-frames 24
```

## API Endpoints

- `GET /api/options`
- `POST /api/upload`
- `POST /api/extract-frames`
- `POST /api/build-contact-sheet`
- `GET /generated/*`

## Output

Generated files are written to:

```text
generated/storyboard_builder/<job_id>/
```

Typical artifacts:

- `frames/frame_*.png`
- `timestamps.json`
- `contact_sheet.jpg`
- `summary.md`
