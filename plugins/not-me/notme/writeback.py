"""The write-back layer: everything the skills post to GitHub, composed here and sent through GhClient.

Bodies are composed as text, written to a private temp file, passed to gh by path, and removed again.
Comments carry hidden markers so a later run updates its own comment instead of adding a second one.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import gh as _gh
from . import writes
from .writes import WriteTarget

MARKER = "<!-- not-me:{kind} -->"
RUN_MARKER = "<!-- not-me:run={run_id} -->"
REVIEW_KIND = "review"
NEW_PR_KIND = "new"
FOLLOWUP_KIND = "followup"
COLLAPSE_AFTER = 15

_KIND_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_RUN_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MARKER_PREFIX = "<!-- not-me:"
_REFS_LINE_RE = re.compile(r"^(?:(?:Closes|Fixes|Resolves|Refs)\b|\*\*References:\*\*)")
_DEFAULT_STEPS_HEADING = "(Functional )?[Tt]esting steps"
_SUMMARY_HEADING = r"(?i:summary|description)\b"
_TESTING_FALLBACK = r"(?i:testing|test plan)\b"
_COMPANION_HEADING = r"(?i:companion prs)\s*$"
_TEMPLATE_PATHS = (".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md")
_VERDICTS = {"approve": ("APPROVE", "APPROVED"), "request_changes": ("REQUEST_CHANGES", "CHANGES_REQUESTED"),
             "comment": ("COMMENT", "COMMENTED")}


class WritebackMismatch(Exception):
    """What GitHub holds after a write is not what was asked for."""


# --- small helpers -----------------------------------------------------------


def _kind_marker(kind: str) -> str:
    if not _KIND_RE.fullmatch(str(kind)):
        raise ValueError(f"invalid marker kind {kind!r}")
    return MARKER.format(kind=kind)


def _run_marker(run_id: str) -> str:
    if not _RUN_RE.fullmatch(str(run_id)):
        raise ValueError(f"invalid run id {run_id!r}")
    return RUN_MARKER.format(run_id=run_id)


def _tmp_file(text: str, suffix: str = ".md"):
    """A private temp file under not-me's temp dir, removed on exit."""
    return _gh.tmp_text_file(text, prefix="not-me-wb-", suffix=suffix)


def _ensure_markers(body: str, markers: Sequence[str]) -> str:
    """body with each marker present, the missing ones added on their own lines at the end."""
    missing = [m for m in markers if m not in body]
    out = body.rstrip()
    return (out + "\n\n" + "\n".join(missing) if missing else out) + "\n"


def _meta(cfg: Any, doc: str) -> Dict[str, Any]:
    return (getattr(cfg, "meta", None) or {}).get(doc) or {}


def _comment_id_from(result: Any) -> Optional[int]:
    out = result.get("output", "") if isinstance(result, dict) else ""
    m = re.search(r"issuecomment-(\d+)", out)
    return int(m.group(1)) if m else None


def _ledger_only(gh: Any, repo: str, argv: Sequence[str], description: str, payload: Any = None) -> List[str]:
    """Record a write that a dry run cannot send yet (it needs the number of something not created)."""
    target = gh._repo(repo)
    gh._gate(target)
    if gh.ledger is not None:
        gh.ledger.append("gh " + " ".join(argv[:2]), target.describe(), list(argv), None, description,
                         payload_bytes=json.dumps(payload).encode("utf-8") if payload is not None else None)
    return list(argv)


# --- comments ----------------------------------------------------------------


