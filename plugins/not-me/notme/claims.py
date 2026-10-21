"""Pieces shared by the label and board queues: the claim marker comment, ClaimLost, local claim locks."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

Clock = Callable[[], datetime]

_MARKER_RE = re.compile(r"<!--\s*not-me:claim\s+(.*?)\s*-->", re.S)
_PAIR_RE = re.compile(r"([A-Za-z_]+)=(\S+)")
_SAFE_RE = re.compile(r"^[A-Za-z0-9._:\[\]+-]+$")


class ClaimLost(Exception):
    """Another worker holds, or won, the claim. Nothing of ours is left set when this is raised."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> Optional[datetime]:
    """An aware UTC datetime from an ISO string (a trailing Z is fine) or a datetime; None if unreadable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def stale(ts: Any, hours: float, now: Optional[datetime] = None) -> bool:
    """True when ts is older than `hours`. A timestamp that cannot be read counts as stale."""
    moment = parse_ts(ts)
    if moment is None:
        return True
    return (now or utcnow()) - moment > timedelta(hours=hours)


# --- the marker comment ----------------------------------------------------


def _field(name: str, value: str) -> str:
    if not _SAFE_RE.match(value):
        raise ValueError(f"unsafe {name} for a claim marker: {value!r}")
    return f"{name}={value}"


def marker_body(login: str, run_id: str, ts: str, released: bool = False) -> str:
    """The comment text: one human line, then the machine marker."""
    fields = [_field("login", login), _field("run", run_id), _field("ts", ts)]
    if released:
        fields.append("state=released")
    line = f"not-me released its claim for {login}" if released else f"not-me claimed this for {login}"
    return f"{line}\n\n<!-- not-me:claim {' '.join(fields)} -->\n"


def parse_marker(body: Optional[str]) -> Optional[Dict[str, str]]:
    """{login, run, ts, ...} from the first claim marker in a comment body, or None."""
    m = _MARKER_RE.search(body or "")
    if not m:
        return None
    found = dict(_PAIR_RE.findall(m.group(1)))
    return found if found.get("login") and found.get("ts") else None


def is_released(marker: Dict[str, str]) -> bool:
    return marker.get("state") == "released"


# --- local lock ------------------------------------------------------------


def lock_path(claims_dir: Union[str, Path], number: int) -> Path:
    return Path(claims_dir) / f"issue-{int(number)}.lock"


def read_lock(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def acquire_lock(path: Path, run_id: str, stale_hours: float, now: Optional[datetime] = None) -> None:
    """Create the lock file, or refresh it when it is ours or stale. ClaimLost when another live run holds it."""
    now = now or utcnow()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps({"run_id": run_id, "ts": iso(now)}) + "\n").encode("utf-8")
    for _ in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            held = read_lock(path)
            if held and held.get("run_id") != run_id and not stale(held.get("ts"), stale_hours, now):
                raise ClaimLost(f"another session on this machine (run {held.get('run_id')}) holds {path.name}")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        return
    raise ClaimLost(f"could not take {path.name}")


def release_lock(path: Path, run_id: str) -> bool:
    """Remove the lock if it is ours (or unreadable). True when a file was removed."""
    held = read_lock(path)
    if held is not None and held.get("run_id") != run_id:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True
