"""Work queues: where not-me finds the next issue and how it claims it.

Two sources share one interface: issues carrying a pick-up label (labels.LabelQueue) and a GitHub
Projects v2 board (board.BoardQueue).
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

try:  # Protocol is 3.8+
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from .. import claims, paths
from ..gh import GhClient, _login, notme_tmp_dir
from ..states import Labels
from ..writes import WriteTarget, valid_slug


@dataclass
class Candidate:
    number: int
    title: str
    url: str
    author: str
    labels: List[str]
    priority: Tuple = ()  # sorts ascending: smaller means sooner
    is_epic: bool = False
    parent: Optional[int] = None
    created: str = ""

    def sort_key(self) -> Tuple:
        return (self.priority, self.created, self.number)


@dataclass
class Claim:
    number: int
    previous: str
    now: str
    marker_comment_id: Optional[int]
    run_id: str


class Queue(Protocol):
    def candidates(self) -> List[Candidate]: ...

    def claim(self, n: int) -> Claim: ...

    def release(self, n: int, to: str = "todo") -> None: ...

    def move(self, n: int, state: str, require_current: Optional[str] = None) -> None: ...

    def state(self, n: int) -> Optional[str]: ...


class BaseQueue:
    """What both queues share: config, the claim marker comment, assignee edits and the local lock."""

    def __init__(self, cfg: Any, gh: GhClient, roster: Any, operator: str, run_id: str, tracker: str,
                 claims_dir: Optional[Path] = None, clock: Optional[claims.Clock] = None):
        self.meta: Dict[str, Any] = (cfg.meta or {}).get("project", {})
        self.queue_cfg: Dict[str, Any] = self.meta.get("queue") or {}
        self.labels = Labels(self.meta.get("labels") or {})
        self.stale_hours = float((self.meta.get("limits") or {}).get("claim_stale_hours", 8))
        if not valid_slug(tracker):
            raise ValueError(f"invalid tracker repo {tracker!r}")
        self.gh, self.roster, self.run_id, self.tracker = gh, roster, run_id, tracker
        self.operator = _login(operator)
        self.claims_dir = Path(claims_dir) if claims_dir else None
        self._clock = clock or claims.utcnow

    def now(self) -> datetime:
        return self._clock()

    def _target(self) -> WriteTarget:
        return WriteTarget.repo(self.tracker)

    # --- label and assignee helpers ---

    def _has(self, names: Sequence[str], abstract: str) -> bool:
        wanted = self.labels.name(abstract).lower()
        return any(str(n).lower() == wanted for n in names)

    def _edit_assignee(self, n: int, flag: str, login: str, what: str) -> None:
        self.gh.write(self._target(), ["issue", "edit", str(int(n)), "-R", self.tracker, flag, _login(login)],
                      description=f"{what} {login} on {self.tracker}#{int(n)}")

    def assign(self, n: int) -> None:
        self._edit_assignee(n, "--add-assignee", self.operator, "assign")

    def unassign(self, n: int) -> None:
        self._edit_assignee(n, "--remove-assignee", self.operator, "unassign")

    def trusted(self, author: Optional[str]) -> bool:
        """Whether the roster trusts this author to direct work. Both queues filter candidates through this."""
        return bool(self.roster.is_trusted(author))

    def _is_me(self, login: Optional[str]) -> bool:
        return (login or "").lower() == self.operator.lower()

    # --- the marker comment ---

    @contextlib.contextmanager
    def _body_file(self, text: str) -> Iterator[Path]:
        fd, name = tempfile.mkstemp(prefix="not-me-claim-", suffix=".md", dir=str(notme_tmp_dir()))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            yield Path(name)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass

    def marker_comments(self, n: int) -> List[Tuple[Dict[str, Any], Dict[str, str]]]:
        out = []
        for comment in self.gh.comments(self.tracker, n):
            marker = claims.parse_marker(comment.get("body"))
            if marker:
                out.append((comment, marker))
        return out

    def _mine(self, n: int) -> Optional[Dict[str, Any]]:
        """Our newest marker comment on the issue (released or not), if any."""
        mine = [c for c, m in self.marker_comments(n) if self._is_me(m.get("login"))]
        return max(mine, key=lambda c: c["id"]) if mine else None

    def upsert_marker(self, n: int, released: bool = False) -> Optional[int]:
        """Create our marker comment, or edit the one we already have. Never a second one."""
        body = claims.marker_body(self.operator, self.run_id, claims.iso(self.now()), released=released)
        existing = self._mine(n)
        with self._body_file(body) as path:
            if existing is not None:
                self.gh.comment_update(self.tracker, existing["id"], path)
                return existing["id"]
            self.gh.comment_create(self.tracker, n, path)
        if self.gh.dry_run:
            return None
        created = self._mine(n)
        return created["id"] if created else None

    def mark_released(self, n: int) -> None:
        if self._mine(n) is not None:
            self.upsert_marker(n, released=True)

    # --- local lock ---

    def _lock_file(self, n: int) -> Optional[Path]:
        return claims.lock_path(self.claims_dir, n) if self.claims_dir else None

    def lock(self, n: int) -> None:
        path = self._lock_file(n)
        if path is not None:
            claims.acquire_lock(path, self.run_id, self.stale_hours, self.now())

    def unlock(self, n: int) -> None:
        path = self._lock_file(n)
        if path is not None:
            claims.release_lock(path, self.run_id)


def make_queue(cfg: Any, gh: GhClient, roster: Any, operator: str, run_id: str, *, tracker: Optional[str] = None,
               project_dir: Any = None, clock: Optional[claims.Clock] = None) -> Queue:
    """The queue for `queue.source` (labels or board)."""
    meta = (cfg.meta or {}).get("project", {})
    source = (meta.get("queue") or {}).get("source", "labels")
    slug = tracker or meta.get("tracker")
    if slug in (None, "auto"):
        slug = gh.ctx.origin_repo
    if not slug:
        raise ValueError("no tracker repo: set `tracker` in project.md or run inside a clone with an origin")
    where = project_dir or getattr(cfg, "project_dir", None)
    common = paths.git_common_dir(where) if where else None
    claims_dir = common / "not-me" / "claims" if common else None
    kwargs = dict(claims_dir=claims_dir, clock=clock)
    if source == "labels":
        from .labels import LabelQueue
        return LabelQueue(cfg, gh, roster, operator, run_id, slug, **kwargs)
    if source == "board":
        from .board import BoardQueue
        return BoardQueue(cfg, gh, roster, operator, run_id, slug, **kwargs)
    raise ValueError(f"queue.source must be labels or board, got {source!r}")