def find_own_comment(gh: Any, repo: str, n: int, kind: str, legacy: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """The newest comment by the operator that carries the kind marker or any legacy marker."""
    markers = [_kind_marker(kind), *[m for m in legacy if m]]
    me = (gh.me() or "").lower()
    mine = [c for c in gh.comments(repo, n) if (c.get("author") or "").lower() == me
            and any(m in (c.get("body") or "") for m in markers)]
    if not mine:
        return None
    return max(mine, key=lambda c: (c.get("created_at") or "", c["id"]))


def comment_upsert(gh: Any, repo: str, n: int, kind: str, body_text: str, run_id: str,
                   legacy: Sequence[str] = ()) -> Dict[str, Any]:
    """Update the operator's own marker comment, else create it. Never makes a second one."""
    body = _ensure_markers(body_text, [_kind_marker(kind), _run_marker(run_id)])
    existing = find_own_comment(gh, repo, n, kind, legacy)
    with _tmp_file(body) as path:
        if existing:
            result = gh.comment_update(repo, existing["id"], path)
            out: Dict[str, Any] = {"id": existing["id"], "updated": True}
        else:
            result = gh.comment_create(repo, n, path)
            out = {"id": _comment_id_from(result), "created": True}
    if isinstance(result, dict) and result.get("dry_run"):
        out["dry_run"] = True
    return out


def reply_thread(gh: Any, repo: str, n: int, reply_to: int, body_text: str, run_id: str) -> Any:
    """Reply in a review thread with the run marker. Never resolves the thread."""
    body = _ensure_markers(body_text, [_run_marker(run_id)])
    with _tmp_file(body) as path:
        return gh.reply_thread(repo, n, reply_to, path)


def resolve_fixed_threads(gh: Any, repo: str, n: int, thread_ids: Sequence[str]) -> List[str]:
    """Resolve review threads the review skill confirmed as fixed. Only threads of this PR are touched."""
    known = {t["id"] for t in gh.review_threads(repo, n)}
    stray = [t for t in thread_ids if t not in known]
    if stray:
        raise ValueError(f"thread {stray[0]!r} is not a review thread of {repo}#{n}")
    for thread_id in thread_ids:
        gh.resolve_thread(thread_id)
    return list(thread_ids)


# --- summary text --------------------------------------------------------------


def _render_section(name: str, content: Any) -> str:
    if isinstance(content, (list, tuple)):
        lines = [f"- {item}".replace("\n", "\n  ") for item in content]
        lines = "\n".join(lines).split("\n")
    else:
        lines = str(content).strip("\n").split("\n")
    if len(lines) > COLLAPSE_AFTER:
        label = html.escape(str(name), quote=False)
        return (f"<details><summary>{label} ({len(lines)} lines)</summary>\n\n" + "\n".join(lines)
                + "\n\n</details>")
    return f"### {name}\n\n" + "\n".join(lines)


def summary(kind: str, status_line: str, sections: Optional[Dict[str, Any]] = None,
            waiting_on: Optional[Sequence[str]] = None, run_id: str = "") -> str:
    """TL;DR-first markdown: status, who is waited on (never collapsed), sections, then the markers."""
    kind_marker, run_marker = _kind_marker(kind), _run_marker(run_id)
    parts = ["**TL;DR:** " + " ".join(str(status_line).split())]
    logins = [str(w).lstrip("@") for w in waiting_on or [] if str(w).strip()]
    if logins:
        parts.append("**Waiting on:**\n\n" + "\n".join(f"- @{login}" for login in logins))
    else:
        parts.append("**Waiting on:** nobody")
    for name, content in (sections or {}).items():
        if content not in (None, "", [], ()):
            parts.append(_render_section(name, content))
    parts.append(kind_marker + "\n" + run_marker)
    return "\n\n".join(parts) + "\n"


# --- attribution ---------------------------------------------------------------


def attribution(cfg: Any) -> str:
    """The attribution line for PR bodies and commit messages. inherit and none give nothing."""
    value = _meta(cfg, "project").get("attribution", "inherit")
    if value is None or str(value).strip().lower() in ("inherit", "none", ""):
        return ""
    return str(value).strip()


def apply_attribution(body: str, cfg: Any) -> str:
    text = attribution(cfg)
    return body.rstrip() + "\n\n" + text + "\n" if text else body


# --- markdown sections ---------------------------------------------------------


def _headings(lines: Sequence[str]) -> List[Tuple[int, int, str]]:
    """(line index, level, text) for headings outside code fences."""
    found, fenced = [], False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced:
            m = _HEADING_RE.match(line)
            if m:
                found.append((i, len(m.group(1)), m.group(2)))
    return found


def _find_section(lines: Sequence[str], pattern: str) -> Optional[Tuple[int, int, int]]:
    """(heading index, level, end) of the first heading matching pattern; end is exclusive.

    A section ends at the next heading of the same or a higher level, at a not-me marker line, or at a
    closing-keyword or References line.
    """
    heads = _headings(lines)
    for k, (i, level, text) in enumerate(heads):
        if re.match(pattern, text):
            end = len(lines)
            for j, lvl, _ in heads[k + 1:]:
                if lvl <= level:
                    end = j
                    break
            for j in range(i + 1, end):
                if lines[j].startswith(_MARKER_PREFIX) or _REFS_LINE_RE.match(lines[j]):
                    end = j
                    break
            return i, level, end
    return None


def _marker_index(lines: Sequence[str]) -> Optional[int]:
    return next((i for i, line in enumerate(lines) if line.startswith(_MARKER_PREFIX)), None)


def _set_section(body: str, pattern: str, heading: str, content: str) -> str:
    """Replace the content of the section matching pattern, or add it above the markers (else at the end)."""
    lines = body.split("\n")
    block = content.strip("\n").split("\n") if content.strip() else []
    span = _find_section(lines, pattern)
    if span:
        start, _, end = span
        rest = lines[end:]
        while rest and not rest[0].strip():
            rest = rest[1:]
        new = lines[:start + 1] + [""] + block + ([""] + rest if rest else [])
    else:
        at = _marker_index(lines)
        head = lines[:at] if at is not None else list(lines)
        while head and not head[-1].strip():
            head.pop()
        tail = ([""] + lines[at:]) if at is not None else []
        new = head + ([""] if head else []) + [heading, ""] + block + tail
    return "\n".join(new)


def _companion_lines(companions: Sequence[Dict[str, Any]]) -> str:
    if not companions:
        return "_No companion PRs._"
    out = []
    for c in companions:
        ref = f"{c['repo']}#{c['number']}"
        out.append(f"- [{ref}]({c['url']})" if c.get("url") else f"- {ref}")
    return "\n".join(out)


def _steps_pattern(cfg: Any = None) -> str:
    heading = _meta(cfg, "prove").get("steps_heading") or _DEFAULT_STEPS_HEADING
    try:
        re.compile(heading)
    except re.error:
        heading = _DEFAULT_STEPS_HEADING
    return f"(?:{heading})|{_TESTING_FALLBACK}"


def _checklist(steps: Sequence[str]) -> str:
    return "\n".join("- [ ] " + str(s).strip().replace("\n", "\n  ") for s in steps)


# --- PR body -------------------------------------------------------------------


def _tracker(cfg: Any, repo: Optional[str]) -> Optional[str]:
    tracker = _meta(cfg, "project").get("tracker")
    return repo if tracker in (None, "auto") else str(tracker)


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and a.lower() == b.lower()


def _template_text(cfg: Any) -> Optional[str]:
    value = _meta(cfg, "standards").get("pr_template", "auto")
    root = getattr(cfg, "project_dir", None)
    if value in (None, "", "none", False):
        return None
    candidates = _TEMPLATE_PATHS if value == "auto" else (str(value),)
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            if root is None:
                continue
            path = Path(root) / path
        if path.is_file():
            return _gh.check_input_path(path, root).read_text(encoding="utf-8")
    return None


def pr_body(cfg: Any, issue_number: int, issue_title: str, description: str, testing_steps: Sequence[str],
            companions: Sequence[Dict[str, Any]] = (), refs: Sequence[str] = (),
            repo: Optional[str] = None) -> Tuple[str, str]:
    """(title, body). repo is the repository the PR opens in; it decides how the issue is referenced."""
    standards = _meta(cfg, "standards")
    title = str(standards.get("pr_title") or "{issue}: {issue_title}")
    title = title.replace("{issue_title}", issue_title).replace("{issue}", str(issue_number))

    tracker = _tracker(cfg, repo)
    local = _same(tracker, repo) or _meta(cfg, "project").get("tracker") in (None, "auto")
    issue_ref = f"#{issue_number}" if local else f"{tracker}#{issue_number}"
    keyword = standards.get("closing_keyword", "auto")
    if keyword in (None, "", "auto"):
        keyword = "Closes" if local else "Refs"
    qualified = []
    for ref in refs:
        ref = f"{tracker}{ref}" if re.fullmatch(r"#\d+", ref) and not local and tracker else str(ref)
        if ref != issue_ref and ref not in qualified:
            qualified.append(ref)
    references = [f"{keyword} {issue_ref}"]
    if qualified:
        references.append("**References:** " + ", ".join(qualified))

    references_block = "## References\n\n" + "\n\n".join(references)
    template = _template_text(cfg)
    if template is None:
        body = "\n\n".join([
            "## Summary\n\n" + description.strip(),
            "## Testing steps\n\n" + _checklist(testing_steps),
            "## Companion PRs\n\n" + _companion_lines(companions),
            references_block,
        ])
    else:
        body = template.replace("\r\n", "\n").rstrip()
        body = _set_section(body, _SUMMARY_HEADING, "## Summary", description.strip())
        body = _set_section(body, _steps_pattern(cfg), "## Testing steps", _checklist(testing_steps))
        if companions or _find_section(body.split("\n"), _COMPANION_HEADING):
            body = _set_section(body, _COMPANION_HEADING, "## Companion PRs", _companion_lines(companions))
        body = body.rstrip() + "\n\n" + references_block
    body += "\n\n" + _kind_marker(NEW_PR_KIND) + "\n"
    return title, apply_attribution(body, cfg)


def _edit_pr_body(gh: Any, repo: str, n: int, new_body: str) -> Any:
    with _tmp_file(new_body) as path:
        return gh.pr_edit_body(repo, n, path)


def companion_prs_upsert(gh: Any, repo: str, n: int, companions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Rewrite only the Companion PRs section of the PR body (add it when missing)."""
    old = (gh.pr(repo, n).get("body") or "").replace("\r\n", "\n")
    new = _set_section(old, _COMPANION_HEADING, "## Companion PRs", _companion_lines(companions))
    if new == old:
        return {"unchanged": True}
    result = _edit_pr_body(gh, repo, n, new)
    return {"updated": True, **({"dry_run": True} if isinstance(result, dict) and result.get("dry_run") else {})}


def testing_steps_append(gh: Any, repo: str, n: int, steps: Sequence[str], cfg: Any = None) -> Dict[str, Any]:
    """Add new checklist items to the end of the testing section (create it when missing)."""
    old = (gh.pr(repo, n).get("body") or "").replace("\r\n", "\n")
    lines = old.split("\n")
    pattern = _steps_pattern(cfg)
    span = _find_section(lines, pattern)
    if span is None:
        new = _set_section(old, pattern, "## Testing steps", _checklist(steps))
    else:
        start, _, end = span
        present = {re.sub(r"^\s*[-*]\s*\[[ xX]\]\s*", "", ln).strip() for ln in lines[start + 1:end]}
        fresh = [s for s in steps if str(s).strip() not in present]
        if not fresh:
            return {"unchanged": True}
        last = end
        while last > start + 1 and not lines[last - 1].strip():
            last -= 1
        gap = [""] if last == start + 1 else []
        new = "\n".join(lines[:last] + gap + _checklist(fresh).split("\n") + lines[last:])
    if new == old:
        return {"unchanged": True}
    result = _edit_pr_body(gh, repo, n, new)
    return {"updated": True, **({"dry_run": True} if isinstance(result, dict) and result.get("dry_run") else {})}


# --- PRs, reviews, labels --------------------------------------------------------


def open_pr(gh: Any, cfg: Any, repo: str, head: str, base: str, title: str, body: str, labels: Sequence[str],
            reviewers: Sequence[str], assignee: Optional[str], draft: bool = False) -> Dict[str, Any]:
    """Create the PR, then set the full label set, then request review. A dry run only records the writes."""
    del cfg  # the body is already composed; kept so every writer has the same leading arguments
    with _tmp_file(body) as path:
        created = gh.pr_create(repo, head, base, title, path, assignee=assignee, draft=draft)
    if isinstance(created, dict) and created.get("dry_run"):
        would = [created.get("would")]
        if labels:
            would.append(_ledger_only(gh, repo, ["api", "-X", "PUT", f"repos/{repo}/issues/<new PR>/labels"],
                                      f"set labels on {repo}#<new PR>", {"labels": list(labels)}))
        if reviewers:
            argv = ["api", "-X", "POST", f"repos/{repo}/pulls/<new PR>/requested_reviewers"]
            argv += [part for r in reviewers for part in ("-f", f"reviewers[]={_gh._login(r)}")]
            would.append(_ledger_only(gh, repo, argv, f"request review on {repo}#<new PR>"))
        return {"number": None, "url": None, "dry_run": True, "would": would}
    number = created.get("number")
    if number is None:
        raise WritebackMismatch("the PR was created but its number could not be read from gh")
    if labels:
        try:
            gh.labels_set(repo, number, list(labels))
        except _gh.ReadbackMismatch as exc:
            raise WritebackMismatch(str(exc)) from exc
    if reviewers:
        gh.request_review(repo, number, list(reviewers))
    return {"number": number, "url": created.get("url")}


def review_post(gh: Any, repo: str, n: int, verdict: str, body_text: str,
                inline: Optional[Sequence[Dict[str, Any]]] = None, labels_set: Optional[Sequence[str]] = None,
                run_id: str = "") -> Dict[str, Any]:
    """Post a review, set labels if asked, then read both back and raise WritebackMismatch on a difference."""
    if verdict not in _VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    event, state = _VERDICTS[verdict]
    run_marker = _run_marker(run_id)
    body = _ensure_markers(body_text, [_kind_marker(REVIEW_KIND), run_marker])
    comments = [{"path": c["path"], "line": c["line"], "body": c["body"]} for c in inline or []]
    with _tmp_file(body) as body_path:
        if comments:
            with _tmp_file(json.dumps(comments), ".json") as comments_path:
                result = gh.review_post(repo, n, event, body_path, comments_json=comments_path,
                                        allow_approve=(verdict == "approve"))
        else:
            result = gh.review_post(repo, n, event, body_path, allow_approve=(verdict == "approve"))
    if labels_set is not None:
        try:
            gh.labels_set(repo, n, list(labels_set))
        except _gh.ReadbackMismatch as exc:
            raise WritebackMismatch(str(exc)) from exc
    if gh.dry_run or (isinstance(result, dict) and result.get("dry_run")):
        return {"dry_run": True, "event": event}
    if not writes.valid_slug(repo):
        raise ValueError(f"invalid repo {repo!r}")
    reviews = gh.read(["api", f"repos/{repo}/pulls/{_gh._num(n)}/reviews?per_page=100"]) or []
    mine = [r for r in reviews if run_marker in (r.get("body") or "")]
    if not mine:
        raise WritebackMismatch(f"review on {repo}#{n} not found after posting")
    if mine[-1].get("state") != state:
        raise WritebackMismatch(f"review on {repo}#{n} reads back as {mine[-1].get('state')}, wanted {state}")
    if labels_set is not None:
        actual = gh.labels(repo, n)
        if set(actual) != set(labels_set):
            raise WritebackMismatch(f"labels on {repo}#{n} read back as {sorted(actual)}, wanted {sorted(labels_set)}")
    return {"event": event, "state": state}


def label_swap(gh: Any, repo: str, n: int, remove: Sequence[str], add: Sequence[str],
               current: Sequence[str]) -> List[str]:
    """Compute the full label set (remove, then add, keep the rest) and set it with one call."""
    result = [name for name in current if name not in set(remove)]
    result += [name for name in add if name not in result]
    gh.labels_set(repo, n, result)
    return result


def label_sync(gh: Any, repo: str, n: int, *, set_to: Optional[Sequence[str]] = None,
               add: Sequence[str] = (), remove: Sequence[str] = (), check: bool = False) -> Dict[str, Any]:
    """Make an item's labels match: set_to replaces the whole set, else add and remove edit it.

    check=True only reports. Returns {repo, number, in_sync, changed, labels (current, or the new set)}.
    """
    current = gh.labels(repo, n)
    have = {x.lower() for x in current}
    if set_to is not None:
        in_sync = have == {x.lower() for x in set_to}
    else:
        in_sync = all(a.lower() in have for a in add) and not any(r.lower() in have for r in remove)
    if check or in_sync:
        return {"repo": repo, "number": n, "in_sync": in_sync, "changed": False, "labels": current}
    if set_to is not None:
        gh.labels_set(repo, n, list(set_to))
        new = list(set_to)
    else:
        new = label_swap(gh, repo, n, remove, add, current)
    return {"repo": repo, "number": n, "in_sync": False, "changed": True, "labels": new}


# --- follow-up issues --------------------------------------------------------------


def followup(gh: Any, cfg: Any, title: str, body: str, source_number: Optional[int] = None,
             parent: Optional[int] = None, labels: Sequence[str] = (),
             source_repo: Optional[str] = None, repo: Optional[str] = None) -> Dict[str, Any]:
    """Create a follow-up issue in `repo` (default: the tracker); optionally tick it into a parent issue's task
    list. The write gate decides whether `repo` may be written."""
    tracker = repo or _tracker(cfg, gh.ctx.origin_repo)
    if not writes.valid_slug(tracker):
        raise ValueError("no tracker repository is known")
    text = body.rstrip()
    if source_number is not None:
        text += f"\n\nFollow-up from {source_repo or gh.ctx.origin_repo or tracker}#{int(source_number)}"
    text = _ensure_markers(text, [_kind_marker(FOLLOWUP_KIND)])

    def parse(result: Any) -> Dict[str, Any]:
        out = result.get("output", "") if isinstance(result, dict) else ""
        m = re.search(r"/issues/(\d+)", out)
        return {"url": out.splitlines()[-1] if out else "", "number": int(m.group(1)) if m else None}

    with _tmp_file(text) as path:
        argv = ["issue", "create", "-R", tracker, "--title", title, "--body-file", str(path)]
        argv += [part for label in labels for part in ("--label", label)]
        created = gh.write(WriteTarget.repo(tracker), argv, payload_path=path,
                           description=f"create follow-up in {tracker}", post=parse)
    dry = isinstance(created, dict) and bool(created.get("dry_run"))
    number = None if dry else created.get("number")
    out: Dict[str, Any] = {"number": number, "url": None if dry else created.get("url")}
    if dry:
        out["dry_run"] = True
    elif number is None:
        raise WritebackMismatch("the follow-up was created but its number could not be read from gh")
    if parent is not None:
        item = f"- [ ] #{number}" if number is not None else "- [ ] #<new follow-up>"
        old = (gh.issue(tracker, parent).get("body") or "").replace("\r\n", "\n")
        with _tmp_file(old.rstrip() + "\n" + item + "\n") as path:
            gh.issue_edit_body(tracker, parent, path)
        out["parent"] = parent
    return out


# --- lint ----------------------------------------------------------------------------

_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.S)
_BARE_REF_RE = re.compile(r"(?<![\w/&#])#(\d+)\b")
_PATH_RE = re.compile("(?:/" + "Users/|/home/[^/\\s]+/|[A-Za-z]:\\\\" + "Users\\\\)")
_AI_RES = (re.compile(r"\bAI\b"), re.compile("\\bCl" + "aude\\b", re.I), re.compile(r"\bmodels?\b", re.I))


def check_text(body: str, tracker: Optional[str] = None, repo: Optional[str] = None, attribution_text: str = "",
               is_summary: bool = False) -> List[str]:
    """Problems with a text bound for GitHub; empty when clean.

    A bare #N is fine only when tracker and repo are the same repository. attribution_text is the
    configured attribution, the one place an assistant may be named.
    """
    problems: List[str] = []
    text = body.replace("\r\n", "\n")
    if attribution_text.strip():
        text = text.replace(attribution_text.strip(), "")
    if is_summary:
        first = next((ln for ln in text.split("\n") if ln.strip()), "")
        if not first.startswith("**TL;DR:**"):
            problems.append("summary does not start with a TL;DR line")
    blank = lambda m: "\n" * m.group(0).count("\n")  # noqa: E731 - keeps line numbers stable
    scan = re.sub(r"<!--.*?-->", blank, text, flags=re.S)
    ref_lines = _CODE_RE.sub(blank, scan).split("\n")
    for number, line in enumerate(scan.split("\n"), 1):
        if not _same(tracker, repo):
            for m in _BARE_REF_RE.finditer(ref_lines[number - 1]):
                problems.append(f"line {number}: bare #{m.group(1)}; write owner/repo#{m.group(1)}")
        if _PATH_RE.search(line):
            problems.append(f"line {number}: local file path")
        for pattern in _AI_RES:
            m = pattern.search(line)
            if m:
                problems.append(f"line {number}: mentions {m.group(0)!r}")
    for m in re.finditer(r"<details[^>]*>", text):
        rest = text[m.end():]
        s = re.match(r"\s*<summary>.*?</summary>", rest, re.S)
        if not (rest[s.end():] if s else rest).startswith("\n\n"):
            problems.append(f"line {text.count(chr(10), 0, m.start()) + 1}: <details> needs a blank line after it")
    return problems
