"""The write gate: which targets not-me may change, and the global allow list.

Shared by the GitHub client and (later) the pre-tool hook. It gathers facts and
answers allowed or blocked; it never decides what work to do.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from . import frontmatter, git, paths
from .dryrun import utc_now

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*$")  # no leading "-" in either part
GUARD_RULES = ("other_repo", "protected_branches", "force_push", "destructive_git")
ALLOWLIST_FILE = "allowed-repos.json"

_SCHEMES = ("http", "https", "ssh", "git")
_PATH_SCHEMES = ("http", "https")  # web URLs gh reads, where allow_path applies
_BAD_NETLOC = re.compile(r"[\s#?\\]")
_REPO_PATH = re.compile(r"^/(?P<o>[^/]+)/(?P<n>[^/]+?)(?:\.git)?/?$")
_REPO_PATH_PREFIX = re.compile(r"^/(?P<o>[^/]+)/(?P<n>[^/]+?)(?:\.git)?(?:/.*)?$")
_SCP = re.compile(r"^(?P<pre>[^:]+):(?P<o>[^/:\s]+)/(?P<n>[^/:\s]+?)(?:\.git)?/?$")
_BAD_SCP_PREFIX = re.compile(r"[\s#?/\\]")


def valid_slug(value: Any) -> bool:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        return False
    return not any(part in (".", "..") for part in value.split("/"))


def _github_host(host: Optional[str]) -> bool:
    return bool(host) and host.lower() in {"github.com", os.environ.get("GH_HOST", "github.com").lower()}


def _slug_or_none(owner: str, name: str) -> Optional[str]:
    slug = f"{owner}/{name}"
    return slug if valid_slug(slug) else None


def parse_origin(url: Optional[str], allow_path: bool = False) -> Optional[str]:
    """owner/name from an https, scp-style ssh or ssh:// remote URL on github.com (or $GH_HOST); else None.

    The host is the one git and curl would contact (urlsplit), so userinfo tricks such as
    https://evil.example#@github.com/o/n do not pass. A scheme URL's path must be exactly
    /owner/name (optional .git), with no query or fragment.

    allow_path also takes an https URL with more after the repo (https://github.com/o/n/pull/3?x).
    Leave it off for URLs git itself fetches or pushes, where a path such as /../x changes the repo.
    """
    if not url:
        return None
    url = url.strip()
    if any(c.isspace() for c in url):
        return None
    if "://" in url:
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - raises ValueError on a bad port
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme not in _SCHEMES or _BAD_NETLOC.search(parts.netloc) or not _github_host(parts.hostname):
            return None
        loose = allow_path and scheme in _PATH_SCHEMES
        if not loose and (parts.query or parts.fragment or "?" in url or "#" in url):
            return None
        m = (_REPO_PATH_PREFIX if loose else _REPO_PATH).match(parts.path)
        return _slug_or_none(m.group("o"), m.group("n")) if m else None
    m = _SCP.match(url)
    if not m or _BAD_SCP_PREFIX.search(m.group("pre")) or m.group("pre").count("@") > 1:
        return None
    if not _github_host(m.group("pre").rpartition("@")[2]):
        return None
    return _slug_or_none(m.group("o"), m.group("n"))


# --- targets ---------------------------------------------------------------


@dataclass(frozen=True)
class WriteTarget:
    kind: str  # repo | board | local
    owner: Optional[str] = None
    name: Optional[str] = None
    number: Optional[int] = None
    path: Optional[str] = None

    @classmethod
    def repo(cls, slug: str) -> "WriteTarget":
        owner, _, name = slug.partition("/")
        return cls("repo", owner=owner, name=name)

    @classmethod
    def board(cls, owner: str, number: int) -> "WriteTarget":
        return cls("board", owner=owner, number=int(number))

    @classmethod
    def local(cls, path: Any) -> "WriteTarget":
        return cls("local", path=str(path))

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    def describe(self) -> str:
        if self.kind == "repo":
            return self.slug
        if self.kind == "board":
            return f"board {self.owner}/{self.number}"
        return f"local {self.path}"


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""
    allow_cmd: Optional[str] = None
    rule: Optional[str] = None


# --- allow list file -------------------------------------------------------


def allowlist_path(home: Any) -> Path:
    return Path(home) / ALLOWLIST_FILE


def _read_allowlist(home: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """The parsed file and a warning; a missing file is empty, a bad one is ignored."""
    path = allowlist_path(home)
    if not path.exists():
        return {"version": 1, "projects": {}}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "projects": {}}, f"{ALLOWLIST_FILE} is not valid JSON; ignoring it"
    if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
        return {"version": 1, "projects": {}}, f"{ALLOWLIST_FILE} has an unexpected shape; ignoring it"
    data.setdefault("projects", {})
    return data, None


def _scope_allow(data: Mapping[str, Any], scope: str) -> List[Any]:
    """The allow entries of every project key matching scope (owner/name compares case-insensitively)."""
    out: List[Any] = []
    for key, block in data.get("projects", {}).items():
        allow = block.get("allow") if isinstance(block, dict) else None
        if isinstance(key, str) and key.lower() == scope.lower() and isinstance(allow, list):
            out += allow
    return out


def _entries(data: Mapping[str, Any], scope: str) -> List[dict]:
    return [e for e in _scope_allow(data, scope) if isinstance(e, dict) and valid_slug(e.get("repo"))]


def _write_allowlist(home: Any, data: Mapping[str, Any]) -> None:
    path = allowlist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".allowed-repos.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _scope(project: Optional[str], everywhere: bool) -> str:
    if everywhere:
        return "*"
    if not valid_slug(project):
        raise ValueError(f"invalid project {project!r}: expected owner/name")
    return str(project)


def _load_for_edit(home: Any) -> Dict[str, Any]:
    data, warning = _read_allowlist(home)
    if warning:
        raise ValueError(f"{warning}; fix or delete {ALLOWLIST_FILE} first")
    return data


BOARD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[0-9]+")


def _entry_key(repo: Optional[str], board: Optional[str]) -> Tuple[str, str]:
    """("repo", slug) or ("board", owner/number); exactly one must be given and valid."""
    if (repo is None) == (board is None):
        raise ValueError("give exactly one of repo or board")
    if board is not None:
        if not isinstance(board, str) or not BOARD_RE.fullmatch(board):
            raise ValueError(f"invalid board {board!r}: expected owner/number")
        return "board", board
    if not valid_slug(repo):
        raise ValueError(f"invalid repo {repo!r}: expected owner/name, no URLs or wildcards")
    return "repo", str(repo)


def allowlist_add(home: Any, project: Optional[str], repo: Optional[str] = None, everywhere: bool = False,
                  board: Optional[str] = None) -> bool:
    """Allow a repo (or, with board="owner/number", a project board) for one project or all (everywhere).

    True if it was newly added.
    """
    key, value = _entry_key(repo, board)
    scope = _scope(project, everywhere)
    data = _load_for_edit(home)
    block = data["projects"].setdefault(scope, {"allow": []})
    allow = block.setdefault("allow", [])
    if any(isinstance(e, dict) and str(e.get(key, "")).lower() == value.lower() for e in allow):
        return False
    allow.append({key: value, "added": utc_now()})
    _write_allowlist(home, data)
    return True


def allowlist_remove(home: Any, project: Optional[str], repo: Optional[str] = None, everywhere: bool = False,
                     board: Optional[str] = None) -> bool:
    key, value = _entry_key(repo, board)
    scope = _scope(project, everywhere)
    data = _load_for_edit(home)
    block = data["projects"].get(scope)
    allow = block.get("allow") if isinstance(block, dict) else None
    if not isinstance(allow, list):
        return False
    kept = [e for e in allow if not (isinstance(e, dict) and str(e.get(key, "")).lower() == value.lower())]
    if len(kept) == len(allow):
        return False
    block["allow"] = kept
    _write_allowlist(home, data)
    return True


def _board_entries(data: Mapping[str, Any], scope: str) -> List[str]:
    return [e["board"] for e in _scope_allow(data, scope) if isinstance(e, dict) and isinstance(e.get("board"), str)
            and BOARD_RE.fullmatch(e["board"])]


def allowlist_list(home: Any, project: Optional[str] = None) -> List[Dict[str, str]]:
    """Entries as {scope, repo|board, added}; for a project: its own plus the "*" ones. Bad file = empty."""
    data, _ = _read_allowlist(home)
    out: List[Dict[str, str]] = []
    for scope in sorted(s for s in data["projects"] if project is None or s in (project, "*")):
        block = data["projects"][scope]
        allow = block.get("allow") if isinstance(block, dict) else None
        for e in allow if isinstance(allow, list) else []:
            if not isinstance(e, dict):
                continue
            if valid_slug(e.get("repo")):
                out.append({"scope": scope, "repo": e["repo"], "added": str(e.get("added", ""))})
            elif isinstance(e.get("board"), str) and BOARD_RE.fullmatch(e["board"]):
                out.append({"scope": scope, "board": e["board"], "added": str(e.get("added", ""))})
    return out


# --- the repo layer on the default branch ------------------------------------

REPO_LAYER_PROJECT = ".claude/not-me/project.md"
_ORIGIN_REF = "refs/remotes/origin/"


def default_branch_names(project_dir: Any) -> Dict[str, FrozenSet[str]]:
    """The repos and board the repo layer names as committed on the default branch, lowercased by key: tracker,
    repos (their names) and board ("owner/number"). The default branch is the commit refs/remotes/origin/HEAD
    points at in project_dir's repository; the file is read from that commit, never from the working tree, so a
    checked-out PR branch or an uncommitted edit adds nothing. A missing origin/HEAD, a missing file or one that
    does not parse gives empty sets, so nothing extra counts."""
    out: Dict[str, FrozenSet[str]] = {"tracker": frozenset(), "repos": frozenset(), "board": frozenset()}
    if project_dir is None:
        return out
    ref = paths.run_git(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], project_dir)
    if not ref or not ref.startswith(_ORIGIN_REF) or len(ref) == len(_ORIGIN_REF):
        return out
    text = paths.run_git(["cat-file", "blob", f"{ref}:{REPO_LAYER_PROJECT}"], project_dir)
    if not text:
        return out
    try:
        meta, _ = frontmatter.parse(text, source=REPO_LAYER_PROJECT)
    except Exception:  # FrontmatterError or anything a hostile file provokes: nothing counts
        return out
    tracker = meta.get("tracker")
    repos = meta.get("repos") if isinstance(meta.get("repos"), list) else []
    board = ((meta.get("queue") or {}).get("board") or {}) if isinstance(meta.get("queue"), dict) else {}
    board = board if isinstance(board, dict) else {}
    number = board.get("number")
    out["tracker"] = frozenset({tracker.lower()} if valid_slug(tracker) else ())
    out["repos"] = frozenset(str(n).lower() for n in (r.get("name") if isinstance(r, dict) else r for r in repos)
                             if valid_slug(n))
    if board.get("owner") and isinstance(number, int) and not isinstance(number, bool):
        out["board"] = frozenset({f"{board['owner']}/{number}".lower()})
    return out


# --- guard context ---------------------------------------------------------


@dataclass(frozen=True)
class GuardContext:
    origin_repo: Optional[str] = None
    allowed_repos: Tuple[str, ...] = ()
    board: Optional[Tuple[str, int]] = None
    protected_branches: Tuple[str, ...] = ()
    rules: Mapping[str, bool] = field(default_factory=lambda: {r: True for r in GUARD_RULES})
    mode: str = "dry-run"
    warnings: Tuple[str, ...] = ()
    project_dir: Optional[Path] = None
    dry_run: Optional[bool] = None  # None = decide from mode and $NOT_ME_DRY_RUN when built
    allowed_repos_lower: FrozenSet[str] = field(default=frozenset(), init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_repos_lower", frozenset(a.lower() for a in self.allowed_repos))
        if self.dry_run is None:
            object.__setattr__(self, "dry_run", self.mode == "dry-run" or os.environ.get("NOT_ME_DRY_RUN") == "1")

    @classmethod
    def from_config(cls, cfg: Any, origin_url: Optional[str] = None, checkout: Any = None) -> "GuardContext":
        """Build from a loaded Config; origin_url overrides asking git (for tests)."""
        meta = cfg.meta.get("project", {}) if getattr(cfg, "meta", None) else {}
        project_dir = Path(checkout) if checkout else cfg.project_dir
        if origin_url is None and project_dir is not None:
            origin_url = git.origin_url(project_dir)
        origin = parse_origin(origin_url)
        warnings: List[str] = []

        home = cfg.home or paths.home_dir()
        data, warning = _read_allowlist(home)
        if warning:
            warnings.append(warning)
        confirmed = [e["repo"] for scope in ([origin] if origin else []) + ["*"] for e in _entries(data, scope)]
        confirmed_lower = {c.lower() for c in confirmed}
        # the repo layer as merged to the default branch, read only when a repo-layer name needs it
        merged: List[Dict[str, Any]] = []

        def on_default_branch() -> Dict[str, Any]:
            if not merged:
                merged.append(default_branch_names(getattr(cfg, "project_dir", None) or project_dir))
            return merged[0]

        def from_repo_layer(key: str) -> bool:
            return (getattr(cfg, "sources", {}).get(f"project.{key}") or {}).get("layer") == "repo"

        def untrusted(slug: Any, key: str) -> bool:
            """A repo-layer name (untrusted as read: the working tree may hold any branch) that is neither on the
            default branch under the same key nor confirmed in the global allow list."""
            if not valid_slug(slug) or not from_repo_layer(key) or str(slug).lower() in confirmed_lower:
                return False
            if str(slug).lower() in on_default_branch()[key]:
                return False
            where = (cfg.sources.get(f"project.{key}") or {}).get("path") or ".claude/not-me/project.md"
            warnings.append(f"{slug} is listed in {where} but not confirmed: it counts once it is on the default "
                            f"branch, or run /not-me:allow-repo {slug}")
            return True

        tracker = meta.get("tracker")
        if tracker in (None, "auto"):
            tracker = origin
        candidates: List[Any] = [origin]
        if not untrusted(tracker, "tracker"):
            candidates.append(tracker)
        names = [r.get("name") if isinstance(r, dict) else r for r in meta.get("repos") or []]
        candidates += [n for n in names if not untrusted(n, "repos")]
        candidates += [r.get("repo") if isinstance(r, dict) else r for r in meta.get("allow_repos") or []]
        candidates += confirmed
        allowed: List[str] = []
        for c in candidates:
            if valid_slug(c) and str(c).lower() not in (a.lower() for a in allowed):
                allowed.append(str(c))

        board_meta = (meta.get("queue") or {}).get("board") or {}
        owner, number = board_meta.get("owner"), board_meta.get("number")
        board = (str(owner), int(number)) if owner and isinstance(number, int) and not isinstance(number, bool) \
            else None
        if board is not None and any(
                (getattr(cfg, "sources", {}).get(f"project.queue.board.{k}") or {}).get("layer") == "repo"
                for k in ("owner", "number")):
            confirmed_boards = {b.lower() for scope in ([origin] if origin else []) + ["*"]
                                for b in _board_entries(data, scope)}
            label = f"{board[0]}/{board[1]}"
            if label.lower() not in confirmed_boards and label.lower() not in on_default_branch()["board"]:
                where = (cfg.sources.get("project.queue.board.owner") or {}).get("path") or ".claude/not-me/project.md"
                warnings.append(f"board {label} is listed in {where} but not confirmed: it counts once it is on the "
                                f"default branch, or run /not-me:allow-repo --board {label}")
                board = None

        guard = meta.get("guard")
        guard = guard if isinstance(guard, dict) else {}
        rules = {r: guard.get(r, "on") not in ("off", False) for r in GUARD_RULES}
        protected = tuple(b for b in meta.get("protected_branches") or ()
                          if isinstance(b, str) and b and not b.startswith("!"))
        return cls(origin_repo=origin, allowed_repos=tuple(allowed), board=board,
                   protected_branches=protected, rules=rules,
                   mode=str(meta.get("mode") or "dry-run"), warnings=tuple(warnings + list(cfg.warnings or [])),
                   project_dir=project_dir)


def check_write(target: WriteTarget, ctx: GuardContext) -> Decision:
    if target.kind == "local":
        return Decision(True)
    if target.kind == "board":
        if ctx.board and target.owner and target.owner.lower() == ctx.board[0].lower() \
                and target.number == ctx.board[1]:
            return Decision(True)
        configured = f"{ctx.board[0]}/{ctx.board[1]}" if ctx.board else "none"
        return Decision(False, f"write to {target.describe()} blocked: the configured board is {configured}",
                        None, "other_repo")
    if target.kind != "repo":
        return Decision(False, f"unknown write target kind {target.kind!r}", None, "other_repo")
    if not ctx.rules.get("other_repo", True):
        return Decision(True)
    if target.slug.lower() in ctx.allowed_repos_lower:
        return Decision(True)
    allowed = ", ".join(ctx.allowed_repos) or "none"
    return Decision(False, f"write to {target.slug} blocked: allowed repos are {allowed}",
                    f"/not-me:allow-repo {target.slug}", "other_repo")
