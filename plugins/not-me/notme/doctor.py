"""The `notme doctor` probes: tools, gh auth and scopes, config, mode, the queue source, the guard self-test and the
spawn depth.

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

from . import states
from .gh import GhClient, GhError
from .queue import board_ref, describe
from .queue.board import ABSTRACT_TO_BOARD, status_options

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


def pass_label_check(result: Mapping[str, Any]) -> Check:
    """Advice on the role.md pass labels, from an ensure_labels(check=True) result."""
    if result.get("ok"):
        return check("pass labels", True, f"present in {result.get('repo')}", required=False)
    return check("pass labels", False, f"missing in {result.get('repo')}: {', '.join(result.get('missing') or [])}; "
                                       "create them with `notme labels-ensure`, or leave them out and those roles "
                                       "get no pass label", required=False)


def pass_label_error(error: str) -> Check:
    """The pass labels could not be read."""
    return check("pass labels", False, f"could not be read ({error})", required=False)


QUEUE_FIX = "/not-me:setup project --queue"
QUEUE_FIX_TEXT = f"run `{QUEUE_FIX}` to choose a queue source that works"


def _queue_failed(detail: str) -> Check:
    """An optional queue check that failed; its fix points to the queue setup."""
    return check("queue", False, detail, required=False, fix=QUEUE_FIX_TEXT)


def queue_not_run(why: str, summary: Optional[str] = None) -> Check:
    """The queue check when it could not run: not a failure, and the detail says why."""
    return check("queue", True, f"{summary + '; ' if summary else ''}not run: {why}", required=False)


def _label_problems(q: Mapping[str, Any], where: Mapping[str, Any], gh: GhClient, tracker: str) -> List[str]:
    """The pick label or the opt-in label missing from the tracker, and a queue.pick_label nobody reads."""
    wanted = {"todo": where["label"]}
    if where["opt_in_label"]:
        wanted["opt_in"] = where["opt_in_label"]
    problems = [f"{tracker} has no `{name}` label; create it or choose another with {QUEUE_FIX}"
                for name in states.ensure_labels(gh, tracker, wanted, check=True)["missing"]]
    pick = q.get("pick_label")
    if pick and str(pick).lower() != str(where["label"]).lower():
        problems.append(f"queue.pick_label `{pick}` is not used: the labels source picks the `labels.todo` "
                        f"label `{where['label']}`; set both to one name with {QUEUE_FIX}")
    return problems


def _board_problems(q: Mapping[str, Any], gh: GhClient, confirmed_board: Optional[Sequence[Any]],
                    origin: Optional[str]) -> List[str]:
    """An unconfirmed or missing board, and queue.board_states columns that are not status options."""
    owner, number = board_ref(q) or ("", 0)  # describe() said ok, so the board is set
    label = f"{owner}/{number}"
    problems: List[str] = []
    if not confirmed_board or str(confirmed_board[0]).lower() != owner.lower() or int(confirmed_board[1]) != number:
        problems.append(f"board {label} is not confirmed in your allow list, so not-me cannot move its "
                        f"cards; run /not-me:allow-repo {origin + ' ' if origin else ''}--board {label}")
    status_field = (q.get("board") or {}).get("status_field") or ""
    options = status_options(gh, owner, number, status_field)
    if options is None:
        return problems + [f"board {label} was not found, or this token cannot read it (it needs the project scope)"]
    columns = q.get("board_states") or {}
    for key in dict.fromkeys(ABSTRACT_TO_BOARD.values()):
        column = columns.get(key)
        if column and column not in options:
            problems.append(f"board {label} has no `{column}` option (queue.board_states.{key}); its "
                            f"{status_field} options are: {', '.join(options) or 'none'}")
    return problems


def queue_check(meta: Mapping[str, Any], gh: GhClient, tracker: str, confirmed_board: Optional[Sequence[Any]],
                origin: Optional[str] = None) -> Check:
    """A warning when the chosen queue source cannot work. Labels: the pick label (or the opt-in label) does not
    exist in the tracker, or queue.pick_label differs from labels.todo. Board: the write gate does not trust the
    board (confirmed_board is the guard's board, None when unconfirmed), it cannot be found, or a queue.board_states
    column is not an option of its status field. Only reads; a GitHub error reports the check as not run."""
    where = describe(meta)
    if not where["ok"]:
        return _queue_failed(f"{where['summary']}; fix it with {QUEUE_FIX}")
    q = meta.get("queue") or {}
    try:
        if where["source"] == "labels":
            problems = _label_problems(q, where, gh, tracker)
        else:
            problems = _board_problems(q, gh, confirmed_board, origin)
    except GhError as exc:
        return queue_not_run(f"GitHub could not be read ({exc})", where["summary"])
    except ValueError as exc:  # a board owner or status field name the client refuses to put in a query
        return _queue_failed(f"{where['summary']}, but {exc}; fix it with {QUEUE_FIX}")
    if problems:
        return _queue_failed(f"{where['summary']}, but " + "; ".join(problems))
    return check("queue", True, where["summary"], required=False)


def run(runner: Callable[[Sequence[str]], Any], config_errors: List[str], gh: Callable[[], GhClient],
        guard_state: Callable[[], Mapping[str, Any]], env: Mapping[str, str],
        mode: Optional[Check] = None, config_files: Optional[Mapping[str, Sequence[str]]] = None,
        pass_labels: Optional[Callable[[], Optional[Check]]] = None,
        queue: Optional[Callable[[], Check]] = None) -> List[Check]:
    """Every probe in order. gh, guard_state, pass_labels and queue are called only when needed (gh needs a valid
    config; pass_labels and queue also a logged-in gh, and the queue check is reported as not run otherwise).
    config_files maps a layer to the config docs found in it; the "config files" probe runs only when it is
    given."""
    checks = [python_check(), git_check(), gh_check(runner)]
    installed = checks[-1]["ok"]
    detail = "valid" if not config_errors else f"{len(config_errors)} error(s): {config_errors[0]}"
    checks.append(check("config", not config_errors, detail, fix=CONFIG_FIX))
    if config_files is not None:
        checks.append(config_files_check(config_files))
    if mode is not None:
        checks.append(mode)
    signed_in = False
    if installed and not config_errors:
        auth = auth_checks(gh())
        signed_in = auth[0]["ok"]
        checks += auth
        labels = pass_labels() if pass_labels is not None and signed_in else None
        if labels is not None:
            checks.append(labels)
    if queue is not None:
        checks.append(queue() if signed_in else
                      queue_not_run("needs gh installed and signed in, and a valid config"))
    checks += [guard_check(guard_state()), depth_check(env)]
    return checks


def exit_kind(checks: Sequence[Check]) -> Optional[str]:
    """None when every required check passed; "dependency" when only gh checks failed; else "failed"."""
    failed = {c["name"] for c in checks if c["required"] and not c["ok"]}
    if not failed:
        return None
    return "dependency" if failed <= GH_CHECKS else "failed"
