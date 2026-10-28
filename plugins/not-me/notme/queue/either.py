"""queue.source: either. An issue is eligible when it carries the to-do label OR sits in the board's To Do column.

The label side stays the claim's source of truth: the compare-and-set and its read-back through the issue
timeline are LabelQueue's, unchanged. The board is read for picking and written as a mirror (sync.mirror, always
on for this source), so a failed board write is a warning and never undoes the label claim.

Where the two sides disagree on anything past To Do (labeled `to do` but the Status is In review, or in the To Do
column but labeled `in progress`) the issue is not picked: it is listed in `disagreements` with both values.
The board is checked against the write gate before it is read, so a board the operator has not confirmed on the
global allow list is refused (WriteBlocked) and never read.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import claims, states, writes
from ..gh import WriteBlocked, _num
from ..writes import WriteTarget
from . import sibling_info
from .board import BoardQueue
from .labels import LabelQueue, _names


class EitherQueue(LabelQueue):
    source = "either"

    def __init__(self, cfg: Any, gh: Any, roster: Any, operator: str, run_id: str, tracker: str, **kwargs: Any):
        super().__init__(cfg, gh, roster, operator, run_id, tracker, **kwargs)
        try:
            self.board_q = BoardQueue(cfg, gh, roster, operator, run_id, tracker, clock=self._clock)
        except ValueError:
            raise ValueError("queue.source either needs the board: set queue.board.owner and queue.board.number "
                             "in project.md") from None
        self.board_q.read_fields = []  # only the Status is needed here

    # --- the board side ---

    def _board(self) -> Dict[str, Any]:
        """The board with its items, read only after the write gate accepted the board."""
        target = WriteTarget.board(self.board_q.owner, self.board_q.number)
        decision = writes.check_write(target, self.gh.ctx)
        if not decision.ok:
            raise WriteBlocked(decision, target)
        return self.board_q._fetch()

    def _statuses(self, board: Dict[str, Any]) -> Dict[int, Optional[str]]:
        return {i["number"]: i["status"] for i in board["items"]
                if (i["repo"] or "").lower() == self.tracker.lower()}

    def _disagreement(self, n: int, names: List[str], status: Optional[str]) -> Optional[Dict[str, Any]]:
        """Both sides of issue n when the labels and the board Status disagree on anything past To Do, else None.
        An issue with no Status (not on the board) never disagrees."""
        todo_column = self.board_q.board_states.get("todo")
        later = [s for s in self.labels.current(names) if s != "todo"]
        if self._has(names, "todo") and status is not None and status != todo_column:
            label = self.labels.name("todo")
        elif status == todo_column and later and not self._has(names, "todo"):
            label = self.labels.name(later[0])
        else:
            return None
        return {"number": n, "label": label, "status": status,
                "reason": f"{self.tracker}#{n} is labeled `{label}` but its board Status is `{status}`; "
                          "the two sides disagree, so it is not picked until they match"}

    # --- reading ---

    def _pool(self) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, Optional[Dict[str, Any]]]]:
        self.disagreements = []
        board = self._board()  # the gate first: nothing of the board is read before it
        statuses = self._statuses(board)
        by_number = {i["number"]: i for i in board["items"] if i["number"] in statuses}
        todo_column = self.board_q.board_states.get("todo")
        opt_in = self.queue_cfg.get("opt_in_label")
        listed = self._list("todo", extra=[opt_in] if opt_in else [])
        eligible: Dict[int, Dict[str, Any]] = {}
        known: Dict[int, Optional[Dict[str, Any]]] = {}
        for issue in listed:
            n = int(issue["number"])
            known[n] = sibling_info(issue, self.board_q._sibling_column(statuses.get(n)))
            bad = self._disagreement(n, _names(issue), statuses.get(n))
            if bad:
                self.disagreements.append(bad)
            elif self._eligible(issue):
                eligible[issue["number"]] = issue
        for n, item in by_number.items():
            if n in known:
                continue
            issue = {"number": n, "title": item["title"], "url": item["url"], "state": item["state"],
                     "author": item["author"], "labels": item["labels"], "assignees": item["assignees"],
                     "createdAt": item["created"]}
            known[n] = sibling_info(issue, self.board_q._sibling_column(item["status"]))
            if item["status"] != todo_column:
                continue  # no to-do label (it is not in the list above) and not in the To Do column
            bad = self._disagreement(n, item["labels"], item["status"])
            if bad:
                self.disagreements.append(bad)
            elif self._eligible(issue, need_todo_label=False):
                eligible[n] = issue
        return eligible, known

    def state(self, n: int) -> Optional[str]:
        """The state label, else the abstract state of the board column."""
        labelled = super().state(n)
        if labelled is not None:
            return labelled
        return self.board_q.abstract(self._statuses(self._board()).get(_num(n)))

    # --- claiming: the label side's compare-and-set, with the board checked first ---

    def _claim_labels(self, n: int, names: List[str]) -> List[str]:
        status = self._statuses(self._board()).get(n)
        bad = self._disagreement(n, names, status)
        if bad:
            raise claims.ClaimLost(bad["reason"])
        if self._has(names, "todo"):
            return super()._claim_labels(n, names)
        if status != self.board_q.board_states.get("todo"):
            raise claims.ClaimLost(f"{self.tracker}#{n} is neither labeled `{self.labels.name('todo')}` nor in the "
                                   f"`{self.board_q.board_states.get('todo')}` column")
        return states.swap_state(names, "in_progress", self.labels)  # a To Do column item with no to-do label
