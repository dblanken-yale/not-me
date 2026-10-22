import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = SCRIPTS / "lint-evals.py"


def run(root, *extra):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(FIXTURES / root), *extra],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


class LintEvalsTest(unittest.TestCase):
    def test_clean_case_passes(self):
        code, out = run("evals_clean")
        self.assertEqual(code, 0, out)

    def test_dirty_cases_report_each_rule(self):
        code, out = run("evals_dirty", "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual({v["rule"] for v in data["violations"]}, {"E1", "E2", "E4", "E5"})
        for v in data["violations"]:
            self.assertGreaterEqual(v["line"], 1)

    def test_only_filters(self):
        _, out = run("evals_dirty", "--only", "E2", "--json")
        self.assertEqual({v["rule"] for v in json.loads(out)["violations"]}, {"E2"})

    def test_no_evals_dir_is_a_notice(self):
        code, out = run("clean")
        self.assertEqual(code, 0)
        self.assertIn("no evals", out)


if __name__ == "__main__":
    unittest.main()
