"""`Stack on: <owner/repo#N>`: an issue that may branch from an open PR's head branch.

"Gathers, never decides": this reads the line out of a trusted issue body and returns a fact. The model never
parses an issue body for it. Only a body whose author the roster trusts counts (and whose last editor it trusts),
and the named PR must be in this checkout's repo, open, not a draft, from a trusted author, and from a branch on
that repo (not a fork). `Based on:` and `On top of:` mean the same as `Stack on:`.

With queue.stacking `auto`, an issue with no such line may stack too: when every open blocker (GitHub's
blocked-by dependencies, plus `Blocked by:` or `Depends on:` lines in a trusted body) resolves to one open PR that
passes the same checks, the issue stacks on it; any other open blocker holds it (`auto_fact`).
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
# `Blocked by:` or `Depends on:` and a reference: a blocker named in the body (it also holds a declared line).
_BLOCKER = re.compile(rf"^[ \t]*(?:Blocked by|Depends on):[ \t]*(?:https?://github\.com/(?P<urlrepo>{_SLUG})"
                      rf"/(?:pull|issues)/|(?P<repo>{_SLUG})#)(?P<n>[0-9]+)[ \t]*$", re.MULTILINE | re.IGNORECASE)
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


def parse_blockers(body: Optional[str]) -> List[Tuple[str, int]]:
    """(repo, number) of each `Blocked by:` or `Depends on:` line, in order without repeats."""
    out: List[Tuple[str, int]] = []
    for m in _BLOCKER.finditer(_visible(body)):
        ref = (m.group("repo") or m.group("urlrepo"), int(m.group("n")))
        if ref not in out:
            out.append(ref)
    return out


def _held(ref: str, reason: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ref": ref, "pr": None, "head": None, "base": None, "state": None, "draft": None,
                           "author": None, "author_trusted": None, "stacks": False, "ready": False,
                           "reason": reason, "via": "declared"}
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


def last_editor(gh: GhClient, tracker: str, n: int) -> Tuple[bool, Optional[str]]:
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


def _judge_pr(gh: GhClient, ref: str, repo: str, pr: Dict[str, Any], stacking: str,
              trusted: Callable[[Optional[str]], bool]) -> Tuple[Dict[str, Any], bool]:
    """(fact, final): final is true when the PR alone decides (merged: a normal issue; or a reason it cannot be
    stacked on). Otherwise the fact holds the PR and the issue's other blockers decide."""
    pr_number, state = pr.get("number"), pr.get("state")
    out = _held(ref, "", pr=f"{repo}#{pr_number}", head=pr.get("headRefName"), base=pr.get("baseRefName"),
                state=state, draft=bool(pr.get("isDraft")), author=login_of(pr.get("author")))
    out["author_trusted"] = bool(trusted(out["author"]))
    reason = None
    if state == "MERGED":
        return dict(out, ready=True, reason=None), True  # a normal issue on the default branch
    if state != "OPEN":
        reason = f"{out['pr']} was closed without merging"
    elif stacking == "off":
        reason = "queue.stacking is off, so the issue waits for the merge"
    elif out["draft"]:
        reason = f"{out['pr']} is a draft"
    elif not out["author_trusted"]:
        reason = f"{out['pr']} was written by {out['author'] or 'an unknown author'}, who is not on the roster"
    elif pr.get("isCrossRepository"):
        reason = f"{out['pr']} comes from a fork, so its branch is not on {repo}"
    elif repo.lower() != (gh.ctx.origin_repo or "").lower():  # the branch is created from this checkout's origin
        reason = (f"{out['pr']} is in {repo}, not in this checkout's repo "
                  f"({gh.ctx.origin_repo or 'unknown'}); its branch cannot be used here")
    return (dict(out, reason=reason), True) if reason else (out, False)


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
    ok, editor = last_editor(gh, tracker, number)
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
    pr_number = pr.get("number")
    named.add((repo.lower(), pr_number))
    out, final = _judge_pr(gh, ref, repo, pr, stacking, trusted)
    if final:
        return out
    deps = blocked_by()
    if deps is None:
        return dict(out, reason="the issue's blockers could not be read")
    named |= _closed_issues(gh, repo, pr_number)
    others = [d for d in deps if str(d.get("state")).lower() == "open"
              and ((d.get("repo") or tracker).lower(), d.get("number")) not in named]
    if others:
        names = ", ".join(f"{d.get('repo') or tracker}#{d.get('number')}" for d in others)
        return dict(out, reason=f"also blocked by {names}, still open")
    # `Blocked by:` and `Depends on:` lines in the (trusted) body count too, unless they lead to the same PR
    for r, m in parse_blockers(body):
        if (r.lower(), m) in named:
            continue
        blocker, why = _blocker_pr(gh, r, m, True)
        if why or (blocker is not None and not (r.lower() == repo.lower() and blocker.get("number") == pr_number)):
            return dict(out, reason=f"also blocked by {r}#{m}, still open")
    return dict(out, stacks=True, ready=True, reason=None)


