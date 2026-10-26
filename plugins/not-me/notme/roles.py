"""Reviewer roles (role.md): who must approve a pull request, and the pass label each role earns.

role.md holds one table with the columns GitHub login, Role and Pass label. With no rows, one approval moves a PR
to the approve state as before. With rows, every roster reviewer except the PR's author must approve; until then
the PR stays in needs_review and carries the pass label of each role with an approving reviewer. A pass label sits
next to the main state (see states.set_pass_labels) and is never required: a role with no pass label, or a
reviewer with no row, still counts toward "every required reviewer approved".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import roster as roster_mod
from .roster import _SEPARATOR, _cells, _table_blocks, bot_login

HEADING = "Roles"  # the role.md H2 whose table layers merge row by row (with extends)
HEADER = ("GitHub login", "Role", "Pass label")
APPROVED = "APPROVED"
# Review states that never decide a reviewer's standing: a comment, or a review not yet submitted.
IGNORED_STATES = frozenset({"COMMENTED", "PENDING"})


@dataclass(frozen=True)
class RoleRow:
    login: str
    role: str = ""
    pass_label: str = ""


def _columns(header: List[str]) -> Dict[str, int]:
    cols: Dict[str, int] = {}
    for i, raw in enumerate(header):
        h = raw.lower()
        if "login" in h or h in ("github", "user", "handle"):
            cols.setdefault("login", i)
        elif "label" in h:
            cols.setdefault("pass_label", i)
        elif "role" in h:
            cols.setdefault("role", i)
    return cols


def _find_table(body: str):
    for start, end, rows in _table_blocks(body):
        cols = _columns(_cells(rows[0]))
        if "login" in cols:
            return start, end, rows, cols
    return None


def _cell(cells: List[str], cols: Mapping[str, int], name: str) -> str:
    idx = cols.get(name)
    return cells[idx].strip() if idx is not None and idx < len(cells) else ""


def parse_roles(body: str) -> List[RoleRow]:
    """Rows of the first table in body that has a login column; a row without a login is skipped."""
    found = _find_table(body)
    if found is None:
        return []
    _, _, rows, cols = found
    out: List[RoleRow] = []
    for row in rows[1:]:
        cells = _cells(row)
        if all(_SEPARATOR.match(c) for c in cells if c) and any(cells):
            continue
        login = bot_login(_cell(cells, cols, "login").lstrip("@").strip())
        if login:
            out.append(RoleRow(login, _cell(cells, cols, "role"), _cell(cells, cols, "pass_label")))
    return out


def merge_rows(lower: Iterable[RoleRow], upper: Iterable[RoleRow]) -> List[RoleRow]:
    """Union by login (case-insensitive); the upper layer's row wins in place."""
    merged = list(lower)
    index = {r.login.lower(): i for i, r in enumerate(merged)}
    for row in upper:
        key = row.login.lower()
        if key in index:
            merged[index[key]] = row
        else:
            index[key] = len(merged)
            merged.append(row)
    return merged


def render_table(rows: Iterable[RoleRow]) -> str:
    lines = ["| " + " | ".join(HEADER) + " |", "|" + "|".join("-" * (len(h) + 2) for h in HEADER) + "|"]
    lines += [f"| {r.login} | {r.role} | {r.pass_label} |" for r in rows]
    return "\n".join(lines)


def merge_section(lower: str, upper: str) -> str:
    """Merge two `## Roles` sections: rows union by login, upper rows win, upper prose kept."""
    rows = merge_rows(parse_roles(lower), parse_roles(upper))
    found = _find_table(upper)
    if found is None:
        return upper.rstrip("\n") + "\n\n" + render_table(rows) + "\n"
    start, end, _, _ = found
    lines = upper.splitlines()
    out = "\n".join(lines[:start] + render_table(rows).splitlines() + lines[end:])
    return out + ("\n" if upper.endswith("\n") else "")


