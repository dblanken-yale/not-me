"""The board-driven queue: a GitHub Projects v2 board whose Status column says where an issue is.

Project, field and option ids are looked up on every call and never stored: they differ between boards and
can change when a column is renamed. Claiming is read-then-write and is NOT atomic: GitHub has no
compare-and-set for a board field, so claim() re-reads the Status after writing and reports ClaimLost if it
is not what it set.

Limits: the first 100 board items are read (no paging yet), and only issues from the tracker repo count.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .. import claims
from ..gh import _node, _num, _owner, login_of
from . import BaseQueue, Candidate, Claim

# abstract state -> key in queue.board_states. Other states (needs_work, question, ...) have no column.
ABSTRACT_TO_BOARD = {"todo": "todo", "in_progress": "in_progress", "needs_review": "in_review", "blocked": "blocked"}
_NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]+$")
_QUERY = (
    "query($owner:String!){repositoryOwner(login:$owner){... on ProjectV2Owner{projectV2(number:{n}){"
    "id field(name:\"{status}\"){... on ProjectV2SingleSelectField{id options{id name}}}"
    "items(first:100){nodes{id fieldValueByName(name:\"{status}\"){... on ProjectV2ItemFieldSingleSelectValue{name}}"
    "{priority}"
    "content{... on Issue{number title url state createdAt author{login __typename} labels(first:50){nodes{name}} "
    "assignees(first:20){nodes{login}} repository{nameWithOwner}}}}}}}}}"
)


class UnmappedState(ValueError):
    """The abstract state has no board column (see ABSTRACT_TO_BOARD)."""


def _field_name(value: Any) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ValueError(f"unsupported board field name {value!r}")
    return value


def priority_value(option_name: Optional[str]) -> Optional[int]:
    """The leading digits of an option name such as '4 - Critical'; None when there are none."""
    m = re.match(r"\s*(\d+)", option_name or "")
    return int(m.group(1)) if m else None


class BoardQueue(BaseQueue):

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        board = self.queue_cfg.get("board") or {}
        if not board.get("owner") or not isinstance(board.get("number"), int):
            raise ValueError("queue.board.owner and queue.board.number must be set for a board queue")
        self.owner, self.number = _owner(board["owner"]), int(board["number"])
        self.status_field = _field_name(board.get("status_field") or "Status")
        self.priority_fields = [_field_name(f) for f in board.get("priority_fields") or []]
        self.board_states: Dict[str, str] = dict(self.queue_cfg.get("board_states") or {})

    # --- reading the board ---

    def _fetch(self) -> Dict[str, Any]:
        priority = "".join(f'p{i}:fieldValueByName(name:"{name}"){{... on ProjectV2ItemFieldSingleSelectValue{{name}}}}'
                           for i, name in enumerate(self.priority_fields))
        query = (_QUERY.replace("{n}", str(self.number)).replace("{status}", self.status_field)
                 .replace("{priority}", priority))
        data = self.gh.read(["api", "graphql", "-f", f"query={query}", "-f", f"owner={self.owner}"])
        project = (((data or {}).get("data") or {}).get("repositoryOwner") or {}).get("projectV2")
        if not project:
            raise LookupError(f"board {self.owner}/{self.number} not found")
        field = project.get("field") or {}
        items = []
        for node in (project.get("items") or {}).get("nodes") or []:
            content = node.get("content") or {}
            if content.get("number") is None:
                continue
            items.append({
                "id": node.get("id"), "number": content["number"], "title": content.get("title", ""),
                "url": content.get("url", ""), "state": content.get("state", "OPEN"),
                "created": content.get("createdAt", ""), "author": login_of(content.get("author")),
                "labels": [x["name"] for x in (content.get("labels") or {}).get("nodes") or []],
                "assignees": [x["login"] for x in (content.get("assignees") or {}).get("nodes") or []],
                "repo": (content.get("repository") or {}).get("nameWithOwner"),
                "status": (node.get("fieldValueByName") or {}).get("name"),
                "priorities": [priority_value((node.get(f"p{i}") or {}).get("name"))
                               for i in range(len(self.priority_fields))],
            })
        return {"project_id": project.get("id"), "field_id": field.get("id"),
                "options": {o["name"]: o["id"] for o in field.get("options") or []}, "items": items}

    def _item(self, board: Dict[str, Any], n: int) -> Dict[str, Any]:
        for item in board["items"]:
            if item["number"] == n and (item["repo"] or "").lower() == self.tracker.lower():
                return item
        raise LookupError(f"{self.tracker}#{n} is not on board {self.owner}/{self.number}")

    # --- state names ---

    def column(self, abstract: str) -> str:
        key = ABSTRACT_TO_BOARD.get(abstract)
        if key is None or key not in self.board_states:
            raise UnmappedState(f"state {abstract!r} has no board column; use labels for it")
        return self.board_states[key]

    def abstract(self, column: Optional[str]) -> Optional[str]:
        for abstract, key in ABSTRACT_TO_BOARD.items():
            if column is not None and self.board_states.get(key) == column:
                return abstract
        return None

    def _set_status(self, board: Dict[str, Any], item: Dict[str, Any], abstract: str) -> None:
        column = self.column(abstract)
        option = board["options"].get(column)
        if option is None:
            raise LookupError(f"board has no {column!r} column; found: {', '.join(sorted(board['options']))}")
        self.gh.board_set_status(self.owner, self.number, _node(item["id"]), _node(board["field_id"]), option)

    # --- queue ---

    def candidates(self) -> List[Candidate]:
        opt_in = self.queue_cfg.get("opt_in_label")
        todo = self.board_states.get("todo")
        skip = set()
        for abstract in ("paused", "epic", "blocked"):
            try:
                skip.add(self.labels.name(abstract).lower())
            except KeyError:
                pass
        found = []
        for item in self._fetch()["items"]:
            names = {n.lower() for n in item["labels"]}
            if ((item["repo"] or "").lower() != self.tracker.lower() or item["status"] != todo
                    or str(item["state"]).upper() != "OPEN" or item["assignees"]
                    or (opt_in and opt_in.lower() not in names) or names & skip
                    or not self.trusted(item["author"])):
                continue
            found.append(self._candidate(item))
        return sorted(found, key=Candidate.sort_key)

    @staticmethod
    def _candidate(item: Dict[str, Any]) -> Candidate:
        priority: Tuple = tuple((1, 0) if v is None else (0, -v) for v in item["priorities"])  # higher first, null last
        return Candidate(number=item["number"], title=item["title"], url=item["url"], author=item["author"],
                         labels=item["labels"], priority=priority, is_epic=False, parent=None,
                         created=item["created"])

    def state(self, n: int) -> Optional[str]:
        return self.abstract(self._item(self._fetch(), _num(n))["status"])

    def claim(self, n: int) -> Claim:
        n = _num(n)
        self.lock(n)
        try:
            return self._claim(n)
        except BaseException:
            self.unlock(n)
            raise

    def _claim(self, n: int) -> Claim:
        board = self._fetch()
        item = self._item(board, n)
        if item["status"] != self.column("todo"):
            raise claims.ClaimLost(f"{self.tracker}#{n} is in {item['status']!r}, not {self.column('todo')!r}")
        others = [a for a in item["assignees"] if not self._is_me(a)]
        if others or str(item["state"]).upper() != "OPEN":
            raise claims.ClaimLost(f"{self.tracker}#{n} is closed or assigned to {others[0] if others else 'someone'}")
        self._set_status(board, item, "in_progress")
        self.assign(n)
        marker_id = self.upsert_marker(n)
        if not self.gh.dry_run:
            now = self._item(self._fetch(), n)
            if now["status"] != self.column("in_progress"):
                self.unassign(n)
                self.mark_released(n)
                raise claims.ClaimLost(f"{self.tracker}#{n} moved to {now['status']!r} while claiming")
        return Claim(number=n, previous="todo", now="in_progress", marker_comment_id=marker_id, run_id=self.run_id)

    def release(self, n: int, to: str = "todo") -> None:
        n = _num(n)
        board = self._fetch()
        self._set_status(board, self._item(board, n), to)
        self.unassign(n)
        self.mark_released(n)
        self.unlock(n)

    def move(self, n: int, state: str, require_current: Optional[str] = None) -> None:
        n = _num(n)
        board = self._fetch()
        item = self._item(board, n)
        if require_current is not None and self.abstract(item["status"]) != require_current:
            raise claims.ClaimLost(f"{self.tracker}#{n} is in {item['status']!r}, not {self.column(require_current)!r}")
        self._set_status(board, item, state)
