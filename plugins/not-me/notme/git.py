"""Thin git wrappers: one subprocess call each, parsed results, never a force flag."""
from __future__ import annotations

import fnmatch
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence

from . import paths
from .redact import redact

_BRANCH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


class GitError(Exception):
    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str = ""):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr = redact(stderr)
        super().__init__(f"git {' '.join(self.cmd)} failed (exit {returncode})")


class ProtectedBranchError(Exception):
    def __init__(self, branch: str):
        self.branch = branch
        super().__init__(f"refusing to push protected branch {branch!r}")


def _run(args: Sequence[str], cwd: Any = None, check: bool = True) -> "subprocess.CompletedProcess[str]":
    env = {**os.environ, "LC_ALL": "C"}  # stable output; never parse localised text
    proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc


def valid_branch(name: str) -> str:
    """`name` when it is a plain branch name; ValueError (a usage error) otherwise."""
    return _branch(name)


def _branch(name: str) -> str:
    """A plain branch name: no leading dash or plus, no refspec colon, no spaces."""
    if not isinstance(name, str) or not _BRANCH_RE.fullmatch(name) or ".." in name:
        raise ValueError(f"invalid branch name {name!r}")
    return name


def current_branch(cwd: Any = None) -> Optional[str]:
    """The checked-out branch, or None when HEAD is detached."""
    return _run(["branch", "--show-current"], cwd).stdout.strip() or None


def is_clean(cwd: Any = None) -> bool:
    return _run(["status", "--porcelain"], cwd).stdout.strip() == ""


def origin_url(cwd: Any = None) -> Optional[str]:
    """The origin remote URL, or None when there is no origin (or git fails)."""
    return paths.run_git(["remote", "get-url", "origin"], cwd or os.getcwd())


def default_branch(gh: Any = None, origin_slug: Optional[str] = None, cwd: Any = None) -> str:
    """origin/HEAD, else the repo's default branch from GitHub (if a client is given), else main.

    origin_slug (owner/name) skips the extra git call that finds it.
    """
    proc = _run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd, check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().split("/", 1)[-1]
    if gh is not None:
        from . import writes
        from .gh import GhError
        slug = origin_slug or writes.parse_origin(origin_url(cwd))
        if slug:
            try:
                return gh.repo_default_branch(slug) or "main"
            except GhError:
                pass
    return "main"


def fetch(*refs: str, remote: str = "origin", cwd: Any = None) -> None:
    _run(["fetch", "--", _branch(remote), *[_branch(r) for r in refs]], cwd)


def remote_branch_sha(name: str, cwd: Any = None) -> Optional[str]:
    """The sha of origin's branch `name`, or None when origin has no such branch (ls-remote --exit-code exits 2)."""
    proc = _run(["ls-remote", "--exit-code", "--heads", "origin", _branch(name)], cwd, check=False)
    if proc.returncode == 2:
        return None
    if proc.returncode != 0:
        raise GitError(["ls-remote"], proc.returncode, proc.stderr)
    want = f"refs/heads/{name}"
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == want:
            return sha.strip()
    return None


def remote_tracking_sha(name: str, cwd: Any = None) -> Optional[str]:
    """The sha the local origin/<name> ref points at (no network), or None when there is no such ref."""
    proc = _run(["rev-parse", "-q", "--verify", f"refs/remotes/origin/{_branch(name)}^{{commit}}"], cwd, check=False)
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def merge_base_into_current(base: str, cwd: Any = None) -> bool:
    """Merge base into the current branch (never rebase). True when it stopped on conflicts."""
    proc = _run(["merge", "--ff", "--no-edit", _branch(base)], cwd, check=False)
    if proc.returncode == 0:
        return False
    if _run(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd, check=False).returncode == 0:
        return True
    raise GitError(["merge", base], proc.returncode, proc.stderr)


def push_dry_run(branch: str, cwd: Any = None) -> str:
    """Rehearse a push to a throwaway ref name; returns that ref. Nothing is sent."""
    ref = f"refs/heads/not-me-preflight-{secrets.token_hex(4)}"
    _run(["push", "--dry-run", "origin", f"{_branch(branch)}:{ref}"], cwd)
    return ref


