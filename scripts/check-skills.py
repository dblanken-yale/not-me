#!/usr/bin/env python3
"""Static lint for the not-me plugin's skills, references and defaults.

Prints one line per violation as ``path:line: RULE: message`` and exits 1, or
prints a count and exits 0 when clean. See RULES for the rule ids.
"""
from __future__ import annotations

import argparse
import argparse as _argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "plugins" / "not-me"))
from notme import frontmatter  # noqa: E402

RULES = {
    "R1": "frontmatter",
    "R2": "references resolve",
    "R3": "CLI commands exist",
    "R4": "no absolute personal paths or bare /tmp",
    "R5": "denylist",
    "R6": "no raw GitHub writes",
    "R7": "no graphql mutation",
    "R8": "no add and remove of the same label",
    "R9": "post markers",
    "R10": "prohibitions reference",
    "R11": "no test or lint commands in skills",
    "R12": "CLAUDE_PLUGIN_ROOT usage",
    "R13": "--bg verb list",
    "R14": "no em dashes",
    "R15": "state changes say why",
}

ALLOW_MARKER = "<!-- allowed-raw-write -->"
# A notme call as skill prose writes it: notme, "${CLAUDE_PLUGIN_ROOT}/bin/notme" or "$NOTME", then the command.
_CALL = r"(?:\bnotme\b\"?|\$\{?NOTME\}?\"?)\s+(?P<cmd>[a-z][a-z-]*)"
CALL_RE = re.compile(_CALL)
# Commands whose every call must say why. labels-sync is exempt with --check, which writes nothing, and
# release-issue with --no-comment, which a failure on not-me's own side uses.
REASON_COMMANDS = {"move-issue", "release-issue", "transition", "labels-sync"}


def reason_violations(text: str) -> List[Tuple[int, str]]:
    """(line, call) for each REASON_COMMANDS call in text with no --reason-file. A call runs from its command to the
    closing backtick, the next blank line or the next notme call, so a later call's flag never covers an earlier
    one."""
    out: List[Tuple[int, str]] = []
    for m in CALL_RE.finditer(text):
        if m.group("cmd") not in REASON_COMMANDS:
            continue
        rest = text[m.end():]
        ends = [i for i in (rest.find("`"), rest.find("\n\n")) if i != -1]
        nxt = CALL_RE.search(rest)
        if nxt:
            ends.append(nxt.start())
        span = rest[:min(ends)] if ends else rest
        if "--reason-file" in span or (m.group("cmd") == "labels-sync" and "--check" in span) \
                or (m.group("cmd") == "release-issue" and "--no-comment" in span):
            continue
        out.append((text[:m.start()].count("\n") + 1, text[m.start():m.end()]))
    return out

VERBS = "NONE | PR | PUSHED | PARKED | HELD | REVIEWED | PROVED | FAILED | STOP"
BASH_TAGS = {"bash", "sh", "shell", "zsh", "console"}
# The repo slug covers the URL and the "marketplace add" install command.
EXEMPT_URL = "dblanken-yale/not-me"
EXEMPT_AUTHOR = '"name": "David Blankenship"'
EM_DASH = "\u2014"

Violation = Tuple[str, int, str, str]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def fenced_lines(text: str) -> Iterator[Tuple[int, str, bool, str, bool]]:
    """Yield (lineno, line, in_fence, fence_info, allowed) for each line.

    Fence marker lines count as inside the fence. ``allowed`` is true inside a
    fence whose previous line is the allowed-raw-write marker.
    """
    in_fence = False
    info = ""
    allowed = False
    prev = ""
    for no, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("```", "~~~")):
            if not in_fence:
                in_fence = True
                info = line.lstrip()[3:].strip().split(" ")[0].lower()
                allowed = prev.strip() == ALLOW_MARKER
                yield no, line, True, info, allowed
            else:
                yield no, line, True, info, allowed
                in_fence = False
                info = ""
                allowed = False
        else:
            yield no, line, in_fence, info, allowed
        prev = line


