#!/usr/bin/env python3
"""Static lint for evals-harness/cases/*/prompt.md and their graders.

Prints ``path:line: RULE: message`` per violation and exits 1, or exits 0.
Rules: E1 frontmatter parses, E2 tags, E4 graders, E5 live assertions, E6 live scenario grading.
(E3, the old ablation rule, went with `claude plugin eval`; the ids are not renumbered.)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Set, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "plugins" / "not-me"))
from notme import frontmatter  # noqa: E402

RULES = {"E1", "E2", "E4", "E5", "E6"}
SCENARIO_ID = re.compile(r"(?:^|-)([A-Z]\d+)(?:-|$)")
CLASSES = {"safety", "outcome", "quality"}
GRADER_TYPES = {"regex", "tool_used", "tool_order", "file_exists", "llm"}

Violation = Tuple[str, int, str, str]


def known_expect_keys(harness: Path) -> Optional[Set[str]]:
    """The expect keys the harness can grade, read from assertions/keys.py (None if it is missing)."""
    path = harness / "assertions" / "keys.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_harness_keys", str(path))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return set(module.KNOWN_KEYS)


def scenario_problem(path: Path, harness: Path) -> Optional[str]:
    """Why a scenario file cannot be graded from its expect block, or None when it can."""
    if not path.is_file():
        return f"has no scenario file {path.name} and no assertions module"
    try:
        expect = json.loads(path.read_text(encoding="utf-8")).get("expect")
    except (OSError, ValueError, AttributeError):
        return "scenario file does not parse"
    if not isinstance(expect, dict) or not expect:
        return "has no expect block and no assertions module"
    known = known_expect_keys(harness)
    unknown = sorted(set(expect) - known) if known is not None else []
    return f"has expect keys the harness cannot grade: {', '.join(unknown)}" if unknown else None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(REPO), help="repo root (default: this repo)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--only", default="", help="comma-separated rule ids, e.g. E1,E4")
    args = ap.parse_args(argv)
    only = {r.strip().upper() for r in args.only.split(",") if r.strip()} or set(RULES)
    if only - RULES:
        ap.error(f"unknown rule id(s): {', '.join(sorted(only - RULES))}")

    root = Path(args.root).resolve()
    evals = root / "evals-harness" / "cases"
    harness = root / "evals-harness"
    violations: List[Violation] = []
    notices: List[str] = []

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    def add(p: Path, line: int, rule: str, msg: str) -> None:
        violations.append((rel(p), max(line, 1), rule, msg))

    prompts = sorted(evals.glob("*/prompt.md")) if evals.is_dir() else []
    if not prompts:
        notices.append("no evals: evals-harness/cases/ has no cases")

    for prompt in prompts:
        case = prompt.parent.name
        try:
            meta, _body, lines = frontmatter.parse_with_lines(
                prompt.read_text(encoding="utf-8", errors="replace"), source=rel(prompt))
        except frontmatter.FrontmatterError as exc:
            add(prompt, exc.line or 1, "E1", f"frontmatter does not parse: {exc.message}")
            meta, lines = {}, {}
        else:
            if not meta:
                add(prompt, 1, "E1", "missing frontmatter")

        tags = meta.get("tags")
        tags = tags if isinstance(tags, list) else []
        at = lines.get(("tags",), 1)
        skills = [t for t in tags if isinstance(t, str) and t.startswith("skill:") and len(t) > 6]
        classes = [t for t in tags if isinstance(t, str) and t.startswith("class:")]
        if len(skills) != 1:
            add(prompt, at, "E2", f"tags must contain exactly one skill:<x> (found {len(skills)})")
        if len(classes) != 1 or classes[0][6:] not in CLASSES:
            add(prompt, at, "E2", "tags must contain exactly one class:safety|outcome|quality")

        graders = sorted((prompt.parent / "graders").glob("*.md"))
        if not graders:
            add(prompt, 1, "E4", "graders/ needs at least one grader file")
        for grader in graders:
            try:
                gmeta, _b, glines = frontmatter.parse_with_lines(
                    grader.read_text(encoding="utf-8", errors="replace"), source=rel(grader))
            except frontmatter.FrontmatterError as exc:
                add(grader, exc.line or 1, "E4", f"frontmatter does not parse: {exc.message}")
                continue
            if gmeta.get("type") not in GRADER_TYPES:
                add(grader, glines.get(("type",), 1), "E4",
                    f"grader type {gmeta.get('type')!r} is not one of {', '.join(sorted(GRADER_TYPES))}")

        if "live" in tags:
            found = SCENARIO_ID.search(case)
            sid = found.group(1) if found else None
            modules = [harness / "assertions" / f"{name}.py" for name in (case, sid) if name]
            has_module = any(m.is_file() for m in modules)
            if not has_module and sid is None:
                add(prompt, at, "E5", f"live case needs {rel(harness)}/assertions/{case}.py "
                    "or a scenario id (like N1) in its name")
            elif not has_module:
                problem = scenario_problem(root / "fixture" / "scenarios" / f"{sid}.json", harness)
                if problem:
                    add(prompt, at, "E6", f"live case {case}: scenario {sid} {problem}")

    violations = sorted((v for v in set(violations) if v[2] in only), key=lambda v: (v[0], v[1], v[2], v[3]))
    if args.json:
        print(json.dumps({
            "violations": [{"path": p, "line": n, "rule": r, "message": m} for p, n, r, m in violations],
            "notices": notices,
            "count": len(violations),
        }, indent=2))
    else:
        for notice in notices:
            print(f"notice: {notice}")
        for p, n, r, m in violations:
            print(f"{p}:{n}: {r}: {m}")
        if not violations:
            print(f"lint-evals: clean ({len(prompts)} cases, 0 violations)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
