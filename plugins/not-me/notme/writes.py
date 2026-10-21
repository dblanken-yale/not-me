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

from . import git, paths
from .dryrun import utc_now

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*$")  # no leading "-" in either part
GUARD_RULES = ("other_repo", "protected_branches", "force_push", "destructive_git")
ALLOWLIST_FILE = "allowed-repos.json"

_URL_FORMS = (
    re.compile(r"^(?:https?|ssh|git)://(?:[^@/]+@)?(?P<h>[^/:]+)(?::\d+)?/(?P<o>[^/]+)/(?P<n>[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^(?:[^@/\s]+@)?(?P<h>[^/:\s]+):(?P<o>[^/:\s]+)/(?P<n>[^/:\s]+?)(?:\.git)?/?$"),
)


def valid_slug(value: Any) -> bool:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        return False
    return not any(part in (".", "..") for part in value.split("/"))


def parse_origin(url: Optional[str]) -> Optional[str]:
    """owner/name from an https, scp-style ssh or ssh:// remote URL on github.com (or $GH_HOST); else None."""
    if not url:
        return None
    url = url.strip()
    for form in _URL_FORMS:
        m = form.match(url)
        if m and m.group("h").lower() in {"github.com", os.environ.get("GH_HOST", "github.com").lower()}:
            slug = f"{m.group('o')}/{m.group('n')}"
            return slug if valid_slug(slug) else None
    return None


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


def _entries(data: Mapping[str, Any], scope: str) -> List[dict]:
    block = data.get("projects", {}).get(scope)
    allow = block.get("allow") if isinstance(block, dict) else None
    return [e for e in allow if isinstance(e, dict) and valid_slug(e.get("repo"))] if isinstance(allow, list) else []


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


def allowlist_add(home: Any, project: Optional[str], repo: str, everywhere: bool = False) -> bool:
    """Allow repo for one project (or all with everywhere). True if it was newly added."""
    if not valid_slug(repo):
        raise ValueError(f"invalid repo {repo!r}: expected owner/name, no URLs or wildcards")
    scope = _scope(project, everywhere)
    data = _load_for_edit(home)
    block = data["projects"].setdefault(scope, {"allow": []})
    allow = block.setdefault("allow", [])
    if any(isinstance(e, dict) and str(e.get("repo", "")).lower() == repo.lower() for e in allow):
        return False
    allow.append({"repo": repo, "added": utc_now()})
    _write_allowlist(home, data)
    return True


def allowlist_remove(home: Any, project: Optional[str], repo: str, everywhere: bool = False) -> bool:
    if not valid_slug(repo):
        raise ValueError(f"invalid repo {repo!r}: expected owner/name")
    scope = _scope(project, everywhere)
    data = _load_for_edit(home)
    block = data["projects"].get(scope)
    allow = block.get("allow") if isinstance(block, dict) else None
    if not isinstance(allow, list):
        return False
    kept = [e for e in allow if not (isinstance(e, dict) and str(e.get("repo", "")).lower() == repo.lower())]
    if len(kept) == len(allow):
        return False
    block["allow"] = kept
    _write_allowlist(home, data)
    return True


def allowlist_list(home: Any, project: Optional[str] = None) -> List[Dict[str, str]]:
    """Entries as {scope, repo, added}; for a project: its own plus the "*" ones. Bad file = empty."""
    data, _ = _read_allowlist(home)
    scopes = [s for s in data["projects"] if project is None or s in (project, "*")]
    return [{"scope": s, "repo": e["repo"], "added": str(e.get("added", ""))}
            for s in sorted(scopes) for e in _entries(data, s)]


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

        def from_repo_layer(key: str) -> bool:
            return (getattr(cfg, "sources", {}).get(f"project.{key}") or {}).get("layer") == "repo"

        def untrusted(slug: Any, key: str) -> bool:
            """A repo-layer name (committed, so untrusted) that the global allow list has not confirmed."""
            if not valid_slug(slug) or not from_repo_layer(key) or str(slug).lower() in confirmed_lower:
                return False
            where = (cfg.sources.get(f"project.{key}") or {}).get("path") or ".claude/not-me/project.md"
            warnings.append(f"{slug} is listed in {where} but not confirmed; run /not-me:allow-repo {slug} "
                            "(setup does this for you)")
            return True

        tracker = meta.get("tracker")
        if tracker in (None, "auto"):
            tracker = origin
        candidates: List[Any] = [origin]
        if not untrusted(tracker, "tracker"):
            candidates.append(tracker)
        candidates += [r.get("name") for r in meta.get("repos") or [] if isinstance(r, dict)
                       and not untrusted(r.get("name"), "repos")]
        candidates += list(meta.get("allow_repos") or []) + confirmed
        allowed: List[str] = []
        for c in candidates:
            if valid_slug(c) and str(c).lower() not in (a.lower() for a in allowed):
                allowed.append(str(c))

        board_meta = (meta.get("queue") or {}).get("board") or {}
        owner, number = board_meta.get("owner"), board_meta.get("number")
        board = (str(owner), int(number)) if owner and isinstance(number, int) else None

        guard = meta.get("guard") or {}
        rules = {r: guard.get(r, "on") != "off" for r in GUARD_RULES}
        return cls(origin_repo=origin, allowed_repos=tuple(allowed), board=board,
                   protected_branches=tuple(meta.get("protected_branches") or ()), rules=rules,
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
