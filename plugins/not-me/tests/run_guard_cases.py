#!/usr/bin/env python3
"""Run every guard case through the real hook entrypoint as a subprocess.

    python3 plugins/not-me/tests/run_guard_cases.py [-v]

Builds throwaway git repos in a temp dir, pipes each case's event JSON into
hooks/repo_guard.py and checks the exit code (2 = block, 0 = allow) and, for
blocks, the rule named on stderr. Exits 1 on any failure.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
HOOK = PLUGIN / "hooks" / "repo_guard.py"
sys.path.insert(0, str(PLUGIN))

from notme.guard import RULE_LABEL  # noqa: E402
from tests import guard_cases as gc  # noqa: E402


def run_case(case: gc.Case, root: Path, env: dict) -> "tuple[int, str]":
    _, home = gc.variant_paths(root, case[5])
    run_env = dict(env, NOT_ME_HOME=str(home))
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(gc.event_for(case, root)).encode("utf-8"),
                          capture_output=True, env=run_env, cwd=str(root), timeout=60)
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def main(argv: "list[str]") -> int:
    verbose = "-v" in argv
    root = Path(tempfile.mkdtemp(prefix="notme-guard-")).resolve()
    passed = failed = 0
    try:
        env = gc.build_fixtures(root)
        for case in gc.CASES:
            cid, want, rule, tool, tin, variant = case
            rc, err = run_case(case, root, env)
            problems = []
            got = {0: "allow", 2: "block"}.get(rc, f"exit {rc}")
            if got != want:
                problems.append(f"want {want}, got {got}")
            if want == "block" and rule and f"not-me guard [{RULE_LABEL.get(rule, rule)}]" not in err:
                problems.append(f"want rule {rule}")
            for text in gc.MESSAGE_CHECKS.get(cid, []):
                if text not in err:
                    problems.append(f"message lacks {text!r}")
            if problems:
                failed += 1
                print(f"FAIL {cid} [{variant}] {tool} {json.dumps(tin)}: {'; '.join(problems)}")
                if err.strip():
                    print("  " + err.strip().replace("\n", "\n  "))
            else:
                passed += 1
                if verbose:
                    print(f"ok   {cid} {want} {tool} {json.dumps(tin)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"guard cases: {passed + failed} run, {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
