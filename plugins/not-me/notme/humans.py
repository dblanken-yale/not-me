"""Is a person running this command? The second layer behind the PreToolUse guard for human-only commands.

A model can fake a terminal (a pty wrapper such as `script`, `expect`, `unbuffer`, `socat` or Python's pty module
makes stdin a tty) and can look up the operator's public gh login. It cannot change which processes started it.
So a command counts as human only when stdin is a terminal, no Claude Code variable is set, no ancestor process is
Claude Code (the CLI, its node entry point, or the desktop app, whose own terminal panel is a child of it), and
the private operator token in <not-me home>/operator-token is given: in NOT_ME_OPERATOR_CONFIRM, or, when that is
unset, typed at a hidden prompt.

The PreToolUse guard is the enforcement; this check is a second layer.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

TOKEN_NAME = "operator-token"
SECOND_LAYER = "the PreToolUse guard is the enforcement; this check is a second layer"
MAX_DEPTH = 64
_SESSION_ROOTS = {"explorer"}  # Windows Explorer, the root of a desktop session's processes

# pid -> (parent pid, command name, full command line)
ProcessTable = Dict[int, Tuple[int, str, str]]

_CLAUDE_NAMES = {"claude", "claude-code"}
_CLAUDE_ARGS = (
    re.compile(r"@anthropic-ai[/\\]claude-code", re.I),
    re.compile(r"claude-code[^\s]*[/\\]cli\.js", re.I),
    re.compile(r"[/\\]Claude\.app[/\\]", re.I),  # macOS desktop app bundle and its helpers
    re.compile(r"[/\\]AnthropicClaude[/\\]", re.I),  # Windows desktop app install dir
    re.compile(r"(?:^|[/\\\s\"])Claude\.exe\b", re.I),
)


def _base(text: str) -> str:
    name = re.split(r"[/\\]", text.strip().strip('"'))[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def is_claude(comm: str, args: str) -> bool:
    """True when a process's command name or command line is Claude Code (CLI, node entry point or desktop app)."""
    argv0 = args.strip().split(" ", 1)[0] if args.strip() else ""
    if _base(comm) in _CLAUDE_NAMES or (argv0 and _base(argv0) in _CLAUDE_NAMES):
        return True
    return any(p.search(f"{comm} {args}") for p in _CLAUDE_ARGS)


# --- reading the process table ----------------------------------------------------


def _run(argv: list) -> Optional[str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None


def _proc_table() -> Optional[ProcessTable]:
    """Linux /proc: ppid and comm from /proc/<pid>/stat, args from /proc/<pid>/cmdline."""
    root = Path("/proc")
    if not (root / "self" / "stat").is_file():
        return None
    table: ProcessTable = {}
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            args = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        m = re.match(r"^\d+ \((.*)\) \S+ (\d+)", stat, re.S)
        if m:
            table[int(entry.name)] = (int(m.group(2)), m.group(1), args)
    return table or None


def _ps_table() -> Optional[ProcessTable]:
    """POSIX ps: two whole-table reads (comm and args may both hold spaces, so each is the last column)."""
    comms = _run(["ps", "-A", "-o", "pid=,ppid=,comm="])
    argss = _run(["ps", "-A", "-o", "pid=,args="])
    if comms is None or argss is None:
        return None
    args_by_pid: Dict[int, str] = {}
    for line in argss.splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            args_by_pid[int(parts[0])] = parts[1] if len(parts) > 1 else ""
    table: ProcessTable = {}
    for line in comms.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            pid = int(parts[0])
            table[pid] = (int(parts[1]), parts[2] if len(parts) > 2 else "", args_by_pid.get(pid, ""))
    return table or None


def _windows_table() -> Optional[ProcessTable]:
    """Windows: wmic CSV (columns Node,CommandLine,Name,ParentProcessId,ProcessId), else PowerShell CIM as CSV."""
    out = _run(["wmic", "process", "get", "CommandLine,Name,ParentProcessId,ProcessId", "/format:csv"])
    if out is None:
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Select-Object Node,CommandLine,Name,ParentProcessId,ProcessId "
                    "| ConvertTo-Csv -NoTypeInformation"])
    if out is None:
        return None
    table: ProcessTable = {}
    for row in csv.reader(io.StringIO(out.strip())):
        if len(row) < 5 or not row[-1].strip().isdigit() or not row[-2].strip().isdigit():
            continue
        args = ",".join(row[1:-3])  # a command line may itself hold commas
        table[int(row[-1])] = (int(row[-2]), row[-3], args)
    return table or None


def read_process_table() -> Optional[ProcessTable]:
    """The whole process table, or None when it cannot be read."""
    if sys.platform.startswith("win"):
        return _windows_table()
    return _proc_table() or _ps_table()


def under_claude_process_tree(table: Optional[ProcessTable] = None, pid: Optional[int] = None,
                              reader: Callable[[], Optional[ProcessTable]] = read_process_table) -> bool:
    """True when any ancestor of pid (default: this process) is Claude Code, or when the ancestry cannot be read
    completely (fail closed). The walk ends at pid 0 or 1, or at a process whose parent is itself."""
    table = table if table is not None else reader()
    if not table:
        return True
    here = os.getpid() if pid is None else pid
    if here not in table:
        return True
    for _ in range(MAX_DEPTH):
        if here in (0, 1):
            return False
        row = table.get(here)
        if row is None:
            return True  # a gap in the chain: cannot vouch for what started us
        ppid, comm, args = row
        if is_claude(comm, args):
            return True
        if _base(comm) in _SESSION_ROOTS:
            return False  # the desktop shell: on Windows its own parent has exited, which is not a gap
        if ppid == here:
            return False
        here = ppid
    return True


# --- the operator token -----------------------------------------------------------


