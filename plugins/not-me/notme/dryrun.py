"""The intended-writes ledger: what a dry run would have done."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import paths


def _write_new(dest: Path, data: bytes) -> None:
    """Create dest exclusively (never through a symlink, mode 0600); an existing one is left alone."""
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
    def __init__(self, path: Any):
        self.path = Path(path)

    @classmethod
    def default_for(cls, project_dir: Any) -> "Ledger":
        common = paths.git_common_dir(project_dir)
        base = common / "not-me" if common is not None else Path(project_dir) / ".not-me"
        return cls(base / "logs" / "intended-writes.jsonl")

    def append(self, op: str, target: str, argv: Sequence[str], payload_path: Optional[Any] = None,
               description: str = "", payload_bytes: Optional[bytes] = None) -> Dict[str, Any]:
        """Record an intended write. The payload comes from payload_path or, if given, payload_bytes."""
        sha = None
        copied = None
        if payload_bytes is None and payload_path is not None:
            payload_bytes = Path(payload_path).read_bytes()
        if payload_bytes is not None:
            sha = hashlib.sha256(payload_bytes).hexdigest()
            dest = self.path.parent / "payloads" / f"{sha}.md"
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_new(dest, payload_bytes)
            copied = f"payloads/{sha}.md"
        entry = {"ts": utc_now(), "op": op, "target": target,
                 "argv": list(argv), "payload_sha256": sha, "payload_path": copied, "description": description}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
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
