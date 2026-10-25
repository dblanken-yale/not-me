"""The `notme doctor` probes: tools, gh auth and scopes, config, mode, the guard self-test and the spawn depth.

Each probe gives {name, ok, detail, required, fix, hint}. `fix` is a short description of a safe change that clears
a failed check; `hint` is an install or repair hint for this operating system (sys.platform). Both are null on a
passing check, and null when no safe fix or hint exists. The CLI exits 7 when the only failed required checks are the gh ones
(a missing dependency or missing auth), 1 when anything else required failed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .gh import GhClient, GhError

SUBAGENT_DEPTH_ENV = "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"
GH_CHECKS = frozenset({"gh", "gh auth", "gh scopes"})

DEPTH_FIX = f'set `{SUBAGENT_DEPTH_ENV}` to "3" in the `env` block of `~/.claude/settings.json`'
CONFIG_FIX = "run `/not-me:config` to see each error, or `/not-me:setup` to write the config"
SETUP_FIX = "run `/not-me:setup` to write the not-me config"
AUTH_FIX = "run `gh auth login` in a terminal and sign in"
SCOPE_FIX = "run `gh auth refresh -s repo` in a terminal to add the `repo` scope"
GUARD_HINT = ("check `python3 --version` is 3.9 or newer, then reinstall with `/plugin update not-me@not-me`; "
              "do not turn guard rules off")

# Per-OS install hints for the tools doctor cannot install. "other" is the fallback for any other platform.
INSTALL_HINTS: Dict[str, Dict[str, str]] = {
    "python": {
        "darwin": "install Python 3.9 or newer: `brew install python` or https://www.python.org/downloads/",
        "linux": "install Python 3.9 or newer with your package manager, e.g. `sudo apt install python3`",
        "win32": "install Python 3.9 or newer: `winget install --id Python.Python.3.13`",
        "other": "install Python 3.9 or newer from https://www.python.org/downloads/",
    },
    "git": {
        "darwin": "install git: `xcode-select --install` or `brew install git`",
        "linux": "install git with your package manager, e.g. `sudo apt install git`",
        "win32": "install git: `winget install --id Git.Git`",
        "other": "install git from https://git-scm.com/downloads",
    },
    "gh": {
        "darwin": "install gh: `brew install gh`",
        "linux": "install gh: see https://github.com/cli/cli#installation",
        "win32": "install gh: `winget install --id GitHub.cli`",
        "other": "install gh from https://cli.github.com",
    },
}

Check = Dict[str, Any]


def check(name: str, ok: bool, detail: str, required: bool = True, *,
          fix: Optional[str] = None, hint: Optional[str] = None) -> Check:
    """A passing check never carries a fix or a hint."""
    ok = bool(ok)
    return {"name": name, "ok": ok, "detail": detail, "required": required,
            "fix": None if ok else fix, "hint": None if ok else hint}


def os_key() -> str:
    """darwin, linux, win32 or other, from sys.platform."""
    return next((key for key in ("darwin", "linux", "win32") if sys.platform.startswith(key)), "other")


def install_hint(tool: str) -> str:
    return INSTALL_HINTS[tool][os_key()]


def python_check() -> Check:
    v = sys.version_info
    return check("python", v >= (3, 9), f"{v[0]}.{v[1]}.{v[2]}", hint=install_hint("python"))


def git_check() -> Check:
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return check("git", False, f"git not found ({exc.__class__.__name__})", hint=install_hint("git"))
    return check("git", proc.returncode == 0, proc.stdout.strip() or "git failed", hint=install_hint("git"))


def gh_check(runner: Callable[[Sequence[str]], Any]) -> Check:
    try:
        proc = runner(["--version"])
    except OSError:
        return check("gh", False, "gh not found on PATH", hint=install_hint("gh"))
    version = proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else "version unknown"
    return check("gh", True, version)


def auth_checks(gh: GhClient) -> List[Check]:
    """gh auth (who is logged in) and gh scopes (a classic token must carry `repo`)."""
    try:
        who = gh.me()
    except GhError as exc:
        return [check("gh auth", False, f"not authenticated ({exc})", fix=AUTH_FIX)]
    out = [check("gh auth", True, f"logged in as {who}")]
    try:
        header = gh.read(["api", "-i", "user"], json=False) or ""
    except GhError:
        return out + [check("gh scopes", True, "could not be read", required=False)]
    m = re.search(r"(?im)^x-oauth-scopes:\s*(.*)$", header)
    scopes = [s.strip() for s in m.group(1).split(",") if s.strip()] if m else None
    if scopes is None:
        return out + [check("gh scopes", True, "not reported (fine-grained token)", required=False)]
    return out + [check("gh scopes", "repo" in scopes, ", ".join(scopes), fix=SCOPE_FIX)]


def guard_check(state: Mapping[str, Any]) -> Check:
    """Never a fix: doctor does not touch the guard. The hint says what a person does by hand."""
    if not state.get("active"):
        return check("guard", True, "not active in this project", required=False)
    ok = bool(state.get("selftest"))
    return check("guard", ok, "self-test blocked the canned event" if ok else "self-test did not block",
                 hint=GUARD_HINT)


def depth_check(env: Mapping[str, str]) -> Check:
    depth = env.get(SUBAGENT_DEPTH_ENV)
    ok = bool(depth and depth.isdigit() and int(depth) >= 3)
    detail = f"{SUBAGENT_DEPTH_ENV}={depth}" if depth else f"{SUBAGENT_DEPTH_ENV} is unset; background runs need 3"
    return check("spawn depth", ok, detail, required=False, fix=DEPTH_FIX)


def config_files_check(files: Mapping[str, Sequence[str]]) -> Check:
    """A warning when neither the global nor the repo layer holds a not-me config file (only defaults apply).
    The fix sends the operator to /not-me:setup; doctor never writes config."""
    found = [f"{layer}: {', '.join(f'{n}.md' for n in files.get(layer) or [])}"
             for layer in ("global", "repo") if files.get(layer)]
    detail = "; ".join(found) if found else "no not-me config file in the global or repo layer; only defaults apply"
    return check("config files", bool(found), detail, required=False, fix=SETUP_FIX)


def mode_check(mode: Any, unconfirmed: Optional[str]) -> Check:
    """A warning when the config says `mode: live` but `notme mode live` never signed it (so it runs dry-run).
    Never a fix: going live is a person's step, and the detail already names it."""
    if unconfirmed:
        return check("mode", False, unconfirmed, required=False)
    return check("mode", True, "live (confirmed)" if mode == "live" else str(mode or "dry-run"), required=False)


def run(runner: Callable[[Sequence[str]], Any], config_errors: List[str], gh: Callable[[], GhClient],
        guard_state: Callable[[], Mapping[str, Any]], env: Mapping[str, str],
        mode: Optional[Check] = None, config_files: Optional[Mapping[str, Sequence[str]]] = None) -> List[Check]:
    """Every probe in order. gh and guard_state are called only when needed (gh needs a valid config).
    config_files maps a layer to the config docs found in it; the "config files" probe runs only when it is given."""
    checks = [python_check(), git_check(), gh_check(runner)]
    installed = checks[-1]["ok"]
    detail = "valid" if not config_errors else f"{len(config_errors)} error(s): {config_errors[0]}"
    checks.append(check("config", not config_errors, detail, fix=CONFIG_FIX))
    if config_files is not None:
        checks.append(config_files_check(config_files))
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
