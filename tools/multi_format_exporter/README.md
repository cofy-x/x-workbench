# Multi Format Exporter

Batch-export one source video to multiple platform presets in one run.

Default presets:

- `tiktok` -> `1080x1920`
- `youtube` -> `1920x1080`
- `square` -> `1080x1080`

## Prerequisites

- Python >= 3.13
- `ffmpeg` and `ffprobe` in PATH

## Run Web UI

```bash
make serve TOOL=multi_format_exporter
```

Open `http://127.0.0.1:8000`.

### Web Preview

- After upload, the page shows a source video preview player.
- After batch export, each preset output shows an inline video player plus a download link.
- Static file serving supports HTTP Range requests for smoother seek/scrub in `<video>`.

## CLI Mode

```bash
uv run python tools/multi_format_exporter/app.py --process -i demo.mp4 \
  --preset tiktok --preset youtube --preset square \
  --fit-mode crop_center --speed 1.25 --cut 8-15 --cut 25-30
```

## API Endpoints

- `GET /api/options`
- `POST /api/upload`
- `POST /api/preview-batch-command`
- `POST /api/process-batch`
- `GET /generated/*`

`POST /api/upload` now also returns:

- `upload_relative_path`
- `upload_url`

## Output

Generated files are written to:

```text
generated/multi_format_exporter/<job_id>/
```

Typical artifacts:

- `tiktok.mp4`
- `youtube.mp4`
- `square.mp4`
- `manifest.json`
