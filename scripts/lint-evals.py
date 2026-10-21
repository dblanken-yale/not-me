#!/usr/bin/env python3
"""Static lint for plugins/not-me/evals/*/prompt.md and their graders.

Prints ``path:line: RULE: message`` per violation and exits 1, or exits 0.
Rules: E1 frontmatter parses, E2 tags, E3 ablation, E4 graders, E5 live assertions.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Set, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "plugins" / "not-me"))
from notme import frontmatter  # noqa: E402

RULES = {"E1", "E2", "E3", "E4", "E5"}
CLASSES = {"safety", "outcome", "quality"}
GRADER_TYPES = {"regex", "tool_used", "tool_order", "file_exists", "llm", "baseline"}

Violation = Tuple[str, int, str, str]


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
    plugin = root / "plugins" / "not-me"
    evals = plugin / "evals"
    violations: List[Violation] = []
    notices: List[str] = []

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    def add(p: Path, line: int, rule: str, msg: str) -> None:
        violations.append((rel(p), max(line, 1), rule, msg))

    prompts = sorted(evals.glob("*/prompt.md")) if evals.is_dir() else []
    if not prompts:
        notices.append("no evals: plugins/not-me/evals/ has no cases")
    harness = next((b for b in (root / "evals-harness", plugin / "evals-harness") if b.is_dir()), None)
    if prompts and harness is None:
        notices.append("E5 skipped: evals-harness/ is absent")

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

        if "ablation" in meta and meta["ablation"] != "none":
            add(prompt, lines.get(("ablation",), 1), "E3", f"ablation must be 'none' (found {meta['ablation']!r})")

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

        if harness is not None and "live" in tags and not (harness / "assertions" / f"{case}.py").is_file():
            add(prompt, at, "E5", f"live case needs {rel(harness)}/assertions/{case}.py")

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
