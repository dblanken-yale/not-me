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
NOT_ME_DRY_RUN can only turn dry-run on. `allow-repo add|remove` and `pause off` also need NOT_ME_OPERATOR_CONFIRM
set to the private operator token in <not-me home>/operator-token (create it with `notme doctor --init-token`).
The PreToolUse guard is the enforcement; this check is a second layer.

--dry-run, NOT_ME_DRY_RUN=1 or `mode: dry-run` send every write to the intended-writes ledger instead of
GitHub; write commands then carry "dry_run": true. Write commands take --log PATH (an existing run log under
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
from notme import humans, writeback, writes  # noqa: E402
from notme.dryrun import LedgerError  # noqa: E402
from notme.gh import csv, names_of, login_of  # noqa: E402
from notme.queue import NotOpen, make_queue, stale_claims  # noqa: E402
from notme.redact import redact, redact_obj  # noqa: E402


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
    (states.StateMismatch, ExitCode.ERROR, "state_mismatch", None),
    (states.IllegalTransition, ExitCode.ERROR, "illegal_transition", None),
    ((LookupError, runlog.ActiveRunError), ExitCode.ERROR, "not_found", None),
    (_tool_missing, ExitCode.DEPENDENCY, "missing_dependency",
     lambda e: {"error": f"{e.filename} is not installed or not on PATH", "dependency": e.filename}),
    (LedgerError, ExitCode.ERROR, "ledger", None),
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
                 process_check: Optional[Callable[[], bool]] = None):
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


class Context:
    """Trust anchor, config, write gate and GitHub client for one invocation (the last three built on use)."""

    def __init__(self, args: argparse.Namespace, deps: Deps):
        self.args, self.deps = args, deps
        self.warnings: List[str] = []
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

    def need_work_dir(self) -> Path:
        if self.work_dir is None:
            raise fail(ExitCode.USAGE, "usage", "no project directory: run inside a clone (or pass --project-dir)")
        return self.work_dir

    def require_human(self, what: str) -> None:
        """Refuse `what` unless a person runs it (not restricted) and NOT_ME_OPERATOR_CONFIRM is the operator
        token. The refusal carries the command to run in a terminal that Claude Code did not start."""
        if not self.restricted and humans.token_matches(self.cfg.home, self.deps.env.get(CONFIRM_ENV)):
            return
        token = humans.token_path(self.cfg.home)
        notme = [str(Path(_PLUGIN_DIR) / "bin" / "notme"), *self.deps.argv]
        command = f'{CONFIRM_ENV}="$(cat {shlex.quote(str(token))})" ' + " ".join(shlex.quote(a) for a in notme)
        raise fail(ExitCode.ERROR, "human_only",
                   f"{what} is for a person: run it in a terminal application that Claude Code did not start "
                   f"(not the desktop app's terminal panel), with {CONFIRM_ENV} set to the operator token "
                   f"(create one with `notme doctor --init-token`); {humans.SECOND_LAYER}",
                   terminal_cmd=command, note=humans.SECOND_LAYER)

    # --- config ---

    @property
    def cfg(self) -> config_mod.Config:
        if self._cfg is None:
            project = self.work_dir or getattr(self.args, "project_dir", None)
            self._cfg = config_mod.load(project_dir=project, env=self.env)
        return self._cfg

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
        return runlog.RunLog.open(real, marker_path=runlog.default_marker_path(self.work_dir))

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
        try:
            marker = runlog.active_run(runlog.default_marker_path(self.work_dir))
        except (ValueError, OSError):
            return None
        return {k: marker.get(k) for k in ("run_id", "skill", "ref", "started")} if marker else None

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
GROUPS = {"log": "run logs", "retro": "retro audits kept in the git directory", "allow-repo": "the global allow list of repos and boards", "guard": "the write guard",
          "git": "git operations behind the write gate"}
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


@command("config", "print the effective configuration", args=[
    arg("--explain", action="store_true", help="where each key came from"),
    arg("--check", action="store_true", help="list configuration errors (exit 4 when any)"),
    arg("--layer", choices=("default", "global", "repo"), help="limit to one layer")])
