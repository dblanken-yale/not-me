"""Gather a PR's review feedback with trust filtering. It collects and labels; the model judges.

Trust rule: every item is filtered by its author's login BEFORE its body is read. The body of an
untrusted item is never returned, stored, or logged; only its login, URL and kind are kept (and a
count). Bot accounts (login ending in "[bot]", or GraphQL type Bot) are untrusted unless the roster
lists them by name. Every returned body travels with separate `author`, `tier` and `association`
fields, so trusted and untrusted text cannot be confused.

Change-request heuristic (`is_change_request`): a trusted issue comment is kept as a finding when its
text contains, as whole words, any of: please, could you, should, needs, change, fix, instead; or a "?"
anywhere. It is a net, not a verdict: the model decides finally what is actually being asked.

Endorsement (`endorsement`): a trusted comment whose own text (quote lines removed) holds both an
agreement word (correct, agree, yes, right) and a do-it word (do it, please implement, go ahead, apply)
gets `endorsement: true` and a fixed `endorsement_note`. Nothing is bound to an untrusted item: the
trusted author must restate the request in their own words, and quoted or untrusted text is never used.
Quote lines (`> ...`) are removed from every trusted body that is returned.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import claims
from .gh import GhClient, GhError, login_of, names_of
from .roster import Roster

MARKER_UPDATE = "<!-- not-me:update -->"
MARKER_NEW = "<!-- not-me:new -->"
ENDORSEMENT_NOTE = "trusted author must restate the request; quoted or untrusted text is never used"

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CHANGE_RE = re.compile(r"\b(?:please|could you|should|needs|change|fix|instead)\b", re.IGNORECASE)
_AGREE_RE = re.compile(r"\b(?:correct|agree|yes|right)\b", re.IGNORECASE)
_DOIT_RE = re.compile(r"\b(?:do it|please implement|go ahead|apply)\b", re.IGNORECASE)
_QUOTE_RE = re.compile(r"^\s*>")
_SAFE_BRANCH_RE = re.compile(r"^[0-9]+-[a-z0-9._/-]{0,100}$")
_CONCLUSION_RE = re.compile(r"^[A-Za-z_]{1,30}$")
_BRANCH_RE = re.compile(r"^(\d+)-")
_CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+((?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?)#(\d+)", re.IGNORECASE)
_XREF_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")

_PR_VIEW_FIELDS = "statusCheckRollup"
_REVIEW_STATES = ("CHANGES_REQUESTED", "COMMENTED")
_FEEDBACK_QUERY = (  # {n} is an int, formatted in; owner and name are -f variables
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){pullRequest(number:{n}){"
    "reviewThreads(first:100){nodes{id isResolved isOutdated comments(first:100){nodes{"
    "id databaseId author{login __typename} authorAssociation body path line url "
    "pullRequestReview{databaseId}}}}}"
    "reviews(first:100){nodes{id databaseId author{login __typename} authorAssociation body state url}}}}}"
)


# --- pure helpers ---

def parse_linked_issue(branch: Optional[str], body: Optional[str], tracker: Optional[str]) -> Optional[int]:
    """The issue number a PR is for. Precedence: branch prefix `<digits>-`, then `Closes|Fixes|Resolves`
    (`#N` or `tracker#N`), then a bare `tracker#N` reference. References to other repos are ignored."""
    m = _BRANCH_RE.match(branch or "")
    if m:
        return int(m.group(1))
    want = (tracker or "").lower()
    text = body or ""
    for m in _CLOSES_RE.finditer(text):
        if not m.group(1) or m.group(1).lower() == want:
            return int(m.group(2))
    for m in _XREF_RE.finditer(text):
        if want and m.group(1).lower() == want:
            return int(m.group(2))
    return None


def is_change_request(text: Optional[str]) -> bool:
    """True if text looks like it asks for something (see the module docstring)."""
    if not text:
        return False
    return "?" in text or bool(_CHANGE_RE.search(text))


def endorsement(text: Optional[str]) -> bool:
    """True if text holds both an agreement word and a do-it word."""
    return bool(text) and bool(_AGREE_RE.search(text)) and bool(_DOIT_RE.search(text))


# --- trust ---

def _listed(roster: Roster, login: str) -> bool:
    return any(m.login.lower() == login.lower() for m in roster.entries)


def is_bot(login: Optional[str], typename: Optional[str] = None) -> bool:
    return bool(login) and (login.endswith("[bot]") or (typename or "").lower() == "bot")


def trusted(roster: Roster, login: Optional[str], typename: Optional[str] = None) -> bool:
    if not login:
        return False
    if is_bot(login, typename) and not _listed(roster, login):
        return False
    return roster.is_trusted(login)


def tier_of(roster: Roster, login: Optional[str]) -> str:
    """lead, authority or member from the roster; "none" for anyone not on it."""
    if not login:
        return "none"
    low = login.lstrip("@").lower()
    if low in {x.lower() for x in roster.leads}:
        return "lead"
    for m in roster.entries:
        if m.login.lower() == low:
            return m.tier
    return "none"


def _dig(obj: Any, *keys: str) -> Any:
    for key in keys:
        obj = (obj or {}).get(key) if isinstance(obj, dict) else None
    return obj


def strip_quotes(text: Optional[str]) -> "tuple[str, int]":
    """text without quote lines (`> ...`, how GitHub quote-replies embed other people's text), and how
    many lines were removed. A trusted body may quote an untrusted one, so quotes never leave this module."""
    kept, removed = [], 0
    for line in (text or "").splitlines():
        if _QUOTE_RE.match(line):
            removed += 1
        else:
            kept.append(line)
    return "\n".join(kept).strip(), removed


def _clean(item: Dict[str, Any], key: str = "body") -> Dict[str, Any]:
    """{key: stripped body, quoted_lines_removed: n} for a trusted item."""
    body, removed = strip_quotes(item.get("body"))
    return {key: body, "quoted_lines_removed": removed}


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _repo_ok(repo: str) -> str:
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise ValueError(f"invalid repo {repo!r}")
    return repo


def _repo_names(repos: Optional[Iterable[Any]]) -> List[str]:
    names = []
    for item in repos or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


# --- bundle ---

@dataclass
class Bundle:
    repo: str
    number: int
    title: Optional[str]
    body: Optional[str]
    head_ref: Optional[str]
    head_sha: Optional[str]
    base: Optional[str]
    state: Optional[str]
    is_draft: bool
    is_cross_repository: bool
    author: Optional[str]
    author_tier: str
    author_trusted: bool
    labels: List[str]
    review_requests: List[str]
    review_decision: Optional[str]
    mergeable: Optional[str]
    linked_issue: Optional[Dict[str, Any]]
    companions: List[Dict[str, Any]] = field(default_factory=list)
    companions_skipped: List[Dict[str, Any]] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    head_ref_untrusted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_checks(checks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Check names and conclusions only, names cut to 80 chars: for PRs whose author sets those names."""
    out = []
    for c in checks:
        concl = c.get("conclusion") or c.get("bucket") or c.get("state")
        out.append({"name": str(c.get("name") or "")[:80],
                    "conclusion": concl if isinstance(concl, str) and _CONCLUSION_RE.match(concl) else None,
                    "untrusted": True})
    return out


def _checks(gh: GhClient, repo: str, n: int) -> List[Dict[str, Any]]:
    try:
        data = gh.read(["pr", "checks", str(int(n)), "-R", repo, "--json", "name,state,bucket,link,workflow"])
        if isinstance(data, list):
            return data
    except GhError:
        pass  # gh exits non-zero while checks are pending or failing; fall back to the rollup
    try:
        data = gh.read(["pr", "view", str(int(n)), "-R", repo, "--json", _PR_VIEW_FIELDS])
    except GhError:
        return []
    return list((data or {}).get("statusCheckRollup") or [])


def _linked_issue(gh: GhClient, roster: Roster, tracker: str, number: Optional[int]) -> Optional[Dict[str, Any]]:
    if number is None:
        return None
    try:
        issue = gh.issue(tracker, number)
    except (GhError, ValueError):
        return {"number": number, "trusted": False, "error": "unreadable"}
    login = _dig(issue, "author", "login")
    if not trusted(roster, login):
        return {"number": number, "trusted": False}
    return {"number": number, "trusted": True, "repo": tracker, "title": issue.get("title"),
            "body": issue.get("body"), "state": issue.get("state"), "author": login,
            "tier": tier_of(roster, login), "url": issue.get("url"),
            "labels": [x.get("name") for x in issue.get("labels") or []]}


def _companions(gh: GhClient, roster: Roster, repo: str, head_ref: Optional[str], repos: Sequence[str]):
    found: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    if not head_ref:
        return found, skipped
    for other in repos:
        if other.lower() == repo.lower():
            continue
        try:
            prs = gh.read(["pr", "list", "-R", _repo_ok(other), "--state", "open", "--head", head_ref, "--limit", "200",
                           "--json",
                           "number,title,headRefName,isCrossRepository,isDraft,author,url"]) or []
        except (GhError, ValueError):
            skipped.append({"repo": other, "number": None, "reason": "repo unreadable"})
            continue
        for pr in prs:
            if pr.get("headRefName") != head_ref:
                continue
            ref = {"repo": other, "number": pr.get("number")}
            login = _dig(pr, "author", "login")
            if pr.get("isCrossRepository"):
                skipped.append({**ref, "reason": "cross-repository (fork) PR"})
            elif not trusted(roster, login, _dig(pr, "author", "type")):
                skipped.append({**ref, "reason": "author not trusted"})
            else:
                found.append({**ref, "title": pr.get("title"), "url": pr.get("url"), "head_ref": head_ref,
                              "is_draft": bool(pr.get("isDraft")), "author": login,
                              "tier": tier_of(roster, login)})
    return found, skipped


def bundle(gh: GhClient, repo: str, n: int, roster: Roster, *, tracker: Optional[str] = None,
           repos: Optional[Iterable[Any]] = None) -> Bundle:
    """The PR's facts. tracker (default: repo) holds the linked issue; repos are the configured
    `repos[*]` (names or {name: ...} items) searched for companion PRs on the same branch."""
    pr = gh.pr(repo, n)
    tracker = tracker or repo
    login = _dig(pr, "author", "login")
    ok = trusted(roster, login, _dig(pr, "author", "type"))
    cross = bool(pr.get("isCrossRepository"))
    # An untrusted author's title and body are untrusted text: withheld. A fork's or an untrusted author's
    # branch name and check names are attacker-chosen too: neither feeds issue or companion lookup.
    body = pr.get("body") if ok else None
    head_ref = pr.get("headRefName")
    checks = _checks(gh, repo, n)
    reason = "untrusted PR author" if not ok else "cross-repository PR" if cross else None
    if reason:
        head_untrusted = True
        head_ref = head_ref if isinstance(head_ref, str) and _SAFE_BRANCH_RE.match(head_ref) else None
        linked: Optional[Dict[str, Any]] = {"trusted": False}
        companions: List[Dict[str, Any]] = []
        skipped = [{"repo": None, "number": None, "reason": reason}]
        checks = _safe_checks(checks)
    else:
        head_untrusted = False
        linked = _linked_issue(gh, roster, tracker, parse_linked_issue(head_ref, body, tracker))
        companions, skipped = _companions(gh, roster, repo, head_ref, _repo_names(repos))
    return Bundle(
        repo=repo, number=int(n), title=pr.get("title") if ok else None, body=body, head_ref=head_ref,
        head_sha=pr.get("headRefOid"), base=pr.get("baseRefName"), state=pr.get("state"),
        is_draft=bool(pr.get("isDraft")), is_cross_repository=cross,
        author=login, author_tier=tier_of(roster, login), author_trusted=ok,
        labels=[x.get("name") for x in pr.get("labels") or []],
        review_requests=[x.get("login") or x.get("slug") or x.get("name") for x in pr.get("reviewRequests") or []],
        review_decision=pr.get("reviewDecision"), mergeable=pr.get("mergeable"),
        linked_issue=linked, companions=companions, companions_skipped=skipped,
        checks=checks, head_ref_untrusted=head_untrusted)


# --- feedback ---

@dataclass
class Feedback:
    repo: str
    number: int
    findings: List[Dict[str, Any]] = field(default_factory=list)
    ignored_count: int = 0
    ignored: List[Dict[str, Any]] = field(default_factory=list)
    deduped_count: int = 0
    prior_summaries: List[Dict[str, Any]] = field(default_factory=list)
    last_marker_at: Optional[str] = None
    answered_since_marker: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def _ignore(self, login: Optional[str], url: Optional[str], kind: str) -> None:
        self.ignored.append({"login": login, "url": url, "kind": kind})
        self.ignored_count += 1


def _who(roster: Roster, login: Optional[str], assoc: Optional[str]) -> Dict[str, Any]:
    return {"author": login, "tier": tier_of(roster, login), "association": assoc}


def _threads(gh: GhClient, repo: str, n: int):
    owner, _, name = _repo_ok(repo).partition("/")
    query = _FEEDBACK_QUERY.replace("{n}", str(int(n)))
    data = gh.read(["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"])
    pull = _dig(data, "data", "repository", "pullRequest") or {}
    return (_dig(pull, "reviewThreads", "nodes") or []), (_dig(pull, "reviews", "nodes") or [])


def _thread_findings(fb: Feedback, roster: Roster, threads: Sequence[Dict[str, Any]]):
    for t in threads:
        if t.get("isResolved"):
            continue
        comments = _dig(t, "comments", "nodes") or []
        if not comments:
            continue
        root = comments[0]
        login = _dig(root, "author", "login")
        # Decide on the login only; the body is read after this check.
        if not trusted(roster, login, _dig(root, "author", "__typename")):
            fb._ignore(login, root.get("url"), "thread")
            for reply in comments[1:]:
                rlogin = _dig(reply, "author", "login")
                if not trusted(roster, rlogin, _dig(reply, "author", "__typename")):
                    continue
                cleaned = _clean(reply)
                endorsed = endorsement(cleaned["body"])
                if is_change_request(cleaned["body"]) or endorsed:
                    fb.findings.append({
                        "kind": "comment", "id": reply.get("databaseId"), "thread_id": t.get("id"),
                        **_who(roster, rlogin, reply.get("authorAssociation")), "url": reply.get("url"),
                        "thread_url": root.get("url"), "path": reply.get("path"), "line": reply.get("line"),
                        **cleaned, **_endorsed(endorsed)})
            continue
        replies = []
        for reply in comments[1:]:
            rlogin = _dig(reply, "author", "login")
            if trusted(roster, rlogin, _dig(reply, "author", "__typename")):
                replies.append({"author": rlogin, "tier": tier_of(roster, rlogin),
                                "association": reply.get("authorAssociation"), **_clean(reply)})
            else:
                fb._ignore(rlogin, reply.get("url"), "reply")
        fb.findings.append({
            "kind": "thread", "id": root.get("databaseId"), "thread_id": t.get("id"),
            **_who(roster, login, root.get("authorAssociation")), "url": root.get("url"),
            "path": root.get("path"), "line": root.get("line"), **_clean(root),
            "reply_to": root.get("databaseId"), "replies": replies, "is_outdated": bool(t.get("isOutdated")),
            "review_id": _dig(root, "pullRequestReview", "databaseId")})


def _endorsed(flag: bool) -> Dict[str, Any]:
    return {"endorsement": flag, **({"endorsement_note": ENDORSEMENT_NOTE} if flag else {})}


def _review_findings(fb: Feedback, roster: Roster, reviews: Sequence[Dict[str, Any]]):
    for r in reviews:
        state = r.get("state")
        if state not in _REVIEW_STATES:
            continue
        login = _dig(r, "author", "login")
        if not trusted(roster, login, _dig(r, "author", "__typename")):
            fb._ignore(login, r.get("url"), "review")
            continue
        cleaned = _clean(r)
        if state == "COMMENTED" and not cleaned["body"]:
            continue
        rid = r.get("databaseId")
        threads = [f["thread_id"] for f in fb.findings if f["kind"] == "thread" and rid is not None
                   and f.get("review_id") == rid]
        fb.findings.append({"kind": "review", "id": rid, "state": state,
                            **_who(roster, login, r.get("authorAssociation")), **cleaned,
                            "url": r.get("url"), "thread_ids": threads})


def _marker_kind(text: str, legacy: Sequence[str]) -> Optional[str]:
    if MARKER_UPDATE in text:
        return "update"
    if MARKER_NEW in text:
        return "new"
    if any(m and m in text for m in legacy):
        return "legacy"
    return None


def _comment_findings(fb: Feedback, roster: Roster, comments: Sequence[Dict[str, Any]],
                      legacy: Sequence[str], since_marker: Optional[str]):
    mine: List[Dict[str, Any]] = []
    for c in comments:
        login = c.get("author")
        if not trusted(roster, login):
            fb._ignore(login, c.get("url"), "comment")
            continue
        text = c.get("body") or ""
        marker = _marker_kind(text, legacy)
        if marker:
            fb.prior_summaries.append({"id": c.get("id"), "marker": marker, "author": login,
                                       "created_at": c.get("created_at"), "url": c.get("url")})
        else:
            mine.append(c)
    stamps = [s["created_at"] for s in fb.prior_summaries if s["created_at"]]
    fb.last_marker_at = max(stamps) if stamps else None
    cutoff = since_marker or fb.last_marker_at
    for c in mine:
        login = c.get("author")
        text, quoted = strip_quotes(c.get("body"))
        who = _who(roster, login, c.get("association"))
        if cutoff is None or (c.get("created_at") or "") > cutoff:
            fb.answered_since_marker.append({"id": c.get("id"), **who, "body": text, "quoted_lines_removed": quoted,
                                             "url": c.get("url"), "created_at": c.get("created_at")})
        endorsed = endorsement(text)
        if is_change_request(text) or endorsed:
            fb.findings.append({"kind": "comment", "id": c.get("id"), **who, "body": text,
                                "quoted_lines_removed": quoted, "url": c.get("url"), **_endorsed(endorsed)})


def _dedupe(fb: Feedback) -> None:
    """One finding per text across review bodies and comments (threads are never merged)."""
    first: Dict[str, Dict[str, Any]] = {}
    kept = []
    for f in fb.findings:
        key = _norm(f["body"] or "") if f["kind"] in ("review", "comment") else ""
        if key:  # empty bodies are never duplicates of each other
            if key in first:
                fb.deduped_count += 1
                if f["kind"] == "review" and first[key]["kind"] == "review":
                    first[key]["thread_ids"] += [t for t in f["thread_ids"] if t not in first[key]["thread_ids"]]
                continue
            first[key] = f
        kept.append(f)
    fb.findings = kept


def feedback(gh: GhClient, repo: str, n: int, roster: Roster, *, since_marker: Optional[str] = None,
             legacy_markers: Sequence[str] = ()) -> Feedback:
    """Findings from trusted authors only. since_marker is an ISO timestamp that overrides our newest
    marker's time when picking `answered_since_marker` (with neither, every trusted comment counts).
    legacy_markers is `markers.legacy` from the config."""
    fb = Feedback(repo=repo, number=int(n))
    threads, reviews = _threads(gh, repo, n)
    comments = gh.comments(repo, n)
    _thread_findings(fb, roster, threads)
    _review_findings(fb, roster, reviews)
    _comment_findings(fb, roster, comments, list(legacy_markers), since_marker)
    _dedupe(fb)
    return fb


# --- issue view and review candidates -------------------------------------------


def issue_view(gh: GhClient, repo: str, n: int, roster: Roster, operator: str, labels: Any) -> Dict[str, Any]:
    """An issue's facts. The title, body and comments of an author the roster does not trust are withheld, and
    an untrusted commenter's comments are always dropped (only counted). labels is a states.Labels."""
    issue = gh.issue(repo, n)
    author = login_of(issue.get("author"))
    ok = trusted(roster, author) or author.lower() == operator.lower()
    every = issue.get("comments") or []
    comments = [c for c in every if trusted(roster, login_of(c.get("author")), (c.get("author") or {}).get("type"))]
    names = names_of(issue.get("labels"))
    current = labels.current(names)
    return {"repo": repo, "number": issue.get("number"), "title": issue.get("title") if ok else None,
            "state": issue.get("state"), "url": issue.get("url"), "author": author, "author_trusted": ok,
            "labels": names, "status": current[0] if current else None,
            "assignees": [login_of(a) for a in issue.get("assignees") or []],
            "body": issue.get("body") if ok else None,
            "comments": [{"id": c.get("id"), "author": login_of(c.get("author")), "body": c.get("body")}
                         for c in comments] if ok else [],
            "comments_ignored": len(every) - len(comments)}


def review_candidates(gh: GhClient, repo: str, operator: str, roster: Roster,
                      label: str) -> "tuple[List[Dict[str, Any]], List[Dict[str, Any]]]":
    """(picks, skipped) among open PRs carrying `label`, lowest number first.

    A pick is a non-draft PR where the operator is a requested reviewer, the author is trusted and is not the
    operator, and something changed since the operator's last submitted review (a new head SHA, or a comment
    by the author after it). picks: {number, why, head_sha, title, url}; skipped: {number, reason}.
    """
    me = operator.lower()
    picks: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in sorted(gh.pr_list(repo, label=label), key=lambda p: p["number"]):
        n = item["number"]
        pr = gh.pr(repo, n)
        author = login_of(pr.get("author"))
        requested = {login_of(r).lower() for r in pr.get("reviewRequests") or []}
        reason = None
        if pr.get("isDraft"):
            reason = "draft"
        elif me not in requested:
            reason = "operator is not a requested reviewer"
        elif author.lower() == me:
            reason = "operator is the author"
        elif not trusted(roster, author, (pr.get("author") or {}).get("type")):
            reason = "author not trusted"
        why = None if reason else _review_reason(gh, repo, n, pr, author, me)
        if why is None:
            skipped.append({"number": n, "reason": reason or "unchanged since the last review"})
        else:
            picks.append({"number": n, "why": why, "head_sha": pr.get("headRefOid"), "title": pr.get("title"),
                          "url": item.get("url")})
    return picks, skipped


def _review_reason(gh: GhClient, repo: str, n: int, pr: Dict[str, Any], author: str, me: str) -> Optional[str]:
    """Why the operator should look again, or None when nothing changed since their last review."""
    reviews = [r for r in gh.reviews(repo, n)
               if login_of(r.get("user")).lower() == me and r.get("state") != "PENDING"]
    if not reviews:
        return "never reviewed"
    last = max(reviews, key=lambda r: (r.get("submitted_at") or "", r.get("id") or 0))
    if last.get("commit_id") != pr.get("headRefOid"):
        return "new commits since the last review"
    since = claims.parse_ts(last.get("submitted_at"))
    for c in gh.comments(repo, n):
        if (c.get("author") or "").lower() == author.lower() and \
                (since is None or (claims.parse_ts(c.get("created_at")) or since) > since):
            return "author commented since the last review"
    return None
