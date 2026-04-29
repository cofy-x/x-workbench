from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the workbench topology Streamlit app."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind.")
    return parser.parse_args(argv)


def build_streamlit_command(app_path: Path, host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.headless",
        "true",
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--browser.gatherUsageStats",
        "false",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    app_path = Path(__file__).with_name("streamlit_app.py")
    command = build_streamlit_command(app_path, args.host, args.port)
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
