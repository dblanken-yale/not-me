"""queue.sync: keep the side queue.source does not read in step with it.

queue.source stays the source of truth: it alone decides what can be picked, and the claim's compare-and-set and
read-back run against it only. With queue.sync true, each claim, move and release then mirrors the new state to
the other side, after the source write succeeded:

- labels source: the board Status, for states that have a column (todo, in_progress, needs_review, blocked).
  Other states leave the Status alone.
- board source: the full label set (one PUT, then read back), for every state.

A mirror write that fails never fails or undoes the source write: it adds exactly one warning to
queue.sync_warnings, naming the cause and the fix. The mirror never adds an item to a board, and every write goes
through the client's gated write path, so a board the operator has not allowed is refused there (and checked
before the board is read).
"""
from __future__ import annotations

from typing import Any

from .. import states, writes
from ..gh import GhError, ReadbackMismatch, WriteBlocked
from ..writes import Decision, WriteTarget


class MirrorFailed(Exception):
    """A mirror write could not be made; the message names the cause and the fix."""


def mirror(q: Any, n: int, state: str) -> None:
    """Mirror issue n's new `state` to the side q's source does not read. Does nothing when queue.sync is off.
    A failure becomes one entry in q.sync_warnings; it is never raised."""
    if not q.sync:
        return
    try:
        if q.source == "board":
            set_labels(q, n, state)
        else:
            _set_board_status(q, n, state)
    except MirrorFailed as exc:
        q.sync_warnings.append(f"queue.sync: {exc}")


def write_labels(q: Any, n: int, state: str) -> None:
    """Swap issue n's state label for `state` (other labels kept) with one full-set PUT and its read-back.
    Additive states (paused) are added next to the main state. Errors are not wrapped: KeyError when
    labels.<state> names no label, and WriteBlocked, ReadbackMismatch or GhError from the write itself. Used
    directly where the label is the only record of a move (a board queue's state with no column)."""
    name = q.labels.name(state)
    current = q.gh.labels(q.tracker, n)
    have = {c.lower() for c in current}
    if state in states.ADDITIVE_ISSUE:
        new = current if name.lower() in have else current + [name]
    else:
        new = states.swap_state(current, state, q.labels)
    # a dry run always records the write: earlier dry-run writes reached only the ledger
    if q.gh.dry_run or {c.lower() for c in new} != have:
        q.gh.labels_set(q.tracker, n, new)


def set_labels(q: Any, n: int, state: str) -> None:
    """write_labels as a mirror: MirrorFailed, naming the cause and the fix, when it cannot be done."""
    where = f"{q.tracker}#{n}"
    try:
        name = q.labels.name(state)
    except KeyError:
        raise MirrorFailed(f"labels on {where} not set: labels.{state} names no label; "
                           f"map labels.{state} in project.md") from None
    try:
        write_labels(q, n, state)
        q.change["label"] = name
    except WriteBlocked as exc:
        raise MirrorFailed(f"labels on {where} not set to `{name}`: "
                           + _refused(exc.decision, f"allow the repo with /not-me:allow-repo {q.tracker}")) from None
    except (GhError, ReadbackMismatch) as exc:
        raise MirrorFailed(f"labels on {where} not set to `{name}` ({exc}); check the token can edit issues "
                           f"in {q.tracker} and set the label by hand") from None


def _refused(decision: Decision, allow_fix: str) -> str:
    """The cause and fix for a write the gate refused. The allow-list advice (decision.allow_cmd, else
    allow_fix) is given only for an allow-list refusal; any other refusal names its own reason."""
    if decision.allow_cmd:
        fix = f"run {decision.allow_cmd}"
    elif decision.rule == "other_repo":
        fix = allow_fix
    else:
        fix = "clear that cause, then set it by hand"
    return f"the write was refused ({decision.reason}); {fix}"


def _set_board_status(q: Any, n: int, state: str) -> None:
    """Set issue n's board Status to the column for `state`, reusing BoardQueue's board reads and write. States
    with no column are skipped. MirrorFailed when it cannot be done; an item is never added to the board."""
    from . import board_ref
    from .board import ABSTRACT_TO_BOARD, BoardQueue, UnmappedState

    key = ABSTRACT_TO_BOARD.get(state)
    if key is None:
        return
    where = f"{q.tracker}#{n}"
    if board_ref(q.queue_cfg) is None:
        raise MirrorFailed(f"board Status of {where} not set: queue.board.owner and queue.board.number are not "
                           "set; set them in project.md or turn queue.sync off")
    try:
        board_q = BoardQueue(q.cfg, q.gh, q.roster, q.operator, q.run_id, q.tracker, clock=q._clock)
    except ValueError as exc:
        raise MirrorFailed(f"board Status of {where} not set: queue.board cannot be used ({exc}); fix queue.board "
                           "in project.md or turn queue.sync off") from None
    board_q.read_fields = []  # the mirror needs the Status only
    label = f"{board_q.owner}/{board_q.number}"
    allow_board = f"allow the board with /not-me:allow-repo --board {label}"
    # the gate first, so a board the operator has not allowed is never read
    decision = writes.check_write(WriteTarget.board(board_q.owner, board_q.number), q.gh.ctx)
    if not decision.ok:
        raise MirrorFailed(_board_refused(where, label, decision, allow_board))
    try:
        column = board_q.column(state)
    except UnmappedState:
        raise MirrorFailed(f"board Status of {where} not set: queue.board_states.{key} is not set; "
                           f"map queue.board_states.{key} to a real column of board {label}") from None
    try:
        board = board_q._fetch()
    except LookupError:
        raise MirrorFailed(f"board Status of {where} not set: board {label} was not found; check queue.board "
                           "and that the token has the project scope") from None
    except GhError as exc:
        raise MirrorFailed(f"board Status of {where} not set: board {label} could not be read ({exc}); check "
                           "that the token has the project scope") from None
    try:
        item = board_q._item(board, n)
    except LookupError:
        if board.get("truncated"):
            raise MirrorFailed(f"board Status of {where} not set: board {label} has more items than not-me reads "
                               "and this one was not among them; move its Status by hand") from None
        raise MirrorFailed(f"board Status of {where} not set: it is not on board {label}; add the issue to the "
                           "board (not-me never adds items)") from None
    if column not in board["options"]:
        raise MirrorFailed(f"board Status of {where} not set: board {label} has no {column!r} column (found: "
                           f"{', '.join(sorted(board['options']))}); map queue.board_states.{key} to a real "
                           "column") from None
    if item["status"] == column and not q.gh.dry_run:  # a dry run records it: see write_labels
        q.change["column"] = column
        return
    try:
        board_q._set_status(board, item, state)
        q.change["column"] = column
    except WriteBlocked as exc:
        raise MirrorFailed(_board_refused(where, label, exc.decision, allow_board)) from None
    except (GhError, LookupError) as exc:
        raise MirrorFailed(f"board Status of {where} not set to {column!r} ({exc}); check that the token has "
                           "the project scope") from None


def _board_refused(where: str, label: str, decision: Decision, allow_board: str) -> str:
    if decision.rule == "other_repo" and not decision.allow_cmd:  # the gate's own board refusal
        return (f"board Status of {where} not set: board {label} is not on the global allow list "
                f"({decision.reason}); {allow_board}")
    return f"board Status of {where} not set: " + _refused(decision, allow_board)
