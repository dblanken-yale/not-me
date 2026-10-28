"""`Stack on: <owner/repo#N>`: an issue that may branch from an open PR's head branch.

"Gathers, never decides": this reads the line out of a trusted issue body and returns a fact. The model never
parses an issue body for it. Only a body whose author the roster trusts counts (and whose last editor it trusts),
and the named PR must be in this checkout's repo, open, not a draft, from a trusted author, and from a branch on
that repo (not a fork). `Based on:` and `On top of:` mean the same as `Stack on:`.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .gh import GhClient, GhError, login_of

_SLUG = r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"
# One whole line: a phrase, a colon, then owner/name#N or an issue or pull request URL.
_LINE = re.compile(rf"^[ \t]*(?:Stack on|Based on|On top of):[ \t]*(?:https?://github\.com/(?P<urlrepo>{_SLUG})"
                   rf"/(?:pull|issues)/|(?P<repo>{_SLUG})#)(?P<n>[0-9]+)[ \t]*$", re.MULTILINE | re.IGNORECASE)
# `Stack on:` is never prose, so a line that starts with it and is not a valid reference holds the issue.
# `Based on` and `On top of` count only when the reference is valid ("Based on feedback from ..." is prose).
_STACK_ON = re.compile(r"^[ \t]*Stack on:", re.MULTILINE | re.IGNORECASE)
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1[^\n]*$|\Z)", re.MULTILINE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_EDITOR_QUERY = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
                 "issue(number:{n}){editor{login}}}}")


def _visible(body: Optional[str]) -> str:
    """The body without fenced code blocks and HTML comments: a line inside either is not a declaration."""
    return _COMMENT.sub("", _FENCE.sub("", body or ""))


def parse_refs(body: Optional[str]) -> Dict[str, Any]:
    """{refs: [(repo, number)] in order without repeats, lines: how many declaration lines the body has}."""
    text = _visible(body)
    refs: List[Any] = []
    valid = list(_LINE.finditer(text))
    for m in valid:
        ref = (m.group("repo") or m.group("urlrepo"), int(m.group("n")))
        if ref not in refs:
            refs.append(ref)
    malformed = [m for m in _STACK_ON.finditer(text) if not any(v.start() == m.start() for v in valid)]
    return {"refs": refs, "lines": len(valid) + len(malformed)}


def _held(ref: str, reason: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ref": ref, "pr": None, "head": None, "base": None, "state": None, "draft": None,
                           "author": None, "author_trusted": None, "stacks": False, "ready": False,
                           "reason": reason}
    out.update(extra)
    return out


def _ref_repo(ref: Any, default: str) -> str:
    """owner/name of a gh reference ({repository: {name, owner: {login}}}), else default."""
    repo = ref.get("repository") if isinstance(ref, dict) else None
    repo = repo if isinstance(repo, dict) else {}
    owner = login_of(repo.get("owner")) if repo.get("owner") else ""
    return f"{owner}/{repo['name']}" if owner and repo.get("name") else default


def _read_pr(gh: GhClient, repo: str, n: int) -> Optional[Dict[str, Any]]:
    try:
        data = gh.pr(repo, n)
    except (GhError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("headRefName") else None


def _closed_issues(gh: GhClient, repo: str, n: int) -> Set[Tuple[str, int]]:
    """(repo, number) of the issues PR n closes. GitHub records dependencies between issues, so a blocker of
    the stacked issue is the issue the predecessor PR closes, never the PR itself. Empty when unreadable."""
    try:
        data = gh.read(["pr", "view", str(int(n)), "-R", repo, "--json", "closingIssuesReferences"])
    except (GhError, ValueError):
        return set()
    refs = data.get("closingIssuesReferences") if isinstance(data, dict) else None
    return {(_ref_repo(r, repo).lower(), r["number"]) for r in refs or []
            if isinstance(r, dict) and isinstance(r.get("number"), int)}


def _linked_prs(gh: GhClient, repo: str, n: int) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """(state of issue n, the PRs GitHub links to it as closing it, each read); None when it cannot be read."""
    try:
        issue = gh.read(["issue", "view", str(int(n)), "-R", repo, "--json", "state,closedByPullRequestsReferences"])
    except (GhError, ValueError):
        return None
    if not isinstance(issue, dict):
        return None
    out = []
    for ref in issue.get("closedByPullRequestsReferences") or []:
        number = ref.get("number") if isinstance(ref, dict) else None
        if isinstance(number, int) and _ref_repo(ref, repo).lower() == repo.lower():
            pr = _read_pr(gh, repo, number)
            if pr is not None:
                out.append(pr)
    return str(issue.get("state") or ""), out


def _editor(gh: GhClient, tracker: str, n: int) -> Tuple[bool, Optional[str]]:
    """(read ok, login of the last person to edit the issue body; None when it was never edited)."""
    owner, name = tracker.split("/", 1)
    query = _EDITOR_QUERY.replace("{n}", str(int(n)))
    try:
        data = gh.read(["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"])
    except GhError:
        return False, None
    issue = (((data or {}).get("data") or {}).get("repository") or {}).get("issue")
    if not isinstance(issue, dict):
        return False, None
    return True, login_of(issue.get("editor")) or None


def stack_fact(gh: GhClient, *, number: int, body: Optional[str], author_trusted: bool, tracker: str,
               stacking: str, trusted: Callable[[Optional[str]], bool],
               blocked_by: Callable[[], Optional[Sequence[Dict[str, Any]]]]) -> Optional[Dict[str, Any]]:
    """The declaration fact of issue `number`'s body, or None when there is no line to honour (no line, or an
    author the roster does not trust: that line is ignored).

    {ref, pr, head, base, state, draft, author, author_trusted (of the PR), stacks, ready, reason}. `stacks` is
    true when the issue should branch from `head` and target it; `ready` false means the issue waits, and
    `reason` says why in plain words. A merged predecessor gives ready true and stacks false: a normal issue.
    stacking is the queue.stacking value; blocked_by reads the issue's dependency blockers lazily.
    """
    if not author_trusted:
        return None
    parsed = parse_refs(body)
    if not parsed["lines"]:
        return None
    if len(parsed["refs"]) != 1 or parsed["lines"] != 1:
        return _held("-", "the body has more than one Stack on, Based on or On top of line, or a Stack on line "
                          "that is not owner/repo#N: name exactly one open PR")
    repo, n = parsed["refs"][0]
    ref = f"{repo}#{n}"
    if repo.lower() not in gh.ctx.allowed_repos_lower:
        return _held(ref, f"{repo} is not an allowed repo; run /not-me:allow-repo {repo}")
    ok, editor = _editor(gh, tracker, number)
    if not ok:
        return _held(ref, "who last edited the issue could not be read")
    if editor and not trusted(editor):
        return _held(ref, f"the issue was last edited by {editor}, who is not on the roster")
    named = {(repo.lower(), n)}
    pr = _read_pr(gh, repo, n)
    if pr is None:  # not a PR: an issue whose single open PR is used
        linked = _linked_prs(gh, repo, n)
        if linked is None:
            return _held(ref, f"{ref} could not be read")
        issue_state, prs = linked
        open_prs = [p for p in prs if p.get("state") == "OPEN"]
        merged = [p for p in prs if p.get("state") == "MERGED"]
        if len(open_prs) > 1:
            return _held(ref, f"{ref} has more than one open PR; name the one to stack on")
        pr = open_prs[0] if open_prs else (merged[0] if merged else None)
        if pr is None and issue_state.upper() == "CLOSED":  # done without a PR we can see: nothing to wait for
            return _held(ref, None, state="CLOSED", ready=True)
        if pr is None:
            return _held(ref, f"{ref} has no open PR to stack on")
    pr_number, state = pr.get("number"), pr.get("state")
    named.add((repo.lower(), pr_number))
    out = _held(ref, "", pr=f"{repo}#{pr_number}", head=pr.get("headRefName"), base=pr.get("baseRefName"),
                state=state, draft=bool(pr.get("isDraft")), author=login_of(pr.get("author")))
    out["author_trusted"] = bool(trusted(out["author"]))
    if state == "MERGED":
        return dict(out, ready=True, reason=None)  # a normal issue on the default branch
    if state != "OPEN":
        return dict(out, reason=f"{out['pr']} was closed without merging")
    if stacking != "declared":
        return dict(out, reason="queue.stacking is off, so the issue waits for the merge")
    if out["draft"]:
        return dict(out, reason=f"{out['pr']} is a draft")
    if not out["author_trusted"]:
        return dict(out, reason=f"{out['pr']} was written by {out['author'] or 'an unknown author'}, "
                                "who is not on the roster")
    if pr.get("isCrossRepository"):
        return dict(out, reason=f"{out['pr']} comes from a fork, so its branch is not on {repo}")
    origin = (gh.ctx.origin_repo or "").lower()
    if repo.lower() != origin:  # the branch is created from this checkout's origin
        return dict(out, reason=f"{out['pr']} is in {repo}, not in this checkout's repo "
                                f"({gh.ctx.origin_repo or 'unknown'}); its branch cannot be used here")
    deps = blocked_by()
    if deps is None:
        return dict(out, reason="the issue's blockers could not be read")
    named |= _closed_issues(gh, repo, pr_number)
    others = [d for d in deps if str(d.get("state")).lower() == "open"
              and ((d.get("repo") or tracker).lower(), d.get("number")) not in named]
    if others:
        names = ", ".join(f"{d.get('repo') or tracker}#{d.get('number')}" for d in others)
        return dict(out, reason=f"also blocked by {names}, still open")
    return dict(out, stacks=True, ready=True, reason=None)
