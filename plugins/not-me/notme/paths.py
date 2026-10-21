"""Where not-me looks for things, and how paths are shown to people."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Optional, Union

PathLike = Union[str, "os.PathLike[str]"]


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def plugin_root(env: Optional[Mapping[str, str]] = None) -> Path:
    """The plugin directory: $CLAUDE_PLUGIN_ROOT, else the package's parent dir."""
    value = _env(env).get("CLAUDE_PLUGIN_ROOT")
    if value:
        return Path(value)
    return Path(__file__).resolve().parent.parent


def default_home_dir() -> Path:
    """The global layer location when NOT_ME_HOME is not set (Path.home honors USERPROFILE)."""
    return Path.home() / ".claude" / "not-me"


def home_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """The global layer directory: $NOT_ME_HOME, else ~/.claude/not-me."""
    return Path(_env(env).get("NOT_ME_HOME") or default_home_dir())


def _git(args: list, cwd: PathLike) -> Optional[str]:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def project_dir(arg: Optional[PathLike] = None, env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The project: the argument, else $CLAUDE_PROJECT_DIR, else the git toplevel of cwd."""
    if arg:
        return Path(arg)
    value = _env(env).get("CLAUDE_PROJECT_DIR")
    if value:
        return Path(value)
    top = _git(["rev-parse", "--show-toplevel"], os.getcwd())
    return Path(top) if top else None


def _abs_git_path(base: PathLike, out: str) -> Path:
    p = Path(out)
    return (p if p.is_absolute() else Path(base) / p).resolve()


def git_common_dir(path: PathLike) -> Optional[Path]:
    """The shared .git directory for a checkout or worktree, or None outside git."""
    out = _git(["rev-parse", "--git-common-dir"], path)
    return _abs_git_path(path, out) if out else None


def main_checkout(path: PathLike) -> Path:
    """The main checkout for a linked worktree; the path itself otherwise.

    The repo layer is trusted only from the main checkout. A checkout whose
    git dir is the common dir (plain clone, submodule, --separate-git-dir) is
    its own main checkout. For a linked worktree the main checkout is the
    common dir's core.worktree when set (submodules), else its parent.
    Any git error falls back to the path itself.
    """
    path = Path(path)
    if not (path / ".git").is_file():
        return path
    out = _git(["rev-parse", "--git-dir", "--git-common-dir"], path)
    dirs = out.splitlines() if out else []
    if len(dirs) != 2:
        return path
    git_dir, common = (_abs_git_path(path, d) for d in dirs)
    if git_dir == common:
        return path
    configured = _git(["--git-dir", str(common), "config", "core.worktree"], path)
    main = _abs_git_path(common, configured) if configured else common.parent
    return main if main.is_dir() else path


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _relative(path: Path, base: Optional[Path]) -> Optional[str]:
    if base is None:
        return None
    for p, b in ((path, base), (_resolve(path), _resolve(base))):
        try:
            return p.relative_to(b).as_posix()
        except ValueError:
            continue
    return None


def _join(prefix: Optional[str], rel: str) -> str:
    if prefix is None:
        return rel
    return prefix if rel == "." else f"{prefix}/{rel}"


def display_path(path: PathLike, project_dir: Optional[PathLike], home: Optional[PathLike],
                 plugin_root: Optional[PathLike] = None) -> str:
    """A path for people and logs, never absolute.

    Repo paths are relative to the project, an overridden global layer is
    $NOT_ME_HOME/..., shipped files are ${CLAUDE_PLUGIN_ROOT}/..., anything
    under the user's home is ~/... (which covers the default global layer),
    and anything else is shown by its last component.
    """
    path = Path(path)
    overridden_home = Path(home) if home and _resolve(Path(home)) != _resolve(default_home_dir()) else None
    bases = (
        (project_dir, None),
        (overridden_home, "$NOT_ME_HOME"),
        (plugin_root, "${CLAUDE_PLUGIN_ROOT}"),
        (Path.home(), "~"),
    )
    for base, prefix in bases:
        rel = _relative(path, Path(base) if base else None)
        if rel is not None:
            return _join(prefix, rel)
    return ".../" + path.name
