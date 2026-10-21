#!/usr/bin/env python3
"""not-me PreToolUse hook: exit 2 (reason on stderr) blocks the tool call, exit 0 allows it.

Runs on Python 3.9+ with the standard library only. An internal error fails
closed (exit 2) when the call looks git- or GitHub-related, and open otherwise.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SENTINELS = ("gh ", "git ", "github", ".git/", ".git\\\\", "allowed-repos.json")


def main(raw: "str | None" = None) -> int:
    """Decide one hook call; raw is the stdin text (read from stdin when None)."""
    try:
        if raw is None:
            raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from notme import guard

        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
        if not guard.needs_check(event):
            return 0
        cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        ctx = guard.GuardContext.from_project(cwd)
        if not guard.is_active(ctx):
            return 0
        block = guard.decide(event, ctx)
        if block is None:
            return 0
        log = guard.blocklog_path(ctx)
        if log is not None:
            guard.blocklog_append(log, block)
        print(block.message, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - any failure must still produce a decision
        if any(s in (raw or "").lower() for s in SENTINELS):
            print(f"not-me guard failed closed: {exc.__class__.__name__}: {exc}; run /not-me:doctor",
                  file=sys.stderr)
            return 2
        return 0


if __name__ == "__main__":
    sys.exit(main())
