#!/usr/bin/env python3
"""Run the not-me unit tests. Exits 1 on failure or when zero tests ran."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "not-me"
TESTS = PLUGIN / "tests"
FIXTURE = ROOT / "fixture"
SCRIPT_TESTS = ROOT / "scripts" / "tests"
DOC_TESTS = ROOT / "docs" / "tests"
FIXTURE_TESTS = FIXTURE / "tests"
HARNESS = ROOT / "evals-harness"
HARNESS_TESTS = HARNESS / "tests"

# (start_dir, top_level_dir, extra sys.path entry)
SUITES = [
    (TESTS, PLUGIN, PLUGIN),
    (SCRIPT_TESTS, SCRIPT_TESTS, None),
    (DOC_TESTS, DOC_TESTS, None),
    (FIXTURE_TESTS, FIXTURE_TESTS, FIXTURE),
    (HARNESS_TESTS, HARNESS_TESTS, HARNESS),
]


def main() -> int:
    present = [(d, top, extra) for d, top, extra in SUITES if d.is_dir()]
    if not present:
        print("no tests yet")
        return 0
    sys.path.insert(0, str(PLUGIN))
    suite = unittest.TestSuite()
    for start_dir, top, extra in present:
        if extra is not None and str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
        suite.addTests(unittest.defaultTestLoader.discover(
            start_dir=str(start_dir), top_level_dir=str(top)
        ))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(f"ran {result.testsRun} tests")
    if result.testsRun == 0:
        print("error: zero tests ran", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
