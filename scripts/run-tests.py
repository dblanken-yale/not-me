#!/usr/bin/env python3
"""Run the not-me unit tests. Exits 1 on failure or when zero tests ran."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "not-me"
TESTS = PLUGIN / "tests"
SCRIPT_TESTS = ROOT / "scripts" / "tests"
DOC_TESTS = ROOT / "docs" / "tests"


def main() -> int:
    if not TESTS.is_dir() and not SCRIPT_TESTS.is_dir() and not DOC_TESTS.is_dir():
        print("no tests yet")
        return 0
    sys.path.insert(0, str(PLUGIN))
    suite = unittest.TestSuite()
    if TESTS.is_dir():
        suite.addTests(unittest.defaultTestLoader.discover(
            start_dir=str(TESTS), top_level_dir=str(PLUGIN)
        ))
    if SCRIPT_TESTS.is_dir():
        suite.addTests(unittest.defaultTestLoader.discover(
            start_dir=str(SCRIPT_TESTS), top_level_dir=str(SCRIPT_TESTS)
        ))
    if DOC_TESTS.is_dir():
        suite.addTests(unittest.defaultTestLoader.discover(
            start_dir=str(DOC_TESTS), top_level_dir=str(DOC_TESTS)
        ))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(f"ran {result.testsRun} tests")
    if result.testsRun == 0:
        print("error: zero tests ran", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
