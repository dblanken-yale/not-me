#!/usr/bin/env python3
"""not-me PreToolUse hook: exit 2 (reason on stderr) blocks the tool call, exit 0 allows it.

Runs on Python 3.9+ with the standard library only. An internal error fails
closed (exit 2) when the call looks git- or GitHub-related, and open otherwise.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

SENTINELS = ("gh ", "git ", "github", ".git/", ".git\\\\", "allowed-repos.json")
# Below the 10 s timeout in hooks.json: a hook that times out does not block, so a slow check blocks instead.
WATCHDOG_SECONDS = 8.0


def _too_slow() -> None:
    sys.stderr.write("not-me guard failed closed: the check took too long; split the command into simpler ones\n")
    sys.stderr.flush()
    os._exit(2)


def main(raw: "str | None" = None) -> int:
    """Decide one hook call; raw is the stdin text (read from stdin when None)."""
    watchdog = None
    try:
        if raw is None:
            raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from notme import guard

        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
        watchdog = threading.Timer(WATCHDOG_SECONDS, _too_slow)
        watchdog.daemon = True
        watchdog.start()
        if not guard.needs_check(event):
            return 0
        cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        ctx = guard.GuardContext.from_project(cwd)
        active = guard.is_active(ctx)
        if not active:
            ctx.self_only()  # outside a not-me project only self-protection applies
        block = guard.decide(event, ctx)
        if block is None:
            return 0
        if active:  # never write a log into a repo that is not a not-me project
            guard.log_block(ctx, block)
        print(block.message, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - any failure must still produce a decision
        if any(s in (raw or "").lower() for s in SENTINELS):
            print(f"not-me guard failed closed: {exc.__class__.__name__}: {exc}; run /not-me:doctor",
                  file=sys.stderr)
            return 2
        return 0
    finally:
        if watchdog is not None:
            watchdog.cancel()


if __name__ == "__main__":
    sys.exit(main())
