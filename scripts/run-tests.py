#!/usr/bin/env python3
"""Run the not-me unit tests. Exits 1 on failure or when zero tests ran."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "not-me"
TESTS = PLUGIN / "tests"
FIXTURE = ROOT / "fixture"
FIXTURE_TESTS = FIXTURE / "tests"


def main() -> int:
    suite = unittest.TestSuite()
    if TESTS.is_dir():
        sys.path.insert(0, str(PLUGIN))
        suite.addTests(
            unittest.defaultTestLoader.discover(
                start_dir=str(TESTS), top_level_dir=str(PLUGIN)
            )
        )
    if FIXTURE_TESTS.is_dir():
        sys.path.insert(0, str(FIXTURE))
        suite.addTests(
            unittest.defaultTestLoader.discover(
                start_dir=str(FIXTURE_TESTS), top_level_dir=str(FIXTURE_TESTS)
            )
        )
    if not TESTS.is_dir() and not FIXTURE_TESTS.is_dir():
        print("no tests yet")
        return 0
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(f"ran {result.testsRun} tests")
    if result.testsRun == 0:
        print("error: zero tests ran", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
