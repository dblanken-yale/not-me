"""`Stack on: <owner/repo#N>`: an issue that may branch from an open PR's head branch.

"Gathers, never decides": this reads the line out of a trusted issue body and returns a fact. The model never
parses an issue body for it. Only a body whose author the roster trusts counts, and the named PR must be in a
repo not-me may touch, open, not a draft, from a trusted author, and from a branch on that repo (not a fork).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from .gh import GhClient, GhError, login_of

# One whole line: Stack on: owner/name#N, or an issue or pull request URL of that repo.
_LINE = re.compile(r"^[ \t]*Stack on:[ \t]*(?:https?://github\.com/(?P<urlrepo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)"
                   r"/(?:pull|issues)/|(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#)(?P<n>[0-9]+)[ \t]*$",
                   re.MULTILINE | re.IGNORECASE)
_ANY_LINE = re.compile(r"^[ \t]*Stack on:", re.MULTILINE | re.IGNORECASE)


def parse_refs(body: Optional[str]) -> Dict[str, Any]:
    """{refs: [(repo, number)] in order without repeats, lines: how many `Stack on:` lines the body has}."""
    text = body or ""
    refs: List[Any] = []
    for m in _LINE.finditer(text):
        ref = (m.group("repo") or m.group("urlrepo"), int(m.group("n")))
        if ref not in refs:
            refs.append(ref)
    return {"refs": refs, "lines": len(_ANY_LINE.findall(text))}


def _held(ref: str, reason: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ref": ref, "pr": None, "head": None, "base": None, "state": None, "draft": None,
                           "author": None, "author_trusted": None, "stacks": False, "ready": False,
                           "reason": reason}
    out.update(extra)
    return out


def _read_pr(gh: GhClient, repo: str, n: int) -> Optional[Dict[str, Any]]:
    try:
        data = gh.pr(repo, n)
    except (GhError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("headRefName") else None


def _linked_prs(gh: GhClient, repo: str, n: int) -> Optional[List[Dict[str, Any]]]:
    """The PRs GitHub links to issue n as closing it (read each one); None when the issue cannot be read."""
    try:
        issue = gh.read(["issue", "view", str(int(n)), "-R", repo, "--json", "closedByPullRequestsReferences"])
    except (GhError, ValueError):
        return None
    if not isinstance(issue, dict):
        return None
    out = []
    for ref in issue.get("closedByPullRequestsReferences") or []:
        slug = ((ref.get("repository") or {}).get("nameWithOwner") if isinstance(ref, dict) else None) or repo
        number = ref.get("number") if isinstance(ref, dict) else None
        if isinstance(number, int) and slug.lower() == repo.lower():
            pr = _read_pr(gh, repo, number)
            if pr is not None:
                out.append(pr)
    return out


def stack_fact(gh: GhClient, *, body: Optional[str], author_trusted: bool, tracker: str, stacking: str,
               trusted: Callable[[Optional[str]], bool],
               blocked_by: Callable[[], Optional[Sequence[Dict[str, Any]]]]) -> Optional[Dict[str, Any]]:
    """The `Stack on:` fact of an issue body, or None when there is no line to honour (no line, or an author the
    roster does not trust: that line is ignored).

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
        return _held("-", "the body has more than one Stack on line, or one that is not owner/repo#N: "
                          "name exactly one open PR")
    repo, n = parsed["refs"][0]
    ref = f"{repo}#{n}"
    if repo.lower() not in gh.ctx.allowed_repos_lower:
        return _held(ref, f"{repo} is not an allowed repo; run /not-me:allow-repo {repo}")
    pr = _read_pr(gh, repo, n)
    if pr is None:  # not a PR: an issue whose single open PR is used
        linked = _linked_prs(gh, repo, n)
        if linked is None:
            return _held(ref, f"{ref} could not be read")
        open_prs = [p for p in linked if p.get("state") == "OPEN"]
        merged = [p for p in linked if p.get("state") == "MERGED"]
        if len(open_prs) > 1:
            return _held(ref, f"{ref} has more than one open PR; name the one to stack on")
        pr = open_prs[0] if open_prs else (merged[0] if merged else None)
        if pr is None:
            return _held(ref, f"{ref} has no open PR to stack on")
    number, state = pr.get("number"), pr.get("state")
    out = _held(ref, "", pr=f"{repo}#{number}", head=pr.get("headRefName"), base=pr.get("baseRefName"),
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
    deps = blocked_by()
    if deps is None:
        return dict(out, reason="the issue's blockers could not be read")
    named = {(repo.lower(), n), (repo.lower(), number)}
    others = [d for d in deps if str(d.get("state")).lower() == "open"
              and ((d.get("repo") or tracker).lower(), d.get("number")) not in named]
    if others:
        names = ", ".join(f"{d.get('repo') or tracker}#{d.get('number')}" for d in others)
        return dict(out, reason=f"also blocked by {names}, still open")
    return dict(out, stacks=True, ready=True, reason=None)
