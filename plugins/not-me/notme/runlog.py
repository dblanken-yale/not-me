"""The run log and the active-run marker.

A run log is one plain-markdown file per skill run, written under the log root:
a title line, a ``## Summary`` section (a placeholder while the run is going,
filled in by ``finish``), and a ``## Timeline`` of one-line entries.

Logs are local working records. Never commit them: the caller keeps the log
root out of version control (the default root sits inside the git common dir,
which git never tracks; a configured or env root inside the repo needs a
gitignore entry).

The active-run marker is a small JSON file saying a run is in progress. Other
commands read it to find the current run; a marker older than ``stale_hours``
is ignored (not deleted), so a crashed run cannot block anything for long.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import paths

PathLike = paths.PathLike

KINDS = ("ACTION", "REFUSED", "IGNORED", "CHECK", "GATE", "WARN", "DELEGATE", "AUDIT")
ALL_KINDS = KINDS + ("QUESTION",)
IN_PROGRESS = "(run in progress)"
MARKER_NAME = "active-run"
LOG_ROOT_ENV = "NOT_ME_LOG_ROOT"
CREATE_ATTEMPTS = 20

SUMMARY_MARK = "## Summary"
TIMELINE_MARK = "## Timeline"

_TITLE_RE = re.compile(r"^# not-me:(\S+) (.*?)   run=(\S+)\s*$")
_ENTRY_RE = re.compile(r"^- (\S+)  ([A-Z]+)  (.*)$")
_QFIELD_RE = re.compile(r"^  - (DECISION|UNDO): (.*)$")
_STATUS_RE = re.compile(r"^Status: (.*)$", re.M)
_NEWLINES_RE = re.compile(r"\s*[\r\n]+\s*")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ActiveRunError(RuntimeError):
    """start() found a fresh marker owned by another run; ``existing`` is that marker."""

    def __init__(self, existing: dict) -> None:
        super().__init__(f"another run is active: {existing.get('run_id')}")
        self.existing = existing


def _one_line(text: Any) -> str:
    return _NEWLINES_RE.sub(" / ", str(text)).strip()


def _slug(text: str, limit: int = 40) -> str:
    return _NON_SLUG_RE.sub("-", str(text).lower()).strip("-")[:limit].strip("-") or "run"


def _now(now: Optional[datetime] = None) -> datetime:
    """A timezone-aware local time; a naive injected time is read as local."""
    return (now if now is not None else datetime.now()).astimezone()


def _title_line(skill: str, ref: str, run_id: str) -> str:
    return f"# not-me:{skill} {ref}   run={run_id}"


def _entry_line(stamp: str, kind: str, message: str) -> str:
    return f"- {stamp}  {kind}  {message}"


def _summary_block(status: str, problems: List[str], questions: List[Dict[str, str]],
                   counts: Mapping[str, int]) -> str:
    lines = [f"Status: {status}", "", "Refused / failures first:"]
    lines += [f"- {p}" for p in problems] or ["none"]
    lines += ["", "Questions and decisions:"]
    for number, q in enumerate(questions, 1):
        lines += [f"{number}. {q['question']}", f"   - Decision: {q['decision']}", f"   - Undo: {q['undo']}"]
    if not questions:
        lines.append("none")
    lines += ["", "Counts: " + ", ".join(f"{kind} {counts[kind]}" for kind in ALL_KINDS)]
    return "\n".join(lines)


def _split(text: str) -> Tuple[str, str, str]:
    """(head before the summary, summary body, timeline body) of a log file."""
    head, rest = _cut_at_line(text, SUMMARY_MARK)
    summary, timeline = _cut_at_line(rest, TIMELINE_MARK)
    return head, summary.strip("\n"), timeline.lstrip("\n")


def _cut_at_line(text: str, marker: str) -> Tuple[str, str]:
    """Split at the first line that is exactly ``marker`` (so a marker inside
    an entry message cannot be mistaken for a section heading)."""
    match = re.search(r"(?m)^" + re.escape(marker) + r"[ \t]*$", text)
    if match is None:
        return text, ""
    return text[:match.start()], text[match.end():]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_marker(marker_path: PathLike) -> Optional[dict]:
    """The marker as a dict, or None when missing or unreadable."""
    try:
        data = json.loads(Path(marker_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_fresh(marker: dict, stale_hours: float, now: Optional[datetime]) -> bool:
    try:
        started = _now(datetime.fromisoformat(marker["started"]))
    except (KeyError, TypeError, ValueError):
        return False
    return _now(now) - started <= timedelta(hours=stale_hours)


def _owned_by(marker: dict, run_id: str) -> bool:
    return marker.get("run_id") == run_id


def log_root(project_dir: Optional[PathLike], config_root: Optional[PathLike] = None,
             env: Optional[Mapping[str, str]] = None, create: bool = True) -> Path:
    """The log directory: config value, else $NOT_ME_LOG_ROOT, else <git-common-dir>/not-me/logs.

    create=False only computes the path (read-only commands must not leave directories behind).

    A linked worktree shares the main checkout's root because the git common
    dir is shared. Outside git with nothing configured, falls back to
    <project>/.not-me/logs, which the caller must keep out of version control.
    """
    environ = os.environ if env is None else env
    chosen: Optional[Path] = None
    raw = config_root or environ.get(LOG_ROOT_ENV)
    if raw:
        chosen = Path(raw).expanduser()
    else:
        common = paths.git_common_dir(project_dir) if project_dir else None
        if common is not None:
            chosen = common / "not-me" / "logs"
        elif project_dir:
            chosen = Path(project_dir) / ".not-me" / "logs"
    if chosen is None:
        raise ValueError("cannot resolve a log root: no config value, no $NOT_ME_LOG_ROOT, no project")
    if create:
        chosen.mkdir(parents=True, exist_ok=True)
    return chosen


def is_run_log(path: PathLike) -> bool:
    """True when the file's first line is a run-log title line (`# not-me:<skill> <ref>   run=<id>`)."""
    try:
        with open(path, encoding="utf-8") as handle:
            first = handle.readline(4096)
    except (OSError, UnicodeDecodeError):
        return False
    return _TITLE_RE.match(first.rstrip("\r\n")) is not None


