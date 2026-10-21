"""The project roster: a markdown table of people and how far they are trusted."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from .frontmatter import iter_unfenced_lines

TIERS = ("lead", "authority", "member")
_TRUE = {"yes", "y", "true", "x", "✓", "✔", "1"}
_SEPARATOR = re.compile(r"^:?-+:?$")
HEADER = ("GitHub login", "Name", "Tier", "Assignable", "Reviewer")


@dataclass(frozen=True)
class Member:
    login: str
    name: str = ""
    tier: str = "member"
    assignable: bool = False
    reviewer: bool = False


def _cells(line: str) -> List[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _column(header: List[str]) -> dict:
    cols = {}
    for i, raw in enumerate(header):
        h = raw.lower()
        if "login" in h or h in ("github", "user", "handle"):
            cols.setdefault("login", i)
        elif "name" in h:
            cols.setdefault("name", i)
        elif "tier" in h or "role" in h:
            cols.setdefault("tier", i)
        elif "assign" in h:
            cols.setdefault("assignable", i)
        elif "review" in h:
            cols.setdefault("reviewer", i)
    return cols


def _flag(value: str) -> bool:
    return value.strip().lower() in _TRUE


def _table_blocks(body: str):
    """Yield (start, end, lines) for each run of table lines outside code fences."""
    lines = body.splitlines()
    start = None
    for i, (line, fenced) in enumerate(list(iter_unfenced_lines(body)) + [("", False)]):
        is_row = not fenced and line.strip().startswith("|")
        if is_row and start is None:
            start = i
        elif not is_row and start is not None:
            yield start, i, lines[start:i]
            start = None


def _find_table(body: str):
    for start, end, rows in _table_blocks(body):
        cols = _column(_cells(rows[0]))
        if "login" in cols:
            return start, end, rows, cols
    return None


def _cell(cells: List[str], cols: dict, col: str) -> str:
    idx = cols.get(col)
    return cells[idx] if idx is not None and idx < len(cells) else ""


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def parse_roster(body: str) -> List[Member]:
    """Members from the first table in body that has a login column."""
    found = _find_table(body)
    if found is None:
        return []
    _, _, rows, cols = found
    members: List[Member] = []
    for row in rows[1:]:
        cells = _cells(row)
        if all(_SEPARATOR.match(c) for c in cells if c) and any(cells):
            continue
        login = _cell(cells, cols, "login").lstrip("@").strip()
        if not login:
            continue
        tier = _cell(cells, cols, "tier").lower()
        members.append(Member(
            login=login,
            name=_cell(cells, cols, "name"),
            tier=tier if tier in TIERS else "member",
            assignable=_flag(_cell(cells, cols, "assignable")),
            reviewer=_flag(_cell(cells, cols, "reviewer")),
        ))
    return members


def merge_members(lower: Iterable[Member], upper: Iterable[Member]) -> List[Member]:
    """Union by login (case-insensitive); the upper layer's row wins in place."""
    merged = list(lower)
    index = {m.login.lower(): i for i, m in enumerate(merged)}
    for member in upper:
        key = member.login.lower()
        if key in index:
            merged[index[key]] = member
        else:
            index[key] = len(merged)
            merged.append(member)
    return merged


def render_table(members: Iterable[Member]) -> str:
    lines = ["| " + " | ".join(HEADER) + " |", "|" + "|".join("-" * (len(h) + 2) for h in HEADER) + "|"]
    for m in members:
        lines.append(f"| {m.login} | {m.name} | {m.tier} | {_yes(m.assignable)} | {_yes(m.reviewer)} |")
    return "\n".join(lines)


def replace_table(body: str, members: Iterable[Member]) -> str:
    """Body with its roster table replaced by one rendered from members."""
    found = _find_table(body)
    if found is None:
        return body
    start, end, _, _ = found
    lines = body.splitlines()
    out = "\n".join(lines[:start] + render_table(members).splitlines() + lines[end:])
    return out + ("\n" if body.endswith("\n") else "")


def merge_section(lower: str, upper: str) -> str:
    """Merge two roster sections: rows union by login, upper rows win, upper prose kept."""
    members = merge_members(parse_roster(lower), parse_roster(upper))
    if _find_table(upper) is None:
        return upper.rstrip("\n") + "\n\n" + render_table(members) + "\n"
    return replace_table(upper, members)


@dataclass
class Roster:
    entries: List[Member] = field(default_factory=list)
    operator: Optional[str] = None

    def _logins(self, pred) -> List[str]:
        return [m.login for m in self.entries if pred(m)]

    def _or_operator(self, pred) -> List[str]:
        """With no roster the operator stands in; otherwise the matching logins."""
        if not self.entries:
            return [self.operator] if self.operator else []
        return self._logins(pred)

    @property
    def leads(self) -> List[str]:
        return self._or_operator(lambda m: m.tier == "lead")

    @property
    def authorities(self) -> List[str]:
        return self._logins(lambda m: m.tier == "authority")

    @property
    def members(self) -> List[str]:
        return self._logins(lambda m: m.tier == "member")

    @property
    def reviewers(self) -> List[str]:
        return self._logins(lambda m: m.reviewer)

    @property
    def assignable(self) -> List[str]:
        return self._or_operator(lambda m: m.assignable)

    def is_trusted(self, login: Optional[str]) -> bool:
        """Leads and authorities are trusted; with no roster, only the operator."""
        if not login:
            return False
        trusted = {x.lower() for x in self.leads + self.authorities}
        return login.lstrip("@").lower() in trusted


def effective(members: Iterable[Member], operator: Optional[str]) -> Roster:
    """The roster in force. An empty roster makes the operator the only lead."""
    return Roster(entries=list(members), operator=operator)
