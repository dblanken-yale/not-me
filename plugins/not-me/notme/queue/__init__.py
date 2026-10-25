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
from ..gh import GhClient, GhError, _login, login_of, names_of, tmp_text_file
from ..states import Labels
from ..writes import WriteTarget, valid_slug


# sibling_info column for a board item in a named column that board_states does not map (Done, Backlog, ...):
# started, unlike None (no column at all: a label queue, or a board item with no Status value).
UNMAPPED_COLUMN = "<unmapped column>"


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
    position: Optional[int] = None  # 1-based place in the parent epic's unchecked task list
    board: Optional[Dict[str, Any]] = None  # board queues only: {status: column, fields: {field: option name}}

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


_REACTION_PAGE = 100  # GitHub's largest page; reactions are read one page only


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
        self.include_unassigned = self.queue_cfg.get("include_unassigned") is True
        self.unassigned_skipped = 0  # eligible issues the last candidates() left out only because nobody is assigned
        self.held_back: List[Dict[str, int]] = []  # eligible issues the last candidates() left out for epic order

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

    def assign(self, n: int, assignees: Sequence[str] = ()) -> bool:
        """Assign the operator, unless `assignees` (as just read) already has them. True when it assigned."""
        if any(self._is_me(a) for a in assignees):
            return False
        self._edit_assignee(n, "--add-assignee", self.operator, "assign")
        return True

    def unassign(self, n: int) -> None:
        self._edit_assignee(n, "--remove-assignee", self.operator, "unassign")

    def claim_assigned(self, n: int) -> bool:
        """Whether our claim marker on n says the claim itself assigned the operator. No marker, or no record:
        False, so a release never removes an assignment a person made."""
        mine = self._mine(n)
        return bool(mine) and (claims.parse_marker(mine.get("body")) or {}).get("assigned") == "claim"

    def unassign_if_claimed(self, n: int, added: Optional[bool] = None) -> None:
        """Unassign the operator only when the claim added the assignment (added, else the marker's record)."""
        if added if added is not None else self.claim_assigned(n):
            self.unassign(n)

    def trusted(self, author: Optional[str]) -> bool:
        """Whether the roster trusts this author to direct work. Both queues filter candidates through this."""
        return bool(self.roster.is_trusted(author))

    def _is_me(self, login: Optional[str]) -> bool:
        return (login or "").lower() == self.operator.lower()

    def _assignees_allow(self, assignees: Sequence[str]) -> bool:
        """Check this last, after every other eligibility rule. Assigned to the operator alone: yes. Assigned to
        anyone else: never. Nobody: only with queue.include_unassigned; otherwise counted in unassigned_skipped."""
        if any(not self._is_me(a) for a in assignees):
            return False
        if assignees or self.include_unassigned:
            return True
        self.unassigned_skipped += 1
        return False

    def list_limit(self) -> Optional[int]:
        """queue.list_limit: how many issues pick-issue lists with facts. Not set: no cap."""
        value = self.queue_cfg.get("list_limit")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"queue.list_limit must be a whole number of at least 1, got {value!r}")
        return value

    # --- epic order ---

    def _read_epics(self) -> List[Dict[str, Any]]:
        return self.gh.read(["issue", "list", "-R", self.tracker, "--state", "open", "--label",
                             self.labels.name("epic"), "--json", "number,body,author", "--limit", "200"]) or []

    def trusted_epics(self, epics: Sequence[Dict[str, Any]]) -> List[Tuple[int, List[int]]]:
        """(epic number, unchecked task-list numbers) for each epic a trusted author wrote, lowest number first.
        An untrusted epic is dropped whole: its body must not steer the queue."""
        from .labels import task_list_numbers
        return [(int(e["number"]), task_list_numbers(e.get("body"), self.tracker))
                for e in sorted(epics, key=lambda e: e["number"]) if self.trusted(login_of(e.get("author")))]

    def _sibling(self, n: int, known: Dict[int, Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
        """open, labels, assignees (and board column) of issue n; None when it cannot be read."""
        if n not in known:
            try:
                issue = self.gh.read(["issue", "view", str(int(n)), "-R", self.tracker, "--json",
                                      "state,labels,assignees"])  # never the body
            except (GhError, ValueError):
                issue = None
            known[n] = sibling_info(issue) if isinstance(issue, dict) else None
        return known[n]

    def _unstarted(self, info: Optional[Dict[str, Any]]) -> bool:
        """Open, not blocked and not claimed. Claimed: any state but to do (label or board column), or assigned
        to someone other than the operator. On a board, any named column but To Do (UNMAPPED_COLUMN included)
        counts as started; no Status value does not. Unknown (unreadable): not unstarted, so it holds nothing back."""
        if not info or not info["open"]:
            return False
        names, column = info["labels"], info.get("column")
        if self._has(names, "blocked") or column == "blocked":
            return False
        if any(not self._is_me(a) for a in info["assignees"]):
            return False
        if any(s != "todo" for s in self.labels.current(names)):
            return False
        return column in (None, "todo")

    def epic_order(self, found: List[Candidate], epics: Sequence[Dict[str, Any]],
                   known: Dict[int, Optional[Dict[str, Any]]]) -> List[Candidate]:
        """Set parent and position from trusted epics, then drop every issue that sits in an epic's task list
        after an earlier open, unstarted issue; held_back records them as {number, behind, epic}.

        known: sibling facts already read (number -> sibling_info), so only the rest are read, one at a time,
        and each epic stops reading at its first unstarted issue."""
        by_number = {c.number: c for c in found}
        held: Dict[int, Dict[str, int]] = {}
        for epic, numbers in self.trusted_epics(epics):
            for place, n in enumerate(numbers, start=1):
                c = by_number.get(n)
                if c is not None and c.parent is None:
                    c.parent, c.position = epic, place
            last = max((i for i, n in enumerate(numbers) if n in by_number), default=-1)
            holder: Optional[int] = None
            for n in numbers[:last + 1]:
                if holder is not None:
                    if n in by_number and n not in held:
                        held[n] = {"number": n, "behind": holder, "epic": epic}
                elif n in by_number or self._unstarted(self._sibling(n, known)):
                    holder = n
        self.held_back = sorted(held.values(), key=lambda h: h["number"])
        return [c for c in found if c.number not in held]

    # --- facts for pick-issue ---

    def facts(self, found: Sequence[Candidate]) -> List[Dict[str, Any]]:
        """Metadata to judge each issue by. Never a body; reactions count only the operator and trusted logins.
        A fact whose read fails is null for that issue only."""
        now = self.now()
        out = []
        for c in found:
            created = claims.parse_ts(c.created)
            out.append({"number": c.number, "title": c.title, "url": c.url, "author": c.author,
                        "labels": list(c.labels), "created": c.created or None,
                        "age_days": (now - created).days if created else None,
                        "parent": c.parent, "position": c.position,
                        "blocked_by": self._dependencies(c.number, "blocked_by"),
                        "blocking": self._dependencies(c.number, "blocking"),
                        "reactions": self._reactions(c.number), "board": c.board})
        return out

    def _api_list(self, path: str) -> Optional[List[Any]]:
        try:
            data = self.gh.read(["api", f"repos/{self.tracker}/issues/{path}"])
        except GhError:
            return None
        return data if isinstance(data, list) else ([] if data is None else None)

    def _dependencies(self, n: int, kind: str) -> Optional[List[Dict[str, Any]]]:
        """Issues GitHub records as blocking n (blocked_by) or blocked by n (blocking): repo, number, state."""
        data = self._api_list(f"{int(n)}/dependencies/{kind}?per_page=100")
        if data is None:
            return None
        out = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("number"), int):
                url = str(item.get("repository_url") or "")
                repo = "/".join(url.rstrip("/").split("/")[-2:]) if "/repos/" in url else None
                out.append({"repo": repo, "number": item["number"], "state": item.get("state")})
        return out

    def _reactions(self, n: int) -> Optional[Dict[str, int]]:
        """Reaction counts by kind, counting only reactions by the operator or a login the roster trusts."""
        data = self._api_list(f"{int(n)}/reactions?per_page={_REACTION_PAGE}")
        if data is None or len(data) >= _REACTION_PAGE:
            return None  # a full page may have more: a partial count could hide trusted reactions
        counts: Dict[str, int] = {}
        for r in data:
            if isinstance(r, dict) and isinstance(r.get("content"), str) \
                    and self.marker_trusted(login_of(r.get("user")) or None):
                counts[r["content"]] = counts.get(r["content"], 0) + 1
        return counts

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

    def upsert_marker(self, n: int, released: bool = False, assigned: Optional[str] = None) -> Optional[int]:
        """Create our marker comment, or edit the one we already have. Never a second one.

        A claim passes assigned="claim" when it added the operator as assignee. Releasing keeps whatever the
        existing marker recorded, so a later release still knows."""
        existing = self._mine(n)
        if released and existing is not None:
            assigned = (claims.parse_marker(existing.get("body")) or {}).get("assigned")
        body = claims.marker_body(self.operator, self.run_id, claims.iso(self.now()), released=released,
                                  assigned=assigned)
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


def sibling_info(issue: Dict[str, Any], column: Optional[str] = None) -> Dict[str, Any]:
    """What epic order needs to know of an issue: open, label names, assignee logins, board column (abstract)."""
    return {"open": str(issue.get("state", "OPEN")).upper() == "OPEN",
            "labels": names_of(issue.get("labels")),
            "assignees": [login_of(x) for x in issue.get("assignees") or []], "column": column}


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