def token_path(home: os.PathLike) -> Path:
    return Path(home) / TOKEN_NAME


def create_token(home: os.PathLike) -> str:
    """Write a new random token (mode 0600) and return it. FileExistsError when one is already there."""
    path = token_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def read_token(home: os.PathLike) -> Optional[str]:
    """The operator token, or None when it is missing, empty, a symlink, or (POSIX) readable by others or not ours."""
    path = token_path(home)
    try:
        if path.is_symlink():
            return None
        st = path.stat()
        if os.name == "posix" and (st.st_mode & 0o077 or st.st_uid != os.getuid()):
            return None
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def ask_token(prompt: str = "not-me operator token (input hidden): ") -> Optional[str]:
    """Read the token from the terminal without echoing it; None when it cannot be read without echo. getpass
    writes the prompt to the terminal (or stderr), never to stdout."""
    import getpass
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)  # its fallback would echo the token
            return getpass.getpass(prompt)
    except (EOFError, OSError, getpass.GetPassWarning):
        return None


def token_matches(home: os.PathLike, given: Optional[str]) -> bool:
    expected = read_token(home)
    return bool(expected and given) and hmac.compare_digest(expected, str(given).strip())


# --- the live-mode marker ---------------------------------------------------------
#
# `mode: live` in the global project.md takes effect only next to <not-me home>/mode-live.sig, which holds an
# HMAC-SHA256 over LIVE_MESSAGE and the resolved not-me home, keyed by the operator token and the epoch: the
# random hex in <not-me home>/mode-live.epoch (key = token bytes + NUL + epoch bytes). Only `notme mode live`,
# after the person check, writes the marker (creating the epoch when it is missing); going back to dry-run in the
# global layer deletes the marker and replaces the epoch, so a copy of an old marker saved while live never
# verifies again. Rotating the token or moving the home invalidates it too. The guard keeps the agent from
# reading or writing the epoch, exactly as it does for the token.

LIVE_MARKER = "mode-live.sig"
LIVE_EPOCH = "mode-live.epoch"
LIVE_MESSAGE = b"not-me mode live v2\0"
_MARKER_LIMIT = 256  # a hex SHA-256 is 64 characters; anything much longer is not ours
_EPOCH_RE = re.compile(r"^[0-9a-f]{64}$")


def live_marker_path(home: os.PathLike) -> Path:
    return Path(home) / LIVE_MARKER


def live_epoch_path(home: os.PathLike) -> Path:
    return Path(home) / LIVE_EPOCH


def _live_mac(token: str, epoch: str, home: os.PathLike) -> str:
    bound = os.path.normcase(os.path.realpath(str(home)))
    key = token.encode("utf-8") + b"\0" + epoch.encode("ascii")
    return hmac.new(key, LIVE_MESSAGE + bound.encode("utf-8"), hashlib.sha256).hexdigest()


def _write_private(path: Path, text: str, prefix: str) -> None:
    """Write text to path atomically through a mode-0600 temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(path.parent))  # mkstemp: 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_live_epoch(home: os.PathLike) -> Optional[str]:
    """The epoch, or None when it is missing, a symlink, garbled, or (POSIX) readable by others or not ours."""
    path = live_epoch_path(home)
    try:
        if path.is_symlink():
            return None
        st = path.stat()
        if os.name == "posix" and (st.st_mode & 0o077 or st.st_uid != os.getuid()):
            return None
        with open(str(path), "rb") as fh:
            data = fh.read(_MARKER_LIMIT + 1)
        text = data.decode("ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if _EPOCH_RE.match(text) else None


def new_live_epoch(home: os.PathLike) -> str:
    """Replace the epoch with a new random one (mode 0600, atomic) and return it."""
    epoch = secrets.token_hex(32)
    _write_private(live_epoch_path(home), epoch + "\n", ".mode-live-epoch-")
    return epoch


def write_live_marker(home: os.PathLike) -> Path:
    """Sign live mode for home with the operator token and the epoch (created when missing or unusable): write
    the marker atomically (mode 0600 where the platform has it) and return its path. ValueError when there is no
    usable token."""
    token = read_token(home)
    if not token:
        raise ValueError("no usable operator token; create one with `notme doctor --init-token`")
    epoch = read_live_epoch(home) or new_live_epoch(home)
    path = live_marker_path(home)
    _write_private(path, _live_mac(token, epoch, home) + "\n", ".mode-live-")
    return path


def clear_live_marker(home: os.PathLike) -> bool:
    """Remove the marker (whatever sits at its path) and replace the epoch, so no saved copy of an old marker
    verifies again; True when there was a marker. When the epoch cannot be replaced it is removed (best effort):
    a missing epoch is unconfirmed, and the next `notme mode live` makes a new one."""
    try:
        live_marker_path(home).unlink()
        had = True
    except FileNotFoundError:
        had = False
    try:
        new_live_epoch(home)
    except OSError:
        try:
            live_epoch_path(home).unlink()
        except OSError:
            pass
    return had


def live_confirmed(home: Optional[os.PathLike]) -> bool:
    """True only when the marker exists and matches the current operator token and epoch for this home. With no
    marker neither the token nor the epoch is read. Never raises for a missing or garbled file."""
    if home is None:
        return False
    path = live_marker_path(home)
    try:
        if not path.is_file():
            return False
        with open(str(path), "rb") as fh:
            given = fh.read(_MARKER_LIMIT + 1)
    except OSError:
        return False
    if len(given) > _MARKER_LIMIT:
        return False
    token = read_token(home)
    epoch = read_live_epoch(home)
    if not token or not epoch:
        return False
    try:
        text = given.decode("ascii").strip()
    except UnicodeDecodeError:
        return False
    return hmac.compare_digest(_live_mac(token, epoch, home), text)
