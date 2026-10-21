"""The intended-writes ledger: what a dry run would have done."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import paths


class LedgerError(RuntimeError):
    pass


def _real_dir(path: Path, base: Optional[Path] = None) -> None:
    """Make path a real directory. With a trusted base, every component below it is lstat-checked.

    Missing components are made one at a time (0700) and re-checked; any symlink or non-directory
    component raises LedgerError. Without a base only the last component is checked.
    """
    if base is None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise LedgerError(f"{path.name} is a symlink; refusing to write the ledger through it")
        return
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        raise LedgerError(f"{path.name} is not under the ledger's base directory") from None
    current = base
    for part in parts:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise LedgerError(f"{part} is not a plain directory; refusing to write the ledger through it")


def _write_new(dest: Path, data: bytes) -> None:
    """Create dest exclusively (never through a symlink, mode 0600); an existing one is left alone."""
    if os.path.islink(dest):  # O_NOFOLLOW does not exist everywhere
        raise LedgerError(f"{dest.name} is a symlink; refusing to write through it")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(dest), flags, 0o600)
    except FileExistsError:
        return  # content-addressed: the same sha is the same bytes
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, path: Any, base: Optional[Any] = None):
        """base: the trusted directory above path; every directory between them is checked for symlinks."""
        self.path = Path(path)
        self.base = Path(base) if base is not None else None

    @classmethod
    def default_for(cls, project_dir: Any) -> "Ledger":
        common = paths.git_common_dir(project_dir)
        base = common if common is not None else Path(project_dir)
        rel = ("not-me", "logs") if common is not None else (".not-me", "logs")
        return cls(base.joinpath(*rel, "intended-writes.jsonl"), base=base)

    def append(self, op: str, target: str, argv: Sequence[str], payload_path: Optional[Any] = None,
               description: str = "", payload_bytes: Optional[bytes] = None) -> Dict[str, Any]:
        """Record an intended write. The payload comes from payload_path or, if given, payload_bytes."""
        sha = None
        copied = None
        if payload_bytes is None and payload_path is not None:
            payload_bytes = Path(payload_path).read_bytes()
        _real_dir(self.path.parent, self.base)
        if payload_bytes is not None:
            sha = hashlib.sha256(payload_bytes).hexdigest()
            dest = self.path.parent / "payloads" / f"{sha}.md"
            _real_dir(dest.parent, self.base)
            _write_new(dest, payload_bytes)
            copied = f"payloads/{sha}.md"
        entry = {"ts": utc_now(), "op": op, "target": target,
                 "argv": list(argv), "payload_sha256": sha, "payload_path": copied, "description": description}
        if os.path.islink(self.path):  # O_NOFOLLOW does not exist everywhere
            raise LedgerError(f"{self.path.name} is a symlink; refusing to write the ledger through it")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(self.path), flags, 0o600)
        except OSError as exc:
            raise LedgerError(f"cannot open the ledger {self.path.name} safely ({exc.strerror})") from exc
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def read(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out