@dataclass
class Roles:
    """The effective role table. `configured` is false when it has no rows (the one-approval rule applies).
    `ignored` holds repo-layer rows dropped because they would change a trusted row, and repo-layer pass labels
    dropped because only the trusted layers set pass labels (see trusted_merge; `part` says which)."""

    rows: List[RoleRow] = field(default_factory=list)
    ignored: List[Dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_login: Dict[str, RoleRow] = {}
        self._labels: Dict[str, str] = {}
        for row in self.rows:
            self._by_login.setdefault(row.login.lower(), row)
            if row.role and row.pass_label:
                self._labels.setdefault(row.role.lower(), row.pass_label)

    @property
    def configured(self) -> bool:
        return bool(self.rows)

    def role_of(self, login: str) -> str:
        row = self._by_login.get(login.lower())
        return row.role if row else ""

    def pass_labels(self) -> Dict[str, str]:
        """role -> pass label, from the first row of each role that names one (roles without one are absent)."""
        return dict(self._labels)

    def pass_label_of(self, login: str) -> str:
        """The pass label of this login's role; "" when the login has no row, no role or the role has no label."""
        return self._labels.get(self.role_of(login).lower(), "")

    def describe(self) -> List[Dict[str, str]]:
        return [{"login": r.login, "role": r.role, "pass_label": self.pass_label_of(r.login)} for r in self.rows]


def _same(a: RoleRow, b: RoleRow) -> bool:
    return a.role.lower() == b.role.lower() and a.pass_label.lower() == b.pass_label.lower()


LABEL_REASON = "repo layer may not name pass labels; set them in the global role.md"


def trusted_merge(trusted: Iterable[RoleRow], repo: Iterable[RoleRow]) -> Roles:
    """The approval gate's table: the trusted rows (default and global layers) plus repo-layer rows for logins the
    trusted rows lack. The repo layer is committed, so it may only add reviewers and roles, never pass labels:
    not-me adds and removes pass labels on PRs, so a repo-named one could strip any label (`do not merge`). A repo
    row that blanks or changes a trusted row's role or pass label is ignored; a new repo row keeps its login and
    role and drops its pass label, which is listed in `ignored` unless it is the trusted label of that role (a
    repo reviewer in a trusted role earns that role's trusted label). A repo table without a trusted login keeps
    that login's trusted row."""
    rows = list(trusted)
    labels = {role: label.lower() for role, label in Roles(rows).pass_labels().items()}
    index = {r.login.lower(): r for r in rows}
    ignored: List[Dict[str, str]] = []
    for row in repo:
        known = index.get(row.login.lower())
        if known is None:
            if row.pass_label and row.pass_label.lower() != labels.get(row.role.lower()):
                ignored.append({"login": row.login, "role": row.role, "pass_label": row.pass_label,
                                "part": "pass_label", "reason": LABEL_REASON})
            row = RoleRow(row.login, row.role, "")
            index[row.login.lower()] = row
            rows.append(row)
        elif not _same(known, row):
            ignored.append({"login": row.login, "role": row.role, "pass_label": row.pass_label, "part": "row",
                            "reason": "repo layer may only add role rows"})
    return Roles(rows, ignored)


def errors(rows: Sequence[RoleRow], roster_logins: Iterable[str], state_labels: Mapping[str, str]) -> List[str]:
    """Config errors: a login not on the roster, a role with two pass labels, two roles sharing one pass label, or
    a pass label that is one of the state labels (the `labels` map in project.md). Case-insensitive throughout."""
    out: List[str] = []
    known = {x.lower() for x in roster_logins}
    for row in rows:
        if row.login.lower() not in known:
            out.append(f"{row.login} is not on the roster in project.md")
    by_role: Dict[str, str] = {}
    by_label: Dict[str, List[str]] = {}
    for row in rows:
        if not row.pass_label:
            continue
        if not row.role:
            out.append(f"{row.login} has a pass label but no role")
            continue
        seen = by_role.setdefault(row.role.lower(), row.pass_label)
        if seen.lower() != row.pass_label.lower():
            out.append(f"role {row.role} has two pass labels, {seen!r} and {row.pass_label!r}")
        roles = by_label.setdefault(row.pass_label.lower(), [])
        if row.role.lower() not in roles:
            roles.append(row.role.lower())
    for label, roles in sorted(by_label.items()):
        if len(roles) > 1:
            out.append(f"roles {', '.join(roles)} share the pass label {label!r}; each role needs its own")
    states = {str(name).lower(): key for key, name in state_labels.items()}
    for label in sorted(by_label):
        if label in states:
            out.append(f"pass label {label!r} is the {states[label]} state label; pick another name")
    return out


def warnings(rows: Sequence[RoleRow], roster: roster_mod.Roster) -> List[str]:
    """A row for a roster member who is not a reviewer: their approval is never required."""
    reviewers = {x.lower() for x in roster.reviewers}
    known = {m.login.lower() for m in roster.entries}
    return [f"role.md: {r.login} is on the roster but not a reviewer (Reviewer = yes), so their approval is "
            "never required" for r in rows if r.login.lower() in known and r.login.lower() not in reviewers]


# --- the approval rule ----------------------------------------------------------------


def latest_states(reviews: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """login (lowercased) -> state of that login's latest review, in the order given (oldest first). COMMENTED and
    PENDING reviews are skipped; DISMISSED and CHANGES_REQUESTED count, as not approved."""
    out: Dict[str, str] = {}
    for login, state in reviews:
        state = str(state or "").upper()
        if login and state and state not in IGNORED_STATES:
            out[login.lower()] = state
    return out


@dataclass
class Outcome:
    """What the reviews say: who must approve, who has, whether that is everyone, and the pass labels earned."""

    required: List[str]
    approved: List[str]
    waiting: List[str]
    all_approved: bool
    earned: List[str]
    managed: List[str]

    def to_json(self) -> Dict[str, object]:
        return {"required": self.required, "approved": self.approved, "waiting": self.waiting,
                "all_approved": self.all_approved, "pass_labels": self.earned}


def outcome(reviews: Iterable[Tuple[str, str]], author: Optional[str], reviewers: Sequence[str], roles: Roles,
            managed: Optional[Sequence[str]] = None) -> Outcome:
    """Apply the rule to a PR's reviews. Required: every reviewer given except the author. With nobody required,
    any approval is enough. managed: the pass labels to add or remove, in the repo's own spelling (the ones that
    exist there); None means every role.md pass label as written."""
    latest = latest_states(reviews)
    author_key = (author or "").lower()
    required = [r for r in reviewers if r.lower() != author_key]
    approved = [login for login, state in latest.items() if state == APPROVED]
    if required:
        done = [r for r in required if latest.get(r.lower()) == APPROVED]
        waiting = [r for r in required if latest.get(r.lower()) != APPROVED]
        all_approved = not waiting
    else:
        done, waiting, all_approved = approved, [], bool(approved)
    managed = list(roles.pass_labels().values()) if managed is None else list(managed)
    spelling = {m.lower(): m for m in managed}
    earned: List[str] = []
    for login in approved:
        label = spelling.get(roles.pass_label_of(login).lower())
        if label and label not in earned:
            earned.append(label)
    return Outcome(required=required, approved=done, waiting=waiting, all_approved=all_approved, earned=earned,
                   managed=managed)
