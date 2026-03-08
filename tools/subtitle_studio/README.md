# Subtitle Studio

Local subtitle workflow tool for videos:

1. Upload video
2. Transcribe speech to subtitle segments (via `faster-whisper`)
3. Edit SRT text
4. Render subtitled output video

## Prerequisites

- Python >= 3.13
- `ffmpeg` and `ffprobe` in PATH
- `faster-whisper` installed from project dependencies

## Run Web UI

```bash
make serve TOOL=subtitle_studio
```

Open `http://127.0.0.1:8000`.

## CLI Mode

```bash
uv run python tools/subtitle_studio/app.py --process -i demo.mp4 \
  --lang auto --model small --burn-in --style clean --out-format mp4 --export-srt
```

## API Endpoints

- `GET /api/options`
- `POST /api/upload`
- `POST /api/transcribe`
- `POST /api/render`
- `GET /generated/*`

## Output

Generated files are written to:

```text
generated/subtitle_studio/<job_id>/
```

Typical artifacts:

- `captions.srt`
- `segments.json`
- `*-subtitled.mp4` or `*-subtitled.webm`
