# x-workbench

`x-workbench` is the `cofy-x` monorepo for execution-focused tooling.

- Coordination: human-AI collaboration as the core workflow.
- Fly: accelerate execution and delivery.
- X: infinite possibilities unlocked by AI capabilities.

## Monorepo Layout

```text
.
├── Makefile
├── tools/
│   └── <tool_name>/
│       ├── app.py
│       ├── web/index.html
│       └── README.md
├── generated/                 # generated artifacts (gitignored)
├── pyproject.toml
├── uv.lock
└── README.md
```

## Prerequisites

- Python >= 3.13 (managed via [uv](https://docs.astral.sh/uv/))
- Tool-specific dependencies:
  - **video_kit** — [ffmpeg](https://ffmpeg.org/) and ffprobe (`brew install ffmpeg` / `apt install ffmpeg`)

## Quick Start

List all available tools:

```bash
make list-tools
```

### logo_generator

Generate brand logos with configurable icon variants.

```bash
# Start web UI
make serve TOOL=logo_generator

# Generate from CLI
make generate TOOL=logo_generator BRAND=Avant
```

### video_kit

Process demo videos — speed up, cut segments, trim, resize, convert to GIF.

```bash
# Start web UI
make serve TOOL=video_kit

# Speed up 2.67x (CLI)
uv run python tools/video_kit/app.py --process -i demo.mp4 --speed 2.67

# Cut waiting segments + target 15s output
uv run python tools/video_kit/app.py --process -i demo.mp4 \
  --cut 8-15 --cut 25-30 --target-duration 15
```

See [tools/video_kit/README.md](tools/video_kit/README.md) for full CLI options.

## Current Tools

| Tool | Description | Docs |
|------|-------------|------|
| `logo_generator` | Brand logo generation with icon variants | [README](tools/logo_generator/README.md) |
| `video_kit` | FFmpeg video processing (speed, cut, trim, GIF) | [README](tools/video_kit/README.md) |

## Add a New Tool

1. Create `tools/<tool_name>/app.py`.
2. Create `tools/<tool_name>/web/index.html`.
3. Add `tools/<tool_name>/README.md`.
4. Run `make check TOOL=<tool_name>`.
5. Add new dependencies in `pyproject.toml` when required.

## License

Licensed under Apache-2.0. See [LICENSE](LICENSE).
