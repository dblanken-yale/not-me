"""Drift between an issue's state label and its board Status, and the fix: `notme doctor`, `notme status` and
`notme queue-resync`.

Drift is read for open issues of the tracker that are on queue.board. queue.source is the truth: with a labels
source the Status should show the state label, with a board source the labels should show the Status.

- labels source: an issue is out of step when its state label has a column (todo, in_progress, needs_review,
  blocked) and the Status is not that column. A state with no column (needs_work, question, ...) leaves the Status
  alone by design, and an issue with no state label has nothing to copy, so neither counts.
- board source: an issue is out of step when its Status is a mapped column and its labels do not carry that state.
  An issue whose labels carry a state with no column is not: that move is recorded by the label alone.

The report only reads. The fix goes through the same gated writes the queue.sync mirror uses (sync.set_labels and
sync._set_board_status), so it follows the effective mode (dry-run reaches only the ledger), refuses a board the
operator has not allowed before any board read, never adds an item to a board and never touches a closed issue.
Limits: board items are read up to board.BOARD_PAGE * board.BOARD_MAX_PAGES, and open issues up to ISSUE_LIST_LIMIT.
When either read is cut short the result says so (`truncated`, `issues_truncated`), drift beyond it is not listed, and
an issue that may be past the board read is never called "not on the board".
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..gh import GhError
from .board import ABSTRACT_TO_BOARD, UnmappedState
from .sync import MirrorFailed, _set_board_status, open_board, set_labels

ISSUE_LIST_LIMIT = 200  # open issues read for the not-on-board report; more is reported as issues_truncated
NO_LABEL = "(no state label)"
NO_STATUS = "(no Status)"


def _label_name(q: Any, state: Optional[str]) -> str:
    if state is None:
        return NO_LABEL
    try:
        return q.labels.name(state)
    except KeyError:
        return state


def _entry(q: Any, board_q: Any, board: Dict[str, Any], item: Dict[str, Any], main: Optional[str],
           status_state: Optional[str]) -> Dict[str, Any]:
    """One drifted issue: what both sides say, which side the fix would write and, when the fix cannot be made,
    why (skip)."""
    out: Dict[str, Any] = {
        "number": item["number"], "title": item["title"], "url": item["url"],
        "label_state": main, "label": _label_name(q, main),
        "status_state": status_state, "status": item["status"] or NO_STATUS,
        "side": "board" if q.source != "board" else "labels", "state": None, "to": None, "skip": None}
    if q.source == "board":
        out["state"] = status_state
        try:
            out["to"] = q.labels.name(status_state)
        except KeyError:
            out["skip"] = f"labels.{status_state} names no label; map labels.{status_state} in project.md"
        return out
    out["state"] = main
    try:
        column = board_q.column(main)
    except UnmappedState:
        key = ABSTRACT_TO_BOARD[main]
        out["skip"] = (f"queue.board_states.{key} is not set; map queue.board_states.{key} to a real column of "
                       f"board {board_q.owner}/{board_q.number}")
        return out
    out["to"] = column
    if column not in board["options"]:
        out["skip"] = (f"board {board_q.owner}/{board_q.number} has no {column!r} column (found: "
                       f"{', '.join(sorted(board['options']))}); map queue.board_states."
                       f"{ABSTRACT_TO_BOARD[main]} to a real column")
    return out


def read(q: Any, include_missing: bool = False) -> Dict[str, Any]:
    """The drift of q's tracker against queue.board. {"board": "owner/number", "drift": [entry], "not_on_board":
    [{number, title, url, label, beyond_read}], "truncated", "read", "issues_truncated"}. not_on_board (open issues
    whose state label has a column but that are not among the board items read; labels source only) is filled only
    when include_missing is true, as it costs one more read. truncated: the board has more items than were read
    (`read` of them were), so drift past them is not listed and each not_on_board entry has beyond_read true: it
    may be on the board. issues_truncated: more open issues than ISSUE_LIST_LIMIT, so not_on_board is partial.

    MirrorFailed when the board is not set, cannot be used or is not on the allow list (checked before any board
    read). LookupError when the board cannot be found; GhError when GitHub cannot be read. Only reads."""
    board_q, label, _ = open_board(q, q.tracker, what="drift of", or_off="")
    board = board_q._fetch()
    on_board = set()
    drift: List[Dict[str, Any]] = []
    for item in board["items"]:
        if (item["repo"] or "").lower() != q.tracker.lower():
            continue
        on_board.add(item["number"])
        if str(item["state"]).upper() != "OPEN":
            continue
        current = q.labels.current(item["labels"])
        main = current[0] if current else None
        status_state = board_q.abstract(item["status"])
        if q.source == "board":
            if status_state is None or main == status_state or (main is not None and main not in ABSTRACT_TO_BOARD):
                continue
        elif main not in ABSTRACT_TO_BOARD or main == status_state:
            continue
        drift.append(_entry(q, board_q, board, item, main, status_state))
    missing: List[Dict[str, Any]] = []
    issues_truncated = False
    if include_missing and q.source != "board":
        rows = q.gh.read(["issue", "list", "-R", q.tracker, "--state", "open", "--json", "number,title,url,labels",
                          "--limit", str(ISSUE_LIST_LIMIT + 1)]) or []
        issues_truncated, rows = len(rows) > ISSUE_LIST_LIMIT, rows[:ISSUE_LIST_LIMIT]
        for row in rows:
            names = [x.get("name") if isinstance(x, dict) else str(x) for x in row.get("labels") or []]
            current = q.labels.current(names)
            if row["number"] not in on_board and current and current[0] in ABSTRACT_TO_BOARD:
                missing.append({"number": row["number"], "title": row.get("title", ""), "url": row.get("url", ""),
                                "label": _label_name(q, current[0]),
                                "beyond_read": board["truncated"]})
    return {"board": label, "drift": sorted(drift, key=lambda e: e["number"]),
            "not_on_board": sorted(missing, key=lambda e: e["number"]), "truncated": board["truncated"],
            "read": board["read"], "issues_truncated": issues_truncated}


def describe(entry: Dict[str, Any]) -> str:
    """One line for a person: the issue URL and both values."""
    return f"{entry['url']}: label `{entry['label']}`, Status `{entry['status']}`"


def resync(q: Any) -> Dict[str, Any]:
    """Set the side q.source does not read to match the side it does, for each drifted open issue on the board.
    Returns {"board", "dry_run", "changes": [{number, url, side, from, to, result, reason}], "skipped": [...]}
    where result is "changed" (live, and read back), "planned" (dry-run: the write reached only the ledger) or
    "failed" (reason says why and how to fix it). skipped lists drifted issues that cannot be fixed (no column,
    no label mapped) and open issues with a column state label that are not on the board (labels source), each
    with a reason. Never adds a board item. Also truncated, read and issues_truncated as read() gives them: a
    cut-short read is never reported as an issue being off the board. Raises what read() raises."""
    found = read(q, include_missing=True)
    dry = bool(q.gh.dry_run)
    changes: List[Dict[str, Any]] = []
    beyond = f"not among the first {found['read']} board items read; it may be on board {found['board']}"
    skipped: List[Dict[str, Any]] = [
        {"number": m["number"], "url": m["url"],
         "reason": beyond if m["beyond_read"] else f"not on board {found['board']}; add the issue to the board "
                                                   "(not-me never adds items)"}
        for m in found["not_on_board"]]
    for e in found["drift"]:
        if e["skip"]:
            skipped.append({"number": e["number"], "url": e["url"], "reason": e["skip"]})
            continue
        change = {"number": e["number"], "url": e["url"], "side": "board Status" if e["side"] == "board" else "labels",
                  "from": e["status"] if e["side"] == "board" else e["label"], "to": e["to"], "result": None,
                  "reason": None}
        try:
            if e["side"] == "board":
                _set_board_status(q, e["number"], e["state"])
            else:
                set_labels(q, e["number"], e["state"])
            change["result"] = "planned" if dry else "changed"
        except MirrorFailed as exc:
            change["result"], change["reason"] = "failed", str(exc)
        except (GhError, LookupError) as exc:
            change["result"], change["reason"] = "failed", f"could not be read or written ({exc})"
        changes.append(change)
    if not dry:
        _verify(q, changes)
    return {"board": found["board"], "dry_run": dry, "changes": changes, "skipped": skipped,
            "truncated": found["truncated"], "read": found["read"], "issues_truncated": found["issues_truncated"]}


def _verify(q: Any, changes: List[Dict[str, Any]]) -> None:
    """Read the board Status back for each board change made in live mode (label writes are read back by the
    write itself); a Status that is not the one set turns the change into a failure."""
    done = [c for c in changes if c["result"] == "changed" and c["side"] == "board Status"]
    if not done:
        return
    try:
        board_q, _, _ = open_board(q, q.tracker, what="read-back of", or_off="")
        items = {i["number"]: i for i in board_q._fetch()["items"]
                 if (i["repo"] or "").lower() == q.tracker.lower()}
    except (MirrorFailed, GhError, LookupError) as exc:
        for c in done:
            c["result"], c["reason"] = "failed", f"the Status could not be read back ({exc})"
        return
    for c in done:
        now = (items.get(c["number"]) or {}).get("status")
        if now != c["to"]:
            c["result"], c["reason"] = "failed", f"the Status reads {now!r} after the write, not {c['to']!r}"