def _push_urls(cwd: Any = None) -> List[str]:
    """Every URL `git push origin` would send to (pushurl and insteadOf rewrites included)."""
    return _run(["remote", "get-url", "--push", "--all", "origin"], cwd).stdout.split()


def push(branch: str, ctx: Any, cwd: Any = None, *, dry_run: bool = False, ledger: Any = None) -> None:
    """Push one branch to origin, after the write gate. No force flag, ever.

    dry_run: run every check, rehearse the push with --dry-run, record it in ledger (when given) and send
    nothing.

    The name is normalised first (a leading refs/heads/ or heads/ is stripped; HEAD and other refs/* are
    refused). ctx.protected_branches are fnmatch patterns ("release/*" works). The push is the explicit
    refspec refs/heads/<name>:refs/heads/<name>, so a remote.origin.push setting cannot redirect it, and
    every push URL of origin must be one repo that ctx may write to.
    """
    from . import writes
    from .gh import WriteBlocked

    name = _branch(branch)
    for prefix in ("refs/heads/", "heads/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if name in ("HEAD", "") or name.startswith("refs/"):
        raise ValueError(f"invalid branch name {branch!r}")
    patterns = ctx.protected_branches if ctx.rules.get("protected_branches", True) else ()
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
        raise ProtectedBranchError(name)
    urls = _push_urls(cwd)
    slugs = {writes.parse_origin(u) for u in urls}
    if not urls or None in slugs or len({s.lower() for s in slugs}) != 1:
        raise GitError(["push"], 1, f"origin push URLs do not all point at one GitHub repo: {len(urls)} url(s)")
    target = writes.WriteTarget.repo(next(iter(slugs)))
    decision = writes.check_write(target, ctx)
    if not decision.ok:
        raise WriteBlocked(decision, target)
    argv = ["push", "--", "origin", f"refs/heads/{name}:refs/heads/{name}"]
    if dry_run:
        push_dry_run(name, cwd)
        if ledger is not None:
            ledger.append("git push", target.describe(), ["git", *argv], description=f"push {name}")
        return
    _run(argv, cwd)


@dataclass(frozen=True)
class Status:
    branch: Optional[str]
    clean: bool
    default_branch: str
    origin: Optional[str]  # owner/name of a GitHub origin, else None


def status(cwd: Any = None) -> Status:
    """The checkout's branch (None when detached), cleanliness, default branch and GitHub origin repo."""
    from . import writes
    return Status(current_branch(cwd), is_clean(cwd), default_branch(None, None, cwd),
                  writes.parse_origin(origin_url(cwd)))


def stash_by_sha(cwd: Any = None) -> Optional[str]:
    """Stash tracked and untracked changes; returns the stash commit sha, None if nothing to stash."""
    if is_clean(cwd):
        return None
    _run(["stash", "push", "--include-untracked", "-m", "not-me"], cwd)
    return _run(["rev-parse", "stash@{0}"], cwd).stdout.strip()


def stash_restore(sha: str, drop: bool = True, cwd: Any = None) -> None:
    if not _SHA_RE.fullmatch(sha or ""):
        raise ValueError(f"invalid sha {sha!r}")
    _run(["stash", "apply", sha], cwd)
    if drop:
        shas: List[str] = _run(["stash", "list", "--format=%H"], cwd).stdout.split()
        if sha in shas:
            _run(["stash", "drop", f"stash@{{{shas.index(sha)}}}"], cwd)


def checkout_no_track(branch: str, base: str, cwd: Any = None) -> None:
    _run(["checkout", "--no-track", "-b", _branch(branch), _branch(base)], cwd)


def worktree_add_detached(path: Any, sha: str, cwd: Any = None) -> None:
    if not _SHA_RE.fullmatch(sha or ""):
        raise ValueError(f"invalid sha {sha!r}")
    _run(["worktree", "add", "--detach", "--", str(path), sha], cwd)


def worktree_remove(path: Any, cwd: Any = None) -> None:
    _run(["worktree", "remove", "--", str(path)], cwd)