class Linter:
    def __init__(self, root: Path):
        self.root = root
        self.plugin = root / "plugins" / "not-me"
        self.violations: List[Violation] = []
        self.notices: List[str] = []
        self.skill_mds = sorted((self.plugin / "skills").glob("*/SKILL.md"))
        self.skill_docs = sorted((self.plugin / "skills").rglob("*.md"))
        self.ref_docs = sorted((self.plugin / "references").rglob("*.md"))
        self.default_docs = sorted((self.plugin / "defaults").rglob("*.md"))
        self.texts: Dict[Path, str] = {}

    def text(self, path: Path) -> str:
        if path not in self.texts:
            self.texts[path] = read(path)
        return self.texts[path]

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def add(self, path: Path, line: int, rule: str, message: str) -> None:
        self.violations.append((self.rel(path), max(line, 1), rule, message))

    def lines_matching(self, path: Path, pattern: "re.Pattern[str]") -> Iterator[Tuple[int, str, "re.Match[str]"]]:
        for no, line in enumerate(self.text(path).splitlines(), start=1):
            for m in pattern.finditer(line):
                yield no, line, m

    # R1 ---------------------------------------------------------------
    def meta(self, path: Path) -> Tuple[Dict, Dict]:
        try:
            meta, _body, lines = frontmatter.parse_with_lines(self.text(path), source=self.rel(path))
            return meta, lines
        except frontmatter.FrontmatterError as exc:
            self.add(path, exc.line or 1, "R1", f"frontmatter does not parse: {exc.message}")
            return {}, {}

    def r1(self) -> None:
        for path in self.skill_mds:
            meta, lines = self.meta(path)
            if not meta and not self.text(path).startswith("---"):
                self.add(path, 1, "R1", "missing frontmatter")
                continue
            name = meta.get("name")
            desc = meta.get("description")
            if not name:
                self.add(path, 1, "R1", "frontmatter is missing 'name'")
            elif name != path.parent.name:
                self.add(path, lines.get(("name",), 1), "R1",
                         f"name '{name}' does not match directory '{path.parent.name}'")
            if not desc or not isinstance(desc, str):
                self.add(path, 1, "R1", "frontmatter is missing 'description'")
            else:
                at = lines.get(("description",), 1)
                if len(desc) > 1024:
                    self.add(path, at, "R1", f"description is {len(desc)} chars (max 1024)")
                if "NOT for" not in desc and "Not for" not in desc:
                    self.add(path, at, "R1", "description must say 'NOT for' what the skill is not for")

    # R2 ---------------------------------------------------------------
    def r2(self) -> None:
        pat = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./\-]+)")
        for path in self.skill_docs + self.ref_docs + self.default_docs:
            for no, line, m in self.lines_matching(path, pat):
                nxt = line[m.end():m.end() + 1]
                if nxt in ("<", "*", "{", "$"):
                    continue  # a template, not a concrete path
                rel = m.group(1).rstrip(".,;:)")
                if rel and not (self.plugin / rel).exists():
                    self.add(path, no, "R2", f"${{CLAUDE_PLUGIN_ROOT}}/{rel} does not exist")

    # R3 ---------------------------------------------------------------
    def known_commands(self) -> Optional[Set[str]]:
        cli = self.plugin / "notme" / "cli.py"
        if cli.is_file():
            if str(self.plugin) not in sys.path:
                sys.path.append(str(self.plugin))
            try:
                spec = importlib.util.spec_from_file_location("_notme_cli_under_lint", str(cli))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)  # type: ignore[union-attr]
                parser = module.build_parser()
                found: Set[str] = set()
                for action in parser._actions:
                    if isinstance(action, _argparse._SubParsersAction):
                        found.update(action.choices.keys())
                return found
            except Exception as exc:  # report, do not crash the lint
                self.add(cli, 1, "R3", f"cannot read subcommands from build_parser(): {exc!r}")
                return None
        for base in (self.root / "scripts", HERE):
            fallback = base / "known-commands.txt"
            if fallback.is_file():
                return {ln.split()[0] for ln in read(fallback).splitlines()
                        if ln.strip() and not ln.strip().startswith("#")}
        self.notices.append("R3 skipped: plugins/not-me/notme/cli.py and scripts/known-commands.txt are both absent")
        return None

    def r3(self) -> None:
        known = self.known_commands()
        if known is None:
            return
        long_form = re.compile(r'\$\{CLAUDE_PLUGIN_ROOT\}/bin/notme"?\s+([A-Za-z][A-Za-z-]*)')
        bare = re.compile(r'(?:^|(?<=[`(|&;$\s]))notme\s+([A-Za-z][A-Za-z-]*)')
        for path in self.skill_docs + self.ref_docs:
            for no, line in enumerate(self.text(path).splitlines(), start=1):
                words = [m.group(1) for m in long_form.finditer(line)]
                stripped = long_form.sub("", line)
                for m in bare.finditer(stripped):
                    before = stripped[:m.start()]
                    if before.endswith(("/", "-", ".")):
                        continue
                    words.append(m.group(1))
                for word in words:
                    if word not in known:
                        self.add(path, no, "R3", f"unknown notme subcommand '{word}'")

    # R4 ---------------------------------------------------------------
    def r4(self) -> None:
        pat = re.compile(r"/Users/|/home/|C:\\|~/code|(?<![\w$}])(?<!not )/tmp(?=/|\b)")
        for path in self.skill_docs + self.ref_docs + self.default_docs:
            for no, _line, m in self.lines_matching(path, pat):
                self.add(path, no, "R4", f"absolute personal path or bare temp dir '{m.group(0)}'")

    # R5 ---------------------------------------------------------------
    def r5(self) -> None:
        deny_file = None
        for base in (self.root / "scripts", HERE):
            if (base / "lint-denylist.txt").is_file():
                deny_file = base / "lint-denylist.txt"
                break
        if deny_file is None:
            self.notices.append("R5 skipped: scripts/lint-denylist.txt is absent")
            return
        tokens = [ln.strip().lower() for ln in read(deny_file).splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
        for path in self.r5_files():
            rel = self.rel(path)
            if rel == "LICENSE" or "/fixture/" in "/" + rel or rel.startswith("docs/migration/") \
                    or rel.startswith("plugins/not-me/tests/fixtures/legacy/"):
                continue
            for no, line in enumerate(self.text(path).splitlines(), start=1):
                low = line.replace(EXEMPT_URL, "").replace(EXEMPT_AUTHOR, "").lower()
                for token in tokens:
                    if token in low:
                        self.add(path, no, "R5", f"denylisted token '{token}'")

    def r5_files(self) -> List[Path]:
        found: List[Path] = []
        plugin = self.plugin
        found += sorted(plugin.rglob("*.md"))
        found += sorted((plugin / "notme").rglob("*.py"))
        for sub in ("hooks", "bin"):
            found += sorted(p for p in (plugin / sub).rglob("*") if p.is_file())
        if (self.root / "README.md").is_file():
            found.append(self.root / "README.md")
        found += sorted(p for p in (self.root / "docs").rglob("*") if p.is_file())
        seen: Set[Path] = set()
        out = []
        for p in found:
            if p not in seen and p.is_file() and p.stat().st_size < 2_000_000:
                seen.add(p)
                out.append(p)
        return out

    # R6, R7, R8, R11, R12 (per skill doc) -----------------------------
    def r6_to_r12_lines(self) -> None:
        write_pats = [re.compile(p, re.I) for p in (
            r"\bgh\s+(?:pr|issue)\s+(?:edit|comment)\b",
            r"\bgh\s+pr\s+(?:review|create)\b",
            r"\bgh\s+api\b[^\n]*?(?:-X[ =]?(?:POST|PATCH|PUT|DELETE)\b|--method\b)",
            r"\bgit\s+push\b",
        )]
        tools = re.compile(r"\b(?:npm test|pytest|ruff|phpunit|composer)\b", re.I)
        bare_root = re.compile(r"\$CLAUDE_PLUGIN_ROOT\b")
        for path in self.skill_docs:
            text = self.text(path)
            low = text.lower()
            if "graphql" in low and "mutation" in low:
                no = next((n for n, ln in enumerate(text.splitlines(), 1) if "mutation" in ln.lower()), 1)
                self.add(path, no, "R7", "graphql mutation in skill prose; use the notme CLI")
            pending = ""
            pending_start = 0
            for no, line, in_fence, info, allowed in fenced_lines(text):
                if not allowed:
                    for pat in write_pats:
                        m = pat.search(line)
                        if m:
                            self.add(path, no, "R6", f"raw GitHub write '{m.group(0)}' outside an allowed-raw-write block")
                            break
                m = tools.search(line)
                if m:
                    self.add(path, no, "R11", f"'{m.group(0)}' belongs in verify.md, not in a skill")
                if in_fence and info in BASH_TAGS and bare_root.search(line):
                    self.add(path, no, "R12", "bare $CLAUDE_PLUGIN_ROOT in a bash block; use the ${CLAUDE_PLUGIN_ROOT} literal")
                # R8 on logical lines (backslash continuations joined)
                if not pending:
                    pending_start = no
                if line.rstrip().endswith("\\"):
                    pending += line.rstrip()[:-1] + " "
                    continue
                logical = pending + line
                pending = ""
                if self.label_clash(logical):
                    self.add(path, pending_start, "R8", "same label in --add-label and --remove-label")

    @staticmethod
    def label_clash(line: str) -> bool:
        def labels(flag: str) -> Set[str]:
            out: Set[str] = set()
            for m in re.finditer(flag + r"[ =]+([\"']?)([^\s\"']+)\1", line):
                out.update(x for x in m.group(2).split(",") if x)
            return out
        return bool(labels("--add-label") & labels("--remove-label"))

    # R9, R10, R12 (skill level), R13 ---------------------------------
    def r9_to_r13_skills(self) -> None:
        marker_re = re.compile(r"<!--\s*not-me:([A-Za-z0-9_-]+)\s*-->")
        seen: Dict[str, Path] = {}
        for path in self.skill_mds:
            skill = path.parent.name
            text = self.text(path)
            meta, lines = {}, {}
            try:
                meta, _b, lines = frontmatter.parse_with_lines(text, source=self.rel(path))
            except frontmatter.FrontmatterError:
                pass  # already reported by R1
            markers = {}
            for no, line in enumerate(text.splitlines(), start=1):
                for m in marker_re.finditer(line):
                    markers.setdefault(m.group(1), no)
            trigger = re.search(r"comment|review|PR body", text, re.I)
            if trigger and skill not in markers:
                no = text[:trigger.start()].count("\n") + 1
                self.add(path, no, "R9", f"skill posts but does not name its marker <!-- not-me:{skill} -->")
            for name, no in markers.items():
                if name in seen and seen[name] != path:
                    self.add(path, no, "R9", f"marker <!-- not-me:{name} --> is also used in {self.rel(seen[name])}")
                else:
                    seen.setdefault(name, path)
            if skill in ("new", "update", "review") and \
                    "${CLAUDE_PLUGIN_ROOT}/references/prohibitions.md" not in text:
                self.add(path, 1, "R10", "skill must reference ${CLAUDE_PLUGIN_ROOT}/references/prohibitions.md")
            if "CLAUDE_PLUGIN_ROOT" in text and "substituted at load time" not in text:
                no = next(n for n, ln in enumerate(text.splitlines(), 1) if "CLAUDE_PLUGIN_ROOT" in ln)
                self.add(path, no, "R12", "skill must say the ${CLAUDE_PLUGIN_ROOT} path is substituted at load time")
            hint = meta.get("argument-hint")
            if isinstance(hint, str) and "--bg" in hint and VERBS not in text:
                self.add(path, lines.get(("argument-hint",), 1), "R13",
                         f"--bg skill must contain the literal verb list '{VERBS}'")

    # R14 --------------------------------------------------------------
    def r14(self) -> None:
        for path in self.skill_docs + self.ref_docs + self.default_docs:
            for no, line in enumerate(self.text(path).splitlines(), start=1):
                if EM_DASH in line:
                    self.add(path, no, "R14", "em dash (U+2014)")

    # R15 --------------------------------------------------------------
    def r15(self) -> None:
        """Every call that changes a state passes --reason-file (see reason_violations)."""
        for path in self.skill_docs + self.ref_docs:
            for no, found in reason_violations(self.text(path)):
                self.add(path, no, "R15", f"'{found}' without --reason-file: every state change says why")

    def run(self, only: Set[str]) -> None:
        if "R1" in only:
            self.r1()
        if "R2" in only:
            self.r2()
        if "R3" in only:
            self.r3()
        if "R4" in only:
            self.r4()
        if "R5" in only:
            self.r5()
        if only & {"R6", "R7", "R8", "R11", "R12"}:
            self.r6_to_r12_lines()
        if only & {"R9", "R10", "R12", "R13"}:
            self.r9_to_r13_skills()
        if "R14" in only:
            self.r14()
        if "R15" in only:
            self.r15()
        self.violations = sorted({v for v in self.violations if v[2] in only},
                                 key=lambda v: (v[0], v[1], int(v[2][1:]), v[3]))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(REPO), help="repo root (default: this repo)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--only", default="", help="comma-separated rule ids, e.g. R1,R5")
    args = ap.parse_args(argv)
    only = {r.strip().upper() for r in args.only.split(",") if r.strip()} or set(RULES)
    unknown = only - set(RULES)
    if unknown:
        ap.error(f"unknown rule id(s): {', '.join(sorted(unknown))}")
    linter = Linter(Path(args.root).resolve())
    linter.run(only)
    if args.json:
        print(json.dumps({
            "violations": [{"path": p, "line": n, "rule": r, "message": m} for p, n, r, m in linter.violations],
            "notices": linter.notices,
            "count": len(linter.violations),
        }, indent=2))
    else:
        for notice in linter.notices:
            print(f"notice: {notice}")
        for p, n, r, m in linter.violations:
            print(f"{p}:{n}: {r}: {m}")
        if not linter.violations:
            print(f"check-skills: clean ({len(linter.skill_mds)} skills, 0 violations)")
    return 1 if linter.violations else 0


if __name__ == "__main__":
    sys.exit(main())
