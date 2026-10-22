#!/usr/bin/env python3
"""Run one live eval scenario end to end: seed, run the skill in a real Claude Code session, grade, tear down.

    python3 evals-harness/run_live.py --skill new --scenario N1 [--runs N] [--backend github|ledger]
        [--launch print|interactive] [--repo O/N] [--companion O/N] [--owner O] [--out DIR]
        [--keep-temp] [--dry-run]
    python3 evals-harness/run_live.py --restore-global RUN_ID [--force]

Per run: seed_fixture.py --reset and --scenario, scaffold.py prepare, stage the workspace (the scenario's
global config layer goes into your real ~/.claude/not-me, which is backed up first and restored after),
launch `claude` in the workspace with your own login, grade the transcript and the GitHub (or ledger) state,
then clean up and seed_fixture.py --teardown (also when a step failed). One result file per run:
<out>/<ID>-<n>.json. Runs are sequential on purpose: a skill scans the whole fixture repo.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import graders  # noqa: E402
import transcript as transcript_mod  # noqa: E402

SEED = ROOT / "fixture" / "seed_fixture.py"
SCAFFOLD = HERE / "scaffold" / "scaffold.py"
GRADE = HERE / "grade_state.py"
PLUGIN_DIR = ROOT / "plugins" / "not-me"
STATE_DIR = ROOT / "evals" / ".state"
CASES_DIR = graders.CASES
BACKUP_RECORD = "global-backup.json"
RESTORE_FAILED = 3  # run_once's code when the global layer could not be put back: stop the loop
SCENARIO_RE = re.compile(r"^[A-Z]\d+$")
RID_RE = re.compile(r"^[a-z0-9-]+$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DEFAULT_TIMEOUT = 1800
FUTURE_SLACK = 3600  # a record `created` further ahead of now than this was not written by a run
START_SLACK = 2.0  # start times read through ps have one-second resolution
# Credentials the session must not see: it uses the interactive Claude login and the fixture GH_TOKEN only.
STRIPPED_ENV = ("NOT_ME_FIXTURE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN")

# runner(argv, env, **opts): opts are cwd, stdout_path (capture stdout to a file), stdin_devnull and timeout.
Runner = Callable[..., int]


def subprocess_runner(argv: Sequence[str], env: Dict[str, str], cwd: Optional[Path] = None,
                      stdout_path: Optional[Path] = None, stdin_devnull: bool = False,
                      timeout: Optional[float] = None) -> int:
    kwargs: Dict[str, Any] = {"env": env, "cwd": str(cwd or ROOT), "timeout": timeout}
    if stdin_devnull:
        kwargs["stdin"] = subprocess.DEVNULL
    try:
        if stdout_path is not None:
            with open(str(stdout_path), "wb") as fh:
                return subprocess.run(list(argv), stdout=fh, **kwargs).returncode
        return subprocess.run(list(argv), **kwargs).returncode
    except subprocess.TimeoutExpired:
        return 124


TOKEN_RE = re.compile(r"(?:github_pat|gh[pousr])_[A-Za-z0-9_]+")


def redact(text: str) -> str:
    """Hide GitHub tokens, and the exact NOT_ME_FIXTURE_TOKEN value, in anything printed."""
    secret = os.environ.get("NOT_ME_FIXTURE_TOKEN")
    if secret:
        text = text.replace(secret, "<redacted-token>")
    return TOKEN_RE.sub("<redacted-token>", text)


def quote(argv: Sequence[str]) -> str:
    return redact(" ".join(shlex.quote(str(a)) for a in argv))


def new_run_id(scenario: str, n: int) -> str:
    return f"{scenario.lower()}-{n}-{time.strftime('%y%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"


def read_session_json(path: Path) -> Dict[str, Any]:
    """The result object of `claude -p --output-format json` ({} when the file is missing or not JSON)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, list):  # an event stream: the last result event
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
    return data if isinstance(data, dict) else {}


def global_dir(home: Path) -> Path:
    return home / ".claude" / "not-me"


def backup_path(home: Path, rid: str) -> Path:
    """Where the operator's config waits: a sibling of ~/.claude/not-me, so the rename stays on one filesystem,
    and outside the run's state directory, which the session can read."""
    if not RID_RE.match(rid):
        raise RuntimeError(f"unsafe run id {rid!r}")
    return global_dir(home).parent / f"not-me.backup-{rid}"


