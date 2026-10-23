"""Merge grants: one named merge a person allows from their own prompt.

Merging stays a human action. A person may still hand one merge to the session by typing
`/not-me:allow-merge owner/name#N` (a command the model cannot run: disable-model-invocation).

- The UserPromptSubmit hook (hooks/merge_grant.py) fires on what the person submits, never on anything the
  model does. A prompt that is exactly that command with one valid `owner/name#N` writes this session's grant;
  any other prompt in the session deletes it. So a grant lasts until the person's next message.
- The PreToolUse guard lets exactly one `gh pr merge N -R owner/name` through when a fresh grant for the calling
  session names that PR, and deletes the grant as it does (single use). MAX_AGE is a backstop for a session in
  which no further prompt arrives.
- No tool may read or write the grant directory: the guard's self-protection blocks every path under it and
  every Bash command that names it, in every directory.
- A grant is signed: an HMAC-SHA256 keyed by the operator token (the secret `/not-me:mode live` also signs with)
  over the grant and the not-me home it lives in. The hook signs it, the guard verifies it, so a grant file
  written by any other route is ignored. No operator token, no grant.

Standard library only; imports only `humans` (stdlib only) from not-me, since it runs on every prompt.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from . import humans

COMMAND = "/not-me:allow-merge"
GRANT_DIR = "merge-grants"  # under the global not-me home; the guard blocks any tool that names or reaches it
MAX_AGE = 1800  # seconds
MAX_BYTES = 4096
METHODS = ("squash", "merge", "rebase")
DEFAULT_METHOD = "squash"  # a grant that names no method allows only --squash
_MESSAGE = b"not-me merge grant v1\0"
_SIGNED = ("session", "repo", "number", "method", "created")


class NoOperatorToken(Exception):
    """There is no usable operator token to sign a grant with."""


def required_flag(grant: Dict[str, Any]) -> str:
    """The one gh pr merge method flag a grant allows."""
    return "--" + (grant.get("method") or DEFAULT_METHOD)


def _mac(token: str, data: Dict[str, Any], home: PathLike) -> str:
    bound = os.path.normcase(os.path.realpath(str(home)))
    payload = json.dumps({k: data.get(k) for k in _SIGNED}, sort_keys=True, separators=(",", ":"))
    key = token.encode("utf-8") + b"\0merge-grant"
    return hmac.new(key, _MESSAGE + bound.encode("utf-8") + b"\0" + payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()

_SID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_STARTS = re.compile(r"\A\s*" + re.escape(COMMAND) + r"(?![A-Za-z0-9_:./-])")
# owner: GitHub's rules (letters, digits, single hyphens, 1-39); name: letters, digits, . _ - (not . or ..)
_ARG = re.compile(r"\A([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})#([1-9][0-9]{0,9})"
                  r"(?:[ \t]+--(squash|merge|rebase))?\Z")

PathLike = Union[str, Path]


def session_key(session_id: Any) -> Optional[str]:
    """The session id when it is 1 to 128 characters of [A-Za-z0-9_-]; None otherwise (never stripped, so two ids
    cannot map to one grant file)."""
    if not isinstance(session_id, str) or not _SID.fullmatch(session_id):
        return None
    return session_id


def grant_dir(home: PathLike) -> Path:
    return Path(home) / GRANT_DIR


def grant_path(home: PathLike, sid: str) -> Path:
    return grant_dir(home) / f"{sid}.json"


def parse_prompt(prompt: Any) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """(names the command, grant). A prompt that does not start with the command gives (False, None). One that
    does gives a grant only when the rest is exactly `owner/name#N`, optionally followed by --squash, --merge or
    --rebase, on one line; anything else gives (True, None)."""
    if not isinstance(prompt, str) or not _STARTS.match(prompt):
        return False, None
    rest = prompt.strip()[len(COMMAND):].strip(" \t")
    m = _ARG.match(rest)
    if not m or m.group(2) in (".", ".."):
        return True, None
    owner, name, number, method = m.groups()
    return True, {"repo": f"{owner}/{name}", "number": int(number), "method": method}


def clear(home: PathLike, sid: str) -> None:
    try:
        grant_path(home, sid).unlink()
    except FileNotFoundError:
        pass


def write(home: PathLike, sid: str, grant: Dict[str, Any], now: Optional[float] = None) -> Path:
    """Sign and write this session's grant atomically (a temp file renamed into place); returns its path.
    Raises NoOperatorToken when there is no usable operator token to sign with."""
    method = grant.get("method")
    if method is not None and method not in METHODS:
        raise ValueError(f"unknown merge method {method!r}")
    token = humans.read_token(home)
    if not token:
        raise NoOperatorToken("no usable operator token")
    d = grant_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(d), 0o700)
    except OSError:
        pass
    data: Dict[str, Any] = {"session": sid, "repo": grant["repo"], "number": int(grant["number"]),
                            "method": method, "created": float(time.time() if now is None else now)}
    data["mac"] = _mac(token, data, home)
    final = grant_path(home, sid)
    tmp = d / f".{sid}.{os.getpid()}.tmp"
    with open(str(tmp), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, sort_keys=True))
    os.replace(str(tmp), str(final))
    return final


def read(home: PathLike, sid: Optional[str], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """This session's grant when it is a regular file, well formed, for this session, signed with the current
    operator token, and at most MAX_AGE old (by both its recorded time and its modification time); else None."""
    if not sid:
        return None
    path = grant_path(home, sid)
    now = time.time() if now is None else now
    try:
        st = os.lstat(str(path))
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None
        with open(str(path), "rb") as fh:
            data = json.loads(fh.read(MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("session") != sid:
        return None
    repo, number, created = data.get("repo"), data.get("number"), data.get("created")
    if not isinstance(repo, str) or not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    if not isinstance(created, (int, float)) or isinstance(created, bool):
        return None
    if data.get("method") is not None and data.get("method") not in METHODS:
        return None
    for t in (created, st.st_mtime):
        if not -5 <= now - t <= MAX_AGE:  # a few seconds of clock skew, never a grant from the future
            return None
    token = humans.read_token(home)
    mac = data.get("mac")
    if not token or not isinstance(mac, str) or not hmac.compare_digest(
            _mac(token, data, home).encode("ascii"), mac.encode("utf-8", "replace")):
        return None
    return data


def consume(home: PathLike, sid: str) -> bool:
    """Take this session's grant for one use: renamed away (atomic, so two calls cannot both win), then deleted.
    False when it was already gone."""
    path = grant_path(home, sid)
    used = path.with_name(f".{sid}.{os.getpid()}.used")
    try:
        os.replace(str(path), str(used))
    except OSError:
        return False
    try:
        used.unlink()
    except OSError:
        pass
    return True
