"""Abstract work states, the repo's label names for them, and the legal transitions between them."""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence

STATES = ("todo", "in_progress", "needs_review", "needs_work", "ready_to_merge", "question", "blocked", "epic",
          "needs_help", "needs_human", "paused")

# Labels that sit next to the main state instead of replacing it.
ADDITIVE_ISSUE = frozenset({"paused"})
ADDITIVE_PR = frozenset({"paused", "needs_human"})

ISSUE_EDGES: Dict[str, FrozenSet[str]] = {
    "todo": frozenset({"in_progress"}),
    "in_progress": frozenset({"needs_review", "blocked", "todo", "needs_human"}),
    "needs_review": frozenset({"needs_work", "ready_to_merge", "question"}),
    "needs_work": frozenset({"in_progress"}),
    "question": frozenset({"needs_review", "in_progress"}),
    "blocked": frozenset({"todo"}),
    "needs_human": frozenset({"in_progress", "todo"}),
}
PR_EDGES: Dict[str, FrozenSet[str]] = {
    "needs_review": frozenset({"needs_work", "ready_to_merge", "question"}),
    "needs_work": frozenset({"needs_review"}),
    "question": frozenset({"needs_review"}),
}
EDGES = {"issue": ISSUE_EDGES, "pr": PR_EDGES}

# Which labels count as "the" state when reading a label set (additive ones are read separately).
_STATE_ORDER = ("in_progress", "needs_work", "needs_review", "ready_to_merge", "question", "blocked", "todo",
                "needs_human")


class IllegalTransition(Exception):
    """The requested state change is not an edge in the graph, or the item is not in the state it was said to be."""


class StateMismatch(IllegalTransition):
    """move() was told the item should be in one state and it is in another."""


class Labels:
    """Abstract state <-> the label names a repo uses (the `labels` map in project.md)."""

    def __init__(self, mapping: Mapping[str, str]):
        self._by_abstract: Dict[str, str] = {k: str(v) for k, v in mapping.items()}
        self._by_name: Dict[str, str] = {v.lower(): k for k, v in self._by_abstract.items()}

    def name(self, abstract: str) -> str:
        try:
            return self._by_abstract[abstract]
        except KeyError:
            raise KeyError(f"unknown state {abstract!r}; known states: {', '.join(sorted(self._by_abstract))}") from None

    def abstract(self, label: str) -> str:
        try:
            return self._by_name[label.lower()]
        except (KeyError, AttributeError):
            raise KeyError(f"label {label!r} is not one of the configured state labels: "
                           f"{', '.join(sorted(self._by_name))}") from None

    def knows(self, label: str) -> bool:
        return isinstance(label, str) and label.lower() in self._by_name

    def current(self, label_names: Iterable[str], kind: str = "issue") -> List[str]:
        """The abstract main-state labels present, in priority order (additive labels excluded)."""
        present = {self._by_name[n.lower()] for n in label_names if self.knows(n)}
        additive = _additive(kind)
        return [s for s in _STATE_ORDER if s in present and s not in additive]

    def additive(self, label_names: Iterable[str], kind: str = "issue") -> List[str]:
        present = {self._by_name[n.lower()] for n in label_names if self.knows(n)}
        return [s for s in sorted(_additive(kind)) if s in present]


def _additive(kind: str) -> FrozenSet[str]:
    return ADDITIVE_PR if kind == "pr" else ADDITIVE_ISSUE


def allowed(frm: str, to: str, kind: str = "issue") -> bool:
    if kind not in EDGES:
        raise ValueError(f"kind must be issue or pr, got {kind!r}")
    if to in _additive(kind) and to in STATES:
        return True
    return to in EDGES[kind].get(frm, frozenset())


def transition(current_labels: Sequence[str], frm: str, to: str, labels: Labels, kind: str = "issue") -> List[str]:
    """The full new label set for frm -> to, or IllegalTransition. Unrelated labels are kept as they are."""
    if not allowed(frm, to, kind):
        raise IllegalTransition(f"{kind} cannot go from {frm} to {to}")
    current = list(current_labels)
    from_name, to_name = labels.name(frm), labels.name(to)
    if from_name.lower() not in {c.lower() for c in current}:
        raise IllegalTransition(f"{kind} is not {frm}: it does not carry the {from_name!r} label")
    additive = to in _additive(kind)
    out = [c for c in current if additive or c.lower() != from_name.lower()]
    if to_name.lower() not in {c.lower() for c in out}:
        out.append(to_name)
    return out


def apply(gh: Any, repo: str, n: int, frm: str, to: str, labels: Labels, kind: str = "issue") -> List[str]:
    """Read the labels, validate the edge, then replace the whole label set (PUT + read-back)."""
    new = transition(gh.labels(repo, n), frm, to, labels, kind)
    gh.labels_set(repo, n, new)
    return new
