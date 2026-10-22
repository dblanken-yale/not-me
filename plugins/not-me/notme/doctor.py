"""The `notme doctor` probes: tools, gh auth and scopes, config, mode, the guard self-test and the spawn depth.

Each probe gives {name, ok, detail, required}. The CLI exits 7 when the only failed required checks are the gh
ones (a missing dependency or missing auth), 1 when anything else required failed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .gh import GhClient, GhError

SUBAGENT_DEPTH_ENV = "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"
GH_CHECKS = frozenset({"gh", "gh auth", "gh scopes"})

Check = Dict[str, Any]


def check(name: str, ok: bool, detail: str, required: bool = True) -> Check:
    return {"name": name, "ok": bool(ok), "detail": detail, "required": required}


def python_check() -> Check:
    v = sys.version_info
    return check("python", v >= (3, 9), f"{v.major}.{v.minor}.{v.micro}")


def git_check() -> Check:
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return check("git", False, f"git not found ({exc.__class__.__name__})")
    return check("git", proc.returncode == 0, proc.stdout.strip() or "git failed")


def gh_check(runner: Callable[[Sequence[str]], Any]) -> Check:
    try:
        proc = runner(["--version"])
    except OSError:
        return check("gh", False, "gh not found on PATH")
    version = proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else "version unknown"
    return check("gh", True, version)


def auth_checks(gh: GhClient) -> List[Check]:
    """gh auth (who is logged in) and gh scopes (a classic token must carry `repo`)."""
    try:
        who = gh.me()
    except GhError as exc:
        return [check("gh auth", False, f"not authenticated ({exc})")]
    out = [check("gh auth", True, f"logged in as {who}")]
    try:
        header = gh.read(["api", "-i", "user"], json=False) or ""
    except GhError:
        return out + [check("gh scopes", True, "could not be read", required=False)]
    m = re.search(r"(?im)^x-oauth-scopes:\s*(.*)$", header)
    scopes = [s.strip() for s in m.group(1).split(",") if s.strip()] if m else None
    if scopes is None:
        return out + [check("gh scopes", True, "not reported (fine-grained token)", required=False)]
    return out + [check("gh scopes", "repo" in scopes, ", ".join(scopes))]


def guard_check(state: Mapping[str, Any]) -> Check:
    if not state.get("active"):
        return check("guard", True, "not active in this project", required=False)
    ok = bool(state.get("selftest"))
    return check("guard", ok, "self-test blocked the canned event" if ok else "self-test did not block")


def depth_check(env: Mapping[str, str]) -> Check:
    depth = env.get(SUBAGENT_DEPTH_ENV)
    ok = bool(depth and depth.isdigit() and int(depth) >= 3)
    detail = f"{SUBAGENT_DEPTH_ENV}={depth}" if depth else f"{SUBAGENT_DEPTH_ENV} is unset; background runs need 3"
    return check("spawn depth", ok, detail, required=False)


def mode_check(mode: Any, unconfirmed: Optional[str]) -> Check:
    """A warning when the config says `mode: live` but `notme mode live` never signed it (so it runs dry-run)."""
    if unconfirmed:
        return check("mode", False, unconfirmed, required=False)
    return check("mode", True, "live (confirmed)" if mode == "live" else str(mode or "dry-run"), required=False)


def run(runner: Callable[[Sequence[str]], Any], config_errors: List[str], gh: Callable[[], GhClient],
        guard_state: Callable[[], Mapping[str, Any]], env: Mapping[str, str],
        mode: Optional[Check] = None) -> List[Check]:
    """Every probe in order. gh and guard_state are called only when needed (gh needs a valid config)."""
    checks = [python_check(), git_check(), gh_check(runner)]
    installed = checks[-1]["ok"]
    detail = "valid" if not config_errors else f"{len(config_errors)} error(s): {config_errors[0]}"
    checks.append(check("config", not config_errors, detail))
    if mode is not None:
        checks.append(mode)
    if installed and not config_errors:
        checks += auth_checks(gh())
    checks += [guard_check(guard_state()), depth_check(env)]
    return checks


def exit_kind(checks: Sequence[Check]) -> Optional[str]:
    """None when every required check passed; "dependency" when only gh checks failed; else "failed"."""
    failed = {c["name"] for c in checks if c["required"] and not c["ok"]}
    if not failed:
        return None
    return "dependency" if failed <= GH_CHECKS else "failed"