def default_marker_path(root: PathLike) -> Path:
    return Path(root).parent / MARKER_NAME


class RunLog:
    """One run's log file. Build with ``RunLog.start`` or ``RunLog.open``."""

    def __init__(self, path: PathLike, skill: str, ref: str, run_id: str, marker_path: PathLike) -> None:
        self.path = Path(path)
        self.skill = skill
        self.ref = ref
        self.run_id = run_id
        self.marker_path = Path(marker_path)
        self.status: Optional[str] = None
        self.entries: List[Dict[str, str]] = []

    @property
    def questions(self) -> List[Dict[str, str]]:
        return [{"question": e["message"], "decision": e.get("decision", ""), "undo": e.get("undo", "")}
                for e in self.entries if e["kind"] == "QUESTION"]

    @classmethod
    def start(cls, root: PathLike, skill: str, ref: str, *, now: Optional[datetime] = None,
              marker_path: Optional[PathLike] = None, force: bool = False) -> "RunLog":
        root = Path(root)
        marker = Path(marker_path) if marker_path else default_marker_path(root)
        existing = active_run(marker, now=now)
        if existing and not force:
            raise ActiveRunError(existing)
        skill_slug = _slug(skill)
        ref = _one_line(ref)
        folder = root / skill_slug
        folder.mkdir(parents=True, exist_ok=True)
        moment = _now(now)
        prefix = f"{moment.strftime('%Y%m%d-%H%M%S')}-{_slug(ref)}"
        for _ in range(CREATE_ATTEMPTS):
            run_id = f"{prefix}-{secrets.token_hex(3)}"
            path = folder / f"{run_id}.md"
            try:
                with open(path, "x", encoding="utf-8", newline="\n") as handle:
                    handle.write(f"{_title_line(skill_slug, ref, run_id)}\n\n"
                                 f"{SUMMARY_MARK}\n\n{IN_PROGRESS}\n\n{TIMELINE_MARK}\n\n")
            except FileExistsError:
                continue
            break
        else:
            raise FileExistsError(f"could not create a unique log file in {folder.name}")
        _atomic_write(marker, json.dumps({
            "run_id": run_id, "skill": skill_slug, "ref": ref,
            "started": moment.isoformat(timespec="seconds"), "pid": os.getpid(),
        }))
        return cls(path, skill_slug, ref, run_id, marker)

    @classmethod
    def open(cls, path: PathLike, marker_path: Optional[PathLike] = None) -> "RunLog":
        path = Path(path)
        head, summary, timeline = _split(path.read_text(encoding="utf-8"))
        title = _TITLE_RE.match(head.strip())
        skill, ref, run_id = title.groups() if title else (path.parent.name, "", path.stem)
        log = cls(path, skill, ref, run_id, marker_path or default_marker_path(path.parent.parent))
        found = _STATUS_RE.search(summary)
        log.status = found.group(1).strip() if found else None
        for line in timeline.splitlines():
            entry = _ENTRY_RE.match(line)
            field = _QFIELD_RE.match(line)
            if entry:
                log.entries.append(dict(zip(("time", "kind", "message"), entry.groups())))
            elif field and log.entries:
                log.entries[-1][field.group(1).lower()] = field.group(2)
        return log

    def _record(self, kind: str, message: str, extra_lines: Tuple[str, ...] = (),
                now: Optional[datetime] = None, **extra: str) -> None:
        text = _one_line(message)
        stamp = _now(now).isoformat(timespec="seconds")
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join((_entry_line(stamp, kind, text),) + extra_lines) + "\n")
        self.entries.append({"time": stamp, "kind": kind, "message": text, **extra})

    def action(self, message: str, kind: str = "ACTION", *, now: Optional[datetime] = None) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
        self._record(kind, message, now=now)

    def question(self, question: str, decision: str, undo: str, *, now: Optional[datetime] = None) -> None:
        d, u = _one_line(decision), _one_line(undo)
        self._record("QUESTION", question, (f"  - DECISION: {d}", f"  - UNDO: {u}"), now, decision=d, undo=u)

    def finish(self, status: str, *, failures: Optional[List[str]] = None,
               refused: Optional[List[str]] = None) -> str:
        status = _one_line(status)
        problems = [_one_line(x) for x in (refused or []) + (failures or [])]
        counts = Counter(e["kind"] for e in self.entries)
        summary = _summary_block(status, problems, self.questions, counts)
        _, _, timeline = _split(self.path.read_text(encoding="utf-8"))
        _atomic_write(self.path, f"{_title_line(self.skill, self.ref, self.run_id)}\n\n"
                                 f"{SUMMARY_MARK}\n\n{summary}\n\n{TIMELINE_MARK}\n\n{timeline}")
        self.status = status
        marker_clear(self.marker_path, self.run_id)
        return summary


def last(root: PathLike, skill: Optional[str] = None) -> Optional[RunLog]:
    """The newest log by filename (they start with a timestamp), optionally for one skill."""
    root = Path(root)
    if not root.is_dir():
        return None
    folders = [root / _slug(skill)] if skill else [p for p in root.iterdir() if p.is_dir()]
    files = [f for folder in folders if folder.is_dir() for f in folder.glob("*.md")]
    return RunLog.open(max(files, key=lambda f: f.name)) if files else None


def active_run(marker_path: PathLike, stale_hours: float = 6, now: Optional[datetime] = None) -> Optional[dict]:
    """The marker as a dict when it exists and is fresher than stale_hours, else None."""
    marker = _read_marker(marker_path)
    return marker if marker is not None and _is_fresh(marker, stale_hours, now) else None


def marker_clear(marker_path: PathLike, run_id: Optional[str] = None) -> bool:
    """Remove the marker; with run_id, only if the marker belongs to that run."""
    if run_id is not None:
        marker = _read_marker(marker_path)
        if marker is None or not _owned_by(marker, run_id):
            return False
    try:
        Path(marker_path).unlink()
    except OSError:
        return False
    return True
