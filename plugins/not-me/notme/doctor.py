"""The `notme doctor` probes: tools, gh auth and scopes, config, mode, the queue source, the guard self-test and the
spawn depth, and the recommended permission allow rules (reported, never written).

Each probe gives {name, ok, detail, required, fix, hint}. `fix` is a short description of a safe change that clears
a failed check; `hint` is an install or repair hint for this operating system (sys.platform). Both are null on a
passing check, and null when no safe fix or hint exists. The CLI exits 7 when the only failed required checks are the gh ones
(a missing dependency or missing auth), 1 when anything else required failed.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
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

# permissions.allow rules that let routine not-me housekeeping run in auto permission mode without a refusal
# (docs/permissions.md explains each one). Doctor only reports the missing ones; it never edits settings.
HOUSEKEEPING_RULES = ("Bash(gh issue close:*)", "Bash(gh issue comment:*)", "Bash(gh issue edit:*)",
                      "Bash(gh pr comment:*)", "Bash(notme:*)")
BLANKET_RULES = ("Bash", "Bash(*)", "Bash(gh:*)", "Bash(gh *)")  # cover the rules, but allow far too much
_PREFIX_RULE = re.compile(r"^Bash\((?P<prefix>.+?)(?::\*| \*)\)$")


def permission_rules(launcher: str) -> List[str]:
    """The recommended allow rules: the housekeeping ones and this plugin's launcher by its full path."""
    return [*HOUSEKEEPING_RULES, f"Bash({launcher}:*)"]


def _covers(have: str, want: str) -> bool:
    """True when the allow rule `have` allows everything the recommended prefix rule `want` does."""
    if have == want or have in ("Bash", "Bash(*)"):
        return True
    h, w = _PREFIX_RULE.match(have), _PREFIX_RULE.match(want)
    return bool(h and w) and (w.group("prefix") + " ").startswith(h.group("prefix") + " ")


