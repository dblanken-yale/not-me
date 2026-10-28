"""Work queues: where not-me finds the next issue and how it claims it.

Two sources share one interface: issues carrying a pick-up label (labels.LabelQueue) and a GitHub
Projects v2 board (board.BoardQueue). A third, either.EitherQueue, picks from both and claims on the label side.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

try:  # Protocol is 3.8+
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from .. import claims, paths
from ..gh import GhClient, GhError, _login, login_of, names_of, tmp_text_file
from ..layers import is_field_name, is_positive_int
from ..states import LabelCollision, Labels
from ..stacking import stack_fact
from ..writes import WriteTarget, valid_slug


# sibling_info column for a board item in a named column that board_states does not map (Done, Backlog, ...):
# started, unlike None (no column at all: a label queue, or a board item with no Status value).
UNMAPPED_COLUMN = "<unmapped column>"

# One project field value of any kind, as one GraphQL selection; field_value() reads it back.
FIELD_VALUE = ("... on ProjectV2ItemFieldSingleSelectValue{name} ... on ProjectV2ItemFieldTextValue{text} "
               "... on ProjectV2ItemFieldNumberValue{number} ... on ProjectV2ItemFieldDateValue{date} "
               "... on ProjectV2ItemFieldIterationValue{title}")
# An issue's project items with the named field values and each project's owner: any GitHub user can add a public
# issue to a project they own, so only items in projects the tracker's owner owns are read (see _fields_of).
_ISSUE_FIELDS_QUERY = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name){issue(number:{n}){"
                       "projectItems(first:20){nodes{project{owner{... on User{login} ... on Organization{login}}} "
                       "{fields}}}}}}")


def field_name(value: Any) -> str:
    """A board field name that is safe to write into a GraphQL query; ValueError otherwise."""
    if not is_field_name(value):
        raise ValueError(f"unsupported board field name {value!r}")
    return value


def field_selection(names: Sequence[str]) -> str:
    """`p0:fieldValueByName(name:"A"){...} p1:...` for already checked field names."""
    return "".join(f'p{i}:fieldValueByName(name:"{field_name(n)}"){{{FIELD_VALUE}}}' for i, n in enumerate(names))


def field_value(node: Any, max_chars: int) -> Any:
    """The value of one field selection: option name, text, iteration title, number or date; None when unset.
    Text is cut to max_chars (picking.value_max_chars): field text can be written by people off the roster."""
    if not isinstance(node, dict):
        return None
    for key in ("name", "text", "title", "number", "date"):
        value = node.get(key)
        if isinstance(value, str):
            return value[:max_chars]
        if value is not None:
            return value
    return None


def _project_owner(item: Dict[str, Any]) -> str:
    """The owner login of a project item's project; "" when it cannot be read."""
    login = (((item.get("project") or {}).get("owner")) or {}).get("login")
    return login if isinstance(login, str) else ""


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
    board: Optional[Dict[str, Any]] = None  # board queues only: {status: column, fields: {field: value}}
    target: Optional[Tuple[int, int]] = None  # (rank in picking.target_epics, 1-based place in that epic's list)
    target_epic: Optional[int] = None  # the target epic whose task list holds this issue

    def sort_key(self) -> Tuple:
        """Target epic issues first (epic order, then task-list order), then the queue's own order."""
        return ((0,) + self.target if self.target else (1, 0, 0), self.priority, self.created, self.number)


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
        self.cfg = cfg
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
        # queue.sync: mirror each state change to the other side. source: either reads both sides, so it writes both
        self.sync = self.queue_cfg.get("sync") is True or self.queue_cfg.get("source") == "either"
        # the state change the last move or release made: {from, to, label?, column?}; label and column are the
        # label and board column written alongside (see sync), so one comment can name both sides
        self.change: Dict[str, Any] = {}
        self.sync_warnings: List[str] = []  # one per mirror write that failed (see sync.mirror)
        self.unassigned_skipped = 0  # eligible issues the last candidates() left out only because nobody is assigned
        # /not-me:suggest: only issues nobody is assigned to (the operator's own are already in their queue), whatever
        # queue.include_unassigned says. Set by the caller; grants nothing, since claiming still checks assignees.
        self.only_unassigned = False
        # [{number, author}] of otherwise eligible, unassigned issues the last candidates() left out for an untrusted
        # author: numbers and logins only, never a title or body
        self.untrusted_skipped: List[Dict[str, Any]] = []
        self.held_back: List[Dict[str, int]] = []  # eligible issues the last candidates() left out for epic order
        # issues the last candidates() left out because the label and the board Status disagree (source: either)
        self.disagreements: List[Dict[str, Any]] = []
        picking = self.meta.get("picking") or {}
        if not isinstance(picking, dict):
            raise ValueError("picking must be a map")
        fields, targets = picking.get("fields") or [], picking.get("target_epics") or []
        if not isinstance(fields, list) or not isinstance(targets, list):
            raise ValueError("picking.fields and picking.target_epics must be lists")
        self.pick_fields: List[str] = [field_name(f) for f in fields]
        if not all(is_positive_int(n) for n in targets):
            raise ValueError(f"picking.target_epics must be issue numbers, got {targets!r}")
        self.target_epics: List[int] = list(targets)
        # the deprecated queue.board.priority_fields, which a board queue still ranks by (see Config.ranked_fields)
        self.rank_fields: List[str] = [field_name(f) for f in getattr(cfg, "ranked_fields", None) or []]
        self.value_max_chars: int = 0  # picking.value_max_chars; checked only when a field is read
        if self.pick_fields or self.rank_fields:
            cap = picking.get("value_max_chars")
            if not is_positive_int(cap):
                raise ValueError(f"picking.value_max_chars must be a whole number of at least 1, got {cap!r}")
            self.value_max_chars = cap
        self._stack_facts: Dict[int, Optional[Dict[str, Any]]] = {}  # stack_on() by issue number
        self._blockers: Dict[int, Optional[List[Dict[str, Any]]]] = {}  # blocked_by() by issue number
        self.target_epics_used: List[int] = []  # target epics the last candidates() honoured
        self.target_epics_ignored: List[Dict[str, Any]] = []  # [{number, reason}]: not found, closed, untrusted

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

    def _trusted_author(self, number: Any, author: Optional[str], assignees: Sequence[str]) -> bool:
        """trusted(author). An untrusted, unassigned issue is noted in untrusted_skipped by number and login only."""
        if self.trusted(author):
            return True
        if not assignees:
            self.untrusted_skipped.append({"number": int(number), "author": author or None})
        return False

    def _is_me(self, login: Optional[str]) -> bool:
        return (login or "").lower() == self.operator.lower()

    def _assignees_allow(self, assignees: Sequence[str]) -> bool:
        """Check this last, after every other eligibility rule. Assigned to the operator alone: yes. Assigned to
        anyone else: never. Nobody: only with queue.include_unassigned; otherwise counted in unassigned_skipped.
        With only_unassigned (the suggest list): nobody assigned, and nothing else."""
        if any(not self._is_me(a) for a in assignees):
            return False
        if self.only_unassigned:
            return not assignees
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
            issue = self._read_issue(n, "state,labels,assignees")  # never the body
            known[n] = sibling_info(issue) if issue is not None else None
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

    def order(self, found: List[Candidate], epics: Sequence[Dict[str, Any]],
              known: Dict[int, Optional[Dict[str, Any]]]) -> List[Candidate]:
        """Epic order (drops held-back issues), then target epics, then sorted by Candidate.sort_key."""
        return sorted(self.target_order(self.epic_order(found, epics, known), epics), key=Candidate.sort_key)

    # --- target epics (picking.target_epics) ---

    def target_order(self, found: List[Candidate], epics: Sequence[Dict[str, Any]]) -> List[Candidate]:
        """Mark each candidate in a target epic's unchecked task list with its epic and place, so sort_key puts
        them first in picking.target_epics order, then task-list order. Only candidates are marked: a target
        epic never adds an issue. A target epic counts only when it is open and a trusted roster member wrote
        it; the others are recorded in target_epics_ignored by number and reason only (never their text).

        epics: open epics already read (with number, body, author); a target not among them is read alone."""
        from .labels import task_list_numbers
        self.target_epics_used, self.target_epics_ignored = [], []
        by_number = {c.number: c for c in found}
        read = {int(e["number"]): e for e in epics if isinstance(e, dict) and "number" in e}
        for rank, n in enumerate(self.target_epics):
            epic = read.get(n) or self._read_issue(n, "number,body,author,state")
            reason = None
            if epic is None:
                reason = "not found"
            elif str(epic.get("state", "OPEN")).upper() != "OPEN":
                reason = "closed"
            elif not self.trusted(login_of(epic.get("author"))):
                reason = "untrusted author"
            if reason:
                self.target_epics_ignored.append({"number": n, "reason": reason})
                continue
            self.target_epics_used.append(n)
            for place, m in enumerate(task_list_numbers(epic.get("body"), self.tracker), start=1):
                c = by_number.get(m)
                if c is not None and c.target is None:
                    c.target, c.target_epic = (rank, place), n
        return found

    def _read_issue(self, n: int, fields: str) -> Optional[Dict[str, Any]]:
        try:
            issue = self.gh.read(["issue", "view", str(int(n)), "-R", self.tracker, "--json", fields])
        except (GhError, ValueError):
            return None
        return issue if isinstance(issue, dict) else None

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
                        "target_epic": c.target_epic, "target_position": c.target[1] if c.target else None,
                        "blocked_by": self.blocked_by(c.number),
                        "blocking": self._dependencies(c.number, "blocking"),
                        "stack_on": self.stack_on(c.number, c.author),
                        "reactions": self._reactions(c.number), "fields": self._fields_of(c), "board": c.board})
        return out

    def _fields_of(self, c: Candidate) -> Optional[Dict[str, Any]]:
        """{field: value} for each picking.fields name: from the board item on a board queue, else from the
        issue's project items in projects owned by the tracker's owner (the first such item with a value wins;
        items in anyone else's project are ignored). {} when no field is named; None when the read fails."""
        if not self.pick_fields:
            return {}
        if c.board is not None:
            return dict(c.board.get("fields") or {})
        owner, name = self.tracker.split("/", 1)
        query = (_ISSUE_FIELDS_QUERY.replace("{n}", str(int(c.number)))
                 .replace("{fields}", field_selection(self.pick_fields)))
        try:
            data = self.gh.read(["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"])
        except GhError:
            return None
        issue = (((data or {}).get("data") or {}).get("repository") or {}).get("issue")
        if not isinstance(issue, dict):
            return None
        nodes = [x for x in (issue.get("projectItems") or {}).get("nodes") or []
                 if isinstance(x, dict) and _project_owner(x).casefold() == owner.casefold()]
        out: Dict[str, Any] = {}
        for i, f in enumerate(self.pick_fields):
            values = (field_value(x.get(f"p{i}"), self.value_max_chars) for x in nodes)
            out[f] = next((v for v in values if v is not None), None)
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

    # --- stacking (`Stack on:`) ---

    def stacking(self) -> str:
        """queue.stacking: declared | off. Not set (a config without the default layer) counts as off."""
        value = self.queue_cfg.get("stacking")
        if value not in (None, "declared", "off"):
            raise ValueError(f"queue.stacking must be declared or off, got {value!r}")
        return value or "off"

    def blocked_by(self, n: int) -> Optional[List[Dict[str, Any]]]:
        """Issues GitHub records as blocking n: repo, number, state; None when the read fails. Read once per issue."""
        if n not in self._blockers:
            self._blockers[n] = self._dependencies(n, "blocked_by")
        return self._blockers[n]

    def stack_on(self, n: int, author: Optional[str], body: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The `Stack on:` fact of issue n (see stacking.stack_fact), or None: no honoured line, or the body
        cannot be read. The line counts only when the roster trusts the author. body: already read, else read here."""
        if n not in self._stack_facts:
            if body is None:
                issue = self._read_issue(n, "body")
                body = issue.get("body") if issue else None
            self._stack_facts[n] = stack_fact(
                self.gh, number=n, body=body, author_trusted=self.trusted(author), tracker=self.tracker,
                stacking=self.stacking(), trusted=self.trusted,
                blocked_by=lambda: self.blocked_by(n))
        return self._stack_facts[n]

    def stack_filter(self, found: List[Candidate], limit: Optional[int]) -> Tuple[List[Candidate], List[Dict[str, Any]]]:
        """(found without the issues a `Stack on:` line holds back, [{number, reason}] of those held back).
        Issues are read in order until `limit` ready ones are known; the rest are passed through unread."""
        kept: List[Candidate] = []
        held: List[Dict[str, Any]] = []
        for i, c in enumerate(found):
            if limit is not None and len(kept) >= limit:
                kept.extend(found[i:])
                break
            fact = self.stack_on(c.number, c.author)
            if fact is not None and not fact["ready"]:
                held.append({"number": c.number, "ref": fact["ref"], "reason": fact["reason"]})
            else:
                kept.append(c)
        return kept, held

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


def board_ref(queue_cfg: Mapping[str, Any]) -> Optional[Tuple[str, int]]:
    """(owner, number) of queue.board, or None when either is not set (a bool is not a number)."""
    board = queue_cfg.get("board") or {}
    owner, number = board.get("owner"), board.get("number")
    if owner and is_positive_int(number):
        return str(owner), number
    return None


def describe(meta: Mapping[str, Any]) -> Dict[str, Any]:
    """Where the issue queue looks, from the project config alone (never calls GitHub).

    {source, ok, summary, opt_in_label, label (labels source) | board and column (board source) | all three
    (either source)}. summary is one
    plain sentence such as "picks issues labeled `to do`, assigned to you" (with queue.sync, followed by what it
    keeps in step); ok is false when the config cannot
    work as written (no board, no to-do column, an unknown source), and summary then says why.
    """
    q = meta.get("queue") or {}
    source = q.get("source")
    opt_in = q.get("opt_in_label") or None
    out: Dict[str, Any] = {"source": source}
    suffix = (f" that also carry `{opt_in}`" if opt_in else "") + \
        (", assigned to you or to nobody" if q.get("include_unassigned") is True else ", assigned to you")
    if source == "labels":
        try:
            label: Optional[str] = Labels(meta.get("labels") or {}).name("todo")
        except (KeyError, LabelCollision):
            label = None
        out["label"] = label
        if label:
            out.update(ok=True, summary=f"picks issues labeled `{label}`{suffix}")
        else:
            out.update(ok=False, summary="labels source, but `labels.todo` names no label")
    elif source == "board":
        ref = board_ref(q)
        column = (q.get("board_states") or {}).get("todo")
        out["board"] = f"{ref[0]}/{ref[1]}" if ref else None
        out["column"] = column
        if not ref:
            out.update(ok=False, summary="board source, but queue.board.owner and queue.board.number are not set")
        elif not column:
            out.update(ok=False, summary=f"board source, but queue.board_states.todo names no column of board "
                                         f"{out['board']}")
        else:
            out.update(ok=True, summary=f"picks issues in the `{column}` column of board {out['board']}{suffix}")
    elif source == "either":
        try:
            label = Labels(meta.get("labels") or {}).name("todo")
        except (KeyError, LabelCollision):
            label = None
        ref = board_ref(q)
        column = (q.get("board_states") or {}).get("todo")
        out.update(label=label, board=f"{ref[0]}/{ref[1]}" if ref else None, column=column)
        if not label:
            out.update(ok=False, summary="either source, but `labels.todo` names no label")
        elif not ref:
            out.update(ok=False, summary="either source, but queue.board.owner and queue.board.number are not set")
        elif not column:
            out.update(ok=False, summary=f"either source, but queue.board_states.todo names no column of board "
                                         f"{out['board']}")
        else:
            out.update(ok=True, summary=f"picks issues labeled `{label}` or in the `{column}` column of board "
                                        f"{out['board']}{suffix}; keeps both in step")
    else:
        out.update(ok=False, summary=f"queue.source `{source}` is not supported; use labels, board or either")
    if out["ok"] and source != "either" and q.get("sync") is True:
        out["summary"] += "; keeps the " + ("labels" if source == "board" else "board Status") + " in step"
    out["opt_in_label"] = opt_in
    return out


def make_queue(cfg: Any, gh: GhClient, roster: Any, operator: str, run_id: str, *, kind: str = "issue",
               repo: Optional[str] = None, tracker: Optional[str] = None, project_dir: Any = None,
               clock: Optional[claims.Clock] = None) -> Any:
    """The queue for `queue.source` (labels, board or either); kind="pr" gives the BaseQueue that claims pull requests
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
    if source == "either":
        from .either import EitherQueue
        return EitherQueue(cfg, gh, roster, operator, run_id, slug, **kwargs)
    raise ValueError(f"queue.source must be labels, board or either, got {source!r}")
