"""Read and set `mode` (dry-run or live) in a project.md layer file.

The edit is line-level: only the top-level `mode:` line changes (or one is inserted), so every other key, comment,
line ending and the markdown body stay byte-for-byte. The result is parsed again before it is written; an edit that
would change anything but `mode` is refused. Which layer may be written, and by whom, is the caller's business.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .frontmatter import DELIMITER, FrontmatterError, parse_with_lines
from .layers import PROJECT_FILE, REPO_SUBDIR
from .migrations import CURRENT_SCHEMA

# a top-level mode key, plain or quoted as the frontmatter parser accepts it ("mode": / 'mode':)
_MODE_LINE = re.compile(r"^(?P<pre>(?:mode|\"mode\"|'mode')[ \t]*:[ \t]*)(?P<val>\"[^\"]*\"|'[^']*'|[^\s#]*)"
                        r"(?P<post>.*)$")
_SCHEMA_LINE = re.compile(r"^schema[ \t]*:")


class ModeEditError(ValueError):
    """The file cannot be edited safely (unreadable frontmatter, or the edit would touch other keys)."""


def _newline(text: str) -> str:
    first = text.split("\n", 1)
    return "\r\n" if len(first) > 1 and first[0].endswith("\r") else "\n"


def _with_value(line: str, value: str) -> str:
    """line (a top-level `mode:` line, without its line ending) with its value replaced; a trailing comment keeps
    its column when there is room."""
    m = _MODE_LINE.match(line)
    if m is None:
        return f"mode: {value}"
    post = m.group("post")
    if post.strip().startswith("#"):
        gap = len(post) - len(post.lstrip(" \t"))
        width = len(m.group("val")) + gap
        post = " " * max(1, width - len(value)) + post.lstrip(" \t")
    return m.group("pre") + value + post


def set_mode_text(text: str, value: str) -> str:
    """text (a whole project.md, possibly empty) with the top-level `mode` set to value."""
    bom = "﻿" if text.startswith("﻿") else ""
    body = text[len(bom):]
    nl = _newline(body)
    if not body:
        return f"{bom}{DELIMITER}{nl}schema: {CURRENT_SCHEMA}{nl}mode: {value}{nl}{DELIMITER}{nl}"
    lines = body.splitlines(keepends=True)
    if lines[0].rstrip() != DELIMITER:  # text but no frontmatter: not a project.md this command should touch
        raise ModeEditError("the file has text but no frontmatter")
    close = next((i for i in range(1, len(lines)) if lines[i].rstrip() == DELIMITER), None)
    if close is None:
        raise ModeEditError("the frontmatter opens with '---' but never closes")
    found = [i for i in range(1, close) if _MODE_LINE.match(lines[i].rstrip("\r\n"))]  # top level: no indent
    if found:
        for i in found:
            raw = lines[i]
            ending = raw[len(raw.rstrip("\r\n")):]
            lines[i] = _with_value(raw.rstrip("\r\n"), value) + ending
    else:
        at = next((i + 1 for i in range(1, close) if _SCHEMA_LINE.match(lines[i])), 1)
        lines.insert(at, f"mode: {value}{nl}")
    return bom + "".join(lines)


def _parse(text: str, source: str) -> Tuple[Dict[str, Any], str]:
    try:
        meta, body, _ = parse_with_lines(text, source=source)
    except FrontmatterError as exc:
        raise ModeEditError(f"{source}: {exc.message} (line {exc.line or 1}); fix the file by hand") from None
    return meta, body


def _check_target(path: Path, follow_links: bool) -> Path:
    """The file to edit for path, or ModeEditError. The repo layer (follow_links False) is committed and so
    agent-writable: a symlink there (or at its directory), a hard-linked file or anything but a regular file is
    refused, so `notme mode` never writes through a planted link. The global layer may be a symlink (stowed
    dotfiles), but only to a regular file named project.md."""
    def refuse(why: str) -> ModeEditError:
        return ModeEditError(f"{path}: {why}; fix it by hand")

    target = path
    if not follow_links:
        for part in (path, path.parent, path.parent.parent):
            if part.is_symlink():
                raise refuse(f"{part.name} is a symlink, which the repo layer may not be")
    elif path.is_symlink():
        target = Path(os.path.realpath(str(path)))
        if target.name != PROJECT_FILE or not target.is_file():
            raise refuse(f"the symlink does not lead to a regular file named {PROJECT_FILE}")
    try:
        st = os.lstat(str(target))
    except FileNotFoundError:
        return target
    if not stat.S_ISREG(st.st_mode):
        raise refuse("it is not a regular file")
    if st.st_nlink > 1:
        raise refuse("it is hard-linked to another file")
    return target


def write_mode(path: Path, value: str, follow_links: bool = False) -> Path:
    """Set `mode: value` in the project.md at path (created with minimal frontmatter when missing or empty) and
    return the file written. Only the global layer passes follow_links, so its symlink stays a link (see
    _check_target). Refuses (ModeEditError) when the file is not a plain project.md, has text but no frontmatter,
    its frontmatter does not parse, or the edit would change any other key or the body."""
    target = _check_target(path, follow_links)
    try:
        old = target.read_bytes().decode("utf-8")
    except FileNotFoundError:
        old = ""
    except UnicodeDecodeError:
        raise ModeEditError(f"{path} is not UTF-8 text; fix the file by hand") from None
    before, body_before = _parse(old, str(path))
    new = set_mode_text(old, value)
    after, body_after = _parse(new, str(path))
    rest_before = {k: v for k, v in before.items() if k != "mode"}
    rest_after = {k: v for k, v in after.items() if k != "mode"}
    if after.get("mode") != value or body_after != body_before or (old and rest_after != rest_before):
        raise ModeEditError(f"{path}: setting mode would change more than the mode line; edit it by hand")
    if new != old:
        _atomic_write_bytes(target, new.encode("utf-8"))
    return target


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write data to target through a temp file in the same directory, keeping an existing file's permissions.
    (The shared text writers in runlog and writes would reset the permissions; bytes keep CRLF and a BOM.)"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".project-", suffix=".md.tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, target.stat().st_mode & 0o777)
        except OSError:
            pass  # a new file keeps mkstemp's private mode
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- reading what the layers say ------------------------------------------------


def layer_file(cfg: Any, layer: str) -> Optional[Path]:
    """The project.md the global or repo layer reads (absolute), or None when the layer has no directory."""
    if layer == "global":
        return Path(cfg.home) / PROJECT_FILE if cfg.home else None
    return Path(cfg.project_dir).joinpath(*REPO_SUBDIR, PROJECT_FILE) if cfg.project_dir else None


def layer_value(cfg: Any, layer: str) -> Tuple[Any, Optional[int], Optional[str]]:
    """(mode as written in that layer or None, its line, display path)."""
    doc = cfg.docs.get("project", {}).get(layer)
    if doc is None or "mode" not in doc.meta:
        return None, None, doc.path if doc else None
    return doc.meta["mode"], doc.lines.get(("mode",)), doc.path


def source(cfg: Any) -> Dict[str, Any]:
    """{layer, path, line} the effective mode came from, plus any ignored attempts."""
    src = cfg.sources.get("project.mode") or {}
    out: Dict[str, Any] = {"layer": src.get("layer"), "path": src.get("path"), "line": src.get("line")}
    ignored: List[Any] = src.get("ignored") or []
    if ignored:
        out["ignored"] = ignored
    return out