def _blocker_pr(gh: GhClient, repo: str, n: int, as_pr: bool) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(the open PR blocker repo#n resolves to, reason it holds the issue). Both None: it no longer blocks.
    as_pr: the reference may name a PR itself (a body line); GitHub dependencies always name issues."""
    ref = f"{repo}#{n}"
    if repo.lower() not in gh.ctx.allowed_repos_lower:
        return None, f"blocked by {ref}, and {repo} is not an allowed repo; run /not-me:allow-repo {repo}"
    pr = _read_pr(gh, repo, n) if as_pr else None
    if pr is not None:
        if pr.get("state") == "MERGED":
            return None, None
        return (pr, None) if pr.get("state") == "OPEN" else (None, f"blocked by {ref}, closed without merging")
    linked = _linked_prs(gh, repo, n)
    if linked is None:
        return None, f"blocker {ref} could not be read"
    state, prs = linked
    if state.upper() == "CLOSED":
        return None, None
    open_prs = [p for p in prs if p.get("state") == "OPEN"]
    if len(open_prs) > 1:
        return None, f"blocker {ref} has more than one open PR; name the one to stack on with a Stack on: line"
    if not open_prs:
        return None, f"blocked by {ref}, and {ref} has no open PR to stack on yet"
    return open_prs[0], None


def auto_fact(gh: GhClient, *, number: int, body: Optional[str], author_trusted: bool, tracker: str,
              trusted: Callable[[Optional[str]], bool],
              blocked_by: Callable[[], Optional[Sequence[Dict[str, Any]]]]) -> Optional[Dict[str, Any]]:
    """queue.stacking auto, for an issue with no declared line: the stack fact its open blockers give, or None
    when nothing open blocks it (or the blockers cannot be read: then nothing changes from declared).

    Blockers are GitHub's open blocked-by dependencies plus `Blocked by:` and `Depends on:` lines in a trusted
    body (whose last editor the roster trusts too). Each resolves to its single open PR (an issue) or itself (a
    PR); a merged PR or a closed issue no longer blocks. Ready to stack only when they all resolve to one PR that
    passes every check a declared line gets. The fact has the stack_fact shape with via "blockers"; ref is the
    first open blocker."""
    deps = blocked_by()
    if deps is None:
        return None
    refs: List[Tuple[str, int, bool]] = [((d.get("repo") or tracker), d["number"], False) for d in deps
                                         if str(d.get("state")).lower() == "open" and isinstance(d.get("number"), int)]
    lines = parse_blockers(body) if author_trusted else []
    if lines:
        ok, editor = last_editor(gh, tracker, number)
        first = f"{lines[0][0]}#{lines[0][1]}"
        if not ok:
            return _held(first, "who last edited the issue could not be read", via="blockers")
        if editor and not trusted(editor):
            return _held(first, f"the issue was last edited by {editor}, who is not on the roster", via="blockers")
    seen = {(r.lower(), n) for r, n, _ in refs}
    for r, n in lines:
        if (r.lower(), n) not in seen:
            seen.add((r.lower(), n))
            refs.append((r, n, True))
    found: Dict[Tuple[str, int], Tuple[str, Dict[str, Any]]] = {}
    first_ref = ""
    for repo, n, as_pr in refs:
        pr, reason = _blocker_pr(gh, repo, n, as_pr)
        if reason:
            return _held(f"{repo}#{n}", reason, via="blockers")
        if pr is not None:
            first_ref = first_ref or f"{repo}#{n}"
            found.setdefault((repo.lower(), pr.get("number")), (repo, pr))
    if not found:
        return None
    if len(found) > 1:
        names = ", ".join(f"{r}#{p.get('number')}" for r, p in found.values())
        return _held(first_ref, f"its open blockers have more than one open PR ({names}); it waits until one PR "
                                "covers them all", via="blockers")
    repo, pr = next(iter(found.values()))
    out, final = _judge_pr(gh, first_ref, repo, pr, "auto", trusted)
    out["via"] = "blockers"
    return out if final else dict(out, stacks=True, ready=True, reason=None)
