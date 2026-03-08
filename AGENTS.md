# AGENTS.md

## Purpose

This file is for AI agents working in `x-workbench`.
Keep it focused on stable conventions. Avoid copying fast-changing behavior here.

## Project Identity

- Repository name: `x-workbench`
- Organization context: `cofy-x` (Coordination + Fly + X)
- Goal: a monorepo for small, execution-focused tools that improve human-AI workflows.

## Core Principles

1. Keep tools independent and simple.
2. Favor fast local iteration (Python script + static HTML UI).
3. Keep repository conventions consistent across tools.
4. Prefer clear, boring structure over custom one-off layouts.
5. Prefer low-drift documentation: stable rules in `AGENTS.md`, changing details in tool READMEs.

## Source of Truth (Read Order)

When checking facts, use this order:

1. Runtime facts and code (`make list-tools`, `tools/<tool>/app.py`).
2. Tool docs (`tools/<tool>/README.md`).
3. Root docs (`README.md`, `Makefile`).
4. This file (`AGENTS.md`) as guardrails, not runtime truth.

If sources conflict, trust the higher-priority source above.

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

Shared helpers can live under `tools/_shared/` when multiple tools need the same media utilities.

### Output layout

- Generated files go to `generated/` (usually `generated/<tool-or-brand>/`).
- Generated artifacts should not be committed.

### Commands

Use the root `Makefile` as the primary interface:

- `make list-tools`
- `make serve TOOL=<tool_name>`
- `make generate TOOL=logo_generator ...`
- `make run TOOL=<tool_name> ARGS='--process ...'`
- `make check TOOL=<tool_name>`
- `make check-all`
- `make clean-generated`

## Current Tools

This section is a lightweight index only. Do not treat it as authoritative feature documentation.
Always verify with `make list-tools` and each tool README.

- `logo_generator` -> `tools/logo_generator/README.md`
- `video_kit` -> `tools/video_kit/README.md`
- `subtitle_studio` -> `tools/subtitle_studio/README.md`
- `multi_format_exporter` -> `tools/multi_format_exporter/README.md`
- `storyboard_builder` -> `tools/storyboard_builder/README.md`

## Dependency Management

- Python dependencies are managed in `pyproject.toml`.
- Keep `uv.lock` in sync after dependency or project metadata changes.
- Prefer `uv run ...` for execution.

## Documentation Rules

1. Update root `README.md` when repository-level conventions change.
2. Update tool README when behavior or CLI/API changes.
3. Use relative links in markdown (no absolute local filesystem links).
4. Avoid duplicating detailed API/CLI behavior in `AGENTS.md`; link to tool READMEs instead.

## Implementation Notes for Future Agents

1. Do not introduce top-level one-off scripts for new tools; place tools under `tools/`.
2. Reuse existing `Makefile` patterns instead of adding custom run commands in docs.
3. Keep HTML UI text in English unless explicitly requested otherwise.
4. Run at least one syntax/health check after edits (`make check` or `make check-all`).
5. Before claiming "current tools" or "latest behavior", verify locally from code and `make list-tools`.
