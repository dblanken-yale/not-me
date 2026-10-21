#!/usr/bin/env python3
"""Entry point for the `notme` command: puts the plugin directory on sys.path and runs notme.cli."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notme import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli.main())
