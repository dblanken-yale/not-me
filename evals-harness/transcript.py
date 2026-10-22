"""Read a Claude Code session transcript (JSONL) for grading.

Claude Code writes one JSON object per line under
``~/.claude/projects/<encoded cwd>/<session-id>.jsonl``. Subagent lines are kept in
``<session-id>/subagents/agent-*.jsonl`` next to it (``isSidechain: true``); `load` merges them in, ordered by
timestamp, because the graders apply to the whole run.

Stdlib only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

ToolUse = Tuple[str, Dict[str, Any], int]


def project_dir_for(cwd: Path, home: Optional[Path] = None) -> Path:
    """Where Claude Code keeps the transcripts for sessions started in `cwd`."""
    home = Path.home() if home is None else Path(home)
    return home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def find_transcripts(cwd: Path, since: float = 0.0, home: Optional[Path] = None,
                     session_id: Optional[str] = None) -> List[Path]:
    """Session files for `cwd` modified after `since` (epoch seconds), newest first.

    With `session_id`, only that session's file (empty list when it does not exist).
    """
    pdir = project_dir_for(cwd, home)
    if session_id:
        one = pdir / f"{session_id}.jsonl"
        return [one] if one.is_file() else []
    if not pdir.is_dir():
        return []
    found = [(p.stat().st_mtime, p) for p in pdir.glob("*.jsonl") if p.is_file()]
    return [p for m, p in sorted(found, key=lambda t: t[0], reverse=True) if m > since]


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _read_lines(path: Path) -> Iterator[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


@dataclass
class Transcript:
    tool_uses: List[ToolUse] = field(default_factory=list)
    last_message: str = ""
    trace: str = ""
    files_written: List[str] = field(default_factory=list)
    questions_asked: int = 0
    session_id: str = ""
    cwd: str = ""


def parse(objects: List[Dict[str, Any]]) -> Transcript:
    t = Transcript()
    trace: List[str] = []
    for obj in objects:
        kind = obj.get("type")
        message = obj.get("message")
        if kind not in ("assistant", "user") or not isinstance(message, dict):
            continue
        t.session_id = t.session_id or str(obj.get("sessionId") or "")
        if not obj.get("isSidechain"):
            t.cwd = t.cwd or str(obj.get("cwd") or "")
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}] if kind == "assistant" else []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if kind == "assistant" and btype == "tool_use":
                name = str(block.get("name", ""))
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                t.tool_uses.append((name, args, len(t.tool_uses)))
                trace.append(f"TOOL {name} {json.dumps(args, sort_keys=True)}")
                if name == "AskUserQuestion":
                    t.questions_asked += 1
                key = WRITE_TOOLS.get(name)
                if key and isinstance(args.get(key), str):
                    t.files_written.append(args[key])
            elif kind == "assistant" and btype == "text":
                text = str(block.get("text", ""))
                trace.append(text)
                if not obj.get("isSidechain") and text.strip():
                    t.last_message = text
            elif kind == "user" and btype == "tool_result":
                trace.append(_result_text(block.get("content")))
    t.trace = "\n".join(trace)
    return t


def _stamped(path: Path) -> List[Tuple[str, Dict[str, Any]]]:
    """Lines with a sort key: their own timestamp, else the last one seen in the same file."""
    out, last = [], ""
    for obj in _read_lines(path):
        last = str(obj.get("timestamp") or last)
        out.append((last, obj))
    return out


def load(path: Union[str, Path]) -> Transcript:
    """Parse a session file, plus its subagent files when they sit next to it."""
    path = Path(path)
    stamped = _stamped(path)
    sub = path.with_suffix("") / "subagents"
    extra = sorted(sub.glob("*.jsonl")) if sub.is_dir() else []
    if extra:
        for f in extra:
            stamped.extend(_stamped(f))
        stamped.sort(key=lambda pair: pair[0])  # stable: file order holds within equal stamps
    return parse([obj for _, obj in stamped])
