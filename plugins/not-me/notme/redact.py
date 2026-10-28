"""Mask credentials in text bound for stdout, stderr or a log: URL user info and token-shaped strings."""
from __future__ import annotations

import re
from typing import Any

_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^@\s/]+@")  # https://user:pw@, ssh://git@, ...
_ACCESS_TOKEN = re.compile(r"(?i)\bx-access-token:[^@\s/]+")
TOKEN_PATTERNS = (  # GitHub token shapes; the guard log reuses them
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
)


def redact(text: Any) -> str:
    """Text with URL credentials, x-access-token values and token-shaped strings masked."""
    out = _URL_USERINFO.sub(r"\1***@", str(text))
    out = _ACCESS_TOKEN.sub("x-access-token:***", out)
    for pattern in TOKEN_PATTERNS:
        out = pattern.sub("***", out)
    return out


def redact_obj(value: Any) -> Any:
    """A copy of a JSON-shaped value with every string redacted (dict keys included)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(k) if isinstance(k, str) else k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value
