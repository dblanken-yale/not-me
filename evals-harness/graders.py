#!/usr/bin/env python3
"""Grade a skill run from its Claude Code session transcript, against a case under evals-harness/cases/.

    python3 evals-harness/graders.py --case new-N2 --transcript SESSION.jsonl --workspace DIR [--json OUT]

Exits 0 when every grader that ran passed, 1 when one failed, 2 on bad input. Python API: `load_case`,
`grade` and `Report` (see below); transcripts come from `transcript.load`. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CASES = HERE / "cases"
for _p in (str(HERE), str(ROOT / "plugins" / "not-me")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import transcript as transcript_mod  # noqa: E402
from notme import frontmatter  # noqa: E402

SUPPORTED_TYPES = ("regex", "tool_used", "tool_order", "file_exists", "llm")
LLM_SKIP_REASON = "llm graders need a judge; advisory only"
FLAGS = {"i": re.I, "m": re.M, "s": re.S}


class CaseError(ValueError):
    """A case directory that cannot be read."""


@dataclass
class Grader:
    name: str
    type: str
    weight: float
    meta: Dict[str, Any]
    body: str


@dataclass
class Case:
    name: str
    path: Path
    meta: Dict[str, Any]
    prompt: str
    graders: List[Grader]


@dataclass
class Result:
    name: str
    type: str
    weight: float
    ok: bool
    detail: str
    skipped: bool = False


@dataclass
class Report:
    case: str
    score: float
    passed: bool
    graders: List[Result] = field(default_factory=list)
    skipped_llm: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"case": self.case, "score": self.score, "passed": self.passed, "skipped_llm": self.skipped_llm,
                "graders": [{"name": g.name, "type": g.type, "weight": g.weight, "ok": g.ok,
                             "detail": g.detail, "skipped": g.skipped} for g in self.graders]}


def load_case(case_dir: Path) -> Case:
    case_dir = Path(case_dir)
    prompt_file = case_dir / "prompt.md"
    try:
        meta, body = frontmatter.parse(prompt_file.read_text(encoding="utf-8"), source=str(prompt_file))
    except (OSError, frontmatter.FrontmatterError) as exc:
        raise CaseError(f"{prompt_file}: {exc}") from exc
    graders = []
    for path in sorted((case_dir / "graders").glob("*.md")):
        try:
            gmeta, gbody = frontmatter.parse(path.read_text(encoding="utf-8"), source=str(path))
        except (OSError, frontmatter.FrontmatterError) as exc:
            raise CaseError(f"{path}: {exc}") from exc
        weight = gmeta.get("weight", 1)
        graders.append(Grader(path.stem, str(gmeta.get("type", "")),
                              float(weight) if isinstance(weight, (int, float)) else 1.0, gmeta, gbody.strip()))
    return Case(str(meta.get("name") or case_dir.name), case_dir, meta, body.strip(), graders)


def _regex(pattern: str, flags: str) -> "re.Pattern[str]":
    bits = re.M
    for ch in str(flags or ""):
        bits |= FLAGS.get(ch, 0)
    return re.compile(pattern, bits)


def _spec(value: Any) -> Tuple[str, Optional[str]]:
    """A tool matcher: {tool, input_match}, or the string `name` / `name:input_regex`."""
    if isinstance(value, dict):
        return str(value.get("tool", "")), value.get("input_match")
    name, _, rest = str(value).partition(":")
    return name, rest or None


def _uses(tr: transcript_mod.Transcript, name: str, input_match: Optional[str]) -> List[int]:
    rx = re.compile(input_match) if input_match else None
    return [i for n, args, i in tr.tool_uses
            if n == name and (rx is None or rx.search(json.dumps(args, sort_keys=True)))]


def _check_regex(g: Grader, tr: transcript_mod.Transcript, workspace: Path) -> Tuple[bool, str]:
    target = g.meta.get("target", "last_message")
    if target == "trace":
        text = tr.trace
    elif target == "last_message":
        text = tr.last_message
    elif isinstance(target, dict) and target.get("source") == "file" and target.get("path"):
        try:
            text = (workspace / str(target["path"])).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    else:
        return False, f"unsupported regex target {target!r}"
    pattern = str(g.meta.get("pattern") or g.body)
    if not pattern:
        return False, "empty pattern"
    count = len(_regex(pattern, g.meta.get("flags", "")).findall(text))
    match = str(g.meta.get("match", "contains"))
    if match == "contains":
        return count > 0, f"{count} match(es) in {_target_name(target)}"
    if match == "not_contains":
        return count == 0, f"{count} match(es) in {_target_name(target)}"
    found = re.fullmatch(r"count:(\d+)", match)
    if found:
        return count == int(found.group(1)), f"{count} match(es), want {found.group(1)}"
    return False, f"unsupported match {match!r}"


def _target_name(target: Any) -> str:
    return target if isinstance(target, str) else str(target.get("path"))


def _check_tool_used(g: Grader, tr: transcript_mod.Transcript) -> Tuple[bool, str]:
    count = len(_uses(tr, str(g.meta.get("tool", "")), g.meta.get("input_match")))
    lo, hi = g.meta.get("min"), g.meta.get("max")
    if lo is None and hi is None:
        lo = 1
    ok = (lo is None or count >= lo) and (hi is None or count <= hi)
    want = ", ".join(f"{k} {v}" for k, v in (("min", lo), ("max", hi)) if v is not None)
    return ok, f"{count} use(s) of {g.meta.get('tool')}, want {want}"


def _check_tool_order(g: Grader, tr: transcript_mod.Transcript) -> Tuple[bool, str]:
    first = {}
    for side in ("before", "after"):
        name, pat = _spec(g.meta.get(side, ""))
        hits = _uses(tr, name, pat)
        if not hits:
            return False, f"no tool use matches {side}: {name}" + (f" /{pat}/" if pat else "")
        first[side] = hits[0]
    ok = first["before"] < first["after"]
    return ok, f"first before at #{first['before']}, first after at #{first['after']}"


def _check_file_exists(g: Grader, workspace: Path) -> Tuple[bool, str]:
    pattern = str(g.meta.get("path", ""))
    if (not pattern or Path(pattern).is_absolute() or pattern[0] in "/\\" or ".." in Path(pattern).parts
            or (len(pattern) > 1 and pattern[1] == ":")):  # drive letters and root slashes on every platform
        return False, f"path must be a relative glob inside the workspace: {pattern!r}"
    hits = sorted(workspace.glob(pattern)) if workspace.is_dir() else []
    want = g.meta.get("exists", True)
    return (bool(hits) == bool(want)), f"{len(hits)} match(es) for {pattern}, want {'some' if want else 'none'}"


def run_grader(g: Grader, tr: transcript_mod.Transcript, workspace: Path) -> Result:
    if g.type == "llm":
        return Result(g.name, g.type, g.weight, True, LLM_SKIP_REASON, skipped=True)
    try:
        if g.type == "regex":
            ok, detail = _check_regex(g, tr, workspace)
        elif g.type == "tool_used":
            ok, detail = _check_tool_used(g, tr)
        elif g.type == "tool_order":
            ok, detail = _check_tool_order(g, tr)
        elif g.type == "file_exists":
            ok, detail = _check_file_exists(g, workspace)
        else:
            ok, detail = False, f"unsupported grader type {g.type!r}"
    except re.error as exc:
        ok, detail = False, f"bad regex: {exc}"
    return Result(g.name, g.type, g.weight, ok, detail)


def grade(case: Case, tr: transcript_mod.Transcript, workspace: Path) -> Report:
    results = [run_grader(g, tr, Path(workspace)) for g in case.graders]
    ran = [r for r in results if not r.skipped]
    total = sum(r.weight for r in ran)
    score = (sum(r.weight for r in ran if r.ok) / total) if total else 0.0
    passed = bool(ran) and all(r.ok for r in ran)
    return Report(case.name, round(score, 4), passed, results, sum(1 for r in results if r.skipped))


def format_table(report: Report) -> str:
    width = max([len(r.name) for r in report.graders] + [4])
    lines = [f"{'type':11} {'result':6} {'name':{width}}  detail"]
    for r in report.graders:
        status = "skip" if r.skipped else ("pass" if r.ok else "FAIL")
        lines.append(f"{r.type:11} {status:6} {r.name:{width}}  {r.detail}")
    lines.append(f"{report.case}: score {report.score:.2f}, {'passed' if report.passed else 'FAILED'}"
                 f" ({report.skipped_llm} llm grader(s) skipped)")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None, out: Any = None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case", required=True, help="case name under evals-harness/cases/, or a case directory")
    ap.add_argument("--transcript", required=True, help="session .jsonl file")
    ap.add_argument("--workspace", required=True, help="the checkout the session ran in")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)
    case_dir = Path(args.case) if Path(args.case).is_dir() else CASES / args.case
    try:
        case = load_case(case_dir)
        if not Path(args.transcript).is_file():
            raise CaseError(f"no transcript at {args.transcript}")
        report = grade(case, transcript_mod.load(Path(args.transcript)), Path(args.workspace))
    except (CaseError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_table(report), file=out)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
