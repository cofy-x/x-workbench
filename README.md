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

## Quick Start

List tools:

```bash
make list-tools
```

Run a tool server:

```bash
make serve TOOL=logo_generator
```

Generate once from CLI:

```bash
make generate TOOL=logo_generator BRAND=Avant
```

## Current Tool

### logo_generator

- Backend: `tools/logo_generator/app.py`
- UI: `tools/logo_generator/web/index.html`
- Docs: `tools/logo_generator/README.md`

## Add a New Tool

1. Create `tools/<tool_name>/app.py`.
2. Create `tools/<tool_name>/web/index.html`.
3. Add `tools/<tool_name>/README.md`.
4. Run `make check TOOL=<tool_name>`.
5. Add new dependencies in `pyproject.toml` when required.

## License

Licensed under Apache-2.0. See [LICENSE](LICENSE).