def replaced_path(home: Path, rid: str) -> Path:
    """Where a restore puts whatever sat at ~/.claude/not-me when it ran: kept, never deleted, so a restore driven
    by a rewritten record can at worst move the operator's config aside."""
    if not RID_RE.match(rid):
        raise RuntimeError(f"unsafe run id {rid!r}")
    return global_dir(home).parent / f"not-me.replaced-{rid}"


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()  # a dangling symlink is still the operator's


def host_name() -> str:
    """Informational only: on macOS the name can change with the network, so liveness never depends on it."""
    return platform.node() or socket.gethostname()


def _kernel32() -> Any:
    """kernel32 with pointer-sized handle types, so a 64-bit handle is not cut to an int."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    return k


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists on this host. Unsure means alive."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel32 = _kernel32()
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # access denied: it exists
            try:
                code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE; an exited process can linger while a handle is open
                return True
            finally:
                kernel32.CloseHandle(handle)
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:  # PermissionError, OSError, ctypes trouble: do not call a live run dead
        return True


def _start_windows(pid: int) -> Optional[float]:
    import ctypes
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        times = [ctypes.c_ulonglong() for _ in range(4)]  # creation, exit, kernel, user as FILETIME
        if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return times[0].value / 1e7 - 11644473600.0  # 100 ns ticks since 1601 to the Unix epoch
    finally:
        kernel32.CloseHandle(handle)


def _start_darwin(pid: int) -> Optional[float]:
    import ctypes
    import ctypes.util
    import struct
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib = (ctypes.c_int * 4)(1, 14, 1, int(pid))  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    buf = ctypes.create_string_buffer(1024)  # struct kinfo_proc is 648 bytes on 64-bit
    size = ctypes.c_size_t(len(buf))
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0 or size.value < 16:
        return None
    sec, usec = struct.unpack_from("qi", buf.raw, 0)  # kp_proc.p_starttime, a struct timeval
    return sec + usec / 1e6


def _start_proc(pid: int) -> Optional[float]:
    proc = Path("/proc")
    if not (proc / "stat").is_file():
        return None
    stat_text = (proc / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace")
    ticks = int(stat_text.rsplit(")", 1)[1].split()[19])  # field 22, starttime; the fields after ")" start at 3
    btime = next(int(line.split()[1]) for line in (proc / "stat").read_text(encoding="utf-8").splitlines()
                 if line.startswith("btime "))
    return btime + ticks / os.sysconf("SC_CLK_TCK")


def _start_ps(pid: int) -> Optional[float]:
    text = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                          env=dict(os.environ, LC_ALL="C"), timeout=10).stdout.strip()
    if not text:
        return None
    return time.mktime(time.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y"))


def process_start_time(pid: int) -> Optional[float]:
    """When the process with this pid started, as Unix time, or None when it cannot be read."""
    if os.name == "nt":
        readers: List[Callable[[int], Optional[float]]] = [_start_windows]
    elif sys.platform == "darwin":
        readers = [_start_darwin, _start_ps]
    else:
        readers = [_start_proc, _start_ps]
    for reader in readers:
        try:
            found = reader(pid)
        except Exception:  # missing /proc, no ps, ctypes trouble: try the next way, then give up
            found = None
        if found is not None:
            return found
    return None


def backup_global(home: Path, rid: str, record_path: Path) -> bool:
    """Rename the operator's ~/.claude/not-me to ~/.claude/not-me.backup-RID and write an informational record
    (pid and created mark the run as live). Returns whether it moved something: the caller restores from this
    value, never from its own earlier look at the filesystem. The record is written first, so a crash at any
    point leaves enough for --restore-global. If the rename fails nothing has moved and the record is removed
    again."""
    gdir, backup = global_dir(home), backup_path(home, rid)
    if _present(backup):
        raise RuntimeError(f"{backup} already exists; run --restore-global first")
    had = _present(gdir)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {"original": str(gdir.absolute()), "backup": str(backup.absolute()), "had_original": had,
              "run_id": rid, "pid": os.getpid(), "host": host_name(), "created": now,
              "timestamp": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if had:
        try:
            os.rename(str(gdir), str(backup))
        except OSError as exc:
            record_path.unlink()
            raise RuntimeError(f"cannot move {gdir} to {backup}: {exc}. The backup must be on the same "
                               "filesystem as ~/.claude; nothing was moved")
    return had


def _force_remove(path: Path) -> None:
    def onerror(func: Callable[..., Any], target: str, exc: Any) -> None:
        if func not in (os.unlink, os.remove, os.rmdir):
            # rmtree also reports failures from os.open/os.scandir; those cannot be retried with one argument
            raise exc[1] if isinstance(exc, tuple) else exc
        mode = stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if func is os.rmdir else 0)
        if not os.path.islink(target):  # a chmod through a symlink would change a file outside the tree
            if os.chmod in os.supports_follow_symlinks:
                os.chmod(target, mode, follow_symlinks=False)  # read-only git objects on Windows
            else:
                os.chmod(target, mode)
        func(target)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(str(path), onerror=onerror)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _move_aside(original: Path, replaced: Path) -> Optional[Path]:
    """Rename `original` to `replaced` (or `replaced`-2, -3, ... when taken). Returns where it went, or None when
    nothing was there. Never deletes."""
    if not _present(original):
        return None
    dest, n = replaced, 1
    while _present(dest):
        n += 1
        dest = replaced.with_name(f"{replaced.name}-{n}")
    os.rename(str(original), str(dest))
    return dest


def restore_dirs(original: Path, backup: Path, replaced: Path, had_original: bool) -> str:
    """Move what is at `original` aside to `replaced` and rename `backup` back over it. Nothing is deleted: the
    layer the run wrote stays at `replaced` for the operator to inspect or delete. Callers pass trusted paths,
    never ones read from the record file. os.rename refuses a non-empty target, so a directory that appears after
    the move makes this fail instead of nesting the backup inside it; the backup is then intact."""
    if _present(backup):
        aside = _move_aside(original, replaced)
        kept = f"; the layer the run wrote is at {aside}" if aside else ""
        try:
            os.rename(str(backup), str(original))
        except OSError as exc:
            raise RuntimeError(f"could not rename {backup} back to {original}: {exc}. Your config is intact in "
                               f"{backup}{kept}")
        return f"restored {original}{kept}" + (", delete it by hand when done" if aside else "")
    if had_original:
        # The run moved the config and the backup is gone: never leave the run's layer as the operator's.
        aside = _move_aside(original, replaced)
        kept = f" The layer the run wrote was moved from {original} to {aside}." if aside else ""
        raise RuntimeError(f"the backup of your config at {backup} is missing; nothing to put back.{kept}")
    aside = _move_aside(original, replaced)
    if aside is None:
        return f"there was no {original} before the run and there is none now; nothing to put back"
    return (f"there was no {original} before the run; the layer the run wrote was renamed to {aside}, "
            "delete it by hand when done")


def read_records(state: Path, home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every backup record under `state`, newest first by the time written inside it (not by name). A `created`
    more than an hour ahead of now is a rewritten record: it counts as 0 so it cannot jump the queue. With `home`,
    each record also lists why it looks tampered with (`tampered`, empty when it looks fine)."""
    found = []
    now = time.time()
    for path in state.glob(f"*/{BACKUP_RECORD}"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = float(data.get("created") or 0)
        except (OSError, ValueError, AttributeError, TypeError, OverflowError):
            data, created = {}, 0.0
        data = data if isinstance(data, dict) else {}
        tampered = []
        if created != created or created > now + FUTURE_SLACK:  # NaN or the future
            tampered.append("its timestamp is in the future")
            created = 0.0
        rid = path.parent.name
        if home is not None and not points_home(data, home, rid):
            tampered.append(f"it does not point at {global_dir(home)} and the backup for {rid}")
        found.append({"path": path, "rid": rid, "created": created, "data": data, "tampered": tampered})
    return sorted(found, key=lambda r: (r["created"], r["rid"]), reverse=True)


def points_home(data: Dict[str, Any], home: Path, rid: str) -> bool:
    """Whether a record names the paths this machine would use for this run id."""
    if not RID_RE.match(rid):
        return False
    return (Path(str(data.get("original"))) == global_dir(home).absolute()
            and Path(str(data.get("backup"))) == backup_path(home, rid).absolute())


def record_is_live(data: Dict[str, Any], created: Optional[float] = None) -> bool:
    """A record whose run may still be going: its pid is alive (and is not this process), and that process
    started before the record was written. A process that started later reuses the pid of a dead run. When the
    start time cannot be read the run counts as live; `--restore-global RID --force` is the way out. The host
    name is not used: on macOS it changes with the network. Callers pass `created` as cleaned by read_records."""
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid() or not pid_alive(pid):
        return False
    started = process_start_time(pid)
    if started is None:
        return True
    if created is None:
        try:
            created = float(data.get("created") or 0)
        except (ValueError, TypeError, OverflowError):
            return True
    return started <= created + START_SLACK


def force_hint(rid: str, pid: Any) -> str:
    return (f"If `ps -p {pid}` (Windows: `tasklist /FI \"PID eq {pid}\"`) shows that run is gone, and only "
            f"then, run `python3 evals-harness/run_live.py --restore-global {rid} --force`")


def tampered_hint(records: List[Dict[str, Any]], home: Path) -> str:
    """The refusal text for records that look rewritten, with how to clear each one."""
    bad = [r for r in records if r["tampered"]]
    if not bad:
        return ""
    lines = [" These records look tampered with and are never acted on: " + "; ".join(
        f"{r['path']} ({', '.join(r['tampered'])})" for r in bad) + "."]
    lines.append(f" For each, check that your config is in {global_dir(home)}; if a "
                 f"{global_dir(home).parent / 'not-me.backup-<RID>'} directory for it still exists, move it back "
                 "by hand, then delete the record file.")
    return "".join(lines)


def restore_global(state: Path, rid: str, home: Path, force: bool = False) -> str:
    """--restore-global RID. The record is checked against what this machine expects and then only read for
    liveness; the paths always come from `home` and the run id. `force` skips the liveness check only."""
    if not RID_RE.match(rid):
        raise RuntimeError(f"{rid!r} is not a run id")
    records = read_records(state, home)
    mine = next((r for r in records if r["rid"] == rid), None)
    if mine is None:
        raise RuntimeError(f"no backup record at {state / rid / BACKUP_RECORD}")
    data = mine["data"]
    real = [r for r in records if not r["tampered"]]  # a tampered record never counts as live or as newer
    if not force:
        live = next((r for r in real if record_is_live(r["data"], r["created"])), None)
        if live is not None:  # before the order check, so it never points at a live run to restore
            pid = live["data"].get("pid")
            raise RuntimeError(f"run {live['rid']} is still going (pid {pid}). Let it finish, or stop it, before "
                               f"restoring anything. {force_hint(live['rid'], pid)}")
    original, backup = global_dir(home), backup_path(home, rid)
    if mine["tampered"]:
        raise RuntimeError(f"the record for {rid} does not point at {original} and {backup}, or looks rewritten "
                           f"({', '.join(mine['tampered'])}); not touching anything. If {backup} exists, move it "
                           f"back to {original} by hand, then delete {mine['path']} to clear this record")
    if len(real) > 1 and real[0]["rid"] != rid:
        listing = ", ".join(f"{r['rid']} ({r['data'].get('timestamp', 'unknown time')})" for r in real)
        raise RuntimeError(f"{len(real)} backup records exist: {listing}. Restore the newest first, "
                           f"{real[0]['rid']}, then run this again for the next.{tampered_hint(records, home)}")
    replaced = replaced_path(home, rid)
    if _present(backup):
        what = restore_dirs(original, backup, replaced, True)
    elif data.get("had_original") is False:
        # Nothing to put back: the run's layer is renamed aside, never deleted, so a rewritten record can at
        # worst move the operator's config to `replaced`, where the message names it.
        what = restore_dirs(original, backup, replaced, False)
    else:
        # The record lives where a session could rewrite it. The run may have died before moving anything, so
        # whatever is at `original` may be the operator's own config: left alone.
        raise RuntimeError(f"no backup at {backup}. Whatever is at {original} now is left alone; check it by "
                           f"hand, then delete {mine['path']} to clear this record")
    try:
        mine["path"].unlink()
    except OSError as exc:
        return f"{what}. Leftover record at {mine['path']}, delete it ({exc})"
    return what


class Plan:
    """The commands for one run, built once so --dry-run and a real run cannot drift apart."""

    def __init__(self, args: argparse.Namespace, rid: str, n: int, out: Path, case: "graders.Case", home: Path):
        self.rid, self.n, self.out, self.case, self.home = rid, n, out, case, home
        flags: List[str] = ["--repo", args.repo, "--owner", args.owner]
        if args.companion:
            flags += ["--companion", args.companion]
        py = sys.executable or "python3"
        sid = args.scenario
        run_dir = STATE_DIR / rid
        self.manifest = run_dir / f"{sid}.json"
        self.stage_dir = run_dir / f"{sid}.stage"
        self.workspace = run_dir / f"{sid}.workspace"
        self.gh_config = self.stage_dir / "gh-config"
        self.token_file = self.stage_dir / "gh-token"
        self.backup_record = run_dir / BACKUP_RECORD
        self.backup_dir = backup_path(home, rid)
        self.session_json = out / f"{sid}-{n}.session.json"
        self.graders_json = out / f"{sid}-{n}.graders.json"
        self.grade_json = out / f"{sid}-{n}.grade.json"
        self.result = out / f"{sid}-{n}.json"
        self.ledger = Path(args.ledger) if args.ledger else self.workspace / ".git" / "not-me" / "logs" / "intended-writes.jsonl"
        self.timeout = case.meta.get("timeout_seconds")
        self.timeout = self.timeout if isinstance(self.timeout, (int, float)) and self.timeout > 0 else DEFAULT_TIMEOUT
        self.reset = [py, str(SEED), *flags, "--reset", "--run-id", rid]
        self.seed = [py, str(SEED), *flags, "--scenario", sid, "--run-id", rid]
        self.prepare = [py, str(SCAFFOLD), "prepare", "--manifest", str(self.manifest),
                        "--stage-dir", str(self.stage_dir), "--backend", args.backend]
        self.stage = [py, str(SCAFFOLD), "stage", "--case", f"{args.skill}-{sid}",
                      "--workspace", str(self.workspace), "--home", str(home)]
        self.launch_mode = args.launch
        self.launch = self.launch_argv(case, args.launch)
        self.clean = [py, str(SCAFFOLD), "clean", "--stage-dir", str(self.stage_dir)]
        grade = [py, str(GRADE), "--manifest", str(self.manifest), "--scenario", sid,
                 "--backend", args.backend, "--json", str(self.grade_json)]
        if args.backend == "ledger":
            grade += ["--ledger", str(self.ledger)]
        self.grade = grade
        self.teardown = [py, str(SEED), *flags, "--teardown", rid]
        self.env_extra = {"EVAL_NOT_ME_RUN_ID": rid, "EVAL_NOT_ME_SCENARIO": sid, "EVAL_NOT_ME_BACKEND": args.backend}
        self.prepare_env = {"NOT_ME_FIXTURE_OWNER": args.owner}

    def launch_argv(self, case: "graders.Case", mode: str) -> List[str]:
        """The token-free claude command. print: headless, JSON result. interactive: the operator's terminal."""
        plugin = str(PLUGIN_DIR)
        if mode == "interactive":
            return ["claude", "--plugin-dir", plugin, case.prompt]
        tools = case.meta.get("allowed_tools") or []
        tools = [tools] if isinstance(tools, str) else [str(t) for t in tools]
        argv = ["claude", "-p", case.prompt, "--plugin-dir", plugin, "--output-format", "json"]
        if tools:
            argv += ["--allowedTools", ",".join(tools)]
        return argv

    def session_env(self, base: Dict[str, str], token: str) -> Dict[str, str]:
        """The child env: the fixture token as GH_TOKEN only, an empty gh config dir so the operator's own gh
        login is invisible, and none of the other credentials. Claude uses the interactive login."""
        env = {k: v for k, v in base.items() if k not in STRIPPED_ENV}
        self.gh_config.mkdir(parents=True, exist_ok=True)
        # The run's own global git config: nothing from the operator's (a url.*.insteadOf could route pushes
        # over their SSH agent), just the credential helper that answers from GH_TOKEN. It lives in a file, not
        # in GIT_CONFIG_COUNT/KEY/VALUE: Claude Code sets those itself for its git proxy and would displace ours.
        run_gitconfig = self.stage_dir / "gitconfig"
        run_gitconfig.write_text(GITCONFIG, encoding="utf-8")
        for key in ("SSH_AUTH_SOCK", "GIT_CONFIG_PARAMETERS", "GIT_SSH"):
            env.pop(key, None)
        env.update(self.env_extra)
        env.update({"GH_TOKEN": token, "GH_CONFIG_DIR": str(self.gh_config), "GH_PROMPT_DISABLED": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    # no global or system git config (a url.*.insteadOf could route pushes over the operator's
                    # SSH agent), and no ssh at all
                    "GIT_CONFIG_GLOBAL": str(run_gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "GIT_SSH_COMMAND": "false"})
        for key in [k for k in env if k.startswith("GIT_CONFIG_") and k not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")]:
            env.pop(key)
        return env

    def commands(self) -> List[List[str]]:
        return [self.reset, self.seed, self.prepare, self.stage, self.launch, self.grade, self.clean, self.teardown]


GITCONFIG = """# written by evals-harness/run_live.py for one run; git asks gh, and gh answers from GH_TOKEN
[credential]
	helper =
	helper = !gh auth git-credential
"""


def find_session_transcript(plan: Plan, session_id: Optional[str], since: float, home: Path) -> Optional[Path]:
    ws = plan.workspace.resolve()
    if session_id and isinstance(session_id, str) and UUID_RE.match(session_id):
        found = transcript_mod.find_transcripts(ws, since=since, home=home, session_id=session_id)
        if found:
            return found[0]
    found = transcript_mod.find_transcripts(ws, since=since, home=home)
    return found[0] if found else None


def merge_result(args: argparse.Namespace, plan: Plan, session: Dict[str, Any], report: Optional["graders.Report"],
                 cost: Optional[float], errors: List[str]) -> dict:
    grade = None
    if plan.grade_json.is_file():
        try:
            grade = json.loads(plan.grade_json.read_text(encoding="utf-8"))
        except ValueError:
            errors.append("grade output is not valid JSON")
    if grade is None:
        errors.append("no grade output")
    if report is None:
        errors.append("no transcript grade")
    ok = bool(grade and grade.get("ok")) and bool(report and report.passed) and not errors
    return {"scenario": args.scenario, "skill": args.skill, "run": plan.n, "run_id": plan.rid,
            "backend": args.backend, "repo": args.repo,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session": session, "graders": report.to_dict() if report else None, "cost_usd": cost,
            "grade": grade, "errors": errors, "ok": ok}


def run_once(args: argparse.Namespace, n: int, runner: Runner, rid_factory: Callable[[str, int], str],
             out: Path, say: Callable[[str], None], home: Path, case: "graders.Case") -> int:
    rid = rid_factory(args.scenario, n)
    plan = Plan(args, rid, n, out, case, home)
    # Only `scaffold.py prepare` sees NOT_ME_FIXTURE_TOKEN. The seeder and grader use the operator's own gh login.
    prep_env = dict(os.environ, **plan.prepare_env)
    base = {k: v for k, v in os.environ.items() if k != "NOT_ME_FIXTURE_TOKEN"}
    env = {k: v for k, v in base.items() if k not in STRIPPED_ENV}  # the stage step needs no credentials
    say(f"# run {n} scenario={args.scenario} run-id={rid} launch={args.launch}")
    if args.dry_run:
        for cmd in plan.commands():
            if cmd is plan.stage:
                say(f"+ (would move {global_dir(home)} to {plan.backup_dir} and restore it after the run)")
            say("+ " + quote(cmd))
            if cmd is plan.launch:
                say(f"+ (in {plan.workspace}; GH_TOKEN from the staged token, GH_CONFIG_DIR={plan.gh_config})")
        say(f"+ (would write {plan.session_json}, {plan.graders_json} and merge into {plan.result})")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    errors: List[str] = []
    session: Dict[str, Any] = {"mode": args.launch, "command": quote(plan.launch)}
    report: Optional[graders.Report] = None
    cost: Optional[float] = None
    attempted = restore_failed = had = False
    try:
        for label, cmd in (("reset", plan.reset), ("seed", plan.seed)):
            say("+ " + quote(cmd))
            if runner(cmd, base) != 0:
                raise RuntimeError(f"{label} failed")
        say("+ " + quote(plan.prepare))
        if runner(plan.prepare, prep_env) != 0:
            raise RuntimeError("prepare failed (is NOT_ME_FIXTURE_TOKEN a dedicated fixture token?)")
        say(f"+ (moving {global_dir(home)} to {plan.backup_dir})")
        # in memory: the finally block never trusts the record file. Set only once the backup returned, so a
        # refused backup (one already exists, the rename failed) never runs a restore over the operator's config.
        had = backup_global(home, rid, plan.backup_record)
        attempted = True
        say("+ " + quote(plan.stage))
        if runner(plan.stage, env) != 0:
            raise RuntimeError("stage failed")
        try:
            token = plan.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read the staged token: {exc}")
        if not token:
            raise RuntimeError("the staged token file is empty")
        try:
            plan.token_file.unlink()  # the session must not find the token on disk
        except OSError as exc:
            raise RuntimeError(f"cannot remove the staged token file: {exc}")
        child_env = plan.session_env(base, token)
        since = time.time() - 2
        say("+ " + quote(plan.launch))
        if args.launch == "print":
            code = runner(plan.launch, child_env, cwd=plan.workspace, stdout_path=plan.session_json,
                          stdin_devnull=True, timeout=plan.timeout)
            info = read_session_json(plan.session_json)
            if code == 124:
                errors.append(f"claude timed out after {plan.timeout:g} s")
            elif code != 0:
                errors.append(f"claude exited {code}")
            if info.get("is_error"):
                errors.append("claude reported is_error in its result")
            cost = info.get("total_cost_usd") if isinstance(info.get("total_cost_usd"), (int, float)) else None
            sid = info.get("session_id")
            session.update({"session_id": sid if isinstance(sid, str) and UUID_RE.match(sid) else None, "turns": info.get("num_turns"),
                            "is_error": info.get("is_error")})
        else:
            code = runner(plan.launch, child_env, cwd=plan.workspace)
            if code != 0:
                errors.append(f"claude exited {code}")
            session.update({"session_id": None, "turns": None, "is_error": None})
        path = find_session_transcript(plan, session.get("session_id"), since, home)
        session["transcript"] = str(path) if path else None
        if path is None:
            errors.append(f"no transcript found under {transcript_mod.project_dir_for(plan.workspace.resolve(), home)}")
        else:
            report = graders.grade(case, transcript_mod.load(path), plan.workspace)
            plan.graders_json.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
            say(graders.format_table(report))
        say("+ " + quote(plan.grade))
        grade_exit = runner(plan.grade, base)
        if grade_exit not in (0, 1):
            errors.append(f"grade_state exited {grade_exit}")
    except (RuntimeError, OSError) as exc:
        errors.append(str(exc))
    finally:
        say("+ " + quote(plan.clean))
        try:
            if runner(plan.clean, base) != 0:
                errors.append("scaffold clean failed: remove the stage directory by hand, it holds a token")
        except OSError as exc:
            errors.append(f"scaffold clean failed: {exc}")
        if not args.keep_temp:
            try:
                _force_remove(plan.workspace)
            except Exception as exc:  # whatever rmtree raises, the restore below must still run
                errors.append(f"could not delete the workspace {plan.workspace}: {exc}")
        if attempted:
            try:
                say("+ " + restore_dirs(global_dir(home), plan.backup_dir, replaced_path(home, rid), had))
            except (OSError, RuntimeError) as exc:
                restore_failed = True
                errors.append(f"could not restore {global_dir(home)}: {exc}. Stopping. Run "
                              f"`run_live.py --restore-global {rid}` before anything else")
            else:
                try:
                    plan.backup_record.unlink(missing_ok=True)
                except OSError as exc:
                    errors.append(f"restored {global_dir(home)}; leftover record at {plan.backup_record}, "
                                  f"delete it ({exc})")
        say("+ " + quote(plan.teardown))
        try:
            if runner(plan.teardown, base) != 0:
                errors.append("teardown failed: the fixture may need --reset")
        except OSError as exc:
            errors.append(f"teardown failed: {exc}")
    result = merge_result(args, plan, session, report, cost, errors)
    plan.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    say(f"result: {plan.result} ok={result['ok']}")
    if restore_failed:
        return RESTORE_FAILED
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skill", help="skill name; the case is <skill>-<scenario>")
    ap.add_argument("--scenario")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--backend", choices=("github", "ledger"), default="github")
    ap.add_argument("--launch", choices=("print", "interactive"), default="print",
                    help="print: headless `claude -p`, no terminal needed (default). interactive: `claude` in "
                         "your terminal; you watch and exit it when the skill is done")
    ap.add_argument("--repo", default=os.environ.get("NOT_ME_FIXTURE_REPO"))
    ap.add_argument("--companion", default=os.environ.get("NOT_ME_FIXTURE_COMPANION"))
    ap.add_argument("--owner", default=os.environ.get("NOT_ME_FIXTURE_OWNER"))
    ap.add_argument("--out", default=str(ROOT / "results" / "evals" / time.strftime("%Y-%m-%d")),
                    help="result directory (default: results/evals/<today>)")
    ap.add_argument("--keep-temp", action="store_true", help="do not delete the run's workspace afterwards")
    ap.add_argument("--ledger", help="ledger JSONL for --backend ledger (default: the workspace's "
                                     ".git/not-me/logs/intended-writes.jsonl)")
    ap.add_argument("--restore-global", metavar="RUN_ID",
                    help="put back the ~/.claude/not-me that a run which died had moved aside, then exit")
    ap.add_argument("--force", action="store_true",
                    help="with --restore-global: skip the check that the run is still going. Only when "
                         "`ps -p <pid>` shows the run's process is gone")
    ap.add_argument("--dry-run", action="store_true", help="print the commands and run nothing")
    return ap


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner,
         rid_factory: Callable[[str, int], str] = new_run_id, out: Any = None, home: Optional[Path] = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out or sys.stdout
    home = Path(home) if home is not None else Path.home()

    def say(line: str) -> None:
        print(redact(line), file=stream)

    if args.restore_global:
        if not RID_RE.match(args.restore_global):
            print(f"error: {args.restore_global!r} is not a run id (lowercase letters, digits and dashes)",
                  file=sys.stderr)
            return 2
        try:
            say(restore_global(STATE_DIR, args.restore_global, home, force=args.force))
        except (OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.force:
        print("error: --force only goes with --restore-global", file=sys.stderr)
        return 2
    missing = [f"--{k}" for k in ("skill", "scenario", "repo", "owner") if not getattr(args, k)]
    if missing:
        print(f"error: {', '.join(missing)} required (--repo and --owner also come from "
              "NOT_ME_FIXTURE_REPO / NOT_ME_FIXTURE_OWNER)", file=sys.stderr)
        return 2
    if not SCENARIO_RE.match(args.scenario):
        print(f"error: --scenario {args.scenario!r} must look like N1 (one capital letter, then digits)",
              file=sys.stderr)
        return 2
    case_dir = CASES_DIR / f"{args.skill}-{args.scenario}"
    try:
        case = graders.load_case(case_dir)
    except graders.CaseError as exc:
        print(f"error: no usable case {case_dir.name}: {exc}", file=sys.stderr)
        return 2
    if not args.dry_run:
        records = read_records(STATE_DIR, home)
        real = [r for r in records if not r["tampered"]]  # tampered records are listed, never live or newest
        live = [r for r in real if record_is_live(r["data"], r["created"])]
        if live:
            pid = live[0]["data"].get("pid")
            print(f"error: run {live[0]['rid']} is still going on this machine (pid {pid}); it has your "
                  f"~/.claude/not-me moved aside. Wait for it to finish. Do not restore it while it runs. "
                  f"{force_hint(live[0]['rid'], pid)}", file=sys.stderr)
            return 2
        if real:
            listing = ", ".join(f"{r['rid']} ({r['data'].get('timestamp', 'unknown time')})" for r in real)
            print(f"error: a previous run died with your ~/.claude/not-me moved aside: {listing}. Restore the "
                  f"newest first: `python3 evals-harness/run_live.py --restore-global {real[0]['rid']}`, "
                  f"then the next, before anything else.{tampered_hint(records, home)}", file=sys.stderr)
            return 2
        if records:
            print(f"error: backup records are left over.{tampered_hint(records, home)}", file=sys.stderr)
            return 2
        if not os.environ.get("NOT_ME_FIXTURE_TOKEN"):
            print("error: NOT_ME_FIXTURE_TOKEN is not set (a fine-grained token for the fixture repos only; "
                  "see docs/testing.md)", file=sys.stderr)
            return 2
    if args.runs < 1:
        print("error: --runs must be at least 1", file=sys.stderr)
        return 2
    outdir = Path(args.out)
    taken = [int(p.stem.rsplit("-", 1)[1]) for p in outdir.glob(f"{args.scenario}-*.json")
             if p.stem.rsplit("-", 1)[1].isdigit()] if outdir.is_dir() else []
    first = max(taken, default=0) + 1  # a second invocation the same day adds runs instead of overwriting
    worst = 0
    for n in range(first, first + args.runs):
        code = run_once(args, n, runner, rid_factory, outdir, say, home, case)
        if code == RESTORE_FAILED:
            print("error: stopped after the first run because your ~/.claude/not-me could not be restored "
                  "(see the result file); no further run was started", file=sys.stderr)
            return 1
        worst = max(worst, code)
    return worst


if __name__ == "__main__":
    sys.exit(main())
