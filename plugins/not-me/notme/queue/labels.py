"""The label-driven queue: issues carrying the pick-up label, claimed by assignee, label and marker comment."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import claims, states
from ..gh import _num, login_of
from . import BaseQueue, Candidate, Claim

_LIST_FIELDS = "number,title,body,state,labels,assignees,author,createdAt,url"
_TASK_RE = re.compile(r"^\s*[-*]\s*\[ \]\s*(?:([A-Za-z0-9][\w.-]*/[\w.-]+))?#(\d+)\b", re.M)
_PAGE = 100
_MAX_TIMELINE_PAGES = 50
_EXCLUDED = ("paused", "blocked", "epic")


def _names(issue: Dict[str, Any]) -> List[str]:
    return [(x.get("name") if isinstance(x, dict) else str(x)) for x in issue.get("labels") or []]


def _assignees(issue: Dict[str, Any]) -> List[str]:
    return [(x.get("login") if isinstance(x, dict) else str(x)) for x in issue.get("assignees") or []]


def _author(issue: Dict[str, Any]) -> str:
    return login_of(issue.get("author"))


def task_list_numbers(body: Optional[str], tracker: str) -> List[int]:
    """Unchecked `- [ ] #N` items in an epic body, in order. Items naming another repo are skipped."""
    out: List[int] = []
    for repo, number in _TASK_RE.findall(body or ""):
        if repo and repo.lower() != tracker.lower():
            continue
        if int(number) not in out:
            out.append(int(number))
    return out


def _comment_author(comment: Dict[str, Any]) -> Optional[str]:
    return login_of(comment.get("author")) or None


def is_stale(issue: Dict[str, Any], claim_stale_hours: float, branches: Sequence[str] = (),
             in_progress_label: str = "in progress", now: Optional[datetime] = None, *,
             is_trusted: Callable[[Optional[str]], bool]) -> bool:
    """An in-progress issue whose newest live claim marker is older than the limit and has no `<n>-` branch.

    Only reports; nothing is ever released automatically. No marker means we cannot tell, so not stale.
    A marker counts only when its comment author is the login it names and is_trusted(login) is true.
    """
    if in_progress_label.lower() not in {n.lower() for n in _names(issue)}:
        return False
    markers = []
    for comment in issue.get("comments") or []:
        m = claims.parse_marker(comment.get("body"))
        if m and not claims.is_released(m) and claims.author_matches(_comment_author(comment), m) \
                and is_trusted(m["login"]):
            markers.append(m)
    stamps = [(claims.parse_ts(m["ts"]), m) for m in markers]
    stamps = [(t, m) for t, m in stamps if t is not None]
    if not stamps:
        return False
    newest = max(t for t, _ in stamps)
    number = int(issue["number"])
    branch_re = re.compile(rf"^(?:.*/)?{number}-")
    if any(branch_re.match(b) for b in branches):
        return False
    return claims.stale(newest, claim_stale_hours, now)


class LabelQueue(BaseQueue):

    # --- reading ---

    def _list(self, *label_abstracts: str, extra: Sequence[str] = ()) -> List[Dict[str, Any]]:
        argv = ["issue", "list", "-R", self.tracker, "--state", "open", "--json", _LIST_FIELDS, "--limit", "200"]
        for name in [self.labels.name(a) for a in label_abstracts] + list(extra):
            argv += ["--label", name]
        return self.gh.read(argv) or []

    def _eligible(self, issue: Dict[str, Any]) -> bool:
        names = _names(issue)
        opt_in = self.queue_cfg.get("opt_in_label")
        return (str(issue.get("state", "OPEN")).upper() == "OPEN"
                and self._has(names, "todo")
                and (not opt_in or opt_in.lower() in {n.lower() for n in names})
                and not any(self._has(names, x) for x in _EXCLUDED)
                and not [a for a in _assignees(issue) if not self._is_me(a)]  # mine, or nobody's
                and self.trusted(_author(issue)))

    def candidates(self) -> List[Candidate]:
        """Eligible issues: epic children first (epic order, then task-list order), then the rest oldest first."""
        opt_in = self.queue_cfg.get("opt_in_label")
        eligible = {i["number"]: i for i in self._list("todo", extra=[opt_in] if opt_in else []) if self._eligible(i)}
        placed: Dict[int, Candidate] = {}
        epics = sorted(self._list("epic"), key=lambda e: e["number"])
        for epic in epics:
            if not self.trusted(_author(epic)):
                continue  # an untrusted epic body must not steer the queue
            for position, number in enumerate(task_list_numbers(epic.get("body"), self.tracker)):
                if number in eligible and number not in placed:
                    placed[number] = self._candidate(eligible[number], (0, epic["number"], position), epic["number"])
        for number, issue in eligible.items():
            if number not in placed:
                placed[number] = self._candidate(issue, (1, 0, 0), None)
        return sorted(placed.values(), key=Candidate.sort_key)

    @staticmethod
    def _candidate(issue: Dict[str, Any], priority: tuple, parent: Optional[int]) -> Candidate:
        return Candidate(number=issue["number"], title=issue.get("title", ""), url=issue.get("url", ""),
                         author=_author(issue), labels=_names(issue), priority=priority, is_epic=False,
                         parent=parent, created=issue.get("createdAt", ""))

    def state(self, n: int) -> Optional[str]:
        current = self.labels.current(self.gh.labels(self.tracker, _num(n)))
        return current[0] if current else None

    def stale(self, n: int, branches: Sequence[str] = ()) -> bool:
        """Whether issue n holds a stale claim. `branches` are the branch names on origin (caller supplies them)."""
        return is_stale(self.gh.issue(self.tracker, _num(n), comments=True), self.stale_hours, branches,
                        self.labels.name("in_progress"), self.now(), is_trusted=self.marker_trusted)

    # --- claiming ---

    def claim(self, n: int) -> Claim:
        """Take issue n. Not atomic: GitHub has no compare-and-set, so this writes, then checks the timeline.

        Order: re-read, assign + swap labels, upsert the marker comment, then read the timeline; if someone
        else's assignment or live marker is older than ours, back out and raise ClaimLost.
        """
        n = _num(n)
        self.lock(n)
        try:
            return self._claim(n)
        except BaseException:
            self.unlock(n)
            raise

    def _claim(self, n: int) -> Claim:
        issue = self.gh.issue(self.tracker, n)
        names = _names(issue)
        if str(issue.get("state", "OPEN")).upper() != "OPEN":
            raise claims.ClaimLost(f"{self.tracker}#{n} is not open")
        if self._has(names, "in_progress"):
            raise claims.ClaimLost(f"{self.tracker}#{n} is already in progress")
        others = [a for a in _assignees(issue) if not self._is_me(a)]
        if others:
            raise claims.ClaimLost(f"{self.tracker}#{n} is assigned to {others[0]}")
        new_labels = states.transition(names, "todo", "in_progress", self.labels)  # IllegalTransition if not to do

        self.assign(n)
        self.gh.labels_set(self.tracker, n, new_labels)
        marker_id = self.upsert_marker(n)
        if not self.gh.dry_run:
            self._check_timeline(n)
        return Claim(number=n, previous="todo", now="in_progress", marker_comment_id=marker_id, run_id=self.run_id)

    def _check_timeline(self, n: int) -> None:
        events = self._timeline(n)
        if not self._has_own_assignment(events):  # cannot prove we are first, so do not keep the claim
            self._back_out(n)
            raise claims.ClaimLost(f"{self.tracker}#{n}: our assignment is not in the timeline; claim released")
        winner = self._older_assignee(events) or self._older_marker(n)
        if winner:
            self._back_out(n)
            raise claims.ClaimLost(f"{self.tracker}#{n} was claimed first by {winner}")

    def _timeline(self, n: int) -> List[Dict[str, Any]]:
        """Every timeline event, read page by page until a page comes back short."""
        events: List[Dict[str, Any]] = []
        for page in range(1, _MAX_TIMELINE_PAGES + 1):
            batch = self.gh.read(["api", f"repos/{self.tracker}/issues/{n}/timeline?per_page={_PAGE}&page={page}"]) or []
            events += batch
            if len(batch) < _PAGE:
                break
        return events

    def _has_own_assignment(self, events: List[Dict[str, Any]]) -> bool:
        last = None
        for e in events:
            if e.get("event") in ("assigned", "unassigned") \
                    and ((e.get("assignee") or {}).get("login") or "").lower() == self.operator.lower():
                last = e
        return bool(last and last["event"] == "assigned")

    def _older_assignee(self, events: List[Dict[str, Any]]) -> Optional[str]:
        """A login that is still assigned and was assigned before us (ties go to the lower login)."""
        last: Dict[str, Dict[str, Any]] = {}
        for e in events:
            if e.get("event") in ("assigned", "unassigned"):
                last[((e.get("assignee") or {}).get("login") or "").lower()] = e
        mine = last.get(self.operator.lower())
        if not mine or mine["event"] != "assigned":
            return None
        ours = (mine.get("created_at", ""), self.operator.lower())
        for login, e in last.items():
            if login != self.operator.lower() and e["event"] == "assigned" and (e.get("created_at", ""), login) < ours:
                return login
        return None

    def _older_marker(self, n: int) -> Optional[str]:
        mine = self._mine(n)
        if mine is None:
            return None
        ours_marker = claims.parse_marker(mine["body"])
        ours = (claims.parse_ts(ours_marker["ts"]), mine["id"])
        for comment, marker in self.marker_comments(n):
            if self._is_me(marker.get("login")) or claims.is_released(marker):
                continue
            theirs = claims.parse_ts(marker["ts"])
            if theirs is None or claims.stale(theirs, self.stale_hours, self.now()):
                continue
            if (theirs, comment["id"]) < ours:
                return marker["login"]
        return None

    def _back_out(self, n: int) -> None:
        """Undo our claim after losing. Labels go back to todo only when nobody else is assigned."""
        self.unassign(n)
        self.mark_released(n)
        remaining = [a for a in _assignees(self.gh.issue(self.tracker, n)) if not self._is_me(a)]
        if not remaining:
            self._relabel(n, "todo")

    def _relabel(self, n: int, to: str) -> None:
        names = self.gh.labels(self.tracker, n)
        if self._has(names, "in_progress"):
            self.gh.labels_set(self.tracker, n, states.transition(names, "in_progress", to, self.labels))

    def release(self, n: int, to: str = "todo") -> None:
        """Give the issue back: unassign us, move in_progress to `to`, mark our comment released, drop the lock."""
        n = _num(n)
        self.unassign(n)
        self._relabel(n, to)
        self.mark_released(n)
        self.unlock(n)

    def move(self, n: int, state: str, require_current: Optional[str] = None) -> None:
        n = _num(n)
        current = self.labels.current(self.gh.labels(self.tracker, n))
        if require_current is not None and require_current not in current:
            raise states.StateMismatch(f"{self.tracker}#{n} is {', '.join(current) or 'in no state'}, "
                                       f"not {require_current}")
        frm = require_current or (current[0] if current else None)
        if frm is None:
            raise states.IllegalTransition(f"{self.tracker}#{n} carries no state label to move from")
        states.apply(self.gh, self.tracker, n, frm, state, self.labels)
