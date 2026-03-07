# AGENTS.md

## Project Identity

- Repository name: `x-workbench`
- Organization context: `cofy-x` (Coordination + Fly + X)
- Goal: a monorepo for small, execution-focused tools that improve human-AI workflows.

## Core Principles

1. Keep tools independent and simple.
2. Favor fast local iteration (Python script + static HTML UI).
3. Keep repository conventions consistent across tools.
4. Prefer clear, boring structure over custom one-off layouts.

## Repository Conventions

### Tool layout

Each tool should live under `tools/<tool_name>/`:

```text
tools/<tool_name>/
├── app.py
├── web/
│   └── index.html
└── README.md
```

### Output layout

- Generated files go to `generated/` (usually `generated/<tool-or-brand>/`).
- Generated artifacts should not be committed.

### Commands

Use the root `Makefile` as the primary interface:

- `make list-tools`
- `make serve TOOL=<tool_name>`
- `make generate TOOL=<tool_name> ...`
- `make check TOOL=<tool_name>`
- `make check-all`

## Current Tools

### `logo_generator`

- Backend: `tools/logo_generator/app.py`
- UI: `tools/logo_generator/web/index.html`
- Supports server mode and CLI generate mode.

### `video_kit`

- Backend: `tools/video_kit/app.py`
- UI: `tools/video_kit/web/index.html`
- FFmpeg-based video processing (speed, trim, scale, GIF conversion).
- Supports server mode and CLI process mode.
- Requires `ffmpeg` and `ffprobe` in PATH.

## Dependency Management

- Python dependencies are managed in `pyproject.toml`.
- Keep `uv.lock` in sync after dependency or project metadata changes.
- Prefer `uv run ...` for execution.

## Documentation Rules

1. Update root `README.md` when repository-level conventions change.
2. Update tool README when behavior or CLI/API changes.
3. Use relative links in markdown (no absolute local filesystem links).

## Implementation Notes for Future Agents

1. Do not introduce top-level one-off scripts for new tools; place tools under `tools/`.
2. Reuse existing `Makefile` patterns instead of adding custom run commands in docs.
3. Keep HTML UI text in English unless explicitly requested otherwise.
4. Run at least one syntax/health check after edits (`make check` or `make check-all`).
