"""The `notme` command line: wires the not-me modules into subcommands.

Contract
    notme [--project-dir P] [--repo O/N] [--dry-run] [--json] <command> ...

stdout is exactly one JSON object per invocation, on success and on error alike (only `--help` prints text).
stderr is chatter. Credentials in URLs and token-shaped strings are redacted from both. Text inputs are always
file paths (--body-file, --title-file, ...) of at most 1 MB, inside the session project or not-me's temp
directory. The global flags are also accepted after the subcommand.

Trust anchor: the session project is the main checkout of the git toplevel of the working directory. Restricted
mode is the default: a --project-dir or CLAUDE_PROJECT_DIR naming another checkout is refused and NOT_ME_HOME is
ignored. Only a person gets those honoured: stdin is a terminal, neither CLAUDECODE nor CLAUDE_PROJECT_DIR is set,
and no ancestor process is Claude Code (the CLI or the desktop app; the app's own terminal panel is a child of it,
so use a separate terminal application). When the process ancestry cannot be read, the CLI stays restricted.
NOT_ME_DRY_RUN can only turn dry-run on. `allow-repo add|remove`, `pause off` and `mode live` also need the
private operator token in <not-me home>/operator-token (create it with `notme doctor --init-token`): a person types
it at a hidden prompt, or a script sets NOT_ME_OPERATOR_CONFIRM to it.
The PreToolUse guard is the enforcement; this check is a second layer.

--dry-run, NOT_ME_DRY_RUN=1 or `mode: dry-run` send every write to the intended-writes ledger instead of
GitHub; so does a `mode: live` that `notme mode live` did not sign (see `mode show`). Write commands then carry "dry_run": true. Write commands take --log PATH (an existing run log under
the log root) and append an ACTION line to it.

Each handler's docstring is its reference entry: the first paragraph says what it does, the `Output:` line lists
its JSON keys. scripts/gen-cli-docs.py turns the command registry into docs/cli.md.
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import re
import secrets
import shlex
import sys
import tempfile
import time
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple, Type, Union

_PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_DIR not in sys.path:  # lets scripts load this file by path (the docs generator, the skill lint)
    sys.path.insert(0, _PLUGIN_DIR)

from notme import claims, config as config_mod, doctor as doctor_mod, feedback as feedback_mod  # noqa: E402
from notme import gh as gh_mod, git as git_mod, guard as guard_mod, paths, prove, runlog, states  # noqa: E402
from notme import humans, modeswitch, roles as roles_mod, verifyplan, writeback, writes  # noqa: E402
from notme.dryrun import LedgerError  # noqa: E402
from notme.gh import csv, names_of, login_of  # noqa: E402
from notme.queue import NotOpen, board_ref, describe as describe_queue, make_queue, stale_claims  # noqa: E402
from notme.queue import resync as resync_mod  # noqa: E402
from notme.queue.signals import picking_signals  # noqa: E402
from notme.queue.sync import MirrorFailed  # noqa: E402
from notme.redact import redact, redact_obj  # noqa: E402
from notme.stacking import last_editor  # noqa: E402


def launcher_path() -> str:
    """The absolute path of this plugin's `notme` launcher, for commands a person pastes into a terminal."""
    return str(Path(_PLUGIN_DIR) / "bin" / "notme")


def shell_name() -> str:
    """The shell the commands handed to a person are written for. bin/notme is a bash script, so on Windows that
    is Git Bash (not PowerShell or cmd); elsewhere any POSIX shell."""
    return "git-bash" if os.name == "nt" else "posix"


def shell_command(launcher: str, argv: Sequence[str], work_dir: Optional[str]) -> str:
    """`cd <work_dir> && <launcher> <argv>`, quoted for a POSIX shell; on Windows the launcher is run through
    `bash` explicitly, for Git Bash."""
    run = " ".join(shlex.quote(a) for a in [launcher, *argv])
    if os.name == "nt":
        run = "bash " + run
    return f"cd {shlex.quote(work_dir)} && {run}" if work_dir else run


class ExitCode(IntEnum):
    """Every exit code, with the line the reference docs show for it."""

    def __new__(cls, value: int, description: str) -> "ExitCode":
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj.description = description  # type: ignore[attr-defined]
        return obj

    OK = 0, "ok, including a {\"none\": true} result"
    ERROR = 1, "error: {\"error\", \"kind\"}"
    USAGE = 2, "usage"
    CLAIM_LOST = 3, "claim lost: {\"claim_lost\": true, ...}"
    CONFIG = 4, "config invalid: {\"errors\": [...]}"
    READBACK = 5, "read-back mismatch"
    BLOCKED = 6, "write target not allowed: {\"blocked\", \"repo\", \"reason\", \"allow_cmd\"}"
    DEPENDENCY = 7, "missing dependency (gh, git) or missing auth scope"
    GUARD = 8, "guard enabled but not working"


PAUSED_FILE = "PAUSED"
MAX_INPUT_BYTES = 1024 * 1024
CLAUDE_ENV = ("CLAUDECODE", "CLAUDE_PROJECT_DIR")  # either one keeps the CLI restricted; never a reason to relax
CONFIRM_ENV = "NOT_ME_OPERATOR_CONFIRM"
_AUTH_RE = re.compile(r"(?i)gh auth login|not logged in|bad credentials|http 401|requires?\b.*\bscopes?\b|"
                      r"missing\b.*\bscopes?\b|token has not been granted|authentication required")
_REF_RES = (
    re.compile(r"^https?://[^/\s]+/(?P<slug>[^/\s]+/[^/\s]+)/(?:pull|issues)/(?P<n>\d+)(?:[/?#].*)?$"),
    re.compile(r"^(?P<slug>[^/\s#]+/[^/\s#]+)#(?P<n>\d+)$"),
    re.compile(r"^#?(?P<n>\d+)$"),
)


# --- errors ------------------------------------------------------------------


class CliExit(Exception):
    """Stop with this exit code and JSON object."""

    def __init__(self, code: int, payload: Dict[str, Any]):
        super().__init__(str(payload.get("error", "")))
        self.code = code
        self.payload = payload


def fail(code: int, kind: str, message: str, **extra: Any) -> CliExit:
    return CliExit(code, {"error": message, "kind": kind, **extra})


class UsageError(Exception):
    pass


class Halted(Exception):
    """not-me is paused or disabled; reason is "paused" or "disabled"."""

    def __init__(self, reason: str):
        super().__init__(f"not-me is {reason}")
        self.reason = reason


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(f"{self.prog}: {message}")


def _gh_missing(exc: BaseException) -> bool:
    return isinstance(exc, gh_mod.GhError) and exc.returncode == 127


def _gh_auth(exc: BaseException) -> bool:
    return isinstance(exc, gh_mod.GhError) and bool(_AUTH_RE.search(exc.stderr or ""))


def _tool_missing(exc: BaseException) -> bool:
    return isinstance(exc, FileNotFoundError) and getattr(exc, "filename", None) in ("git", "gh")


def _blocked(exc: Any) -> Dict[str, Any]:
    body = exc.to_json()
    body.setdefault("allow_cmd", exc.decision.allow_cmd)
    return {"error": exc.decision.reason, **body, **({"target": exc.target.describe()} if exc.target else {})}


Matcher = Union[Type[BaseException], Tuple[Type[BaseException], ...], Callable[[BaseException], bool]]

# (exception type or predicate, exit code, kind, extra fields) walked in order; the first match wins. The
# message is str(exc) unless the extras give "error".
ERRORS: List[Tuple[Matcher, ExitCode, str, Optional[Callable[[Any], Dict[str, Any]]]]] = [
    (UsageError, ExitCode.USAGE, "usage", None),
    (Halted, ExitCode.ERROR, "halted", lambda e: {"kind": e.reason}),
    (gh_mod.WriteBlocked, ExitCode.BLOCKED, "write_blocked", _blocked),
    (claims.ClaimLost, ExitCode.CLAIM_LOST, "claim_lost", lambda e: {"claim_lost": True, "message": str(e)}),
    (gh_mod.ReadbackMismatch, ExitCode.READBACK, "readback_mismatch",
     lambda e: {"expected": e.expected, "actual": e.actual}),
    (writeback.WritebackMismatch, ExitCode.READBACK, "readback_mismatch", None),
    (states.LabelCollision, ExitCode.CONFIG, "config_invalid", lambda e: {"errors": [str(e)]}),
    (_gh_missing, ExitCode.DEPENDENCY, "missing_dependency",
     lambda e: {"error": "gh is not installed or not on PATH", "dependency": "gh"}),
    (_gh_auth, ExitCode.DEPENDENCY, "auth", None),
    (gh_mod.GhError, ExitCode.ERROR, "gh_error", None),
    (gh_mod.UnsafePath, ExitCode.ERROR, "unsafe_path", None),
    (git_mod.GitError, ExitCode.ERROR, "git_error", None),
    (git_mod.ProtectedBranchError, ExitCode.ERROR, "protected_branch", None),
    (NotOpen, ExitCode.ERROR, "not_open", None),
    (MirrorFailed, ExitCode.ERROR, "sync_failed", None),
    (states.StateMismatch, ExitCode.ERROR, "state_mismatch", None),
    (states.IllegalTransition, ExitCode.ERROR, "illegal_transition", None),
    ((LookupError, runlog.ActiveRunError), ExitCode.ERROR, "not_found", None),
    (_tool_missing, ExitCode.DEPENDENCY, "missing_dependency",
     lambda e: {"error": f"{e.filename} is not installed or not on PATH", "dependency": e.filename}),
    (LedgerError, ExitCode.ERROR, "ledger", None),
    (modeswitch.ModeEditError, ExitCode.ERROR, "mode_edit_refused", None),
    (ValueError, ExitCode.USAGE, "usage", None),
    (OSError, ExitCode.ERROR, "io", lambda e: {"error": f"{e.__class__.__name__}: {e.strerror or e}"}),
]


def _matches(matcher: Matcher, exc: BaseException) -> bool:
    if isinstance(matcher, tuple) or isinstance(matcher, type):
        return isinstance(exc, matcher)
    return bool(matcher(exc))


def error_response(exc: BaseException, deps: "Deps") -> Tuple[int, Dict[str, Any]]:
    """Map an exception to (exit code, JSON object)."""
    if isinstance(exc, CliExit):
        return exc.code, exc.payload
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip():
        deps.chatter(stderr.strip()[:500])
    for matcher, code, kind, extras in ERRORS:
        if _matches(matcher, exc):
            return int(code), {"error": str(exc), "kind": kind, **(extras(exc) if extras else {})}
    return int(ExitCode.ERROR), {"error": f"unexpected {exc.__class__.__name__}", "kind": "internal"}


# --- runtime context -----------------------------------------------------------


