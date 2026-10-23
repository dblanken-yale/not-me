"""Work queues: where not-me finds the next issue and how it claims it.

Two sources share one interface: issues carrying a pick-up label (labels.LabelQueue) and a GitHub
Projects v2 board (board.BoardQueue).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:  # Protocol is 3.8+
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from .. import claims, paths
from ..gh import GhClient, _login, tmp_text_file
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


class NotOpen(Exception):
    """The pull request is closed or merged, so it cannot be claimed."""


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

    def _body_file(self, text: str):
        return tmp_text_file(text, prefix="not-me-claim-")

    def marker_trusted(self, login: Optional[str]) -> bool:
        """Whose claim markers are believed: the operator and anyone the roster trusts."""
        return self._is_me(login) or self.trusted(login)

    def marker_comments(self, n: int) -> List[Tuple[Dict[str, Any], Dict[str, str]]]:
        """Claim markers on the issue that were really written by the login they name, and a trusted one."""
        out = []
        for comment in self.gh.comments(self.tracker, n):
            marker = claims.parse_marker(comment.get("body"))
            if marker and claims.author_matches(comment.get("author"), marker) \
                    and self.marker_trusted(marker["login"]):
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

    def _lock_file(self, n: int, kind: str = "issue") -> Optional[Path]:
        return claims.lock_path(self.claims_dir, n, kind) if self.claims_dir else None

    def lock(self, n: int, kind: str = "issue") -> None:
        path = self._lock_file(n, kind)
        if path is not None:
            claims.acquire_lock(path, self.run_id, self.stale_hours, self.now())

    def unlock(self, n: int, kind: str = "issue", force: bool = False) -> None:
        """Drop our lock; force drops it whichever run holds it."""
        path = self._lock_file(n, kind)
        if path is not None:
            claims.release_lock(path, None if force else self.run_id)

    # --- pull request claims ---

    def live_foreign_claims(self, n: int) -> List[Tuple[Dict[str, Any], Dict[str, str]]]:
        """Unreleased, fresh claim markers on n written by someone other than the operator."""
        return [(c, m) for c, m in self.marker_comments(n)
                if not self._is_me(m.get("login")) and not claims.is_released(m)
                and not claims.stale(m["ts"], self.stale_hours, self.now())]

    def claim_pr(self, n: int) -> Optional[int]:
        """Claim PR n of this queue's repo for this run: refuse a closed, paused or needs-human PR or a live
        foreign claim, take the local lock, post the claim marker, then check no earlier claim raced it.

        A dry run records the marker in the ledger and takes no lock. Returns the marker comment id (None in a
        dry run). ClaimLost when someone else holds it, NotOpen when it is closed.
        """
        repo = self.tracker
        pr = self.gh.pr(repo, n)
        if str(pr.get("state", "OPEN")).upper() != "OPEN":
            raise NotOpen(f"{repo}#{n} is not open")
        names = {str(x.get("name") if isinstance(x, dict) else x).lower() for x in pr.get("labels") or []}
        for role in ("paused", "needs_human"):
            if self.labels.name(role).lower() in names:
                raise claims.ClaimLost(f"{repo}#{n} is marked {role}")
        other = self.live_foreign_claims(n)
        if other:
            raise claims.ClaimLost(f"{repo}#{n} is claimed by {other[0][1]['login']}")
        if self.gh.dry_run:
            return self.upsert_marker(n)
        self.lock(n, "pr")
        try:
            marker_id = self.upsert_marker(n)
            mine = self._mine(n)
            ours = (claims.parse_ts(claims.parse_marker(mine["body"])["ts"]), mine["id"]) if mine else None
            older = [m["login"] for c, m in self.live_foreign_claims(n)
                     if ours and (claims.parse_ts(m["ts"]), c["id"]) < ours]
            if older:
                self.mark_released(n)
                raise claims.ClaimLost(f"{repo}#{n} was claimed first by {older[0]}")
        except BaseException:
            self.unlock(n, "pr")
            raise
        return marker_id

    def release_pr(self, n: int) -> None:
        """Mark the operator's claim on PR n released and remove the local lock, whichever run holds it."""
        self.mark_released(n)
        if not self.gh.dry_run:
            self.unlock(n, "pr", force=True)


def stale_claims(gh: GhClient, tracker: str, labels: Labels, hours: float, branches: Sequence[str],
                 is_trusted: Callable[[Optional[str]], bool]) -> List[Dict[str, Any]]:
    """Open in-progress issues whose newest trusted claim marker is older than `hours` and that have no `<n>-`
    branch, as [{number, title, url}]. Only reports."""
    from .labels import is_stale

    name = labels.name("in_progress")
    issues = gh.read(["issue", "list", "-R", tracker, "--state", "open", "--label", name,
                      "--json", "number,title,url,labels", "--limit", "100"]) or []
    # Comments come from REST (gh.comments): `gh issue --json comments` drops the author type, so a bot's
    # marker would not read as `<slug>[bot]`.
    return [{"number": i["number"], "title": i.get("title"), "url": i.get("url")} for i in issues
            if is_stale({**i, "comments": gh.comments(tracker, i["number"])}, hours, branches, name,
                        is_trusted=is_trusted)]


def make_queue(cfg: Any, gh: GhClient, roster: Any, operator: str, run_id: str, *, kind: str = "issue",
               repo: Optional[str] = None, tracker: Optional[str] = None, project_dir: Any = None,
               clock: Optional[claims.Clock] = None) -> Any:
    """The queue for `queue.source` (labels or board); kind="pr" gives the BaseQueue that claims pull requests
    of `repo`. Claim locks live in <git-common-dir>/not-me/claims of project_dir."""
    meta = (cfg.meta or {}).get("project", {})
    slug = repo or tracker or meta.get("tracker")
    if slug in (None, "auto"):
        slug = gh.ctx.origin_repo
    if not slug:
        raise ValueError("no tracker repo: set `tracker` in project.md or run inside a clone with an origin")
    where = project_dir or getattr(cfg, "project_dir", None)
    common = paths.git_common_dir(where) if where else None
    kwargs = dict(claims_dir=common / "not-me" / "claims" if common else None, clock=clock)
    if kind == "pr":
        return BaseQueue(cfg, gh, roster, operator, run_id, slug, **kwargs)
    if kind != "issue":
        raise ValueError(f"queue kind must be issue or pr, got {kind!r}")
    source = (meta.get("queue") or {}).get("source", "labels")
    if source == "labels":
        from .labels import LabelQueue
        return LabelQueue(cfg, gh, roster, operator, run_id, slug, **kwargs)
    if source == "board":
        from .board import BoardQueue
        return BoardQueue(cfg, gh, roster, operator, run_id, slug, **kwargs)
    raise ValueError(f"queue.source must be labels or board, got {source!r}")