def cmd_config(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Effective configuration. --explain adds where each key came from, --check lists errors (exit 4 when any;
    two states sharing one label name is an error), --layer limits the output to one layer (default, global or
    repo).

    Output: config (per document) and roster; with --explain layers, keys, bodies, warnings; with --check ok, errors
    """
    cfg = ctx.cfg
    if args.check:
        ctx.require_valid()
        return {"ok": True, "errors": [], "warnings": list(cfg.warnings)}
    if args.explain:
        out = cfg.explain()
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


@command("pick-issue", "the next issue to work (claims nothing)", running="none", args=[
    arg("--exclude", type=int, action="append", metavar="N", help="skip this issue (repeatable)")])
def cmd_pick_issue(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """The next issue to work, by the queue's order. Nothing is claimed.

    Output: number, title, url, author, labels, parent, candidates (count); or none (with reason when paused)
    """
    skip = set(args.exclude or [])
    found = [c for c in ctx.queue().candidates() if c.number not in skip]
    if not found:
        return {"none": True}
    top = found[0]
    return {"number": top.number, "title": top.title, "url": top.url, "author": top.author,
            "labels": top.labels, "parent": top.parent, "candidates": len(found)}


@command("claim-issue", "claim an issue", writes=True, running="error", ref="tracker", args=[number_arg])
def cmd_claim_issue(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Claim an issue: assign, move to in progress, post the claim marker. Exit 3 when someone else holds it.

    Output: number, previous, now, marker_comment_id, run_id
    """
    return dataclasses.asdict(ctx.queue().claim(n))


@command("release-issue", "give a claimed issue back", writes=True, ref="tracker", args=[
    number_arg, arg("--to", choices=("todo", "blocked"), default="todo")])
def cmd_release_issue(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Give an issue back: unassign, move from in progress to todo or blocked, mark the claim released.

    Output: number, to, released
    """
    ctx.queue().release(n, args.to)
    return {"number": n, "to": args.to, "released": True}


@command("move-issue", "move an issue to a state", writes=True, ref="tracker", args=[
    number_arg, arg("--to", required=True, choices=states.STATES, metavar="STATE"),
    arg("--require-current", choices=states.STATES, metavar="STATE")])
def cmd_move_issue(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Move an issue to a state, optionally only when it is currently in another one.

    Output: number, to, require_current
    """
    ctx.queue().move(n, args.to, args.require_current)
    return {"number": n, "to": args.to, "require_current": args.require_current}


@command("issue", "an issue's facts", ref="tracker", args=[number_arg])
def cmd_issue(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """An issue's facts. The body and comments of an author the roster does not trust are withheld.

    Output: repo, number, title, state, url, author, author_trusted, labels, status, assignees, body, comments,
    comments_ignored
    """
    return feedback_mod.issue_view(ctx.gh, repo, n, ctx.roster, ctx.operator, ctx.labels)


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
    arg("--kind", choices=("issue", "pr"), default="issue")])
def cmd_transition(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Change an item's state label along a legal edge (reads the labels, replaces the set, reads it back).

    Output: repo, number, kind, from, to, labels
    """
    repo, n = ctx.ref(args.number, ctx.tracker if args.kind == "issue" else ctx.repo)
    new = states.apply(ctx.gh, repo, n, args.from_state, args.to, ctx.labels, args.kind)
    return {"repo": repo, "number": n, "kind": args.kind, "from": args.from_state, "to": args.to, "labels": new}


@command("labels-sync", "make an item's labels match", writes=True, ref="tracker", args=[
    arg("number", metavar="N|REF", help="a number (in the tracker), #N, owner/name#N or a URL"),
    OneOf(arg("--set", metavar="a,b", help="replace the whole label set"),
          arg("--add", metavar="a,b", help="labels to add")),
    arg("--remove", metavar="a,b", help="labels to remove (not with --set)"),
    arg("--check", action="store_true", help="only report whether the labels match")])
def cmd_labels_sync(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Make an item's labels match: --set replaces the whole set, --add and --remove edit it. --check only reports. A bare number
    is an item in the tracker; owner/name#N or a URL names an item in another allowed repository (a companion PR).

    Output: repo, number, in_sync, changed, labels (current, or the new set)
    """
    if (args.set is None) == (args.add is None and args.remove is None) or \
            (args.set is not None and args.remove is not None):
        raise fail(ExitCode.USAGE, "usage", "give --set, or --add and/or --remove")
    return writeback.label_sync(ctx.gh, repo, n, set_to=csv(args.set) if args.set is not None else None,
                                add=csv(args.add), remove=csv(args.remove), check=args.check)


@command("labels-ensure", "create the state labels the repo lacks", writes=True, args=[
    arg("--check", action="store_true", help="only report what is missing")])
def cmd_labels_ensure(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Create the configured state labels that the repository lacks (all eleven by default). --check only reports.

    Output: repo, ok (nothing missing), present, missing, created
    """
    return states.ensure_labels(ctx.gh, ctx.tracker, ctx.meta.get("labels") or {}, check=args.check)


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
    arg("--draft", action="store_true")])
def cmd_pr_open(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Open a PR, set its labels, request review. The head branch may not be a protected branch.

    Output: repo, number, url (both null in a dry run), head, base
    """
    repo = ctx.repo
    if ctx.protected(args.head):
        raise fail(ExitCode.ERROR, "protected_branch", f"refusing to open a PR from protected branch {args.head!r}")
    body = with_attribution(ctx, ctx.read_input(args.body_file))
    out = writeback.open_pr(ctx.gh, ctx.cfg, repo, args.head, args.base, ctx.read_line(args.title_file), body,
                            csv(args.labels), csv(args.reviewers), args.assignee, args.draft)
    return {"repo": repo, "number": out.get("number"), "url": out.get("url"), "head": args.head, "base": args.base}


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
    arg("--labels-set", metavar="a,b", help="set the full label set after posting")])
def cmd_review_post(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Post a review, optionally set labels, and read both back. APPROVE is refused unless review.auto_approve is
    true in the global (or default) layer; the repo layer cannot turn it on.

    Output: repo, number, event, state (event only in a dry run)
    """
    if args.event == "APPROVE" and ctx.trusted_meta("review").get("auto_approve") is not True:
        raise fail(ExitCode.ERROR, "approve_refused", "APPROVE needs review.auto_approve: true in the global layer")
    inline = None
    if args.comments_file:
        inline = ctx.read_json(args.comments_file, "comments file")
        if not isinstance(inline, list) or not all(isinstance(c, dict) and {"path", "line", "body"} <= set(c)
                                                    for c in inline):
            raise fail(ExitCode.USAGE, "usage", "comments file must be a JSON list of {path, line, body}")
    out = writeback.review_post(ctx.gh, repo, n, args.event.lower(), ctx.read_input(args.body_file), inline,
                                csv(args.labels_set) if args.labels_set is not None else None, ctx.run_id)
    return {"repo": repo, "number": n, **out}


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


@command("issue-edit-body", "replace an issue's body", writes=True, ref="tracker", args=[
    arg("number", metavar="N|REF", help="a number (in the tracker), #N, owner/name#N or a URL"), body_file_arg])
def cmd_issue_edit_body(ctx: Context, args: argparse.Namespace, repo: str, n: int) -> Dict[str, Any]:
    """Replace an issue's body with a file's text (no attribution is added). The file must pass the input-path
    checks; the target repository goes through the write gate.

    Output: repo, number, updated
    """
    with gh_mod.tmp_text_file(ctx.read_input(args.body_file), prefix="not-me-cli-") as path:
        ctx.gh.issue_edit_body(repo, n, path)
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
    """Lint a text bound for GitHub (bare #N, local paths, assistant mentions, TL;DR line). Exit 1 when it finds
    problems.

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
    """Start a run log and the active-run marker. Exit 1 (kind active_run) when another run is active.

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
    """Finish a run log: write its summary and clear the active-run marker.

    Output: path, status, summary
    """
    log = ctx.open_log(args.path)
    failures = None
    if args.failures_file:
        text = ctx.read_input(args.failures_file)
        failures = [str(x) for x in json.loads(text)] if text.lstrip().startswith("[") \
            else [ln.strip() for ln in text.splitlines() if ln.strip()]
    return {"path": args.path, "status": args.status, "summary": log.finish(args.status, failures=failures)}


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
    it must run from a terminal Claude Code did not start, with NOT_ME_OPERATOR_CONFIRM set to the operator token;
    otherwise the refusal (kind human_only) carries the command to run. The guard is the enforcement; this check is
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


def worktrees_root(ctx: Context) -> Path:
    common = paths.git_common_dir(ctx.need_work_dir())
    if common is None:
        raise fail(ExitCode.USAGE, "usage", "not a git repository")
    return Path(os.path.realpath(str(common))) / "not-me" / "worktrees"


@command("git worktree-add", "add a detached worktree", args=[arg("path", metavar="PATH"), arg("sha", metavar="SHA")])
def cmd_git_worktree_add(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Add a detached worktree at a sha. PATH must be below <git-common-dir>/not-me/worktrees/.

    Output: path, sha
    """
    root = worktrees_root(ctx)
    real = Path(os.path.realpath(args.path))
    if root not in real.parents:
        raise fail(ExitCode.ERROR, "unsafe_path", "worktree path must be under <git-common-dir>/not-me/worktrees/")
    git_mod.worktree_add_detached(real, args.sha, ctx.need_work_dir())
    return {"path": args.path, "sha": args.sha}


@command("git worktree-remove", "remove a worktree", args=[arg("path", metavar="PATH")])
def cmd_git_worktree_remove(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Remove a worktree.

    Output: path, removed
    """
    git_mod.worktree_remove(args.path, ctx.need_work_dir())
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
    """A read-only summary: enabled and paused state, the active run, items needing a person, stale claims, and the
    last run of each skill. When GitHub cannot be read the items are left out with a warning. Creates nothing.

    Output: enabled, paused (global, project), mode, repo, tracker, operator, active_run, items, stale_claims,
    last_runs, warnings
    """
    ctx.require_valid()
    pauses = {scope: path.exists() for scope, path in ctx.pause_files().items()}
    out: Dict[str, Any] = {
        "enabled": ctx.meta.get("enabled") is not False,
        "paused": {"global": pauses.get("global", False), "project": pauses.get("project", False)},
        "mode": str(ctx.meta.get("mode") or "dry-run"), "repo": None, "tracker": None, "operator": None,
        "active_run": ctx.active_run(), "items": [], "stale_claims": [], "last_runs": {},
        "warnings": list(ctx.guard.warnings)}
    try:
        root = ctx.log_root()
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            log = runlog.last(root, folder.name)
            if log is not None:
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


@command("doctor", "check tools, auth, config and the guard", args=[
    arg("--init-token", action="store_true", help="create the operator token (a person only; shown once)")])
def cmd_doctor(ctx: Context, args: argparse.Namespace) -> Dict[str, Any]:
    """Check the tools and setup: python, git and gh versions, gh auth and scopes, config, guard self-test, spawn
    depth. Exit 7 when the only failed required checks are gh, gh auth or gh scopes; exit 1 when another
    required check fails. --init-token instead creates <not-me home>/operator-token (random, mode 0600) and shows
    it once; only a person may run it (kind human_only otherwise), and an existing token is never replaced
    (kind token_exists: delete the file to make a new one).

    Output: ok, checks (name, ok, detail, required); with --init-token token, path
    """
    if args.init_token:
        return _init_token(ctx)
    checks = doctor_mod.run(ctx.deps.runner or gh_mod._default_runner, ctx.config_errors(), lambda: ctx.gh,
                            ctx.guard_state, ctx.env)
    verdict = doctor_mod.exit_kind(checks)
    if verdict:
        code = ExitCode.DEPENDENCY if verdict == "dependency" else ExitCode.ERROR
        raise CliExit(code, {"error": "a required check failed", "kind": "doctor", "ok": False, "checks": checks})
    return {"ok": True, "checks": checks}


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


def main(argv: Optional[Sequence[str]] = None, *, runner: Optional[gh_mod.Runner] = None,
         env: Optional[Mapping[str, str]] = None, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None,
         stderr: Optional[TextIO] = None, hook_cmd: Optional[List[str]] = None, cwd: Optional[str] = None,
         sleep: Optional[Callable[[float], None]] = None, clock: Optional[Callable[[], float]] = None,
         process_check: Optional[Callable[[], bool]] = None) -> int:
    """Run one invocation and return its exit code. The keyword arguments let tests inject the gh runner,
    environment, streams, hook command, working directory, the sleep and clock `wait-checks` uses, and the
    Claude Code ancestry check."""
    argv = list(sys.argv[1:] if argv is None else argv)
    deps = Deps(runner, env, stdin, stdout, stderr, hook_cmd, cwd, argv, sleep, clock, process_check)
    code, payload = int(ExitCode.OK), {}
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
    print(json.dumps(redact_obj(json.loads(json.dumps(payload, default=str)))), file=deps.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
