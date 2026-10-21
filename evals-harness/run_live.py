#!/usr/bin/env python3
"""Run one live eval scenario end to end: seed, run the skill under `claude plugin eval`, grade, tear down.

    python3 evals-harness/run_live.py --skill new --scenario N1 [--runs N] [--backend github|ledger]
        [--repo O/N] [--companion O/N] [--owner O] [--cap USD] [--out DIR] [--dry-run]

Per run: seed_fixture.py --reset, seed_fixture.py --scenario, claude plugin eval, grade_state.py, then
seed_fixture.py --teardown (also when a step failed). One result file per run: <out>/<ID>-<n>.json.
Runs are sequential on purpose: a skill scans the whole fixture repo, so two runs would see each other.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SEED = ROOT / "fixture" / "seed_fixture.py"
GRADE = HERE / "grade_state.py"
PLUGIN_DIR = ROOT / "plugins" / "not-me"
STATE_DIR = ROOT / "evals" / ".state"
COST_KEYS = ("total_cost_usd", "cost_usd", "total_cost", "cost")

Runner = Callable[[Sequence[str], Dict[str, str]], int]


def subprocess_runner(argv: Sequence[str], env: Dict[str, str]) -> int:
    return subprocess.run(list(argv), env=env, cwd=str(ROOT)).returncode


def quote(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(a)) for a in argv)


def new_run_id(scenario: str, n: int) -> str:
    return f"{scenario.lower()}-{n}-{time.strftime('%y%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"


def find_cost(data: Any) -> Optional[float]:
    """Best-effort: the first numeric value under a cost-like key, searched breadth first."""
    queue = [data]
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            for key in COST_KEYS:
                if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool):
                    return float(item[key])
            queue += list(item.values())
        elif isinstance(item, list):
            queue += item
    return None


class Plan:
    """The commands for one run, built once so --dry-run and a real run cannot drift apart."""

    def __init__(self, args: argparse.Namespace, rid: str, n: int, out: Path):
        self.rid, self.n, self.out = rid, n, out
        flags: List[str] = ["--repo", args.repo, "--owner", args.owner]
        if args.companion:
            flags += ["--companion", args.companion]
        py = sys.executable or "python3"
        self.manifest = STATE_DIR / rid / f"{args.scenario}.json"
        self.eval_dir = out / f"{args.scenario}-{n}.eval"
        self.grade_json = out / f"{args.scenario}-{n}.grade.json"
        self.result = out / f"{args.scenario}-{n}.json"
        self.scaffold = out / f"{args.scenario}-{n}.scaffold.json"
        self.ledger = out / f"{args.scenario}-{n}.ledger.jsonl"
        self.reset = [py, str(SEED), *flags, "--reset", "--run-id", rid]
        self.seed = [py, str(SEED), *flags, "--scenario", args.scenario, "--run-id", rid]
        self.evaluate = ["claude", "plugin", "eval", str(PLUGIN_DIR.relative_to(ROOT)),
                         "--case", f"{args.skill}-{args.scenario}", "--runs", "1", "--scaffold", "--trust-plugin",
                         "--ablation", "none", "--allow-tools", "Bash,Write,Edit",
                         "--max-cost-usd", f"{args.cap:g}", "--json", str(self.eval_dir), "--no-publish"]
        grade = [py, str(GRADE), "--manifest", str(self.manifest), "--scenario", args.scenario,
                 "--backend", args.backend, "--json", str(self.grade_json)]
        if args.backend == "ledger":
            grade += ["--ledger", str(self.ledger)]
        self.grade = grade
        self.teardown = [py, str(SEED), *flags, "--teardown", rid]
        self.env_extra = {"NOT_ME_EVAL_RUN_ID": rid, "NOT_ME_EVAL_SCENARIO": args.scenario,
                          "NOT_ME_EVAL_BACKEND": args.backend, "NOT_ME_EVAL_MANIFEST": str(self.manifest),
                          "NOT_ME_FIXTURE_REPO": args.repo, "NOT_ME_FIXTURE_OWNER": args.owner}
        if args.companion:
            self.env_extra["NOT_ME_FIXTURE_COMPANION"] = args.companion
        if args.backend == "ledger":
            self.env_extra["NOT_ME_EVAL_LEDGER"] = str(self.ledger)

    def commands(self) -> List[List[str]]:
        return [self.reset, self.seed, self.evaluate, self.grade, self.teardown]


def merge_result(args: argparse.Namespace, plan: Plan, eval_exit: Optional[int], errors: List[str]) -> dict:
    aggregate = None
    agg_path = plan.eval_dir / "aggregate-result.json"
    if agg_path.is_file():
        try:
            aggregate = json.loads(agg_path.read_text(encoding="utf-8"))
        except ValueError:
            errors.append("aggregate-result.json is not valid JSON")
    grade = None
    if plan.grade_json.is_file():
        try:
            grade = json.loads(plan.grade_json.read_text(encoding="utf-8"))
        except ValueError:
            errors.append("grade output is not valid JSON")
    if grade is None:
        errors.append("no grade output")
    return {"scenario": args.scenario, "skill": args.skill, "run": plan.n, "run_id": plan.rid,
            "backend": args.backend, "repo": args.repo,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "eval_exit": eval_exit, "eval": aggregate, "cost_usd": find_cost(aggregate),
            "grade": grade, "errors": errors, "ok": bool(grade and grade.get("ok")) and not errors}


def run_once(args: argparse.Namespace, n: int, runner: Runner, rid_factory: Callable[[str, int], str],
             out: Path, say: Callable[[str], None]) -> int:
    rid = rid_factory(args.scenario, n)
    plan = Plan(args, rid, n, out)
    env = dict(os.environ)  # GH_TOKEN and the claude login pass through
    env.update(plan.env_extra)
    say(f"# run {n} scenario={args.scenario} run-id={rid}")
    if args.dry_run:
        for cmd in plan.commands():
            say("+ " + quote(cmd))
        say(f"+ (would write {plan.scaffold})")
        say(f"+ (would merge into {plan.result})")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    errors: List[str] = []
    eval_exit: Optional[int] = None
    started = False
    try:
        started = True
        for label, cmd in (("reset", plan.reset), ("seed", plan.seed)):
            say("+ " + quote(cmd))
            if runner(cmd, env) != 0:
                raise RuntimeError(f"{label} failed")
        plan.scaffold.write_text(json.dumps(plan.env_extra, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        say("+ " + quote(plan.evaluate))
        eval_exit = runner(plan.evaluate, env)
        if eval_exit != 0:
            errors.append(f"claude plugin eval exited {eval_exit}")
        say("+ " + quote(plan.grade))
        grade_exit = runner(plan.grade, env)
        if grade_exit not in (0, 1):
            errors.append(f"grade_state exited {grade_exit}")
    except (RuntimeError, OSError) as exc:
        errors.append(str(exc))
    finally:
        if started:
            say("+ " + quote(plan.teardown))
            try:
                if runner(plan.teardown, env) != 0:
                    errors.append("teardown failed: the fixture may need --reset")
            except OSError as exc:
                errors.append(f"teardown failed: {exc}")
    result = merge_result(args, plan, eval_exit, errors)
    plan.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    say(f"result: {plan.result} ok={result['ok']}")
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skill", required=True, help="skill name; the case is <skill>-<scenario>")
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--backend", choices=("github", "ledger"), default="github")
    ap.add_argument("--repo", default=os.environ.get("NOT_ME_FIXTURE_REPO"))
    ap.add_argument("--companion", default=os.environ.get("NOT_ME_FIXTURE_COMPANION"))
    ap.add_argument("--owner", default=os.environ.get("NOT_ME_FIXTURE_OWNER"))
    ap.add_argument("--cap", type=float, default=6.0, help="--max-cost-usd for each claude plugin eval call")
    ap.add_argument("--out", default=str(ROOT / "results" / "evals" / time.strftime("%Y-%m-%d")),
                    help="result directory (default: results/evals/<today>)")
    ap.add_argument("-j", "--jobs", type=int, choices=(1,), default=1, help="always 1: a skill scans the whole repo")
    ap.add_argument("--dry-run", action="store_true", help="print the commands and run nothing")
    return ap


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner,
         rid_factory: Callable[[str, int], str] = new_run_id, out: Any = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out or sys.stdout

    def say(line: str) -> None:
        print(line, file=stream)

    missing = [f"--{k}" for k in ("repo", "owner") if not getattr(args, k)]
    if missing:
        print(f"error: {', '.join(missing)} required (or NOT_ME_FIXTURE_REPO / NOT_ME_FIXTURE_OWNER)", file=sys.stderr)
        return 2
    if args.runs < 1:
        print("error: --runs must be at least 1", file=sys.stderr)
        return 2
    outdir = Path(args.out)
    taken = [int(p.stem.rsplit("-", 1)[1]) for p in outdir.glob(f"{args.scenario}-*.json")
             if p.stem.rsplit("-", 1)[1].isdigit()] if outdir.is_dir() else []
    first = max(taken, default=0) + 1  # a second invocation the same day adds runs instead of overwriting
    worst = 0
    for n in range(first, first + args.runs):
        worst = max(worst, run_once(args, n, runner, rid_factory, outdir, say))
    return worst


if __name__ == "__main__":
    sys.exit(main())
