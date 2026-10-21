"""Frontmatter for not-me config docs: a strict, small YAML subset.

Supported: scalars (plain, "double", 'single', ints, floats, true/false,
null/~), flow lists and maps, block lists whose items are scalars or flow
values, block maps nested by exactly two spaces (at most three levels), and
``#`` comments outside quotes. Anything else raises FrontmatterError with the
1-based line in the file.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

__all__ = ["FrontmatterError", "split", "parse", "parse_with_lines", "dump", "render", "iter_unfenced_lines"]

DELIMITER = "---"
MAX_MAP_DEPTH = 3
INDENT = 2

_INT = re.compile(r"^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][-+]?\d+)$")
_KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_./\-]*)[ ]*:(?:[ ]+(.*)|)$")
_PLAIN_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\-]*$")
_BAD_PLAIN_START = set("&*|>%@`")
_LITERALS: Dict[str, Any] = {
    **{v: True for v in ("true", "True", "TRUE")},
    **{v: False for v in ("false", "False", "FALSE")},
    **{v: None for v in ("null", "Null", "NULL", "~")},
}
# Escape letter -> character, for double-quoted strings. "/" is read but never written.
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
_UNESCAPE = dict(_ESCAPES, **{"/": "/"})
_ESCAPE_OUT = {char: "\\" + letter for letter, char in _ESCAPES.items()}

LinePath = Tuple[Any, ...]


class FrontmatterError(ValueError):
    """Frontmatter outside the supported subset, with its 1-based file line."""

    def __init__(self, message: str, source: Optional[str] = None, line: Optional[int] = None):
        self.message = message
        self.source = source
        self.line = line
        where = source or "<frontmatter>"
        if line is not None:
            where = f"{where}:{line}"
        super().__init__(f"{where}: {message}")


def _split(text: str, source: Optional[str]) -> Tuple[str, str]:
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != DELIMITER:
        return "", text
    for i in range(1, len(lines)):
        if lines[i].rstrip() == DELIMITER:
            return "".join(lines[1:i]), "".join(lines[i + 1:])
    raise FrontmatterError("frontmatter opened with '---' but never closed", source, 1)


def split(text: str) -> Tuple[str, str]:
    """Return (frontmatter_text, body). No frontmatter gives ("", text)."""
    return _split(text, None)


def parse(text: str, *, source: str = "<string>") -> Tuple[Dict[str, Any], str]:
    """Return (meta, body) for a document."""
    meta, body, _ = parse_with_lines(text, source=source)
    return meta, body


def parse_with_lines(text: str, *, source: str = "<string>") -> Tuple[Dict[str, Any], str, Dict[LinePath, int]]:
    """Like parse, plus a map of key path tuple -> 1-based file line."""
    front, body = _split(text, source)
    if not front:
        return {}, body, {}
    lines: Dict[LinePath, int] = {}
    # Frontmatter text starts on file line 2, after the opening delimiter.
    meta = _BlockParser(front, source, first_line=2, lines=lines).parse()
    return meta, body, lines


def iter_unfenced_lines(body: str) -> Iterator[Tuple[str, bool]]:
    """Yield (line, fenced) for each markdown line; fence marker lines count as fenced."""
    fenced = False
    for line in body.splitlines():
        marker = line.lstrip().startswith(("```", "~~~"))
        if marker:
            fenced = not fenced
        yield line, fenced or marker


# --- block level ---------------------------------------------------------


def _strip_comment(raw: str) -> str:
    quote = None
    i = 0
    while i < len(raw):
        c = raw[i]
        if quote == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                quote = None
        elif quote == "'":
            if c == "'":
                quote = None
        elif c in "\"'":
            # A quote opens a quoted scalar only at the start of a token.
            if i == 0 or raw[i - 1] in " \t[{,":
                quote = c
        elif c == "#" and (i == 0 or raw[i - 1] in " \t"):
            return raw[:i]
        i += 1
    return raw


class _BlockParser:
    def __init__(self, text: str, source: str, first_line: int, lines: Dict[LinePath, int]):
        self.source = source
        self.lines_map = lines
        self.items: List[Tuple[int, int, str]] = []
        for lineno, raw in enumerate(text.splitlines(), start=first_line):
            stripped_left = raw.lstrip(" ")
            if stripped_left.startswith("\t"):
                if _strip_comment(raw).strip():
                    raise self.error("tabs are not allowed for indentation; use 2 spaces", lineno)
            content = _strip_comment(raw).rstrip()
            if not content.strip():
                continue
            indent = len(content) - len(content.lstrip(" "))
            if indent % INDENT:
                raise self.error(f"indentation must be a multiple of {INDENT} spaces", lineno)
            self.items.append((lineno, indent, content.strip()))
        self.i = 0

    def error(self, message: str, line: int) -> FrontmatterError:
        return FrontmatterError(message, self.source, line)

    def parse(self) -> Dict[str, Any]:
        if not self.items:
            return {}
        lineno, indent, content = self.items[0]
        if indent != 0:
            raise self.error("unexpected indentation", lineno)
        if _is_item(content):
            raise self.error("frontmatter must be a mapping of keys, not a list", lineno)
        result = self.block_map(0, 0, ())
        if self.i < len(self.items):
            raise self.error("unexpected indentation", self.items[self.i][0])
        return result

    def block_map(self, indent: int, depth: int, path: LinePath) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        while self.i < len(self.items):
            lineno, ind, content = self.items[self.i]
            if ind < indent:
                break
            if ind > indent:
                raise self.error("unexpected indentation (nest by exactly 2 spaces)", lineno)
            if depth >= MAX_MAP_DEPTH:
                raise self.error(f"maps nest at most {MAX_MAP_DEPTH} levels deep; use an inline {{...}} map", lineno)
            if _is_item(content):
                raise self.error("list item where a 'key: value' line was expected", lineno)
            key, rest = self.key_line(content, lineno)
            if key in result:
                raise self.error(f"duplicate key '{key}'", lineno)
            self.i += 1
            key_path = path + (key,)
            self.lines_map[key_path] = lineno
            if rest:
                result[key] = _InlineParser(rest, self.source, lineno, self.lines_map).parse_top(key_path)
                continue
            nxt = self.items[self.i] if self.i < len(self.items) else None
            if nxt is not None and nxt[1] > indent:
                if nxt[1] != indent + INDENT:
                    raise self.error("unexpected indentation (nest by exactly 2 spaces)", nxt[0])
                if _is_item(nxt[2]):
                    result[key] = self.block_list(nxt[1])
                else:
                    result[key] = self.block_map(nxt[1], depth + 1, key_path)
            elif nxt is not None and nxt[1] == indent and _is_item(nxt[2]):
                result[key] = self.block_list(indent)
            else:
                result[key] = None
        return result

    def block_list(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while self.i < len(self.items):
            lineno, ind, content = self.items[self.i]
            if ind < indent or (ind == indent and not _is_item(content)):
                break
            if ind > indent:
                raise self.error("unexpected indentation inside a list", lineno)
            rest = content[1:].strip()
            if not rest:
                raise self.error("empty list item", lineno)
            if rest[0] not in "[{\"'" and _KEY.match(rest):
                raise self.error("block maps inside list items are not supported; write the item as {key: value}",
                                 lineno)
            items.append(_InlineParser(rest, self.source, lineno, self.lines_map).parse_top(None))
            self.i += 1
        return items

    def key_line(self, content: str, lineno: int) -> Tuple[str, str]:
        if content[0] in "\"'":
            parser = _InlineParser(content, self.source, lineno, self.lines_map)
            key = parser.quoted()
            rest = content[parser.pos:]
            if not rest.startswith(":") or (len(rest) > 1 and rest[1] != " "):
                raise self.error("expected ':' after quoted key", lineno)
            return key, rest[1:].strip()
        m = _KEY.match(content)
        if not m:
            raise self.error("expected 'key: value'", lineno)
        return m.group(1), (m.group(2) or "").strip()


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


# --- inline (flow) level -------------------------------------------------


def _scalar(token: str) -> Any:
    if token in _LITERALS:
        return _LITERALS[token]
    if _INT.match(token):
        return int(token)
    if _FLOAT.match(token):
        return float(token)
    return token


class _InlineParser:
    def __init__(self, text: str, source: str, line: int, lines: Dict[LinePath, int]):
        self.s = text
        self.pos = 0
        self.source = source
        self.line = line
        self.lines_map = lines

    def error(self, message: str) -> FrontmatterError:
        return FrontmatterError(message, self.source, self.line)

    def ws(self) -> None:
        while self.pos < len(self.s) and self.s[self.pos] in " \t":
            self.pos += 1

    def peek(self) -> str:
        return self.s[self.pos] if self.pos < len(self.s) else ""

    def parse_top(self, path: Optional[LinePath]) -> Any:
        value = self.value(flow=False, path=path)
        self.ws()
        if self.pos < len(self.s):
            raise self.error(f"unexpected text after value: {self.s[self.pos:]!r}")
        return value

    def value(self, flow: bool, path: Optional[LinePath]) -> Any:
        self.ws()
        c = self.peek()
        if c == "":
            raise self.error("missing value")
        if c == "[":
            return self.flow_list()
        if c == "{":
            return self.flow_map(path)
        if c in "\"'":
            return self.quoted()
        return self.plain(flow)

    def plain(self, flow: bool) -> Any:
        start = self.pos
        if self.s[start] in _BAD_PLAIN_START:
            raise self.error(f"'{self.s[start]}' is not supported here; quote the value")
        while self.pos < len(self.s):
            c = self.s[self.pos]
            if flow and c in ",]}":
                break
            if c == ":" and (self.pos + 1 == len(self.s) or self.s[self.pos + 1] == " "):
                if flow:
                    break
                raise self.error("': ' inside an unquoted value; put the value in quotes")
            self.pos += 1
        token = self.s[start:self.pos].strip()
        if not token:
            raise self.error("missing value")
        return _scalar(token)

    def quoted(self) -> str:
        quote = self.s[self.pos]
        self.pos += 1
        out = []
        while self.pos < len(self.s):
            c = self.s[self.pos]
            if quote == '"' and c == "\\":
                if self.pos + 1 >= len(self.s):
                    break
                esc = self.s[self.pos + 1]
                mapped = _UNESCAPE.get(esc)
                if mapped is None:
                    raise self.error(f"unsupported escape '\\{esc}'")
                out.append(mapped)
                self.pos += 2
                continue
            if c == quote:
                if quote == "'" and self.s[self.pos + 1:self.pos + 2] == "'":
                    out.append("'")
                    self.pos += 2
                    continue
                self.pos += 1
                return "".join(out)
            out.append(c)
            self.pos += 1
        raise self.error("unclosed quoted string")

    def flow_list(self) -> List[Any]:
        self.pos += 1
        items: List[Any] = []
        self.ws()
        if self.peek() == "]":
            self.pos += 1
            return items
        while True:
            items.append(self.value(flow=True, path=None))
            self.ws()
            c = self.peek()
            if c == ",":
                self.pos += 1
                continue
            if c == "]":
                self.pos += 1
                return items
            if c == "":
                raise self.error("unclosed '[' list")
            raise self.error(f"expected ',' or ']' in list, found {c!r}")

    def flow_key(self) -> str:
        self.ws()
        if self.peek() in ("\"", "'"):
            return self.quoted()
        start = self.pos
        while self.pos < len(self.s) and self.s[self.pos] not in ":,{}[]":
            self.pos += 1
        key = self.s[start:self.pos].strip()
        if not key or not _PLAIN_KEY.match(key):
            if self.pos >= len(self.s):
                raise self.error("unclosed '{' map")
            raise self.error(f"bad key {key!r} in inline map")
        return key

    def flow_map(self, path: Optional[LinePath]) -> Dict[str, Any]:
        self.pos += 1
        result: Dict[str, Any] = {}
        self.ws()
        if self.peek() == "}":
            self.pos += 1
            return result
        while True:
            key = self.flow_key()
            self.ws()
            if self.peek() != ":":
                raise self.error("unclosed '{' map" if self.peek() == "" else f"expected ':' after key '{key}'")
            self.pos += 1
            if key in result:
                raise self.error(f"duplicate key '{key}'")
            key_path = path + (key,) if path is not None else None
            if key_path is not None:
                self.lines_map[key_path] = self.line
            self.ws()
            if self.peek() in (",", "}"):
                result[key] = None
            else:
                result[key] = self.value(flow=True, path=key_path)
            self.ws()
            c = self.peek()
            if c == ",":
                self.pos += 1
                continue
            if c == "}":
                self.pos += 1
                return result
            if c == "":
                raise self.error("unclosed '{' map")
            raise self.error(f"expected ',' or '}}' in map, found {c!r}")


# --- dumping -------------------------------------------------------------

_QUOTE_CHARS = set(":#,[]{}\"'\\\n\t\r")
_QUOTE_START = set("-?!&*|>%@`~ ")


def _fmt_str(s: str) -> str:
    plain = (
        s != ""
        and s == s.strip()
        and s[0] not in _QUOTE_START
        and not (set(s) & _QUOTE_CHARS)
        and _scalar(s) == s
    )
    if plain:
        return s
    return '"' + "".join(_ESCAPE_OUT.get(c, c) for c in s) + '"'


def _fmt_key(key: Any) -> str:
    key = str(key)
    return key if _PLAIN_KEY.match(key) else _fmt_str(key)


def _fmt_inline(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("cannot dump nan or infinity")
        return repr(value)
    if isinstance(value, str):
        return _fmt_str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt_inline(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_fmt_key(k)}: {_fmt_inline(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"cannot dump {type(value).__name__}")


def _dump_map(meta: Dict[str, Any], indent: int, out: List[str]) -> None:
    pad = " " * indent
    for key, value in meta.items():
        if isinstance(value, dict) and value and indent < INDENT * (MAX_MAP_DEPTH - 1):
            out.append(f"{pad}{_fmt_key(key)}:")
            _dump_map(value, indent + INDENT, out)
        else:
            out.append(f"{pad}{_fmt_key(key)}: {_fmt_inline(value)}")


def dump(meta: Dict[str, Any]) -> str:
    """Frontmatter text (without delimiters) in the supported subset."""
    out: List[str] = []
    _dump_map(meta, 0, out)
    return "\n".join(out) + "\n" if out else ""


def render(meta: Dict[str, Any], body: str) -> str:
    """A full document: delimited frontmatter followed by the body."""
    return f"{DELIMITER}\n{dump(meta)}{DELIMITER}\n{body}"
