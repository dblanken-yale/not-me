#!/usr/bin/env python3
"""Grade GitHub state (or the dry-run ledger) after a skill run.

    python3 evals-harness/grade_state.py --manifest PATH --scenario ID --backend github|ledger \
        [--ledger PATH] [--guard-log PATH] [--run-log PATH] [--json OUT]

Uses assertions/<ID>.py when it exists (it defines checks(ctx)), else the manifest's `expect` block.
Exit 1 when any safety or outcome assertion fails; quality results never change the exit code.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from assertions import common, generic  # noqa: E402
import readers  # noqa: E402


def load_checks(ctx: common.Ctx, scenario: str, assertions_dir: Optional[Path] = None) -> List[common.Check]:
    module_path = (assertions_dir or HERE / "assertions") / f"{scenario}.py"
    if not module_path.is_file():
        return generic.build_checks(ctx)
    spec = importlib.util.spec_from_file_location(f"assertions_{scenario}", str(module_path))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return list(module.checks(ctx))


def grade(manifest: dict, scenario: str, backend: str, runner: Optional[Callable] = None,
          ledger: Optional[str] = None, guard_log: Optional[str] = None, run_log: Optional[str] = None,
          assertions_dir: Optional[Path] = None) -> dict:
    if backend == "ledger":
        if not ledger:
            raise ValueError("--backend ledger needs --ledger PATH")
        reader: Any = readers.LedgerReader(ledger, manifest)
    else:
        reader = readers.GhReader(runner, manifest)
    ctx = common.Ctx(manifest, reader, guard_log=guard_log, run_log=run_log)
    results = []
    for check in load_checks(ctx, scenario, assertions_dir):
        try:
            res = check.fn(ctx)
        except Exception as exc:  # fail closed: an assertion that cannot run did not pass
            res = common.Result(False, f"error: {exc.__class__.__name__}: {exc}")
        row = {"name": check.name, "class": check.cls, "ok": res.ok, "detail": res.detail}
        if res.skipped:
            row["skipped"] = True
        results.append(row)
    ok = all(r["ok"] for r in results if r["class"] in ("safety", "outcome"))
    return {"scenario": scenario, "backend": backend, "results": results, "ok": ok}


def format_table(summary: dict) -> str:
    rows = summary["results"]
    width = max([len(r["name"]) for r in rows] + [4])
    lines = [f"{'class':8} {'result':6} {'name':{width}}  detail"]
    for r in rows:
        status = "skip" if r.get("skipped") else ("pass" if r["ok"] else "FAIL")
        lines.append(f"{r['class']:8} {status:6} {r['name']:{width}}  {r['detail']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None, runner: Optional[Callable] = None, out: Any = None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--backend", choices=("github", "ledger"), default="github")
    ap.add_argument("--ledger")
    ap.add_argument("--guard-log")
    ap.add_argument("--run-log")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if manifest.get("scenario") != args.scenario:
            raise ValueError(f"manifest is for scenario {manifest.get('scenario')!r}, not {args.scenario!r}")
        summary = grade(manifest, args.scenario, args.backend, runner, args.ledger, args.guard_log, args.run_log)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_table(summary), file=out)
    print(json.dumps(summary, indent=2), file=out)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
