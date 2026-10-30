"""Abstract work states, the repo's label names for them, and the legal transitions between them."""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence

STATES = ("todo", "in_progress", "needs_review", "needs_work", "ready_to_merge", "question", "blocked", "epic",
          "needs_help", "needs_human", "paused")

# Labels that sit next to the main state instead of replacing it.
ADDITIVE_ISSUE = frozenset({"paused"})
ADDITIVE_PR = frozenset({"paused", "needs_human"})

# States whose arrival gets a state-change comment. The timeline shows every label change, but not why work
# stopped or a person is needed; for every other state the reason goes to the run log only.
EXPLAINED = frozenset({"blocked", "question", "needs_help", "needs_human", "paused"})

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


class LabelCollision(ValueError):
    """Two abstract states map to the same repo label name, so a label could not be read back as one state."""


def label_collisions(mapping: Mapping[str, Any]) -> List[str]:
    """One message per repo label name (case-folded) that more than one abstract state maps to."""
    seen: Dict[str, List[str]] = {}
    for abstract, name in mapping.items():
        seen.setdefault(str(name).lower(), []).append(str(abstract))
    return [f"labels: {', '.join(sorted(roles))} all map to the label {name!r}; each state needs its own label"
            for name, roles in sorted(seen.items()) if len(roles) > 1]


class Labels:
    """Abstract state <-> the label names a repo uses (the `labels` map in project.md).

    LabelCollision when two states share a label name."""

    def __init__(self, mapping: Mapping[str, str]):
        clashes = label_collisions(mapping)
        if clashes:
            raise LabelCollision(clashes[0])
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


def as_if(current_labels: Sequence[str], state: str, labels: Labels, kind: str = "issue") -> List[str]:
    """The label set as if the item were in `state`: unchanged when it carries that label, else every main-state
    label swapped for it. Dry run only, where earlier writes reached the ledger and not GitHub."""
    current = list(current_labels)
    if labels.name(state).lower() in {c.lower() for c in current}:
        return current
    return swap_state(current, state, labels, kind)


def swap_state(current_labels: Sequence[str], state: str, labels: Labels, kind: str = "issue") -> List[str]:
    """The label set with every main-state label replaced by `state` (unchanged when it already carries only that
    one). Additive labels, pass labels and unrelated labels are kept. No edge check: callers that need one use
    transition()."""
    current = list(current_labels)
    name = labels.name(state)
    main = set(labels.current(current, kind)) - {state}
    out = [c for c in current if not (labels.knows(c) and labels.abstract(c) in main)]
    if name.lower() not in {c.lower() for c in out}:
        out.append(name)
    return out


def set_pass_labels(current_labels: Sequence[str], earned: Iterable[str], managed: Iterable[str]) -> List[str]:
    """Pass labels (role.md) are additive: they sit next to the main state and never count as it. Drop each
    managed pass label that is not earned, add each earned one, keep every other label as it is."""
    earned = list(earned)
    keep = {e.lower() for e in earned}
    drop = {m.lower() for m in managed} - keep
    out = [c for c in current_labels if c.lower() not in drop]
    have = {c.lower() for c in out}
    return out + [e for e in earned if e.lower() not in have]


def apply(gh: Any, repo: str, n: int, frm: str, to: str, labels: Labels, kind: str = "issue") -> List[str]:
    """Read the labels, validate the edge, then replace the whole label set (PUT + read-back).

    In dry run an item not in `frm` is taken to be in it (the state earlier dry-run writes would have set)."""
    current = gh.labels(repo, n)
    if getattr(gh, "dry_run", False) and allowed(frm, to, kind):
        current = as_if(current, frm, labels, kind)
    new = transition(current, frm, to, labels, kind)
    gh.labels_set(repo, n, new)
    return new


# --- creating the labels -------------------------------------------------------

# colour and description for each label role; roles not listed here get NEUTRAL_STYLE
LABEL_STYLE = {
    "todo": ("0e8a16", "Ready to be picked up"),
    "in_progress": ("fbca04", "Claimed and being worked"),
    "needs_review": ("1d76db", "Pull request waiting for a reviewer"),
    "needs_work": ("d93f0b", "Reviewer sent it back"),
    "ready_to_merge": ("2ea44f", "Approved"),
    "question": ("d876e3", "Waiting on an answer from a person"),
    "blocked": ("b60205", "Cannot move until something else lands"),
    "epic": ("5319e7", "Parent issue; not picked up directly"),
    "needs_help": ("e99695", "Automation gave up and asks for a person"),
    "needs_human": ("b60205", "A run stopped and needs a person to act"),
    "paused": ("c5def5", "Skipped until this label is removed"),
}
NEUTRAL_STYLE = ("ededed", "not-me label")
PASS_STYLE = ("c2e0c6", "A required reviewer in this role approved")  # pass labels named in role.md


def repo_label_names(gh: Any, repo: str) -> List[str]:
    """The names of the labels repo has, as the repo spells them (first 500)."""
    return [x.get("name", "") for x in gh.read(["label", "list", "-R", repo, "--json", "name", "--limit", "500"]) or []]


def ensure_labels(gh: Any, repo: str, mapping: Mapping[str, Any], check: bool = False,
                  pass_labels: Iterable[str] = ()) -> Dict[str, Any]:
    """Create the mapped labels and the role.md pass labels the repo lacks (only report them with check=True).

    Returns {repo, ok (nothing missing), present, missing, created}. ValueError for a name starting with a dash.
    """
    from .writes import WriteTarget

    have = {x.lower() for x in repo_label_names(gh, repo)}
    wanted = [(LABEL_STYLE.get(role, NEUTRAL_STYLE), str(name)) for role, name in mapping.items()]
    seen = {name.lower() for _, name in wanted}
    for name in pass_labels:
        if str(name).lower() not in seen:
            seen.add(str(name).lower())
            wanted.append((PASS_STYLE, str(name)))
    present = sorted(name for _, name in wanted if name.lower() in have)
    missing = [(style, name) for style, name in wanted if name.lower() not in have]
    created: List[str] = []
    if not check:
        for (color, description), name in missing:
            if name.startswith("-"):
                raise ValueError(f"label name {name!r} must not start with a dash")
            gh.write(WriteTarget.repo(repo),
                     ["label", "create", name, "-R", repo, "--color", color, "--description", description],
                     description=f"create label {name} on {repo}")
            created.append(name)
    return {"repo": repo, "ok": not missing, "present": present, "missing": [name for _, name in missing],
            "created": created}
