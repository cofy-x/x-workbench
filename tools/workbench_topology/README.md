# Workbench Topology

Visualize and edit the `x-workbench` tool relationship graph using Streamlit and Graphviz.

## Features

1. Editable edge list using `source -> target` lines
2. Graphviz engine switching (`dot`, `neato`, `fdp`, `sfdp`, `circo`)
3. Layout tuning for hierarchical and force-directed engines
4. DOT source preview for documentation and sharing

## Run

```bash
make serve TOOL=workbench_topology
```

Open `http://127.0.0.1:8000`.

## System Prerequisite

Graphviz binaries must be installed:

- macOS: `brew install graphviz`
- Ubuntu/Debian: `sudo apt update && sudo apt install -y graphviz`

## CLI Smoke

```bash
uv run python tools/workbench_topology/app.py --help
```