def permissions_check(files: Sequence[Any], launcher: str) -> Check:
    """Which recommended permissions.allow rules no settings file has. files: settings.json paths (user, project,
    project local); a missing one is skipped, an unreadable one counts as empty and is named. Never writes."""
    allow: List[str] = []
    unreadable: List[str] = []
    for f in files:
        path = Path(f)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rules = ((data.get("permissions") or {}).get("allow") or []) if isinstance(data, dict) else []
        except (OSError, ValueError, AttributeError):
            unreadable.append(str(path))
            continue
        allow += [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else []
    wanted = permission_rules(launcher)
    missing = [w for w in wanted if not any(_covers(h, w) for h in allow)]
    notes = [f"{', '.join(unreadable)} could not be read"] if unreadable else []
    blanket = [r for r in allow if r in BLANKET_RULES]
    if blanket:
        notes.append(f"{', '.join(blanket)} allows more than not-me needs (repo and release deletes too)")
    if not missing:
        return check("permissions", True, "; ".join(["every recommended allow rule is set", *notes]), required=False)
    add = json.dumps({"permissions": {"allow": missing}}, indent=2)
    detail = f"{len(missing)} of {len(wanted)} recommended allow rules missing: {', '.join(missing)}"
    return check("permissions", False, "; ".join([detail, *notes]), required=False,
                 hint="add these to permissions.allow in ~/.claude/settings.json yourself (see docs/permissions.md); "
                      f"doctor does not edit settings:\n{add}")


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
        return problems  # an unconfirmed board is not read
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
    column is not an option of its status field. Labels with queue.sync on: the board checks as well, and a board
    that is not set at all. Either: both the label and the board checks. Only reads; a GitHub error reports the check as not run."""
    where = describe(meta)
    if not where["ok"]:
        return _queue_failed(f"{where['summary']}; fix it with {QUEUE_FIX}")
    q = meta.get("queue") or {}
    try:
        if where["source"] == "labels":
            problems = _label_problems(q, where, gh, tracker)
            if q.get("sync") is True:
                if board_ref(q) is None:
                    problems.append("queue.sync is on and queue.board is not set, so the board Status cannot "
                                    "follow; set queue.board.owner and queue.board.number in project.md or turn "
                                    "queue.sync off")
                else:
                    problems += _board_problems(q, gh, confirmed_board, origin)
        elif where["source"] == "either":
            problems = _label_problems(q, where, gh, tracker) + _board_problems(q, gh, confirmed_board, origin)
        else:
            problems = _board_problems(q, gh, confirmed_board, origin)
    except GhError as exc:
        return queue_not_run(f"GitHub could not be read ({exc})", where["summary"])
    except ValueError as exc:  # a board owner or status field name the client refuses to put in a query
        return _queue_failed(f"{where['summary']}, but {exc}; fix it with {QUEUE_FIX}")
    if problems:
        return _queue_failed(f"{where['summary']}, but " + "; ".join(problems))
    return check("queue", True, where["summary"], required=False)


DRIFT_FIX = "run `notme queue-resync` (a dry run records what it would change; in live mode it sets each one)"
DRIFT_SKIPPED_FIX = "fix what each skipped issue names (the `queue.board_states` or `labels` key to set)"
DRIFT_SHOWN = 10


def drift_check(drift: Sequence[Mapping[str, Any]], board: str, truncated: bool = False, read: int = 0) -> Check:
    """A warning listing each open issue whose state label and board Status disagree (entries from
    queue.resync.read), the first few in full. An entry resync would skip carries its reason and the key to set
    instead of the resync advice. When the board read hit its cap (truncated), it says drift beyond the first
    `read` items is not listed. Read-only."""
    note = (f"; only the first {read} board items were read, so drift beyond them is not listed"
            if truncated else "")
    if not drift:
        if truncated:
            return check("queue drift", False, f"no drift among the first {read} items of board {board}, but the "
                         "board has more items, which were not read", required=False)
        return check("queue drift", True, f"labels and the Status of board {board} agree", required=False)
    lines = []
    for d in drift[:DRIFT_SHOWN]:
        line = f"{d['url']} has label `{d['label']}` but Status `{d['status']}`"
        if d.get("skip"):
            line += f" (resync would skip it: {d['skip']})"
        lines.append(line)
    more = f"; and {len(drift) - DRIFT_SHOWN} more" if len(drift) > DRIFT_SHOWN else ""
    skipped = sum(1 for d in drift if d.get("skip"))
    fix = DRIFT_SKIPPED_FIX if skipped == len(drift) else DRIFT_FIX + (f"; then {DRIFT_SKIPPED_FIX}" if skipped else "")
    return check("queue drift", False, f"{len(drift)} open issue(s) out of step on board {board}: "
                 + "; ".join(lines) + more + note, required=False, fix=fix)


def drift_not_run(why: str) -> Check:
    return check("queue drift", True, f"not run: {why}", required=False)


def run(runner: Callable[[Sequence[str]], Any], config_errors: List[str], gh: Callable[[], GhClient],
        guard_state: Callable[[], Mapping[str, Any]], env: Mapping[str, str],
        mode: Optional[Check] = None, config_files: Optional[Mapping[str, Sequence[str]]] = None,
        pass_labels: Optional[Callable[[], Optional[Check]]] = None,
        queue: Optional[Callable[[], Check]] = None,
        drift: Optional[Callable[[], Optional[Check]]] = None,
        permissions: Optional[Callable[[], Check]] = None) -> List[Check]:
    """Every probe in order. gh, guard_state, pass_labels, queue and drift are called only when needed (gh needs a
    valid config; pass_labels, queue and drift also a logged-in gh, and the queue check is reported as not run
    otherwise; drift returns None when queue.board is not set and adds no check then).
    config_files maps a layer to the config docs found in it; the "config files" probe runs only when it is
    given; permissions adds the recommended allow rules check (it reads settings files, never writes them)."""
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
    if drift is not None and signed_in:
        found = drift()
        if found is not None:
            checks.append(found)
    checks += [guard_check(guard_state()), depth_check(env)]
    if permissions is not None:
        checks.append(permissions())
    return checks


def exit_kind(checks: Sequence[Check]) -> Optional[str]:
    """None when every required check passed; "dependency" when only gh checks failed; else "failed"."""
    failed = {c["name"] for c in checks if c["required"] and not c["ok"]}
    if not failed:
        return None
    return "dependency" if failed <= GH_CHECKS else "failed"