class Deps:
    """Everything the process supplies; tests replace the pieces."""

    def __init__(self, runner: Optional[gh_mod.Runner] = None, env: Optional[Mapping[str, str]] = None,
                 stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
                 hook_cmd: Optional[List[str]] = None, cwd: Optional[str] = None,
                 argv: Sequence[str] = (), sleep: Optional[Callable[[float], None]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 process_check: Optional[Callable[[], bool]] = None,
                 prompt: Optional[Callable[[], Optional[str]]] = None):
        self.runner = runner
        self.sleep: Callable[[float], None] = sleep or time.sleep
        self.clock: Callable[[], float] = clock or time.monotonic
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.hook_cmd = hook_cmd
        self.cwd = cwd
        self.argv = list(argv)
        self.process_check = process_check or humans.under_claude_process_tree
        # The hidden prompt reads the process's own terminal, so it is offered only when stdin is the process's own
        # (an injected stdin, as in tests, gets no prompt unless one is injected too).
        self.prompt: Optional[Callable[[], Optional[str]]] = prompt or (humans.ask_token if stdin is None else None)

    def isatty(self) -> bool:
        """True only when stdin is a real terminal (a person typing, not a model's tool call)."""
        try:
            return bool(self.stdin.isatty())
        except (AttributeError, ValueError, OSError):
            return False

    def chatter(self, text: str) -> None:
        print(redact(text), file=self.stderr)


def _toplevel(path: Any) -> Optional[Path]:
    top = paths.run_git(["rev-parse", "--show-toplevel"], path)
    return Path(top).resolve() if top else None


def _run_fields(marker: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: marker.get(k) for k in ("run_id", "skill", "ref", "started")}


class Context:
    """Trust anchor, config, write gate and GitHub client for one invocation (the last three built on use)."""

    def __init__(self, args: argparse.Namespace, deps: Deps):
        self.args, self.deps = args, deps
        self.warnings: List[str] = []
        self.log_warnings: List[str] = []  # warnings that also go to the run log as WARN lines (see main)
        self.log_reasons: List[str] = []  # why each state changed, for the run log as ACTION lines (see main)
        # Restricted is the default. A tty alone proves nothing (a pty wrapper fakes one), so a person also needs no
        # Claude Code variable and no Claude Code ancestor process; an unreadable ancestry counts as Claude Code.
        self.restricted = (any(deps.env.get(k) for k in CLAUDE_ENV) or not deps.isatty()
                           or bool(deps.process_check()))
        self.session_project, self.work_dir = self._anchor()
        self.env = self._trusted_env()
        self._cfg: Optional[config_mod.Config] = None
        self._guard: Optional[writes.GuardContext] = None
        self._gh: Optional[gh_mod.GhClient] = None
        self._hook_guard: Optional[guard_mod.GuardContext] = None
        self._operator: Optional[str] = None
        self._run_id: Optional[str] = None

    # --- where: the trust anchor ---

    def _anchor(self) -> Tuple[Optional[Path], Optional[Path]]:
        """(session project, work dir). The session project is the main checkout of the cwd's git toplevel when
        restricted (the default); for a person at a terminal an override is believed. The work dir is the
        override (a linked worktree stays itself) or the cwd's toplevel; None when there is none."""
        requested = getattr(self.args, "project_dir", None) or self.deps.env.get("CLAUDE_PROJECT_DIR")
        cwd_top = _toplevel(self.deps.cwd or os.getcwd())
        if not self.restricted:
            if requested:
                work = Path(requested).resolve() if Path(requested).is_dir() else None
            else:
                work = cwd_top
            return (paths.main_checkout(work) if work else None), work
        session = paths.main_checkout(cwd_top) if cwd_top else None
        if not requested:
            return session, cwd_top
        where = "--project-dir" if getattr(self.args, "project_dir", None) else "CLAUDE_PROJECT_DIR"
        if session is None:
            raise fail(ExitCode.ERROR, "project_override_refused",
                       f"{where} given but the working directory is not inside a git checkout, so it cannot be "
                       "checked against the session project")
        req_top = _toplevel(requested) if Path(requested).is_dir() else None
        if req_top is None or paths.main_checkout(req_top) != session:
            raise fail(ExitCode.ERROR, "project_override_refused",
                       f"{where} names a different checkout than the session project; run notme from inside "
                       "the project instead")
        return session, Path(requested).resolve()

    def _trusted_env(self) -> Dict[str, str]:
        env = dict(self.deps.env)
        env.pop(runlog.LOG_ROOT_ENV, None)  # the log root comes from the global layer only
        if self.work_dir is not None:
            env["CLAUDE_PROJECT_DIR"] = str(self.work_dir)
        else:
            env.pop("CLAUDE_PROJECT_DIR", None)
        if self.restricted:
            if env.pop("NOT_ME_HOME", None):
                self.warn("NOT_ME_HOME is honoured only for a person at a terminal; using the default global layer")
            env["CLAUDE_PLUGIN_ROOT"] = _PLUGIN_DIR
        return env

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        self.deps.chatter(f"notme: warning: {text}")

    def sync_warnings(self, queue: Any) -> None:
        """Report the queue.sync mirror failures of a claim, move or release: in the output's warnings, and as
        WARN lines in the run log when --log is given."""
        for text in getattr(queue, "sync_warnings", None) or []:
            self.warn(text)
            self.log_warnings.append(text)

    def read_reason(self, path: str, repo: Optional[str] = None) -> str:
        """The text of a --reason-file, read before anything is written so a bad file changes nothing. One line of
        at most REASON_MAX_CHARS characters, with no HTML comment (so never a not-me marker that a later read would
        honour), and clean under check_text like any other posted text."""
        text = self.read_line(path)
        if not text:
            raise fail(ExitCode.USAGE, "usage", "--reason-file is empty: write one line saying why")
        if len(text) > REASON_MAX_CHARS:
            raise fail(ExitCode.USAGE, "usage", f"--reason-file is {len(text)} characters; keep the reason to one "
                                                f"line of at most {REASON_MAX_CHARS}")
        if "<!--" in text or "-->" in text:
            raise fail(ExitCode.USAGE, "usage", "--reason-file may not contain an HTML comment or a not-me marker")
        problems = writeback.check_text(text, self.tracker, repo or self.tracker, writeback.attribution(self.cfg),
                                        False)
        if problems:
            raise fail(ExitCode.USAGE, "usage", "--reason-file text is not clean: " + "; ".join(problems))
        return text

    def say_why(self, repo: str, n: int, change: Dict[str, Any], reason: str, post: bool = True) -> bool:
        """After a state change succeeded: the old state, the new state (with the label and board column written
        alongside, when there are any) and the reason go to the run log, and, when the new state is one the
        timeline cannot explain (states.EXPLAINED), to one comment with the run marker. A comment that fails is a
        warning, also in the run log, and never undoes the change. True when it was posted."""
        def shown(state: Any) -> str:
            try:
                return self.labels.name(state)
            except (KeyError, TypeError):
                return str(state)
        sides = [f"{what} `{change[key]}`" for key, what in (("label", "label"), ("column", "board column"))
                 if change.get(key)]
        text = (f"Moved from `{shown(change.get('from'))}` to `{shown(change.get('to'))}`"
                + (f" ({', '.join(sides)})" if sides else "") + f": {reason}")
        return self.post_reason(repo, n, text, post and change.get("to") in states.EXPLAINED)

    def post_reason(self, repo: str, n: int, text: str, post: bool = True) -> bool:
        """Log a reason for the run log and, when `post`, post it as a comment (with the run marker). A failed
        comment is a warning, also in the run log; it never undoes the state change it explains. True when it was
        posted."""
        self.log_reasons.append(f"{repo}#{n} {text}")
        if not post:
            return False
        try:
            writeback.state_change_comment(self.gh, repo, n, text, self.run_id)
            return True
        except (gh_mod.GhError, gh_mod.WriteBlocked, gh_mod.ReadbackMismatch, ValueError, OSError) as exc:
            warning = (f"the reason comment on {repo}#{n} was not posted ({exc}); the state change stands, "
                       "say why by hand")
            self.warn(warning)
            self.log_warnings.append(warning)
            return False

    def state_labels(self, names: Sequence[str]) -> List[str]:
        """The names among `names` that are state labels (labels.* in project.md); pass labels and others are not."""
        return [x for x in names if self.labels.knows(x)]

    def say_labels(self, repo: str, n: int, added: Sequence[str], removed: Sequence[str], reason: str,
                   post: bool = True) -> bool:
        """A change of state labels made outside a move: which were added, which removed, why. Always logged;
        posted as one comment only when `post` and an added label is a state the timeline cannot explain
        (states.EXPLAINED). Removing a label or putting one back is mechanical and only logged."""
        parts = [f"{verb} " + ", ".join(f"`{x}`" for x in names)
                 for verb, names in (("added", added), ("removed", removed)) if names]
        explained = any(self.labels.abstract(x) in states.EXPLAINED for x in added)
        return self.post_reason(repo, n, f"State labels changed ({'; '.join(parts)}): {reason}", post and explained)

    def need_work_dir(self) -> Path:
        if self.work_dir is None:
            raise fail(ExitCode.USAGE, "usage", "no project directory: run inside a clone (or pass --project-dir)")
        return self.work_dir

    def require_human(self, what: str) -> None:
        """Refuse `what` unless a person runs it (not restricted) and gives the operator token: in
        NOT_ME_OPERATOR_CONFIRM, or, when that is unset, at a hidden prompt (asked only after the terminal and
        process checks pass, so the prompt never relaxes anything on its own). A wrong or empty answer is the same
        refusal, which carries the command to run in a terminal that Claude Code did not start."""
        if not self.restricted and humans.token_matches(self.cfg.home, self._confirmation()):
            return
        raise fail(ExitCode.ERROR, "human_only",
                   f"{what} is for a person: run the terminal_cmd command in a terminal application that Claude "
                   "Code did not start (not the desktop app's terminal panel); it asks for the operator token "
                   f"(create one with `notme doctor --init-token`); {humans.SECOND_LAYER}",
                   terminal_cmd=self.terminal_command(self.deps.argv), shell=shell_name(), note=humans.SECOND_LAYER)

    def _confirmation(self) -> Optional[str]:
        """The token from NOT_ME_OPERATOR_CONFIRM, else (when unset and a token exists) typed at the prompt."""
        given = self.deps.env.get(CONFIRM_ENV)
        if given is not None or self.deps.prompt is None or not humans.token_path(self.cfg.home).is_file():
            return given
        return self.deps.prompt()

    def terminal_command(self, argv: Sequence[str]) -> str:
        """argv as a command a person can paste into a fresh terminal: cd into the project, then the absolute
        launcher (never ${CLAUDE_PLUGIN_ROOT}, which only Claude Code sets)."""
        return shell_command(launcher_path(), argv, str(self.work_dir) if self.work_dir else None)

    # --- config ---

    @property
    def cfg(self) -> config_mod.Config:
        if self._cfg is None:
            project = self.work_dir or getattr(self.args, "project_dir", None)
            self._cfg = config_mod.load(project_dir=project, env=self.env)
        return self._cfg

    def reload_cfg(self) -> None:
        """Forget the loaded config so the next use reads the layers again (after a command changed one)."""
        self._cfg = None

    def config_errors(self) -> List[str]:
        return self.cfg.check() + states.label_collisions(self.meta.get("labels") or {})

    def require_valid(self) -> config_mod.Config:
        errors = self.config_errors()
        if errors:
            raise CliExit(ExitCode.CONFIG, {"error": "the not-me configuration is invalid", "kind": "config_invalid",
                                            "errors": errors})
        return self.cfg

    @property
    def meta(self) -> Dict[str, Any]:
        return self.cfg.meta.get("project", {})

    def trusted_meta(self, doc: str) -> Dict[str, Any]:
        """A document's keys from the default and global layers only (the repo layer is committed, so a write
        there must not loosen a security key). Maps one level down are merged key by key."""
        out: Dict[str, Any] = {}
        for layer in ("default", "global"):
            found = self.cfg.docs.get(doc, {}).get(layer)
            for key, value in (found.meta if found else {}).items():
                old = out.get(key)
                out[key] = {**old, **value} if isinstance(old, dict) and isinstance(value, dict) else value
        return out

    @property
    def labels(self) -> states.Labels:
        return states.Labels(self.meta.get("labels") or {})

    @property
    def mode_is_dry(self) -> bool:
        """--dry-run, NOT_ME_DRY_RUN=1 or mode dry-run; the env can only turn it on."""
        return (bool(getattr(self.args, "dry_run", False)) or self.env.get("NOT_ME_DRY_RUN") == "1"
                or str(self.meta.get("mode") or "dry-run") == "dry-run")

    # --- write gate and client ---

    @property
    def guard(self) -> writes.GuardContext:
        if self._guard is None:
            base = writes.GuardContext.from_config(self.require_valid(), checkout=self.work_dir)
            repo = getattr(self.args, "repo", None)
            if repo and not base.origin_repo:
                if not writes.valid_slug(repo):
                    raise fail(ExitCode.USAGE, "usage", f"invalid --repo {repo!r}: expected owner/name")
                base = dataclasses.replace(base, origin_repo=repo)
            self._guard = dataclasses.replace(base, dry_run=self.mode_is_dry)
        return self._guard

    @property
    def gh(self) -> gh_mod.GhClient:
        if self._gh is None:
            self._gh = gh_mod.GhClient(self.guard, runner=self.deps.runner)
        return self._gh

    @property
    def repo(self) -> str:
        slug = getattr(self.args, "repo", None) or self.guard.origin_repo
        if not slug:
            raise fail(ExitCode.USAGE, "usage", "no repository: pass --repo owner/name or run inside a clone "
                                                  "with a GitHub origin")
        if not writes.valid_slug(slug):
            raise fail(ExitCode.USAGE, "usage", f"invalid repository {slug!r}: expected owner/name")
        return slug

    @property
    def tracker(self) -> str:
        configured = self.meta.get("tracker")
        if configured not in (None, "auto") and writes.valid_slug(configured):
            return str(configured)
        return self.repo

    @property
    def operator(self) -> str:
        if self._operator is None:
            configured = self.meta.get("operator")
            self._operator = configured if isinstance(configured, str) and configured != "auto" else self.gh.me()
        return self._operator

    @property
    def roster(self) -> Any:
        return self.cfg.effective_roster(self.operator)

    def queue(self, kind: str = "issue", repo: Optional[str] = None) -> Any:
        return make_queue(self.cfg, self.gh, self.roster, self.operator, self.run_id, kind=kind,
                          repo=repo or self.tracker, project_dir=self.work_dir)

    def protected(self, branch: str) -> bool:
        patterns = self.guard.protected_branches if self.guard.rules.get("protected_branches", True) else ()
        return any(fnmatch.fnmatchcase(branch, p) for p in patterns)

    def ref(self, value: str, default_repo: Optional[str] = None) -> Tuple[str, int]:
        """(repo, number) from `N`, `#N`, `owner/name#N` or an issue or pull request URL."""
        text = str(value).strip()
        for pattern in _REF_RES:
            m = pattern.match(text)
            if not m:
                continue
            slug = m.groupdict().get("slug") or default_repo or self.repo
            if not writes.valid_slug(slug):
                break
            return slug, int(m.group("n"))
        raise fail(ExitCode.USAGE, "usage", f"cannot read {text!r} as a reference: use N, owner/name#N or a URL")

    # --- state: pause, guard ---

    def pause_files(self) -> Dict[str, Path]:
        out = {"global": Path(self.cfg.home or paths.home_dir(self.env)) / PAUSED_FILE}
        if self.work_dir is not None:
            common = paths.git_common_dir(self.work_dir)
            out["project"] = (common / "not-me" if common else self.work_dir / ".not-me") / PAUSED_FILE
        return out

    def halted(self) -> Optional[str]:
        if self.meta.get("enabled") is False:
            return "disabled"
        if any(p.exists() for p in self.pause_files().values()):
            return "paused"
        return None

    def require_running(self) -> None:
        reason = self.halted()
        if reason:
            raise Halted(reason)

    @property
    def hook_guard(self) -> guard_mod.GuardContext:
        """The PreToolUse guard's view of this project."""
        if self._hook_guard is None:
            self._hook_guard = guard_mod.GuardContext.from_project(self.work_dir or ".", home=self.cfg.home,
                                                                   env=self.env)
        return self._hook_guard

    def hook_cmd(self) -> List[str]:
        if self.deps.hook_cmd:
            return list(self.deps.hook_cmd)
        return [sys.executable, str(paths.plugin_root(self.env) / "hooks" / "repo_guard.py")]

    def guard_state(self) -> Dict[str, Any]:
        """{active, selftest[, selftest_stderr]}: selftest is None when the guard is not active in this project; a
        failed self-test carries the hook's stderr (redacted, first 500 characters). The hook always runs as
        [sys.executable, <plugin>/hooks/repo_guard.py], never through the bash launcher."""
        if not guard_mod.is_active(self.hook_guard):
            return {"active": False, "selftest": None}
        ok, stderr = guard_mod.selftest_run(self.hook_cmd(), self.hook_guard, env=self.env)
        return {"active": True, "selftest": ok, **({} if ok else {"selftest_stderr": redact(stderr)[:500]})}

    # --- run log ---

    def log_root(self, create: bool = False) -> Path:
        """`logs.root` from the global layer, else <git-common-dir>/not-me/logs (never an env override)."""
        found = self.cfg.docs.get("project", {}).get("global")
        configured = ((found.meta.get("logs") or {}).get("root")) if found else None
        return runlog.log_root(self.work_dir, config_root=configured, env={}, create=create)

    def open_log(self, path: str) -> runlog.RunLog:
        """An existing run log under the log root (its first line a run-log title); else a usage error."""
        try:
            root: Optional[Path] = Path(os.path.realpath(str(self.log_root())))
        except ValueError:
            root = None
        real = Path(os.path.realpath(path))
        if root is None or root not in real.parents or real.suffix != ".md" or not real.is_file() \
                or not runlog.is_run_log(real):
            raise fail(ExitCode.USAGE, "usage", "the log path must be an existing run log under the log root")
        return runlog.RunLog.open(real, project_dir=self.work_dir)

    @property
    def run_id(self) -> str:
        if self._run_id is None:
            run = self.env.get("NOT_ME_RUN_ID")
            if not run:
                try:
                    active = runlog.active_run(runlog.default_marker_path(self.work_dir))
                except (ValueError, OSError):
                    active = None
                run = (active or {}).get("run_id")
            self._run_id = str(run or "cli-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                               + "-" + secrets.token_hex(3))
        return self._run_id

    def active_run(self) -> Optional[Dict[str, Any]]:
        """The run active in this worktree, or None."""
        try:
            marker = runlog.active_run(runlog.default_marker_path(self.work_dir))
        except (ValueError, OSError):
            return None
        return _run_fields(marker) if marker else None

    def active_runs(self) -> List[Dict[str, Any]]:
        """Every run active in any worktree of this checkout, main worktree first."""
        if self.work_dir is None:
            return []
        try:
            return [_run_fields(m) for m in runlog.checkout_active_runs(self.work_dir)]
        except (ValueError, OSError):
            return []

    # --- input files ---

    def read_input(self, path: str) -> str:
        real = gh_mod.check_input_path(path, self.session_project)
        if real.stat().st_size > MAX_INPUT_BYTES:
            raise fail(ExitCode.ERROR, "input_too_large", f"{real.name} is larger than {MAX_INPUT_BYTES} bytes")
        return real.read_text(encoding="utf-8")

    def read_json(self, path: str, what: str) -> Any:
        try:
            return json.loads(self.read_input(path))
        except ValueError as exc:
            raise fail(ExitCode.USAGE, "usage", f"{what} is not valid JSON: {exc}") from None

    def read_line(self, path: str) -> str:
        return " ".join(self.read_input(path).split())


# --- the command registry --------------------------------------------------------

Arg = Tuple[Tuple[str, ...], Dict[str, Any]]


def arg(*flags: str, **kw: Any) -> Arg:
    return flags, kw


class OneOf:
    """Arguments that may not be given together (an argparse mutually exclusive group)."""

    def __init__(self, *args: Arg):
        self.args = args


ref_arg = arg("ref", metavar="REF")
number_arg = arg("number", metavar="N")
body_file_arg = arg("--body-file", required=True, metavar="F")
REASON_MAX_CHARS = 500  # the longest reason a state-change comment carries
reason_file_arg = arg("--reason-file", required=True, metavar="F",
                      help="file holding one line that says why; goes to the run log, and to one comment on the "
                           "item when the new state needs a person or stops work")
title_file_arg = arg("--title-file", required=True, metavar="F")
labels_arg = arg("--labels", metavar="a,b")


class Command:
    """One registered subcommand. (A plain class: the skill lint loads this file outside sys.modules, where
    dataclasses cannot resolve it.)

    running: "none" turns paused or disabled into {"none": true, "reason"}; "error" makes it exit 1.
    ref: "repo" or "tracker" hands the handler (repo, n) read from args.ref or args.number.
    """

    def __init__(self, path: Tuple[str, ...], handler: Callable[..., Dict[str, Any]], help: str, writes: bool,
                 args: Sequence[Union[Arg, "OneOf"]], running: Optional[str], ref: Optional[str]):
        self.path, self.handler, self.help, self.writes = path, handler, help, writes
        self.args, self.running, self.ref = list(args), running, ref

    @property
    def name(self) -> str:
        return " ".join(self.path)

    def run(self, ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
        if self.running:
            try:
                ctx.require_running()
            except Halted as exc:
                if self.running == "none":
                    return {"none": True, "reason": exc.reason}
                raise
        if self.ref:
            value = args.ref if hasattr(args, "ref") else args.number
            repo, n = ctx.ref(value, ctx.tracker if self.ref == "tracker" else None)
            return self.handler(ctx, args, repo, n) or {}
        return self.handler(ctx, args) or {}


COMMANDS: List[Command] = []
GROUPS = {"log": "run logs", "verify-lock": "the shared test environment lock (verify.md shared_env)", "retro": "retro audits kept in the git directory", "allow-repo": "the global allow list of repos and boards", "guard": "the write guard",
          "mode": "dry-run or live", "git": "git operations behind the write gate"}
GLOBAL_FLAGS: List[Arg] = [
    arg("--project-dir", metavar="P", help="project directory (default: the git toplevel of the working "
                                           "directory; unless a person runs it at a terminal, only another "
                                           "worktree of that project)"),
    arg("--repo", metavar="O/N", help="repository REFs resolve to (default: origin)"),
    arg("--dry-run", action="store_true", help="record writes in the ledger, send nothing"),
    arg("--json", action="store_true", help="accepted; output is always JSON"),
]
GLOBAL_DESTS = {"help"} | {flags[0].lstrip("-").replace("-", "_") for flags, _ in GLOBAL_FLAGS}


def command(name: str, help: str, *, writes: bool = False, args: Sequence[Union[Arg, OneOf]] = (),
            running: Optional[str] = None, ref: Optional[str] = None) -> Callable[[Callable], Callable]:
    """Register a handler as `notme <name>` (a space makes a group subcommand)."""
    def wrap(fn: Callable) -> Callable:
        COMMANDS.append(Command(tuple(name.split()), fn, help, writes, list(args), running, ref))
        return fn
    return wrap


# --- handlers: identity and config ---------------------------------------------


@command("me", "print the operator login")
def cmd_me(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The operator login: project.operator, or the authenticated gh user when it is `auto`.

    Output: operator, source (config or gh)
    """
    configured = ctx.meta.get("operator")
    if isinstance(configured, str) and configured != "auto":
        return {"operator": configured, "source": "config"}
    return {"operator": ctx.operator, "source": "gh"}


@command("tmp-dir", "make a fresh private work directory for input files")
def cmd_tmp_dir(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Create a new directory (mode 0700) inside not-me's temp directory and print its path. Input files such as
    --body-file and --message-file are accepted from there. Run it once per run and use the literal path.
    Local only: nothing is sent to GitHub.

    Output: path
    """
    try:
        base_dir = gh_mod.notme_tmp_dir()
    except RuntimeError as exc:
        raise fail(ExitCode.ERROR, "unsafe_path", str(exc)) from None
    return {"path": tempfile.mkdtemp(prefix="run-", dir=str(base_dir))}


@command("config", "print the effective configuration", args=[
    arg("--explain", action="store_true", help="where each key came from"),
    arg("--check", action="store_true", help="list configuration errors (exit 4 when any)"),
    arg("--layer", choices=("default", "global", "repo"), help="limit to one layer")])
def cmd_config(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Effective configuration. --explain adds where each key came from, --check lists errors (exit 4 when any;
    two states sharing one label name is an error), --layer limits the output to one layer (default, global or
    repo).

    Output: config (per document) and roster; with --explain layers, keys, bodies, picking (rules: the effective
    Picking work section of project.md, layer: where it came from, fields, target_epics), queue (where the issue
    queue looks: source, ok, summary, opt_in_label, and label or board and column), warnings; with --check ok,
    errors
    """
    cfg = ctx.cfg
    if args.check:
        ctx.require_valid()
        return {"ok": True, "errors": [], "warnings": list(cfg.warnings)}
    if args.explain:
        out = cfg.explain()
        out["queue"] = describe_queue(ctx.meta)
        if args.layer:
            out["keys"] = {k: v for k, v in out["keys"].items() if v["source"]["layer"] == args.layer}
            out["layer"] = args.layer
        return json.loads(json.dumps(out, default=str))
    if args.layer:
        return {"layer": args.layer, "dir": cfg.display(cfg.layers.dirs[args.layer]),
                "config": {name: dict(docs[args.layer].meta) for name, docs in cfg.docs.items()
                           if args.layer in docs}}
    members = cfg.effective_roster()
    return {"config": json.loads(json.dumps(cfg.meta, default=str)),
            "roster": {"leads": members.leads, "authorities": members.authorities, "members": members.members,
                       "reviewers": members.reviewers},
            "warnings": list(cfg.warnings)}


@command("preflight", "check state, operator, branch and guard before a run", args=[
    arg("--push-check", action="store_true", help="also rehearse a push of the current branch")])
def cmd_preflight(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Everything a run needs to know before it starts: state, operator, branch, guard. Exit 8 if the guard
    is active but its self-test fails. --push-check rehearses a push of the current branch.

    Output: ok, blockers, enabled, paused, mode, dry_run, repo, tracker, operator, branch, default_branch, clean,
    guard (active, selftest), active_run, warnings, push_check
    """
    ctx.require_valid()
    blockers: List[str] = []
    halted = ctx.halted()
    if halted:
        blockers.append(halted)
    operator = ctx.operator
    status: Optional[git_mod.Status] = None
    if ctx.work_dir is None:
        blockers.append("no project directory")
    else:
        try:
            status = git_mod.status(ctx.work_dir)
        except git_mod.GitError:
            blockers.append("not a git repository")
    guard = ctx.guard_state()
    if guard["active"] and not guard["selftest"]:
        raise fail(ExitCode.GUARD, "guard_broken", "the guard is enabled but its self-test failed; run doctor",
                   guard=guard)
    out: Dict[str, Any] = {
        "enabled": ctx.meta.get("enabled") is not False, "paused": halted == "paused",
        "mode": str(ctx.meta.get("mode") or "dry-run"), "dry_run": ctx.mode_is_dry,
        "repo": ctx.guard.origin_repo or getattr(args, "repo", None), "tracker": ctx.tracker, "operator": operator,
        "branch": status.branch if status else None, "default_branch": status.default_branch if status else None,
        "clean": status.clean if status else None, "guard": guard, "active_run": ctx.active_run(),
        "warnings": list(ctx.guard.warnings)}
    if args.push_check:
        out["push_check"] = push_check(ctx)
        if not out["push_check"]["ok"]:
            blockers.append("push check failed")
    out["blockers"] = blockers
    out["ok"] = not blockers
    return out


def push_check(ctx: Context) -> Dict[str, Any]:
    work = ctx.need_work_dir()
    try:
        branch = git_mod.current_branch(work)
        if not branch:
            return {"ok": False, "error": "HEAD is detached"}
        git_mod.push_dry_run(branch, work)
    except git_mod.GitError as exc:
        return {"ok": False, "error": f"{exc}: {exc.stderr.strip()[:200]}"}
    return {"ok": True, "branch": branch}


# --- handlers: issues ----------------------------------------------------------


@command("pick-issue", "the next issue to work (claims nothing)", args=[
    arg("--exclude", type=int, action="append", metavar="N", help="skip this issue (repeatable)")])
def cmd_pick_issue(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The eligible issues with the facts to choose between them, and a suggested top issue. Nothing is claimed.

    An issue in a trusted epic's task list is left out while an earlier issue in that list is open and
    unstarted (not blocked; no state but to do; assigned to nobody but the operator), so no choice can skip
    ahead in an epic. --exclude drops issues from the list and the top.

    Output: number, title, url, author, labels, parent, candidates (count): the suggested top issue by the
    queue's order; issues: the eligible issues in that order, all of them when the Picking work rules come from
    the global layer (the rules must see the whole queue), else up to queue.list_limit; each with number,
    title, url, author, labels, created, age_days, parent (epic number), position (1-based in the epic's open
    task list), blocked_by and blocking ([{repo, number, state}] from GitHub issue dependencies), reactions
    (counts by kind, from the operator and trusted logins only), target_epic and target_position (the
    picking.target_epics epic whose task list holds it, and its 1-based place there; issues in a target epic
    come first), fields ({name: value} for each picking.fields name, text cut to picking.value_max_chars; on a
    label queue only from projects the tracker's owner owns; {} when none is named), board ({status, fields} on
    a board queue, else null); a fact that cannot be read is null; never a body. truncated (more eligible issues
    than listed: the rules saw only part of the queue); held_back:
    [{number, behind, epic}]; stack_held: [{number, ref, reason}] issues left out because their
    `Stack on:` (or `Based on:`, `On top of:`) line names a predecessor PR that is not ready to stack on (draft,
    closed, another open blocker, queue.stacking off, a repo not allowed or not this checkout's); with none,
    reason stack_held names them; disagreements (only with queue.source either, and only when not empty:
    [{number, label, status, reason}] for issues left out because the label and the board Status disagree);
    picking: {rules (the effective Picking work section of project.md), layer (where
    the rules came from), fields, target_epics (the ones honoured: open and written by a trusted roster member),
    ignored_epics ([{number, reason}])}. Or none with queue_summary (where the queue looked, in plain words) and
    held_back when not empty, with reason when paused or disabled, and with reason unassigned_only, unassigned
    (count), setting and suggest (the skill a person runs to take some: /not-me:suggest) when the queue found no
    issue and the only eligible issues have no assignee (only issues assigned to the operator are picked unless
    queue.include_unassigned is true)
    """
    def none(**extra: Any) -> Dict[str, Any]:
        return {"none": True, **extra, "queue_summary": describe_queue(ctx.meta)["summary"]}

    try:  # handled here, not with running="none", so the paused and disabled results say where it looked too
        ctx.require_running()
    except Halted as exc:
        return none(reason=exc.reason)
    skip = set(args.exclude or [])
    queue = ctx.queue()
    for warning in ctx.cfg.picking_warnings:
        ctx.warn(warning)
    rules = ctx.cfg.picking()
    limit = _limit_for(rules, queue)  # the run log notes `truncated`; eligibility is the same either way
    every = queue.candidates()
    found, stack_held = queue.stack_filter([c for c in every if c.number not in skip], limit)
    if not found:
        out: Dict[str, Any] = {}
        if stack_held:
            out.update({"reason": "stack_held", "stack_held": stack_held})
        if not every and queue.unassigned_skipped:
            out.update({"reason": "unassigned_only", "unassigned": queue.unassigned_skipped,
                        "setting": "queue.include_unassigned", "suggest": "/not-me:suggest"})
        if queue.held_back:
            out["held_back"] = queue.held_back
        if queue.disagreements:
            out["disagreements"] = queue.disagreements
        return none(**out)
    top, listed = found[0], found[:limit]
    picking = {**rules, "target_epics": list(queue.target_epics_used),
               "ignored_epics": list(queue.target_epics_ignored)}
    return {"number": top.number, "title": top.title, "url": top.url, "author": top.author,
            "labels": top.labels, "parent": top.parent, "candidates": len(found),
            "issues": queue.facts(listed), "truncated": len(listed) < len(found), "held_back": queue.held_back,
            "stack_held": stack_held, "picking": picking,
            **({"disagreements": queue.disagreements} if queue.disagreements else {})}


@command("picking-signals", "board fields and labels that could rank the eligible issues", args=[
    arg("--include-unassigned", action="store_true",
        help="count unassigned issues as eligible too, as queue.include_unassigned: true would (changes no setting)")])
def cmd_picking_signals(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The board fields and labels on the eligible issues (the issues pick-issue would choose from), with the values
    in use and how many issues carry each, for the picking step of /not-me:setup. A read: nothing is claimed or
    written, and it runs while not-me is paused. Field values come only from projects owned by the tracker's owner
    (on a board queue, only the configured board). Never an issue title or body, and never a text field's value.

    Output: tracker, source (labels or board), board ({owner, number} or null), eligible (count), include_unassigned,
    unassigned (eligible but left out for having no assignee), held_back (count left out for epic order); fields:
    [{name, type (single_select, number, text, date or iteration), usable (the name is valid in picking.fields),
    projects (numbers), issues (how many carry a value), values ([{value, issues}], most used first, option and
    iteration names cut to picking.value_max_chars; null for text), more_values}]; labels: [{name, issues,
    ranking_like (priority, severity, size, P0-P4 and similar)}]; other_owner_items (project items in projects of
    other owners, ignored); fields_unread (issues whose project items could not be read)
    """
    queue = ctx.queue()
    if args.include_unassigned:
        queue.include_unassigned = True
    return picking_signals(queue)


def _limit_for(rules: Dict[str, Any], queue: Any) -> Optional[int]:
    """How many eligible issues to list with facts. Global Picking work rules may prefer an issue the queue's own
    order puts last, so they see every eligible issue. Repo rules and the plugin default keep the queue.list_limit
    cut: list_limit is tighten-only, and a repo file must not lift it through its rules."""
    return None if rules["layer"] == "global" else queue.list_limit()


def _suggest_queue(ctx: Context) -> Any:
    """The queue with its own eligibility and order, limited to issues nobody is assigned to."""
    queue = ctx.queue()
    queue.only_unassigned = True
    return queue


@command("suggest-issues", "eligible issues nobody is assigned to, for a person to take")
def cmd_suggest_issues(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The issues the queue would pick if they were assigned to the operator: eligible by the same rules as pick-issue
    (state, labels, trusted author, epic order) and assigned to nobody, in the queue's order, with the same facts
    and picking rules, so /not-me:suggest can rank them the way /not-me:new would. A read: nothing is assigned or
    claimed. Issues already assigned to the operator are left out (pick-issue lists them); issues assigned to
    anyone else are never listed. An issue by an author the roster does not trust is reported by number and login
    only, never its title or body.

    Output: tracker, operator, dry_run (assign-issue would only record to the ledger), candidates (count), issues
    (as pick-issue, up to queue.list_limit unless the Picking work rules come from the global layer), truncated,
    held_back ([{number, behind, epic}]), untrusted ([{number, author}]: unassigned issues otherwise eligible but
    written by someone the roster does not trust), picking (as pick-issue). Or none with queue_summary, untrusted
    and held_back when not empty; or none with reason and queue_summary only, when paused or disabled
    """
    summary = describe_queue(ctx.meta)["summary"]
    try:
        ctx.require_running()
    except Halted as exc:
        return {"none": True, "reason": exc.reason, "queue_summary": summary}
    queue = _suggest_queue(ctx)
    for warning in ctx.cfg.picking_warnings:
        ctx.warn(warning)
    rules = ctx.cfg.picking()
    found = queue.candidates()
    untrusted = list(queue.untrusted_skipped)
    if not found:
        return {"none": True, "queue_summary": summary, "untrusted": untrusted,
                **({"held_back": queue.held_back} if queue.held_back else {})}
    listed = found[:_limit_for(rules, queue)]
    return {"tracker": ctx.tracker, "operator": ctx.operator, "dry_run": ctx.mode_is_dry, "candidates": len(found),
            "issues": queue.facts(listed), "truncated": len(listed) < len(found), "held_back": queue.held_back,
            "untrusted": untrusted,
            "picking": {**rules, "target_epics": list(queue.target_epics_used),
                        "ignored_epics": list(queue.target_epics_ignored)}}


@command("assign-issue", "assign the operator to an unassigned issue suggest-issues lists", writes=True,
         running="error", ref="tracker", args=[
             arg("number", metavar="N|REF", help="a number (in the tracker), #N, owner/name#N or a URL")])
def cmd_assign_issue(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Add the operator, and only the operator, as the assignee of one tracker issue, so the queue picks it. For
    /not-me:suggest, after a person said yes to that issue. It takes no login, never removes an assignee and never
    touches labels, the board or a comment. Refused with no read and no write while a not-me run is active in any
    worktree of the checkout (kind active_run, with active_run {run_id, skill, ref, started}): autonomous runs never
    assign; only a person running
    /not-me:suggest does. Refused with no write: an issue outside the tracker (kind not_tracker), assigned to anyone
    else (kind assigned_to_other), or not among the issues suggest-issues lists now (kind not_eligible: closed, not
    to do, held back in an epic, by an untrusted author...). The write goes through the write gate; a dry run records
    it in the ledger and sends nothing. The assignees are read back after a real write: exit 5 when the operator is
    missing; exit 1 (kind assigned_to_other, assignees) when someone else is now assigned too. The operator stays
    assigned and nobody is unassigned; the queue will not pick the issue until a person sorts it out.

    Output: repo, number, assignee (the operator), assigned (true when it added the assignment), already (true when
    the operator was the assignee before; nothing written)
    """
    active = ctx.active_run() or next(iter(ctx.active_runs()), None)
    if active:
        raise fail(ExitCode.ERROR, "active_run",
                   f"a not-me run is active ({active.get('skill')} on {active.get('ref')}); runs never assign "
                   "issues, only a person running /not-me:suggest does; nothing assigned", active_run=active)
    if repo.lower() != ctx.tracker.lower():
        raise fail(ExitCode.ERROR, "not_tracker", f"{repo}#{n} is not in the tracker {ctx.tracker}; nothing assigned")
    operator = ctx.operator
    me = operator.lower()
    assignees = [gh_mod.login_of(a) for a in ctx.gh.issue(repo, n).get("assignees") or []]
    others = [a for a in assignees if a.lower() != me]
    if others:
        raise fail(ExitCode.ERROR, "assigned_to_other",
                   f"{repo}#{n} is assigned to {', '.join(others)}; nothing assigned", assignees=others)
    out: Dict[str, Any] = {"repo": repo, "number": n, "assignee": operator}
    if assignees:
        return {**out, "assigned": False, "already": True}
    queue = _suggest_queue(ctx)
    if n not in {c.number for c in queue.candidates()}:
        raise fail(ExitCode.ERROR, "not_eligible",
                   f"{repo}#{n} is not among the eligible unassigned issues (see notme suggest-issues); "
                   "nothing assigned")
    queue.assign(n, assignees)
    if not ctx.mode_is_dry:
        now = [gh_mod.login_of(a) for a in ctx.gh.issue(repo, n).get("assignees") or []]
        if me not in {a.lower() for a in now}:
            raise writeback.WritebackMismatch(f"{repo}#{n} does not list {operator} as an assignee after the write")
        others = [a for a in now if a.lower() != me]
        if others:
            raise fail(ExitCode.ERROR, "assigned_to_other",
                       f"{operator} was added to {repo}#{n}, but {', '.join(others)} is also assigned, so the queue "
                       "will not pick it; a person should sort out who takes it", assignees=others)
    return {**out, "assigned": True}


@command("claim-issue", "claim an issue", writes=True, running="error", ref="tracker", args=[number_arg])
def cmd_claim_issue(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Claim an issue: assign, move to in progress, post the claim marker. Exit 3 when someone else holds it.
    With queue.sync, the side queue.source does not read follows; a failed mirror write is a warning.

    Output: number, previous, now, marker_comment_id, run_id; warnings when a queue.sync mirror write failed
    """
    queue = ctx.queue()
    try:
        return dataclasses.asdict(queue.claim(n))
    finally:  # a mirror warning stands even when a later step failed
        ctx.sync_warnings(queue)


@command("release-issue", "give a claimed issue back", writes=True, ref="tracker", args=[
    number_arg, arg("--to", choices=("todo", "blocked"), default="todo"),
    arg("--reason-file", metavar="F", help="file holding one line that says why; goes to the run log, and to one "
                                           "comment on a release to blocked; required unless --no-comment"),
    arg("--no-comment", action="store_true", help="post no comment: for a run that stops on a failure on not-me's "
                                                  "own side")])
def cmd_release_issue(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Give an issue back: unassign, move from in progress to todo or blocked, mark the claim released. The old
    state, the new state (and the board column when the board follows) and the text of --reason-file go to the run
    log; a release to blocked also posts them as one comment, since the timeline cannot say why. A release to todo
    posts nothing. A comment that fails is a warning; the release stands. With queue.sync, the side queue.source
    does not read follows; a failed mirror write is a warning.

    --no-comment releases the same way and posts nothing, even to blocked; --reason-file is then optional.

    Output: number, to, released, commented; warnings when a queue.sync mirror write or the comment failed
    """
    if not args.reason_file and not args.no_comment:
        raise fail(ExitCode.USAGE, "usage", "pass --reason-file with one line saying why, or --no-comment")
    reason = ctx.read_reason(args.reason_file, repo) if args.reason_file else None
    queue = ctx.queue()
    try:
        queue.release(n, args.to)
    finally:  # a mirror warning stands even when a later step failed
        ctx.sync_warnings(queue)
    commented = ctx.say_why(repo, n, queue.change, reason, not args.no_comment) if queue.change and reason \
        else False
    return {"number": n, "to": args.to, "released": True, "commented": commented}


@command("move-issue", "move an issue to a state", writes=True, ref="tracker", args=[
    number_arg, arg("--to", required=True, choices=states.STATES, metavar="STATE"),
    arg("--require-current", choices=states.STATES, metavar="STATE"), reason_file_arg])
def cmd_move_issue(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Move an issue to a state, optionally only when it is currently in another one. The old state, the new state
    (and the label and board column the move also wrote) and the text of --reason-file go to the run log, and to
    one comment when the new state is blocked, question, needs help, needs human or paused, which the timeline
    cannot explain. A comment that fails is a warning; the move stands. With queue.sync, the side queue.source
    does not read follows; a failed mirror write is a warning.

    Output: number, to, require_current, commented; warnings when a queue.sync mirror write or the comment failed
    """
    reason = ctx.read_reason(args.reason_file, repo)
    queue = ctx.queue()
    try:
        queue.move(n, args.to, args.require_current)
    finally:  # a mirror warning stands even when a later step failed
        ctx.sync_warnings(queue)
    commented = ctx.say_why(repo, n, queue.change, reason) if queue.change else False
    return {"number": n, "to": args.to, "require_current": args.require_current, "commented": commented}


@command("issue", "an issue's facts", ref="tracker", args=[number_arg])
def cmd_issue(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """An issue's facts. The body and comments of an author the roster does not trust are withheld.

    Output: repo, number, title, state, url, author, author_trusted, labels, status, assignees, body, comments,
    comments_ignored, blocked_by ([{repo, number, state}] from GitHub issue dependencies; null when unknown),
    blocking (the issues GitHub records as blocked by this one, same shape), stack_on. stack_on is null unless a
    trusted author's body has one `Stack on:`, `Based on:` or `On top of:` owner/repo#N line, or queue.stacking is
    auto and the issue has an open blocker (null too when the queue config is invalid: see warnings);
    then {ref, pr, head, base, state, draft, author, author_trusted (of the PR), stacks (branch from head and
    target it), ready, reason, via (declared: the line; blockers: auto, from the open blockers)}. Read it here,
    never from the body: the line counts only from a trusted author, and the PR must be open, not a draft, from a
    trusted author and in this checkout's repo. A merged PR gives ready true and stacks false (a normal issue);
    queue.stacking off, another open blocker, a blocker with no open PR or a closed PR gives ready false
    """
    out = feedback_mod.issue_view(ctx.gh, repo, n, ctx.roster, ctx.operator, ctx.labels)
    out["stack_on"] = out["blocked_by"] = out["blocking"] = None
    if out["author_trusted"] and repo.lower() == ctx.tracker.lower():
        try:  # a queue or picking config error must not stop the read of the issue itself
            queue = ctx.queue()
            out["stack_on"] = queue.stack_on(n, out["author"], out["body"])
            out["blocked_by"] = queue.blocked_by(n)
            out["blocking"] = queue.blocking(n)
        except ValueError as exc:
            ctx.warn(f"stack_on, blocked_by and blocking are null: {exc}")
    return out


# --- handlers: pull requests ---------------------------------------------------


@command("pick-needs-work", "the next PR to rework", running="none", args=[
    arg("--include-answered", action="store_true",
        help="also PRs labeled question that a lead or authority has answered since our last marker"),
    arg("--exclude", type=int, action="append", metavar="N", help="skip this PR (repeatable)")])
def cmd_pick_needs_work(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The lowest-numbered open PR labeled needs work and assigned to the operator, by a trusted author.
    With --include-answered, a PR labeled question (assigned to the operator) whose newest lead or authority
    comment is newer than the last not-me marker also counts, and carries answered: true.

    Output: repo, number, title, url, head_ref, labels, answered (only when true), others; or none (with reason
    when paused)
    """
    legacy = (ctx.meta.get("markers") or {}).get("legacy") or []
    found = feedback_mod.needs_work_candidates(ctx.gh, ctx.repo, ctx.operator, ctx.roster, ctx.labels,
                                               include_answered=args.include_answered, exclude=args.exclude or (),
                                               legacy_markers=legacy)
    if not found:
        return {"none": True}
    top = found[0]
    pr = top["pr"]
    return {"repo": ctx.repo, "number": pr["number"], "title": pr.get("title"), "url": pr.get("url"),
            "head_ref": pr.get("headRefName"), "labels": top["labels"],
            **({"answered": True} if top["answered"] else {}), "others": [c["pr"]["number"] for c in found[1:]]}


@command("pr-children", "open PRs stacked on a PR's branch", ref="repo", args=[ref_arg])
def cmd_pr_children(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Open PRs in the same repository whose base branch is this PR's head branch (stacked children), from
    trusted authors only; the others are listed in skipped with a reason.

    Output: repo, number, head, children ([{number, url, head, base, author}]), skipped
    """
    head = ctx.gh.pr(repo, n).get("headRefName")
    if not isinstance(head, str) or not head:
        raise fail(ExitCode.ERROR, "not_found", f"{repo}#{n} has no head branch")
    children, skipped = feedback_mod.stacked_children(ctx.gh, repo, ctx.roster, head)
    return {"repo": repo, "number": n, "head": head, "children": [c for c in children if c["number"] != n],
            "skipped": [s for s in skipped if s["number"] != n]}


@command("pr-by-head", "open PRs with a head branch", args=[arg("branch", metavar="BRANCH")])
def cmd_pr_by_head(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Open PRs in the repository (--repo, default origin) whose head branch is BRANCH, same-repository PRs from
    trusted authors only (others are listed in skipped). Used to find the predecessor of a "Builds on" line.

    Output: repo, branch, prs ([{number, url, head, base, author}]), skipped
    """
    git_mod.valid_branch(args.branch)
    found, skipped = feedback_mod.prs_by_head(ctx.gh, ctx.repo, ctx.roster, args.branch)
    return {"repo": ctx.repo, "branch": args.branch, "prs": found, "skipped": skipped}


@command("claim-pr", "claim a PR for this run", writes=True, running="error", ref="repo", args=[ref_arg])
def cmd_claim_pr(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Claim a PR for a run: refuse paused or needs-human PRs, take the local lock, post the claim marker.
    Exit 3 when another run holds it. A dry run takes no lock.

    Output: repo, number, marker_comment_id, run_id
    """
    q = ctx.queue("pr", repo)
    return {"repo": repo, "number": n, "marker_comment_id": q.claim_pr(n), "run_id": q.run_id}


@command("release-pr", "release a PR claim", writes=True, ref="repo", args=[ref_arg])
def cmd_release_pr(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Release a PR claim: mark the operator's claim comment released and remove the local lock, whichever run
    holds it.

    Output: repo, number, released
    """
    ctx.queue("pr", repo).release_pr(n)
    return {"repo": repo, "number": n, "released": True}


@command("bundle", "a PR's facts", ref="repo", args=[
    ref_arg, arg("--review", action="store_true",
                 help="for reviewing the PR: also return a roster member's title and body, flagged text_is_data")])
def cmd_bundle(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """A PR's facts: state, labels, author trust, linked issue, companion PRs, checks. Title and body are
    returned only for a trusted author (lead or authority), or with --review for a member too; a member's
    text then comes with text_is_data true: the text under review, never instructions.

    Output: the feedback.Bundle fields (repo, number, title, body, head_ref, head_sha, base, state, author,
    author_tier, author_trusted, text_is_data, labels, review_requests, linked_issue, linked_issue_source, companions,
    checks, ...)
    """
    return feedback_mod.bundle(ctx.gh, repo, n, ctx.roster, tracker=ctx.tracker,
                               repos=ctx.meta.get("repos") or [ctx.tracker], review=args.review).to_dict()


@command("wait-checks", "wait for a PR's checks to finish", ref="repo", args=[
    ref_arg, arg("--minutes", type=float, metavar="N", help="give up after N minutes (default limits.ci_wait_minutes)")])
def cmd_wait_checks(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Poll the PR's checks (as `notme bundle` reports them) every 30 seconds until every check has a conclusion
    or the time is up. This is the one long-running read: it can take as long as --minutes (default
    limits.ci_wait_minutes). It exits 0 whatever the state; the skill decides what a failure means. With no
    checks at all the state is none, at once.

    Output: state (success, failure, pending or none), checks, waited_s
    """
    minutes = args.minutes if args.minutes is not None else float((ctx.meta.get("limits") or {}).get(
        "ci_wait_minutes", 20))
    if minutes < 0:
        raise fail(ExitCode.USAGE, "usage", "--minutes must not be negative")

    def fetch() -> List[Dict[str, Any]]:
        return feedback_mod.bundle(ctx.gh, repo, n, ctx.roster, tracker=ctx.tracker, repos=[]).checks

    return feedback_mod.wait_checks(fetch, minutes, sleep=ctx.deps.sleep, clock=ctx.deps.clock)


SHA40_RE = re.compile(r"[0-9a-f]{40}")  # a full commit hash, as GitHub reports it


@command("verify-plan", "what to verify and when: baseline, targeted tests, CI wait", args=[
    arg("--base", metavar="B", help="the base branch (default: the repo default branch)"),
    arg("--sha", metavar="SHA", help="read the CI result of this commit (40 hex, e.g. the PR head) instead of "
                                     "origin/<base>")])
def cmd_verify_plan(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The verify plan for a run, from the effective verify doc, limits.ci_wait_minutes and a commit's CI.
    Reads only: the local origin/<base> ref (no fetch) and the check runs and statuses on GitHub of the checked
    commit: --sha when given (a rework run passes the PR head), else origin/<base>. The baseline is skipped only
    when verify.baseline is auto and that CI is green; a gh error, or more check runs or statuses than one page
    holds, makes base_ci unknown and the baseline runs. Targeted test commands are the lines of the fenced block
    in the verify doc's Targeted tests section (none when it is left as is). ci_wait is false when
    limits.ci_wait_minutes is 0.

    Output: base, base_sha, checked_sha (the commit whose CI was read), base_ci (success, failure, pending, none
    or unknown: the CI of checked_sha), baseline (setting, run, reason), targeted (configured, commands),
    ci_wait_minutes, ci_wait
    """
    if args.sha is not None and not SHA40_RE.fullmatch(args.sha):
        raise fail(ExitCode.USAGE, "usage", f"invalid sha {args.sha!r}: use the full 40-character commit hash")
    repo = _code_repo(ctx)
    cwd = ctx.work_dir
    base = git_mod.valid_branch(args.base) if args.base else None
    if base is None:
        base = git_mod.default_branch(ctx.gh, repo, cwd) if cwd is not None else ctx.gh.repo_default_branch(repo)
    base_sha = git_mod.remote_tracking_sha(base, cwd) if cwd is not None else None
    checked = args.sha or base_sha
    state = "unknown"
    if checked:
        try:
            runs = ctx.gh.read(["api", f"repos/{repo}/commits/{checked}/check-runs?per_page=100"])
            combined = ctx.gh.read(["api", f"repos/{repo}/commits/{checked}/status?per_page=100"])
        except gh_mod.GhError as exc:
            ctx.warn(f"could not read the CI result at {checked}: {exc}")
        else:
            runs_doc = runs if isinstance(runs, dict) else {}
            status_doc = combined if isinstance(combined, dict) else {}
            state = verifyplan.ci_state(runs_doc.get("check_runs") or [], status_doc.get("statuses") or [],
                                        runs_total=runs_doc.get("total_count"),
                                        statuses_total=status_doc.get("total_count"))
    # always or auto; the value comes from the config layers (defaults/verify.md), never from here
    baseline_setting = (ctx.cfg.meta.get("verify") or {}).get("baseline")
    return verifyplan.plan(setting=baseline_setting, base=base, base_sha=base_sha, checked_sha=checked,
                           base_ci=state, verify_body=ctx.cfg.bodies.get("verify", ""),
                           ci_wait_minutes=(ctx.meta.get("limits") or {}).get("ci_wait_minutes"))


@command("feedback", "trusted review findings on a PR", ref="repo", args=[
    ref_arg, arg("--since-marker", metavar="ISO", help="count answers after this time instead of our last marker")])
def cmd_feedback(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Review findings on a PR from trusted authors only.

    Output: the feedback.Feedback fields (repo, number, findings, ignored_count, ignored, deduped_count,
    prior_summaries, last_marker_at, answered_since_marker)
    """
    legacy = (ctx.meta.get("markers") or {}).get("legacy") or []
    return feedback_mod.feedback(ctx.gh, repo, n, ctx.roster, since_marker=args.since_marker,
                                 legacy_markers=legacy).to_dict()


@command("pick-reviews", "the next PR to review", running="none")
def cmd_pick_reviews(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The next PR to review: labeled needs review, the operator a requested reviewer, an author on the roster at
    any tier (lead, authority or member) who is not the operator, and changed since the operator's last review
    (new head SHA or new author comments). title is null unless author_trusted; read a member's PR text with
    `notme bundle --review`.

    Output: repo, number, title, url, head_sha, why, author, author_tier, author_trusted, others, skipped; or none
    (with skipped)
    """
    picks, skipped = feedback_mod.review_candidates(ctx.gh, ctx.repo, ctx.operator, ctx.roster,
                                                    ctx.labels.name("needs_review"))
    if not picks:
        return {"none": True, "skipped": skipped}
    return {"repo": ctx.repo, **picks[0], "others": [p["number"] for p in picks[1:]], "skipped": skipped}


@command("prove-target", "the URL to test a PR at", ref="repo", args=[ref_arg])
def cmd_prove_target(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """The URL to test a PR at, from the strategies in prove.target.strategy, in order: GitHub deployments for
    the head SHA, a bot comment matching bot_comment_regex (only from prove.target.bots or trusted users),
    url_template, then none. A URL whose host is not in prove.target.hosts (suffix match) or the url_template
    host is skipped with a reason.

    Output: url, strategy, head_sha, tried, skipped (when any); or none (with tried, skipped)
    """
    target = (ctx.cfg.meta.get("prove") or {}).get("target") or {}
    trusted = ctx.trusted_meta("prove").get("target") or {}
    return prove.find_target(ctx.gh, repo, n, target, trusted, ctx.roster)


# --- handlers: labels and states -----------------------------------------------


@command("transition", "change an item's state label along a legal edge", writes=True, args=[
    number_arg, arg("--from", dest="from_state", required=True, choices=states.STATES, metavar="STATE"),
    arg("--to", required=True, choices=states.STATES, metavar="STATE"),
    arg("--kind", choices=("issue", "pr"), default="issue"), reason_file_arg])
def cmd_transition(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Change an item's state label along a legal edge (reads the labels, replaces the set, reads it back). The
    old state, the new state and the text of --reason-file go to the run log, and to one comment when the new
    state is blocked, question, needs help, needs human or paused. A comment that fails is a warning; the change
    stands.

    Output: repo, number, kind, from, to, labels, commented
    """
    repo, n = ctx.ref(args.number, ctx.tracker if args.kind == "issue" else ctx.repo)
    reason = ctx.read_reason(args.reason_file, repo)
    new = states.apply(ctx.gh, repo, n, args.from_state, args.to, ctx.labels, args.kind)
    commented = ctx.say_why(repo, n, {"from": args.from_state, "to": args.to}, reason)
    return {"repo": repo, "number": n, "kind": args.kind, "from": args.from_state, "to": args.to, "labels": new,
            "commented": commented}


@command("labels-sync", "make an item's labels match", writes=True, ref="tracker", args=[
    arg("number", metavar="N|REF", help="a number (in the tracker), #N, owner/name#N or a URL"),
    OneOf(arg("--set", metavar="a,b", help="replace the whole label set"),
          arg("--add", metavar="a,b", help="labels to add")),
    arg("--remove", metavar="a,b", help="labels to remove (not with --set)"),
    arg("--check", action="store_true", help="only report whether the labels match"),
    arg("--reason-file", metavar="F", help="file holding one line that says why; required when the change adds or "
                                           "removes a state label (labels.* in project.md)")])
def cmd_labels_sync(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Make an item's labels match: --set replaces the whole set, --add and --remove edit it. --check only reports. A bare number
    is an item in the tracker; owner/name#N or a URL names an item in another allowed repository (a companion PR).
    A change that adds or removes a state label needs --reason-file; the labels and the reason go to the run log.
    One comment names them only when the change adds blocked, question, needs help, needs human or paused;
    removing a state label or putting one back posts nothing. A failed comment is a warning and the change stands.
    Pass labels and other labels need no reason.

    Output: repo, number, in_sync, changed, labels (current, or the new set); commented (true when posted) when a
    state label changed
    """
    if (args.set is None) == (args.add is None and args.remove is None) or \
            (args.set is not None and args.remove is not None):
        raise fail(ExitCode.USAGE, "usage", "give --set, or --add and/or --remove")
    added: List[str] = []
    removed: List[str] = []
    reason = None
    if not args.check:
        have = ctx.gh.labels(repo, n)
        held = {x.lower() for x in have}
        if args.set is not None:
            wanted = csv(args.set)
            kept = {x.lower() for x in wanted}
            added = [x for x in ctx.state_labels(wanted) if x.lower() not in held]
            removed = [x for x in ctx.state_labels(have) if x.lower() not in kept]
        else:
            added = [x for x in ctx.state_labels(csv(args.add)) if x.lower() not in held]
            removed = [x for x in ctx.state_labels(csv(args.remove)) if x.lower() in held]
        if added or removed:
            if not args.reason_file:
                raise fail(ExitCode.USAGE, "usage", "this change touches the state label(s) "
                           + ", ".join(f"`{x}`" for x in added + removed) + ": pass --reason-file with one line "
                                                                           "saying why")
            reason = ctx.read_reason(args.reason_file, repo)
    out = writeback.label_sync(ctx.gh, repo, n, set_to=csv(args.set) if args.set is not None else None,
                               add=csv(args.add), remove=csv(args.remove), check=args.check)
    if reason is not None:
        out["commented"] = ctx.say_labels(repo, n, added, removed, reason)
    return out


@command("queue-resync", "fix issues whose label and board Status disagree", writes=True, running="error")
def cmd_queue_resync(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Set the side queue.source does not read to match the side it does, for each open issue on queue.board whose
    state label and board Status disagree (the issues `doctor` and `status` list): the board Status for a labels
    source, the labels (one full-set write, read back) for a board source. It follows the effective mode: in
    dry-run the writes reach only the ledger. A board the global allow list does not confirm is refused before
    anything is read. Issues not on the board are skipped and reported, never added; closed issues are never
    touched; neither are issues whose state has no column.

    Nothing is posted on GitHub: matching the other side is mechanical and the timeline shows the label change.
    Why each state label changed goes to the run log (--log).

    Output: board, changes ([{number, url, side, from, to, result (changed, planned or failed), reason,
    commented (label changes only; always false)}]),
    skipped ([{number, url, reason}]), truncated (the board has more items than the `read` count read; issues past
    them are reported as not among the items read, never as off the board), issues_truncated; dry_run when the
    writes were only recorded. Exits 1 when paused or disabled.
    """
    try:
        out = resync_mod.resync(ctx.queue())
    except LookupError as exc:  # a MirrorFailed (board not set or not allowed) has its own kind, sync_failed
        raise fail(ExitCode.ERROR, "board_not_found", f"{exc}; check queue.board and that the token has the "
                                                      "project scope") from None
    for change in out["changes"]:  # a state label that changed is logged, never posted: it only matches the board
        if change["side"] == "labels" and change["result"] in ("changed", "planned"):
            change["commented"] = ctx.post_reason(ctx.tracker, change["number"],
                                                  f"State label changed from `{change['from']}` to `{change['to']}`: "
                                                  "set to match the board Status by notme queue-resync", post=False)
    return out


@command("labels-ensure", "create the state and pass labels the repo lacks", writes=True, args=[
    arg("--check", action="store_true", help="only report what is missing")])
def cmd_labels_ensure(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Create the configured state labels that the tracker lacks (all eleven by default), and the pass labels
    role.md names on the code repository, where pull requests live. --check only reports.

    Output: repo, ok (nothing missing, pass labels included), present, missing, created; when the code repository
    is not the tracker, pass_labels (the same keys for the pass labels there)
    """
    tracker, code = ctx.tracker, _code_repo(ctx)
    wanted = list(ctx.cfg.roles().pass_labels().values())
    if not wanted or code.lower() == tracker.lower():
        return states.ensure_labels(ctx.gh, tracker, ctx.meta.get("labels") or {}, check=args.check,
                                    pass_labels=wanted)
    out = states.ensure_labels(ctx.gh, tracker, ctx.meta.get("labels") or {}, check=args.check)
    out["pass_labels"] = states.ensure_labels(ctx.gh, code, {}, check=args.check, pass_labels=wanted)
    out["ok"] = out["ok"] and out["pass_labels"]["ok"]
    return out


def _code_repo(ctx: Context) -> str:
    """The repository pull requests live in (--repo or origin), else the tracker."""
    try:
        return ctx.repo
    except CliExit:
        return ctx.tracker


# --- handlers: write-back --------------------------------------------------------


def with_attribution(ctx: Context, body: str) -> str:
    text = writeback.attribution(ctx.cfg)
    return body if not text or text in body else writeback.apply_attribution(body, ctx.cfg)


@command("reply-thread", "reply in a review thread", writes=True, ref="repo", args=[
    ref_arg, arg("--to", type=int, required=True, metavar="COMMENT_ID"), body_file_arg])
def cmd_reply_thread(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Reply in a review thread, by the id of a comment in it. Never resolves the thread.

    Output: repo, number, to, replied, id
    """
    result = writeback.reply_thread(ctx.gh, repo, n, args.to, ctx.read_input(args.body_file), ctx.run_id)
    return {"repo": repo, "number": n, "to": args.to, "replied": True,
            "id": result.get("id") if isinstance(result, dict) else None}


@command("comment-upsert", "create or update the operator's marker comment", writes=True, ref="repo", args=[
    ref_arg, arg("--marker", required=True, metavar="KIND"), body_file_arg])
def cmd_comment_upsert(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Create or update the operator's own marker comment on an issue or PR (never a second one).

    Output: repo, number, id, created or updated
    """
    legacy = (ctx.meta.get("markers") or {}).get("legacy") or []
    result = writeback.comment_upsert(ctx.gh, repo, n, args.marker, ctx.read_input(args.body_file), ctx.run_id,
                                      legacy)
    return {"repo": repo, "number": n, **result}


@command("request-review", "request review from logins", writes=True, ref="repo", args=[
    ref_arg, arg("--reviewers", required=True, metavar="a,b")])
def cmd_request_review(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Request review from the given logins.

    Output: repo, number, requested
    """
    logins = csv(args.reviewers)
    if not logins:
        raise fail(ExitCode.USAGE, "usage", "--reviewers needs at least one login")
    ctx.gh.request_review(repo, n, logins)
    return {"repo": repo, "number": n, "requested": logins}


@command("pr-body", "replace a PR's body", writes=True, ref="repo", args=[ref_arg, body_file_arg])
def cmd_pr_body(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Replace a PR's body with a file's text (the configured attribution is added when missing).

    Output: repo, number, updated
    """
    with gh_mod.tmp_text_file(with_attribution(ctx, ctx.read_input(args.body_file)), prefix="not-me-cli-") as path:
        ctx.gh.pr_edit_body(repo, n, path)
    return {"repo": repo, "number": n, "updated": True}


@command("pr-open", "open a PR", writes=True, args=[
    arg("--head", required=True, metavar="H"), arg("--base", required=True, metavar="B"), title_file_arg,
    body_file_arg, labels_arg, arg("--reviewers", metavar="a,b"), arg("--assignee", metavar="L"),
    arg("--draft", action="store_true"),
    arg("--reason-file", metavar="F", help="one line for the run log saying why the state labels are set; default: "
                                           "\"pull request opened\"")])
def cmd_pr_open(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Open a PR, set its labels, request review. The head branch may not be a protected branch. When --labels
    names a state label (needs review, ...), the run log records it and the reason (the first line of
    --reason-file, else "pull request opened"). No comment is posted: opening the PR is the reason, and the
    timeline shows the labels.

    Output: repo, number, url (both null in a dry run), head, base; commented (false) when a state label was set
    """
    repo = ctx.repo
    if ctx.protected(args.head):
        raise fail(ExitCode.ERROR, "protected_branch", f"refusing to open a PR from protected branch {args.head!r}")
    body = with_attribution(ctx, ctx.read_input(args.body_file))
    reason = ctx.read_reason(args.reason_file, repo) if args.reason_file else "pull request opened"
    out = writeback.open_pr(ctx.gh, ctx.cfg, repo, args.head, args.base, ctx.read_line(args.title_file), body,
                            csv(args.labels), csv(args.reviewers), args.assignee, args.draft)
    result = {"repo": repo, "number": out.get("number"), "url": out.get("url"), "head": args.head, "base": args.base}
    state = ctx.state_labels(csv(args.labels))
    if state and out.get("number") is not None:
        result["commented"] = ctx.say_labels(repo, out["number"], state, [], reason, post=False)
    return result


@command("companion-prs", "rewrite a PR's Companion PRs section", writes=True, ref="repo", args=[
    ref_arg, arg("--companions-file", required=True, metavar="J")])
def cmd_companion_prs(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Rewrite the Companion PRs section of a PR body from a JSON list of {repo, number, url?}.

    Output: repo, number, updated or unchanged
    """
    items = ctx.read_json(args.companions_file, "companions file")
    if not isinstance(items, list) or not all(isinstance(i, dict) and i.get("repo") and i.get("number") is not None
                                               for i in items):
        raise fail(ExitCode.USAGE, "usage", "companions file must be a JSON list of {repo, number, url?}")
    return {"repo": repo, "number": n, **writeback.companion_prs_upsert(ctx.gh, repo, n, items)}


@command("testing-steps", "append testing steps to a PR", writes=True, ref="repo", args=[
    ref_arg, arg("--steps-file", required=True, metavar="F")])
def cmd_testing_steps(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Append new checklist items to a PR's testing section (a JSON list file, or one step per line).

    Output: repo, number, updated or unchanged
    """
    text = ctx.read_input(args.steps_file)
    if text.lstrip().startswith("["):
        try:
            steps = [str(s) for s in json.loads(text)]
        except ValueError:
            raise fail(ExitCode.USAGE, "usage", "steps file looks like JSON but is not valid") from None
    else:
        steps = [re.sub(r"^\s*(?:[-*]\s*(?:\[[ xX]\]\s*)?)?", "", ln).strip() for ln in text.splitlines() if ln.strip()]
    return {"repo": repo, "number": n, **writeback.testing_steps_append(ctx.gh, repo, n, steps, ctx.cfg)}


@command("review-post", "post a review", writes=True, ref="repo", args=[
    ref_arg, arg("--event", required=True, choices=("APPROVE", "REQUEST_CHANGES", "COMMENT")), body_file_arg,
    arg("--comments-file", metavar="J"),
    arg("--reason-file", metavar="F", help="one line for the run log saying why the state labels changed; default: "
                                           "the review's own verdict"),
    OneOf(arg("--labels-set", metavar="a,b", help="set the full label set after posting"),
          arg("--labels-auto", action="store_true",
              help="work out the label set from the PR's reviews, role.md and the roster, then set it"))])
def cmd_review_post(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Post a review, optionally set labels, and read both back. APPROVE is refused unless review.auto_approve is
    true in the global (or default) layer; the repo layer cannot turn it on.

    --labels-auto reads the PR's reviews after posting and sets the full label set itself. With no rows in role.md,
    APPROVE moves the PR to review.approve_label and REQUEST_CHANGES to review.changes_label, as one approval did
    before. With rows, every roster reviewer except the author must approve (latest review that is not a comment;
    a dismissed review is not an approval): until then APPROVE keeps needs_review. Each role with an approving
    reviewer gets its pass label and a role with none loses it; REQUEST_CHANGES moves the PR to
    review.changes_label. COMMENT only updates the pass labels. A pass label missing from the repo is skipped with
    one warning.

    When the labels set change a state label (needs review, needs work, ready to merge, ...), the run log records
    what was added and removed and the reason: the first line of --reason-file, else "a <EVENT> review was
    posted". No comment is posted: the review is the reason, and the timeline shows it next to the label change.

    Output: repo, number, event, state (event only in a dry run); with --labels-auto labels and approval (null with
    no roles; else required, approved, waiting, all_approved, pass_labels); commented (false) when a state label
    changed
    """
    if args.event == "APPROVE" and ctx.trusted_meta("review").get("auto_approve") is not True:
        raise fail(ExitCode.ERROR, "approve_refused", "APPROVE needs review.auto_approve: true in the global layer")
    inline = None
    if args.comments_file:
        inline = ctx.read_json(args.comments_file, "comments file")
        if not isinstance(inline, list) or not all(isinstance(c, dict) and {"path", "line", "body"} <= set(c)
                                                    for c in inline):
            raise fail(ExitCode.USAGE, "usage", "comments file must be a JSON list of {path, line, body}")
    report: Dict[str, Any] = {}
    reason = ctx.read_reason(args.reason_file, repo) if args.reason_file else f"a {args.event} review was posted"
    before = ctx.gh.labels(repo, n) if args.labels_auto or args.labels_set is not None else []
    auto = _labels_after_review(ctx, repo, n, args.event, report) if args.labels_auto else None
    out = writeback.review_post(ctx.gh, repo, n, args.event.lower(), ctx.read_input(args.body_file), inline,
                                csv(args.labels_set) if args.labels_set is not None else None, ctx.run_id,
                                labels_from_reviews=auto)
    after = out.get("labels") if args.labels_auto else csv(args.labels_set) if args.labels_set is not None else None
    if after is not None:
        held, kept = {x.lower() for x in before}, {x.lower() for x in after}
        added = [x for x in ctx.state_labels(after) if x.lower() not in held]
        removed = [x for x in ctx.state_labels(before) if x.lower() not in kept]
        if added or removed:
            out["commented"] = ctx.say_labels(repo, n, added, removed, reason, post=False)
    return {"repo": repo, "number": n, **out, **report}


_POSTED_STATE = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}


def _labels_after_review(ctx: Context, repo: str, n: int, event: str,
                         report: Dict[str, Any]) -> Callable[[List[Dict[str, Any]], bool], List[str]]:
    """The callback review-post uses with --labels-auto: (reviews read after posting, dry run) -> the full label
    set. Fills report["approval"]."""
    review = ctx.cfg.meta.get("review") or {}
    target = {"APPROVE": review.get("approve_label"), "REQUEST_CHANGES": review.get("changes_label")}.get(event)
    if event != "COMMENT" and target not in states.STATES:
        raise fail(ExitCode.CONFIG, "config_invalid", "review.approve_label and review.changes_label must name a "
                                                      "state", errors=[f"review label key {target!r}"])

    def compute(reviews: List[Dict[str, Any]], dry: bool) -> List[str]:
        pairs = [(login_of(r.get("user")), str(r.get("state") or "")) for r in reviews]
        if dry:  # the review just posted went to the ledger, not GitHub
            pairs.append((ctx.operator, _POSTED_STATE[event]))
        current = ctx.gh.labels(repo, n)
        roles = ctx.cfg.roles()
        report["approval"] = None
        if not roles.configured:
            return states.swap_state(current, target, ctx.labels, "pr") if target else current
        result = roles_mod.outcome(pairs, login_of((ctx.gh.pr(repo, n) or {}).get("author")),
                                   ctx.cfg.gate_reviewers(), roles, _usable_pass_labels(ctx, repo, roles))
        report["approval"] = result.to_json()
        new = states.set_pass_labels(current, result.earned, result.managed)
        if event == "REQUEST_CHANGES":
            return states.swap_state(new, target, ctx.labels, "pr")
        if event == "APPROVE" and result.all_approved:
            main = ctx.labels.current(new, "pr")
            if main == [target]:
                return new
            if main and states.allowed(main[0], target, "pr"):
                return states.swap_state(new, target, ctx.labels, "pr")
            ctx.warn(f"every required reviewer approved, but {repo}#{n} is in {', '.join(main) or 'no state'}, "
                     f"which cannot move to {target}; its state label was left as it is")
        return new  # not final, or COMMENT: only the pass labels change
    return compute


def _usable_pass_labels(ctx: Context, repo: str, roles: Any) -> List[str]:
    """The role.md pass labels that exist in repo, in the repo's own spelling; one warning names the rest
    (treated as no pass label)."""
    wanted = list(roles.pass_labels().values())
    if not wanted:
        return []
    have = {name.lower(): name for name in states.repo_label_names(ctx.gh, repo)}
    missing = [w for w in wanted if w.lower() not in have]
    if missing:
        ctx.warn(f"pass label(s) {', '.join(missing)} from role.md are not in {repo}; treated as no pass label. "
                 "Create them with /not-me:doctor or `notme labels-ensure`")
    return [have[w.lower()] for w in wanted if w.lower() in have]


@command("resolve-threads", "resolve review threads", writes=True, ref="repo", args=[
    ref_arg, arg("--ids-file", required=True, metavar="J")])
def cmd_resolve_threads(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Resolve review threads of this PR by id (a JSON list of thread ids).

    Output: repo, number, resolved
    """
    ids = ctx.read_json(args.ids_file, "ids file")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise fail(ExitCode.USAGE, "usage", "ids file must be a JSON list of thread ids")
    return {"repo": repo, "number": n, "resolved": writeback.resolve_fixed_threads(ctx.gh, repo, n, ids)}


_MARKER_RE = re.compile(r"<!--\s*not-me:.*?-->", re.S)


def _same_body(a: str, b: str) -> bool:
    """Equal apart from line endings and trailing whitespace."""
    def norm(text: str) -> str:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return "\n".join(line.rstrip() for line in lines).rstrip()
    return norm(a) == norm(b)


@command("issue-body", "replace a trusted author's issue body", writes=True, ref="tracker", args=[
    arg("number", metavar="N|REF", help="a number (in the tracker), #N, owner/name#N or a URL"), body_file_arg])
def cmd_issue_body(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Replace an issue's body with a file's text. The text must pass `check-text` (exit 1, kind text_lint, and no
    write otherwise), and the issue's author must be trusted by the rule `issue` uses (otherwise exit 1, kind
    untrusted_author, and no write), and so must whoever last edited the body (otherwise, or when that cannot be
    read, exit 1, kind untrusted_editor, and no write). Every not-me marker in the current body that the new text lacks is appended
    back, and the configured attribution is added when missing. The issue is read back after the write (exit 5
    when the body differs; skipped in a dry run).

    Output: repo, number, updated
    """
    text = ctx.read_input(args.body_file)
    problems = writeback.check_text(text, ctx.tracker, repo, writeback.attribution(ctx.cfg), False)
    if problems:
        raise CliExit(ExitCode.ERROR, {"error": f"{len(problems)} problem(s) in the text", "kind": "text_lint",
                                       "ok": False, "problems": problems})
    issue = ctx.gh.issue(repo, n)
    author = gh_mod.login_of(issue.get("author"))
    if not feedback_mod.author_trusted(ctx.roster, author, ctx.operator):
        raise fail(ExitCode.ERROR, "untrusted_author",
                   f"{repo}#{n} is by {author or 'an unknown author'}, who is not trusted; its body is not replaced")
    ok, editor = last_editor(ctx.gh, repo, n)  # a rewrite would make an untrusted editor's text ours
    if not ok or (editor and not feedback_mod.author_trusted(ctx.roster, editor, ctx.operator)):
        raise fail(ExitCode.ERROR, "untrusted_editor",
                   f"{repo}#{n} was last edited by {editor or 'someone who could not be read'}, who is not trusted; "
                   "its body is not replaced")
    body = with_attribution(ctx, text)
    missing = [m for m in _MARKER_RE.findall(issue.get("body") or "") if m not in body]
    if missing:
        body = body.rstrip("\n") + "\n\n" + "\n".join(missing) + "\n"
    with gh_mod.tmp_text_file(body, prefix="not-me-cli-") as path:
        ctx.gh.issue_edit_body(repo, n, path)
    if not ctx.mode_is_dry:
        found = ctx.gh.issue(repo, n).get("body") or ""
        if not _same_body(found, body):
            raise writeback.WritebackMismatch(f"{repo}#{n} body differs from what was written")
    return {"repo": repo, "number": n, "updated": True}


@command("followup", "create a follow-up issue", writes=True, args=[
    title_file_arg, body_file_arg, arg("--source", type=int, metavar="N"), arg("--parent", type=int, metavar="N"),
    labels_arg])
def cmd_followup(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Create a follow-up issue in the tracker, or in --repo when given (the allow list decides whether that repo
    may be written); --parent also ticks it into that issue's task list, which must be in the same repo.

    Output: number, url (both null in a dry run), parent
    """
    return writeback.followup(ctx.gh, ctx.cfg, ctx.read_line(args.title_file), ctx.read_input(args.body_file),
                              args.source, args.parent, csv(args.labels), source_repo=ctx.repo,
                              repo=ctx.repo if getattr(args, "repo", None) else None)


@command("check-text", "lint a text bound for GitHub", args=[
    arg("--file", required=True, metavar="F"),
    arg("--summary", action="store_true", help="the text is a run summary (needs a TL;DR line)")])
def cmd_check_text(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Lint a text bound for GitHub (bare #N, local paths, assistant mentions, TL;DR line, an account of local trouble
    the run worked around). Exit 1 when it finds problems.

    Output: ok, problems
    """
    guard_repo = tracker = None
    if ctx.work_dir is not None:
        guard_repo = ctx.guard.origin_repo
        configured = ctx.meta.get("tracker")
        tracker = guard_repo if configured in (None, "auto") else str(configured)
    repo = getattr(args, "repo", None) or guard_repo
    problems = writeback.check_text(ctx.read_input(args.file), tracker, repo, writeback.attribution(ctx.cfg),
                                    args.summary)
    if problems:
        raise CliExit(ExitCode.ERROR, {"error": f"{len(problems)} problem(s) in the text", "kind": "text_lint",
                                       "ok": False, "problems": problems})
    return {"ok": True, "problems": []}


# --- handlers: run log -----------------------------------------------------------


@command("log start", "start a run log", args=[
    arg("--skill", required=True, metavar="S"), arg("--ref", required=True, metavar="R"),
    arg("--force", action="store_true", help="start even when another run looks active")])
def cmd_log_start(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Start a run log and this worktree's active-run marker. Exit 1 (kind active_run) when another run is active
    in this worktree; runs in other worktrees of the checkout do not block it.

    Output: path, run_id, skill, ref
    """
    try:
        log = runlog.RunLog.start(ctx.log_root(create=True), args.skill, args.ref, force=args.force,
                                  project_dir=ctx.work_dir)
    except runlog.ActiveRunError as exc:
        raise fail(ExitCode.ERROR, "active_run", f"another run is active: {exc.existing.get('run_id')}",
                   existing=exc.existing) from None
    return {"path": str(log.path), "run_id": log.run_id, "skill": log.skill, "ref": log.ref}


@command("log action", "append a timeline entry", args=[
    arg("path", metavar="PATH"), arg("--message-file", required=True, metavar="F"),
    arg("--kind", default="ACTION", choices=runlog.KINDS)])
def cmd_log_action(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Append one timeline entry (ACTION by default) with the text of a file, to a run log under the log root.

    Output: path, kind
    """
    log = ctx.open_log(args.path)
    log.action(ctx.read_input(args.message_file), args.kind)
    return {"path": args.path, "kind": args.kind}


@command("log question", "record a question and its decision", args=[
    arg("path", metavar="PATH"), arg("--file", required=True, metavar="Q.json")])
def cmd_log_question(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Record a question and the decision taken, from a JSON file {question, decision, undo}.

    Output: path
    """
    log = ctx.open_log(args.path)
    data = ctx.read_json(args.file, "question file")
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("question", "decision", "undo")):
        raise fail(ExitCode.USAGE, "usage", "question file must be a JSON object with question, decision and undo")
    log.question(data["question"], data["decision"], data["undo"])
    return {"path": args.path}


@command("log finish", "finish a run log", args=[
    arg("path", metavar="PATH"), arg("--status", required=True, metavar="S"), arg("--failures-file", metavar="F")])
def cmd_log_finish(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Finish a run log: write its summary, clear the run's active-run marker (in whichever worktree holds it) and
    drop the shared test environment lock when this run holds it.

    Output: path, status, summary
    """
    log = ctx.open_log(args.path)
    failures = None
    if args.failures_file:
        text = ctx.read_input(args.failures_file)
        failures = [str(x) for x in json.loads(text)] if text.lstrip().startswith("[") \
            else [ln.strip() for ln in text.splitlines() if ln.strip()]
    summary = log.finish(args.status, failures=failures)
    if ctx.work_dir is not None:
        _drop_verify_lock(ctx, log.run_id)
    return {"path": args.path, "status": args.status, "summary": summary}


@command("log last", "the newest run log", args=[arg("--skill", metavar="S")])
def cmd_log_last(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The newest run log, optionally of one skill.

    Output: path, skill, ref, run_id, status, entries; or none
    """
    log = runlog.last(ctx.log_root(), args.skill)
    if log is None:
        return {"none": True}
    return {"path": str(log.path), "skill": log.skill, "ref": log.ref, "run_id": log.run_id, "status": log.status,
            "entries": len(log.entries)}


@command("log list", "recent run logs, newest first", args=[
    arg("--skill", metavar="S"), arg("--limit", type=int, default=20, metavar="N", help="at most N logs (default 20)")])
def cmd_log_list(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Run logs under the log root, newest first (by file name, which starts with a timestamp), optionally of one
    skill.

    Output: logs ([{path, run_id, skill, ref, status}])
    """
    if args.limit < 1:
        raise fail(ExitCode.USAGE, "usage", "--limit must be at least 1")
    root = ctx.log_root()
    if not root.is_dir():
        return {"logs": []}
    folders = [root / runlog._slug(args.skill)] if args.skill else [p for p in root.iterdir() if p.is_dir()]
    files = sorted((f for folder in folders if folder.is_dir() for f in folder.glob("*.md")),
                   key=lambda f: (f.name, str(f)), reverse=True)
    out: List[Dict[str, Any]] = []
    for file in files:
        if len(out) >= args.limit:
            break
        try:
            log = runlog.RunLog.open(file)
        except (OSError, ValueError):
            continue
        out.append({"path": str(file), "run_id": log.run_id, "skill": log.skill, "ref": log.ref,
                    "status": log.status})
    return {"logs": out}


# --- handlers: shared test environment lock ------------------------------------------

VERIFY_LOCK_FILE = "verify.lock"
MAX_LOCK_WAIT_SECONDS = 540  # under the 10-minute cap on one shell call; wait longer by calling again
LOCK_POLL_SECONDS = 15


def _verify_lock_path(ctx: Context) -> Path:
    """<git-common-dir>/not-me/verify.lock: one lock for every worktree of the checkout."""
    work = ctx.need_work_dir()
    common = paths.git_common_dir(work)
    return (common / "not-me" if common else work / ".not-me") / VERIFY_LOCK_FILE


def _drop_verify_lock(ctx: Context, run_id: str) -> bool:
    """Remove the lock only when run_id holds it. True when removed."""
    path = _verify_lock_path(ctx)
    held = claims.read_lock(path)
    return held is not None and held.get("run_id") == run_id and claims.release_lock(path, run_id)


@command("verify-lock acquire", "take the shared test environment for the verify step", args=[
    arg("--log", required=True, metavar="PATH", help="this run's log"),
    arg("--wait-seconds", type=int, default=0, metavar="N",
        help=f"wait up to N seconds for it (at most {MAX_LOCK_WAIT_SECONDS}; default 0, try once)")])
def cmd_verify_lock_acquire(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Take the lock on the shared test environment for this run, when verify.md sets `shared_env: true`. One lock
    covers every worktree of the checkout (<git-common-dir>/not-me/verify.lock). Hold it from environment start
    through the last check, and only then; release it with `verify-lock release`. With `shared_env` false (the
    default) it takes nothing and returns locked false: each worktree verifies on its own. Taking it again while
    holding it is fine. A lock whose run is no longer active in any worktree, or older than
    `limits.claim_stale_hours`, is taken over. Held by another active run: polls every 15 seconds for up to
    --wait-seconds, then exits 1 (kind verify_locked, holder {run_id, since}, waited_seconds). Waiting never
    means skipping verification. Local only: nothing is sent to GitHub.

    Output: shared_env, locked, waited_seconds (when shared_env)
    """
    if not 0 <= args.wait_seconds <= MAX_LOCK_WAIT_SECONDS:
        raise fail(ExitCode.USAGE, "usage", f"--wait-seconds must be between 0 and {MAX_LOCK_WAIT_SECONDS}")
    log = ctx.open_log(args.log)
    if (ctx.cfg.meta.get("verify") or {}).get("shared_env") is not True:
        return {"shared_env": False, "locked": False}
    path = _verify_lock_path(ctx)
    stale_hours = float((ctx.meta.get("limits") or {}).get("claim_stale_hours", 8))
    started = time.monotonic()
    while True:
        holder = (claims.read_lock(path) or {}).get("run_id")
        if holder and holder != log.run_id and holder not in {r["run_id"] for r in ctx.active_runs()}:
            claims.release_lock(path, str(holder))  # its run is over: free, unless someone took it meanwhile
        try:
            claims.acquire_lock(path, log.run_id, stale_hours)
            return {"shared_env": True, "locked": True, "waited_seconds": round(time.monotonic() - started)}
        except claims.ClaimLost:
            pass
        waited = time.monotonic() - started
        if waited >= args.wait_seconds:
            held = claims.read_lock(path) or {}
            raise fail(ExitCode.ERROR, "verify_locked",
                       f"the shared test environment is in use by run {held.get('run_id')}; wait and try again, "
                       "never skip verification", holder={"run_id": held.get("run_id"), "since": held.get("ts")},
                       waited_seconds=round(waited))
        time.sleep(min(LOCK_POLL_SECONDS, max(args.wait_seconds - waited, 0)))


@command("verify-lock release", "give the shared test environment back", args=[
    arg("--log", required=True, metavar="PATH", help="this run's log")])
def cmd_verify_lock_release(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Release the shared test environment lock when this run holds it; another run's lock is left alone.
    `log finish` releases it too. Local only.

    Output: released
    """
    log = ctx.open_log(args.log)
    return {"released": _drop_verify_lock(ctx, log.run_id)}


# --- handlers: retro audits -------------------------------------------------------

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REVIEWED_FILE = "reviewed.txt"


def _retro_dir(ctx: Context) -> Path:
    common = paths.git_common_dir(ctx.need_work_dir())
    if common is None:
        raise fail(ExitCode.ERROR, "git_error", "the project is not a git checkout, so there is no retro directory")
    return common / "not-me" / "retro"


def _run_id(value: str) -> str:
    if not _RUN_ID_RE.match(value or ""):
        raise fail(ExitCode.USAGE, "usage", f"invalid run id {value!r}: letters, digits, dot, dash, underscore")
    return value


def _reviewed_ids(retro: Path) -> List[str]:
    try:
        lines = (retro / REVIEWED_FILE).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [ln.strip() for ln in lines if ln.strip()]


@command("retro save", "keep a retro audit in the git directory", args=[
    arg("--run-id", required=True, metavar="RID"), arg("--audit-file", required=True, metavar="F"),
    arg("--reviewed", action="store_true", help="also mark the run as reviewed")])
def cmd_retro_save(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Copy an audit markdown file (an allowed input file) to <git-common-dir>/not-me/retro/<RID>.md. With
    --reviewed, also add RID to reviewed.txt there (once). Local files only: nothing is sent to GitHub.

    Output: run_id, path, reviewed
    """
    rid = _run_id(args.run_id)
    text = ctx.read_input(args.audit_file)
    retro = _retro_dir(ctx)
    target = retro / f"{rid}.md"
    runlog._atomic_write(target, text)
    if args.reviewed and rid not in _reviewed_ids(retro):
        with open(retro / REVIEWED_FILE, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(rid + "\n")
    return {"run_id": rid, "path": str(target), "reviewed": rid in _reviewed_ids(retro)}


@command("retro reviewed", "list the reviewed runs")
def cmd_retro_reviewed(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The run ids marked reviewed, oldest first.

    Output: reviewed ([run ids])
    """
    return {"reviewed": _reviewed_ids(_retro_dir(ctx))}


@command("retro is-reviewed", "has a run been reviewed", args=[arg("run_id", metavar="RID")])
def cmd_retro_is_reviewed(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Whether the run id is in the reviewed list.

    Output: run_id, reviewed
    """
    rid = _run_id(args.run_id)
    return {"run_id": rid, "reviewed": rid in _reviewed_ids(_retro_dir(ctx))}


# --- handlers: allow list, guard, git ----------------------------------------------


def _origin(ctx: Context) -> Optional[str]:
    work = ctx.work_dir
    return (writes.parse_origin(git_mod.origin_url(work)) if work else None) or getattr(ctx.args, "repo", None)


def _allow_change(ctx: Context, args: argparse.Namespace, add: bool) -> Dict[str, Any]:
    ctx.require_human(f"allow-repo {'add' if add else 'remove'}")
    scope = None
    if not args.everywhere:
        scope = _origin(ctx)
        if not scope:
            raise fail(ExitCode.USAGE, "usage", "no project repository to scope the entry to: pass --repo or "
                                                  "--everywhere")
    change = writes.allowlist_add if add else writes.allowlist_remove
    changed = change(ctx.cfg.home, scope, repo=None if args.board else args.slug, everywhere=args.everywhere,
                     board=args.board)
    return {"added" if add else "removed": changed, "scope": "*" if args.everywhere else scope,
            **({"board": args.board} if args.board else {"repo": args.slug})}


_ALLOW_ARGS = [arg("slug", nargs="?", metavar="SLUG"), arg("--board", metavar="OWNER/NUMBER"),
               arg("--everywhere", action="store_true", help="for every project, not only this one")]


@command("allow-repo add", "allow a repo or board", args=_ALLOW_ARGS)
def cmd_allow_repo_add(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Allow a repo (or, with --board, a project board) for this project or everywhere. Only a person may do this:
    it must run from a terminal Claude Code did not start and be given the operator token (typed at a hidden prompt,
    or in NOT_ME_OPERATOR_CONFIRM for scripts); otherwise the refusal (kind human_only) carries the command to run. The guard is the enforcement; this check is
    a second layer.

    Output: added, scope, repo or board
    """
    return _allow_change(ctx, args, add=True)


@command("allow-repo remove", "remove an entry", args=_ALLOW_ARGS)
def cmd_allow_repo_remove(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Remove a repo (or --board) from the allow list. The same person check as add applies.

    Output: removed, scope, repo or board
    """
    return _allow_change(ctx, args, add=False)


@command("allow-repo list", "list entries")
def cmd_allow_repo_list(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The allow list entries for this project and the global ones.

    Output: entries (scope, repo or board, added)
    """
    return {"entries": writes.allowlist_list(ctx.cfg.home, _origin(ctx))}


@command("guard check", "decide a hook event read from stdin")
def cmd_guard_check(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Decide one hook event read from stdin as JSON.

    Output: decision (allow or block), rule, message
    """
    try:
        event = json.loads(ctx.deps.stdin.read())
        if not isinstance(event, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        raise fail(ExitCode.USAGE, "usage", f"stdin is not a hook event JSON object: {exc}") from None
    allow = {"decision": "allow", "rule": None, "message": ""}
    try:
        if not guard_mod.needs_check(event):
            return allow
        cwd = event.get("cwd") or str(ctx.work_dir or ".")
        gctx = guard_mod.GuardContext.from_project(cwd, home=ctx.cfg.home, env=ctx.env)
        if not guard_mod.is_active(gctx):
            return allow
        block = guard_mod.decide(event, gctx)
    except TypeError as exc:
        raise fail(ExitCode.USAGE, "usage", str(exc)) from None
    if block is None:
        return allow
    return {"decision": "block", "rule": block.rule, "message": block.message or block.reason}


@command("guard selftest", "check the hook blocks a canned event")
def cmd_guard_selftest(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Run a canned always-blocked event through the hook. Exit 8 when the guard is active and does not block it.

    Output: ok, active
    """
    state = ctx.guard_state()
    if state["active"] and not state["selftest"]:
        raise fail(ExitCode.GUARD, "guard_broken", "the guard is enabled but its self-test failed", ok=False,
                   active=True, selftest_stderr=state.get("selftest_stderr", ""))
    return {"ok": True, "active": state["active"]}


@command("git branch-create", "create a branch from a base", args=[
    arg("name", metavar="NAME"), arg("--base", required=True, metavar="B")])
def cmd_git_branch_create(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Create and check out a branch from a base without tracking it. Protected names are refused.

    Output: branch, base
    """
    if ctx.protected(args.name):
        raise fail(ExitCode.ERROR, "protected_branch", f"refusing to create protected branch {args.name!r}")
    git_mod.checkout_no_track(args.name, args.base, ctx.need_work_dir())
    return {"branch": args.name, "base": args.base}


@command("git checkpoint", "merge a base into the current branch", args=[
    arg("--base", required=True, metavar="B"),
    arg("--fetch", action="store_true", help="fetch the base from origin and merge origin/<base>")])
def cmd_git_checkpoint(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Merge a base into the current branch (never a rebase), after an optional fetch. Stops on conflicts.

    Output: branch, base, conflicts
    """
    cwd = ctx.need_work_dir()
    if args.fetch:
        git_mod.fetch(args.base, cwd=cwd)
    conflicts = git_mod.merge_base_into_current(f"origin/{args.base}" if args.fetch else args.base, cwd)
    return {"branch": git_mod.current_branch(cwd), "base": args.base, "conflicts": conflicts}


@command("git push", "push a branch to origin", writes=True, args=[arg("branch", metavar="BRANCH")])
def cmd_git_push(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Push one branch to origin after the write gate (never forced, never a protected branch). A dry run rehearses
    the push and records it in the ledger.

    Output: branch, pushed
    """
    dry = ctx.mode_is_dry
    git_mod.push(args.branch, ctx.guard, ctx.need_work_dir(), dry_run=dry, ledger=ctx.gh.ledger if dry else None)
    return {"branch": args.branch, "pushed": not dry}


@command("git push-check", "rehearse a push")
def cmd_git_push_check(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Rehearse a push of the current branch to a throwaway ref name. Nothing is sent. Exit 1 when it fails.

    Output: ok, branch
    """
    out = push_check(ctx)
    if not out["ok"]:
        raise CliExit(ExitCode.ERROR, {"error": out["error"], "kind": "push_check", "ok": False})
    return out


@command("git stash", "stash changes, returning the sha")
def cmd_git_stash(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Stash tracked and untracked changes and return the stash commit sha.

    Output: stash (sha, or null when there was nothing to stash)
    """
    return {"stash": git_mod.stash_by_sha(ctx.need_work_dir())}


@command("git unstash", "apply and drop a stash by sha", args=[arg("sha", metavar="SHA")])
def cmd_git_unstash(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Apply a stash by sha and drop it.

    Output: restored
    """
    git_mod.stash_restore(args.sha, cwd=ctx.need_work_dir())
    return {"restored": args.sha}


WORKTREES_EXCLUDE = "/.claude/worktrees/"
# review- keeps notme off the other worktrees in the same dir (Claude Code keeps its session worktrees there)
WORKTREE_NAME_RE = re.compile(r"review-[A-Za-z0-9._-]{1,57}")


def worktrees_root(ctx: Context) -> Path:
    """<main checkout>/.claude/worktrees, found from the work dir's toplevel. Never inside the git common dir."""
    work = ctx.need_work_dir()
    top = paths.run_git(["rev-parse", "--show-toplevel"], work)
    common = paths.git_common_dir(work)
    if top is None or common is None:
        raise fail(ExitCode.USAGE, "usage", "worktrees need a checkout with a working tree (not a bare repository)")
    main = Path(os.path.realpath(str(paths.main_checkout(Path(top)))))
    git_dir = Path(os.path.realpath(str(common)))
    main_common = paths.git_common_dir(main)  # core.worktree can point main_checkout anywhere; it must share our git dir
    if main_common is None or Path(os.path.realpath(str(main_common))) != git_dir:
        raise fail(ExitCode.ERROR, "unsafe_path", f"the main checkout {main} does not belong to this repository")
    root = main / ".claude" / "worktrees"
    resolved = Path(os.path.realpath(str(root)))
    if resolved == git_dir or git_dir in resolved.parents:
        raise fail(ExitCode.ERROR, "unsafe_path", "the worktrees dir resolves inside the git directory")
    return root


def worktree_path(root: Path, path: Union[str, Path]) -> Path:
    """`path` realpath'd when it is a review-* dir directly under `root` with no symlink on the way; else unsafe_path."""
    real = Path(os.path.realpath(str(path)))
    if real.parent != root or not WORKTREE_NAME_RE.fullmatch(real.name) or ".." in real.name:
        raise fail(ExitCode.ERROR, "unsafe_path", "worktree path must be a review-* dir directly under <main checkout>/.claude/worktrees/")
    return real


def exclude_worktrees_root(ctx: Context) -> None:
    """Add /.claude/worktrees/ to <git-common-dir>/info/exclude once, so a nested worktree never shows as untracked."""
    common = paths.git_common_dir(ctx.need_work_dir())
    if common is None:
        raise fail(ExitCode.USAGE, "usage", "not a git repository")
    info = Path(common) / "info"
    exclude = info / "exclude"
    if os.path.islink(str(info)) or os.path.islink(str(exclude)) or \
            Path(os.path.realpath(str(exclude))).parent != Path(os.path.realpath(str(common))) / "info":
        raise fail(ExitCode.ERROR, "unsafe_path", f"{exclude} is a symlink or resolves outside the git directory")
    text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if WORKTREES_EXCLUDE in (line.strip() for line in text.splitlines()):
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    sep = "\n" if text and not text.endswith("\n") else ""
    with exclude.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{sep}{WORKTREES_EXCLUDE}\n")


@command("git worktree-add", "add a detached worktree under .claude/worktrees",
         args=[arg("sha", metavar="SHA"), arg("--name", metavar="NAME")])
def cmd_git_worktree_add(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Add a detached worktree at a sha at <main checkout>/.claude/worktrees/NAME; the CLI picks the path.

    NAME defaults to review-<first 12 chars of SHA> and must match review-[A-Za-z0-9._-]{1,57} with no "..";
    anything else, a symlinked .claude, .claude/worktrees, info or info/exclude, or a main checkout (via
    core.worktree) that does not share this repository's git dir, is refused as unsafe_path. An existing path is
    refused as path_exists. The git common dir's info/exclude gets /.claude/worktrees/ so the worktree never shows
    as untracked in the main checkout. Works the same from the main checkout or a linked worktree.

    Output: path (absolute), sha
    """
    if not git_mod._SHA_RE.fullmatch(args.sha):
        raise fail(ExitCode.USAGE, "usage", f"invalid sha {args.sha!r}")
    name = f"review-{args.sha[:12]}" if args.name is None else args.name
    if not WORKTREE_NAME_RE.fullmatch(name) or ".." in name:
        raise fail(ExitCode.ERROR, "unsafe_path", f"bad worktree name {name!r}: use review-[A-Za-z0-9._-]{{1,57}}")
    root = worktrees_root(ctx)
    real = worktree_path(root, root / name)
    if os.path.lexists(str(real)):
        raise fail(ExitCode.ERROR, "path_exists", f"{real} already exists")
    exclude_worktrees_root(ctx)
    root.mkdir(parents=True, exist_ok=True)
    real = worktree_path(root, root / name)  # again, after mkdir
    git_mod.worktree_add_detached(real, args.sha, ctx.need_work_dir())
    return {"path": str(real), "sha": args.sha}


@command("git worktree-remove", "remove a worktree notme made", args=[arg("path", metavar="PATH")])
def cmd_git_worktree_remove(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Remove a worktree made by `git worktree-add`. PATH must be a review-* dir directly under
    <main checkout>/.claude/worktrees/; anything else, including Claude Code's own session worktrees there, is refused
    as unsafe_path.

    Output: path, removed
    """
    real = worktree_path(worktrees_root(ctx), args.path)
    git_mod.worktree_remove(real, ctx.need_work_dir())
    return {"path": args.path, "removed": True}


@command("git remote-branch-exists", "does origin have a branch", args=[arg("name", metavar="NAME")])
def cmd_git_remote_branch_exists(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Whether origin has the branch (git ls-remote --exit-code --heads origin NAME), and its sha.

    Output: exists, sha (null when it does not exist)
    """
    sha = git_mod.remote_branch_sha(args.name, ctx.need_work_dir())
    return {"exists": sha is not None, "sha": sha}


@command("git status", "branch, cleanliness and origin")
def cmd_git_status(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The checkout's branch, cleanliness and origin repo.

    Output: branch, clean, default_branch, origin
    """
    return dataclasses.asdict(git_mod.status(ctx.need_work_dir()))


# --- handlers: status, pause, doctor ---------------------------------------------


@command("status", "read-only summary of state, items and runs")
def cmd_status(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """A read-only summary: enabled and paused state, the active run, items needing a person, stale claims, runs that
    died before they finished, and the last run of each skill, and one line on how issues are picked. When GitHub
    cannot be read the items are left out with a warning. Creates nothing.

    Output: enabled, paused (global, project), mode, repo, tracker, operator, active_run (the run in this worktree),
    active_runs (every fresh run in any worktree of the checkout: [{run_id, skill, ref, started}]), queue (where the issue
    queue looks, as in config --explain), items, stale_claims, interrupted_runs (skill, run_id, ref, file), last_runs,
    picking (one line: "impact default" or "rules from <layer> layer", then "; fields: ..." and "; target epics:
    #N, ..." when set), queue_drift (when queue.board is set: [{number, url, label, status, skip}] of open issues
    whose state label and board Status disagree, [] when they agree; skip is why queue-resync cannot fix one, else
    null), queue_drift_truncated (the board has more items than were read), warnings
    """
    ctx.require_valid()
    pauses = {scope: path.exists() for scope, path in ctx.pause_files().items()}
    out: Dict[str, Any] = {
        "enabled": ctx.meta.get("enabled") is not False,
        "paused": {"global": pauses.get("global", False), "project": pauses.get("project", False)},
        "mode": str(ctx.meta.get("mode") or "dry-run"), "repo": None, "tracker": None, "operator": None,
        "active_run": ctx.active_run(), "active_runs": ctx.active_runs(), "queue": describe_queue(ctx.meta),
        "items": [], "stale_claims": [],
        "interrupted_runs": [], "last_runs": {}, "picking": config_mod.picking_summary(ctx.cfg.picking()),
        "warnings": list(ctx.guard.warnings)}
    if board_ref(ctx.meta.get("queue") or {}) is not None:
        out["queue_drift"], out["queue_drift_truncated"] = [], False
    try:
        root = ctx.log_root()
        active_ids = {r["run_id"] for r in out["active_runs"]}
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            log = runlog.last(root, folder.name)
            if log is not None:
                if log.status is None and log.run_id not in active_ids and runlog.is_run_log(log.path):  # died
                    out["interrupted_runs"].append({"skill": folder.name, "run_id": log.run_id, "ref": log.ref,
                                                    "file": log.path.name})
                out["last_runs"][folder.name] = {"run_id": log.run_id, "ref": log.ref, "status": log.status,
                                                 "file": log.path.name}
    except (ValueError, OSError):
        pass
    try:
        out["repo"], out["tracker"] = ctx.repo, ctx.tracker
        out["operator"] = ctx.operator
        out["items"] = _status_items(ctx)
        branches = (paths.run_git(["branch", "-a", "--format=%(refname:short)"], ctx.work_dir) or "").split() \
            if ctx.work_dir else []
        roster, operator = ctx.roster, ctx.operator.lower()
        out["stale_claims"] = stale_claims(
            ctx.gh, ctx.tracker, ctx.labels, float((ctx.meta.get("limits") or {}).get("claim_stale_hours", 8)),
            branches, lambda who: (who or "").lower() == operator or roster.is_trusted(who))
    except (gh_mod.GhError, CliExit) as exc:
        reason = exc.payload.get("error") if isinstance(exc, CliExit) else str(exc)
        out["warnings"].append(f"GitHub items not read: {reason}")
    if "queue_drift" in out:
        try:
            found = _queue_drift(ctx)
            out["queue_drift"] = [{k: d[k] for k in ("number", "url", "label", "status", "skip")}
                                  for d in found["drift"]]
            out["queue_drift_truncated"] = found["truncated"]
            if found["truncated"]:
                out["warnings"].append(f"queue drift: only the first {found['read']} board items were read; "
                                       "drift beyond them is not listed")
        except (MirrorFailed, gh_mod.GhError, LookupError, ValueError, CliExit) as exc:
            reason = exc.payload.get("error") if isinstance(exc, CliExit) else str(exc)
            out["warnings"].append(f"queue drift not read: {reason}")
    return out


def _status_items(ctx: Context) -> List[Dict[str, Any]]:
    seen: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for role in ("needs_human", "question", "paused"):
        name = ctx.labels.name(role)
        issues = ctx.gh.read(["issue", "list", "-R", ctx.tracker, "--state", "open", "--label", name, "--json",
                              "number,title,url", "--limit", "100"]) or []
        for kind, rows in (("issue", issues), ("pr", ctx.gh.pr_list(ctx.repo, label=name))):
            for row in rows:
                item = seen.setdefault((kind, row["number"]), {"kind": kind, "number": row["number"],
                                                              "title": row.get("title"), "url": row.get("url"),
                                                              "states": []})
                item["states"].append(role)
    return [seen[k] for k in sorted(seen)]


@command("pause", "turn not-me off or on", args=[
    arg("state", choices=("on", "off")),
    arg("--global", dest="global_", action="store_true", help="for every project")])
def cmd_pause(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Turn not-me off or on for this project (or for every project with --global) by writing or removing the
    PAUSED file. Pick and claim commands then report paused. `pause off` needs the same person check as allow-repo
    add (else kind human_only, with the command for a person to run).

    Output: paused, scope
    """
    if args.state == "off":
        ctx.require_human("pause off")
    scope = "global" if args.global_ else "project"
    files = ctx.pause_files()
    if scope not in files:
        raise fail(ExitCode.USAGE, "usage", "no project directory: run inside a clone or use --global")
    path = files[scope]
    if args.state == "on":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return {"paused": args.state == "on", "scope": scope}


def _mode_state(ctx: Context) -> Dict[str, Any]:
    cfg = ctx.cfg
    layers = {}
    for layer in ("global", "repo"):
        value, line, shown = modeswitch.layer_value(cfg, layer)
        target = modeswitch.layer_file(cfg, layer)
        layers[layer] = {"mode": value, "path": shown, "line": line, "file": str(target) if target else None}
    repo = layers["repo"]
    pin = {k: repo[k] for k in ("path", "file", "line")} if repo["mode"] == "dry-run" else None
    return {"mode": ctx.meta.get("mode"), "dry_run": ctx.mode_is_dry, "source": modeswitch.source(cfg),
            "layers": layers, "repo_pin": pin,
            "live_confirmed": humans.live_confirmed(cfg.home),  # verified only: the marker is never shown
            "unconfirmed": cfg.live_unconfirmed,
            "token_present": humans.token_path(cfg.home).is_file(),
            "launcher": launcher_path(), "shell": shell_name(), "live_cmd": ctx.terminal_command(["mode", "live"])}


def _set_mode(ctx: Context, layer: str, value: str) -> Dict[str, Any]:
    target = modeswitch.layer_file(ctx.cfg, layer)
    if target is None:
        raise fail(ExitCode.USAGE, "usage", "no project directory: run inside a clone or use --global")
    home = ctx.cfg.home
    if layer == "global" and value == "dry-run":
        humans.clear_live_marker(home)  # first, so a failed edit below still leaves live unconfirmed
    written = modeswitch.write_mode(target, value, follow_links=layer == "global")
    if value == "live":
        try:
            humans.write_live_marker(home)
        except ValueError as exc:
            raise fail(ExitCode.ERROR, "human_only", str(exc)) from None
    ctx.reload_cfg()
    return {**_mode_state(ctx), "written": {"layer": layer, "file": str(written), "mode": value}}


@command("mode show", "the effective mode and where it comes from")
def cmd_mode_show(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The effective mode, the layer, file and line it comes from, what the global and repo layers each say, and
    whether the repo layer pins dry-run (repo_pin: path, file, line). dry_run also counts --dry-run and
    NOT_ME_DRY_RUN. A global `mode: live` counts only when signed: live_confirmed says whether the marker that
    `notme mode live` writes (<not-me home>/mode-live.sig, an HMAC keyed by the operator token and the epoch)
    verifies; when the layers say live but it does not, mode is dry-run and unconfirmed holds the warning (else
    null).
    token_present says whether the operator token file exists; launcher is the absolute path of this notme, and
    live_cmd the command a person runs in their own terminal to go live, written for shell: posix, or git-bash on
    Windows (bin/notme is a bash script, so not PowerShell or cmd). Neither the token nor the marker is shown.

    Output: mode, dry_run, source (layer, path, line[, ignored, downgraded]), layers (global, repo: mode, path,
    line, file), repo_pin, live_confirmed, unconfirmed, token_present, launcher, shell, live_cmd
    """
    return _mode_state(ctx)


@command("mode dry-run", "set mode: dry-run", args=[
    arg("--global", dest="global_", action="store_true", help="in the global layer, not the repo layer")])
def cmd_mode_dry_run(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Set `mode: dry-run` in the repo layer (<project>/.claude/not-me/project.md), or with --global in the
    global layer (<not-me home>/project.md), which also deletes the live marker and replaces the epoch
    (<not-me home>/mode-live.epoch), so no saved copy of an old marker verifies again (the repo layer leaves both).
    Always allowed: it only tightens. A missing or empty file gets
    minimal frontmatter; otherwise only the mode line changes. Kind mode_edit_refused when the file cannot be
    edited safely: text without frontmatter, frontmatter that does not parse, a symlink or hard link in the repo
    layer, or a global symlink that does not lead to a regular project.md.

    Output: the mode show keys, plus written (layer, file, mode)
    """
    return _set_mode(ctx, "global" if args.global_ else "repo", "dry-run")


@command("mode live", "set mode: live in the global layer (a person only)")
def cmd_mode_live(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Set `mode: live` in the global layer only (the repo layer can never turn live mode on) and sign it: write
    <not-me home>/mode-live.sig, an HMAC-SHA256 keyed by the operator token and the epoch in
    <not-me home>/mode-live.epoch (created when missing), without which `mode: live` runs as dry-run. Only a person may do this, with the same check as allow-repo add (else kind human_only, with the
    command to run). After a token rotation, run it again. When the repo layer still pins dry-run, the global
    layer is written anyway and repo_pin names that file and line.

    Output: the mode show keys, plus written (layer, file, mode) and, when the repo pins dry-run, note
    """
    ctx.require_human("mode live")
    out = _set_mode(ctx, "global", "live")
    pin = out["repo_pin"]
    if pin:
        out["note"] = (f"the repo layer still holds mode at dry-run ({pin['path']}:{pin['line']}); "
                       "remove that line to go live in this project")
    return out


@command("doctor", "check tools, auth, config and the guard", args=[
    arg("--init-token", action="store_true", help="create the operator token (a person only; shown once)")])
def cmd_doctor(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Check the tools and setup: python, git and gh versions, config, mode (a warning when `mode: live` is set but
    not confirmed by `notme mode live`), config files (a warning when neither the global nor the repo layer holds a not-me config file), gh auth
    and scopes, pass labels (advice when role.md names any: the missing ones, for `labels-ensure`), the queue
    source (a warning when the pick label is missing, the board is not confirmed or not found, or a
    board_states column is not a status option; not run without gh), guard self-test, spawn depth, permissions
    (advice: the recommended permissions.allow rules missing from the user and project settings files, with the JSON
    to add in its hint; doctor never writes settings).
    Exit 7 when the only failed required checks are gh, gh auth or gh scopes; exit 1 when another
    required check fails. --init-token instead creates <not-me home>/operator-token (random, mode 0600) and shows
    it once; only a person may run it (kind human_only otherwise), and an existing token is never replaced
    (kind token_exists: delete the file to make a new one).

    Output: ok, checks (name, ok, detail, required, fix, hint); with --init-token token, path
    """
    if args.init_token:
        return _init_token(ctx)
    checks = doctor_mod.run(ctx.deps.runner or gh_mod._default_runner, ctx.config_errors(), lambda: ctx.gh,
                            ctx.guard_state, ctx.env,
                            doctor_mod.mode_check(ctx.meta.get("mode"), ctx.cfg.live_unconfirmed),
                            config_files=ctx.cfg.layers.files, pass_labels=lambda: _pass_label_check(ctx),
                            queue=lambda: _doctor_queue(ctx), drift=lambda: _doctor_drift(ctx),
                            permissions=lambda: doctor_mod.permissions_check(_settings_files(ctx), launcher_path()))
    verdict = doctor_mod.exit_kind(checks)
    if verdict:
        code = ExitCode.DEPENDENCY if verdict == "dependency" else ExitCode.ERROR
        raise CliExit(code, {"error": "a required check failed", "kind": "doctor", "ok": False, "checks": checks})
    return {"ok": True, "checks": checks}


def _settings_files(ctx: Context) -> List[Path]:
    """The Claude Code settings files whose permissions.allow doctor reads: the user's settings.json
    (CLAUDE_CONFIG_DIR, else ~/.claude) and this checkout's .claude/settings.json and settings.local.json."""
    user = Path(ctx.env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = [user / "settings.json"]
    if ctx.work_dir:
        files += [Path(ctx.work_dir) / ".claude" / "settings.json", Path(ctx.work_dir) / ".claude" / "settings.local.json"]
    return files


def _pass_label_check(ctx: Context) -> Optional[Dict[str, Any]]:
    """The doctor's advice on the role.md pass labels, checked on the code repository (None when role.md names
    none)."""
    wanted = list(ctx.cfg.roles().pass_labels().values())
    if not wanted:
        return None
    try:
        result = states.ensure_labels(ctx.gh, _code_repo(ctx), {}, check=True, pass_labels=wanted)
    except (gh_mod.GhError, CliExit) as exc:
        return doctor_mod.pass_label_error(str(exc) or exc.__class__.__name__)
    return doctor_mod.pass_label_check(result)


def _doctor_queue(ctx: Context) -> Dict[str, Any]:
    try:
        tracker = ctx.tracker
    except CliExit as exc:
        return doctor_mod.queue_not_run(str(exc.payload.get("error")))
    return doctor_mod.queue_check(ctx.meta, ctx.gh, tracker, ctx.guard.board, origin=ctx.guard.origin_repo or tracker)


def _doctor_drift(ctx: Context) -> Optional[Dict[str, Any]]:
    """The queue drift check: None when queue.board is not set, else what _queue_drift finds (read-only)."""
    if board_ref(ctx.meta.get("queue") or {}) is None:
        return None
    try:
        found = _queue_drift(ctx)
    except (MirrorFailed, gh_mod.GhError, LookupError, ValueError, CliExit) as exc:
        return doctor_mod.drift_not_run(str(exc.payload.get("error")) if isinstance(exc, CliExit) else str(exc))
    return doctor_mod.drift_check(found["drift"], found["board"], found["truncated"], found["read"])


def _queue_drift(ctx: Context) -> Dict[str, Any]:
    """queue.resync.read for this project's queue: open issues whose state label and board Status disagree.
    MirrorFailed (board not set, or not allowed: checked before any board read), LookupError (board not found),
    GhError or ValueError (the queue cannot be built) are left to the caller. Only reads."""
    return resync_mod.read(ctx.queue())


def _init_token(ctx: Context) -> Dict[str, Any]:
    if ctx.restricted:
        raise fail(ExitCode.ERROR, "human_only", "the operator token is created by a person, in a terminal "
                                                 f"application that Claude Code did not start; {humans.SECOND_LAYER}",
                   note=humans.SECOND_LAYER)
    try:
        token = humans.create_token(ctx.cfg.home)
    except FileExistsError:
        raise fail(ExitCode.ERROR, "token_exists", "an operator token already exists; delete "
                                                   f"{humans.token_path(ctx.cfg.home)} to make a new one") from None
    return {"token": token, "path": str(humans.token_path(ctx.cfg.home)),
            "note": "shown once: keep it out of files the agent can read"}


# --- parser ------------------------------------------------------------------------


def _common() -> argparse.ArgumentParser:
    """Global flags, accepted before or after the subcommand (absent unless given)."""
    p = argparse.ArgumentParser(add_help=False)
    for flags, kw in GLOBAL_FLAGS:
        p.add_argument(*flags, default=argparse.SUPPRESS, **kw)
    return p


def _add_args(p: argparse.ArgumentParser, specs: Sequence[Union[Arg, OneOf]]) -> None:
    for spec in specs:
        if isinstance(spec, OneOf):
            group = p.add_mutually_exclusive_group()
            for flags, kw in spec.args:
                group.add_argument(*flags, **kw)
        else:
            p.add_argument(*spec[0], **spec[1])


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree built from COMMANDS. Every parser gets its own copy of the global flags: parents share
    Action objects, and the top-level defaults must not leak into the subparsers (where they mean "absent")."""
    parser = _Parser(prog="notme", parents=[_common()], description="not-me command line. Every invocation prints "
                     "exactly one JSON object on stdout; stderr is chatter.")
    parser.set_defaults(project_dir=None, repo=None, dry_run=False, json=True)
    top = parser.add_subparsers(dest="top", metavar="<command>", parser_class=_Parser)
    top.required = True
    groups: Dict[str, Any] = {}
    for cmd in COMMANDS:
        subs = top
        if len(cmd.path) == 2:
            name = cmd.path[0]
            if name not in groups:
                g = top.add_parser(name, parents=[_common()], help=GROUPS[name], description=GROUPS[name])
                groups[name] = g.add_subparsers(dest=f"{name}_cmd", metavar="<subcommand>", parser_class=_Parser)
                groups[name].required = True
            subs = groups[name]
        p = subs.add_parser(cmd.path[-1], parents=[_common()], help=cmd.help, description=cmd.help)
        p.set_defaults(cmd=cmd, writes=cmd.writes)
        _add_args(p, cmd.args)
        if cmd.writes:
            p.add_argument("--log", metavar="PATH", help="append an ACTION line to this run log when done")
    return parser


def leaf_parsers(parser: argparse.ArgumentParser) -> Dict[Tuple[str, ...], argparse.ArgumentParser]:
    """Every leaf subparser by command path, for the docs generator."""
    out: Dict[Tuple[str, ...], argparse.ArgumentParser] = {}

    def walk(p: argparse.ArgumentParser, prefix: Tuple[str, ...]) -> None:
        subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            out[prefix] = p
            return
        for name, child in subs[0].choices.items():
            walk(child, prefix + (name,))
    walk(parser, ())
    return out


# --- main ----------------------------------------------------------------------------


def _log_line(cmd: Command, args: argparse.Namespace, dry: bool) -> str:
    ref = getattr(args, "ref", None) or getattr(args, "number", None) or ""
    return " ".join(x for x in (f"notme {cmd.name}", str(ref), "(dry run)" if dry else "") if x)


def _keep_log_warnings(ctx: Optional[Context], log: Any, payload: Dict[str, Any]) -> None:
    """On the error path, the warnings collected for the run log (queue.sync mirror failures) still reach the
    run log as WARN lines and the error payload's warnings."""
    if ctx is None or not ctx.log_warnings:
        return
    existing = payload.get("warnings")
    payload["warnings"] = (existing if isinstance(existing, list) else []) + ctx.log_warnings
    if log is not None:
        try:
            for text in ctx.log_warnings:
                log.action(text, kind="WARN")
        except (OSError, ValueError) as exc:
            payload["warning"] = f"run log not written: {exc.__class__.__name__}"


def main(argv: Optional[Sequence[str]] = None, *, runner: Optional[gh_mod.Runner] = None,
         env: Optional[Mapping[str, str]] = None, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None,
         stderr: Optional[TextIO] = None, hook_cmd: Optional[List[str]] = None, cwd: Optional[str] = None,
         sleep: Optional[Callable[[float], None]] = None, clock: Optional[Callable[[], float]] = None,
         process_check: Optional[Callable[[], bool]] = None,
         prompt: Optional[Callable[[], Optional[str]]] = None) -> int:
    """Run one invocation and return its exit code. The keyword arguments let tests inject the gh runner,
    environment, streams, hook command, working directory, the sleep and clock `wait-checks` uses, the
    Claude Code ancestry check and the hidden operator-token prompt."""
    argv = list(sys.argv[1:] if argv is None else argv)
    deps = Deps(runner, env, stdin, stdout, stderr, hook_cmd, cwd, argv, sleep, clock, process_check, prompt)
    code, payload = int(ExitCode.OK), {}
    ctx: Optional[Context] = None
    log: Any = None
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as exc:  # --help prints argparse text and exits 0
            return int(exc.code or 0)
        ctx = Context(args, deps)
        log = ctx.open_log(args.log) if getattr(args, "log", None) else None
        payload = args.cmd.run(ctx, args)
        dry = args.writes and ctx.mode_is_dry
        if dry:
            payload.setdefault("dry_run", True)
        if log is not None:
            try:
                log.action(_log_line(args.cmd, args, dry))
                for text in ctx.log_reasons:
                    log.action(f"why: {text}")
                for text in ctx.log_warnings:
                    log.action(text, kind="WARN")
            except (OSError, ValueError) as exc:
                payload["warning"] = f"run log not written: {exc.__class__.__name__}"
        if ctx.warnings:
            existing = payload.get("warnings")
            payload["warnings"] = (existing if isinstance(existing, list) else []) + ctx.warnings
    except KeyboardInterrupt:
        code, payload = int(ExitCode.ERROR), {"error": "interrupted", "kind": "interrupted"}
    except Exception as exc:  # noqa: BLE001 - every failure becomes one JSON object
        code, payload = error_response(exc, deps)
        deps.chatter(f"notme: {payload.get('error')}")
        _keep_log_warnings(ctx, log, payload)
    print(json.dumps(redact_obj(json.loads(json.dumps(payload, default=str)))), file=deps.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
