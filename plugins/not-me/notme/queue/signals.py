"""What could rank the queue: board fields and labels on the eligible issues, as names and counts only.

Gathers for /not-me:setup's picking step; decides nothing. The eligible issues are the queue's own candidates()
(the same rules pick-issue uses). Never returns an issue title or body, and never a text field's value: only how
many issues carry one.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from ..gh import GhError
from ..layers import is_field_name, is_positive_int
from . import FIELD_VALUE, field_value, _project_owner

# GitHub project field data types pick-issue can report a value for, and the name this output gives each.
FIELD_TYPES = {"SINGLE_SELECT": "single_select", "NUMBER": "number", "TEXT": "text", "DATE": "date",
               "ITERATION": "iteration"}
MAX_VALUES = 25  # distinct values listed per field; the rest are counted in more_values
# Generic label families that usually rank work: P0-P4, S0-S4, priority, severity, size, impact, effort, ...
_RANKING_LABEL = re.compile(r"^(?:[ps][0-4]|priority|prio|severity|sev|size|impact|effort|estimate|urgency)\b",
                            re.IGNORECASE)
_COMMON = "... on ProjectV2FieldCommon{name dataType}"
# Every field value of an issue's project items, with each field's name and type, and each project's owner,
# number and field list. Values are read through FIELD_VALUE's selections plus the field they belong to.
_QUERY = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name){issue(number:{n}){"
          "projectItems(first:20){nodes{project{number owner{... on User{login} ... on Organization{login}} "
          "fields(first:50){nodes{" + _COMMON + "}}} fieldValues(first:50){nodes{"
          + FIELD_VALUE.replace("}", " field{" + _COMMON + "}}") + "}}}}}}}")


def ranking_like(label: str) -> bool:
    """Whether a label name looks like a ranking signal (priority:high, P1, size/xl, severity: 2, ...)."""
    return bool(_RANKING_LABEL.match(label.strip()))


def _field(node: Any) -> Tuple[str, str]:
    """(name, data type) of a field node or a value's field; ("", "") when unreadable."""
    field = node.get("field") if isinstance(node, dict) and "field" in node else node
    if not isinstance(field, dict):
        return "", ""
    name, kind = field.get("name"), field.get("dataType")
    return (name if isinstance(name, str) else "", kind if isinstance(kind, str) else "")


def picking_signals(queue: Any) -> Dict[str, Any]:
    """Board fields and labels on the queue's eligible issues, with the values in use and how many issues carry
    each. A read: no claim, no write. Field values come only from projects owned by the tracker's owner (on a
    board queue, only the configured board); items in anyone else's project are counted, never read."""
    cap = (queue.meta.get("picking") or {}).get("value_max_chars")
    if not is_positive_int(cap):
        raise ValueError(f"picking.value_max_chars must be a whole number of at least 1, got {cap!r}")
    found = queue.candidates()
    board_number: Optional[int] = getattr(queue, "number", None)
    repo_owner, name = queue.tracker.split("/", 1)
    owner = getattr(queue, "owner", None) or repo_owner  # whose projects count: the board's, else the tracker's

    kinds: Dict[str, str] = {}  # field name -> type, from the projects' field lists
    projects: Dict[str, set] = {}
    counts: Dict[str, Counter] = {}
    carried: Counter = Counter()
    unread = other_owner = 0
    for c in found:
        query = _QUERY.replace("{n}", str(int(c.number)))
        try:
            data = queue.gh.read(["api", "graphql", "-f", f"query={query}", "-f", f"owner={repo_owner}",
                                  "-f", f"name={name}"])
        except GhError:
            unread += 1
            continue
        issue = (((data or {}).get("data") or {}).get("repository") or {}).get("issue")
        if not isinstance(issue, dict):
            unread += 1
            continue
        seen: Dict[str, Any] = {}  # field -> this issue's first value (first project item with one wins)
        for item in (issue.get("projectItems") or {}).get("nodes") or []:
            if not isinstance(item, dict):
                continue
            if _project_owner(item).casefold() != owner.casefold():
                other_owner += 1
                continue
            number = (item.get("project") or {}).get("number")
            if board_number is not None and number != board_number:
                continue
            for node in ((item.get("project") or {}).get("fields") or {}).get("nodes") or []:
                fname, kind = _field(node)
                if fname and kind in FIELD_TYPES:
                    kinds.setdefault(fname, FIELD_TYPES[kind])
                    projects.setdefault(fname, set()).add(number)
            for node in (item.get("fieldValues") or {}).get("nodes") or []:
                fname, kind = _field(node)
                if not fname or kind not in FIELD_TYPES or fname in seen:
                    continue
                value = field_value(node, cap)
                if value is not None:
                    seen[fname] = None if kind == "TEXT" else value  # by the value's own type: text is never kept
                    kinds.setdefault(fname, FIELD_TYPES[kind])
                    projects.setdefault(fname, set()).add(number)
        for fname, value in seen.items():
            carried[fname] += 1
            if kinds[fname] != "text" and value is not None:
                counts.setdefault(fname, Counter())[value] += 1

    fields: List[Dict[str, Any]] = []
    for fname in sorted(kinds, key=str.casefold):
        usable = is_field_name(fname)  # a usable name is shown whole: it is copied into picking.fields
        row: Dict[str, Any] = {"name": fname if usable else fname[:cap], "type": kinds[fname], "usable": usable,
                               "projects": sorted(n for n in projects.get(fname, ()) if isinstance(n, int)),
                               "issues": carried[fname]}
        if kinds[fname] == "text":
            row["values"], row["more_values"] = None, 0  # text is free-form: only how many issues carry one
        else:
            ranked = sorted((counts.get(fname) or Counter()).items(), key=lambda kv: (-kv[1], str(kv[0])))
            row["values"] = [{"value": v, "issues": k} for v, k in ranked[:MAX_VALUES]]
            row["more_values"] = max(0, len(ranked) - MAX_VALUES)
        fields.append(row)

    spelled: Dict[str, str] = {}
    labels: Counter = Counter()
    for c in found:
        for label in {str(x).casefold(): str(x) for x in c.labels}.values():
            key = label.casefold()
            spelled.setdefault(key, label)
            labels[key] += 1
    label_rows = [{"name": spelled[k][:cap], "issues": n, "ranking_like": ranking_like(spelled[k])}
                  for k, n in sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {"tracker": queue.tracker, "source": "board" if board_number is not None else "labels",
            "board": {"owner": owner, "number": board_number} if board_number is not None else None,
            "eligible": len(found), "include_unassigned": bool(queue.include_unassigned),
            "unassigned": queue.unassigned_skipped, "held_back": len(queue.held_back),
            "fields": fields, "labels": label_rows, "other_owner_items": other_owner, "fields_unread": unread}
