import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _support as s

LINT = s.ROOT / "scripts" / "lint-evals.py"
EVAL_SH = s.ROOT / "scripts" / "eval-live.sh"

PROMPT = """---
name: {name}
tags: [skill:new, class:outcome, live]
---
body
"""
GRADER = "---\ntype: regex\n---\nx\n"


def lint_root(case, scenario=None, module=None):
    root = Path(tempfile.mkdtemp())
    case_dir = root / "evals-harness" / "cases" / case
    (case_dir / "graders").mkdir(parents=True)
    (case_dir / "prompt.md").write_text(PROMPT.format(name=case), encoding="utf-8")
    (case_dir / "graders" / "g.md").write_text(GRADER, encoding="utf-8")
    (root / "evals-harness" / "assertions").mkdir(parents=True)
    shutil.copy(str(s.HARNESS / "assertions" / "keys.py"), str(root / "evals-harness" / "assertions" / "keys.py"))
    if scenario is not None:
        (root / "fixture" / "scenarios").mkdir(parents=True)
        (root / "fixture" / "scenarios" / f"{case.split('-')[-1]}.json").write_text(json.dumps(scenario), encoding="utf-8")
    if module:
        (root / "evals-harness" / "assertions" / f"{module}.py").write_text("def checks(ctx): return []\n")
    return root


def lint(root):
    proc = subprocess.run([sys.executable, str(LINT), "--root", str(root), "--json"], capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)["violations"]


class LintE6Test(unittest.TestCase):
    def test_live_case_with_a_mapped_expect_block_is_clean(self):
        code, v = lint(lint_root("new-N1", {"expect": {"labels_exact": {}, "skill": "new"}}))
        self.assertEqual((code, v), (0, []))

    def test_unmapped_expect_key_is_e6(self):
        code, v = lint(lint_root("new-N1", {"expect": {"labels_exactly": {}}}))
        self.assertEqual(code, 1)
        self.assertEqual([x["rule"] for x in v], ["E6"])
        self.assertIn("labels_exactly", v[0]["message"])

    def test_missing_scenario_file_is_e6(self):
        code, v = lint(lint_root("new-N1"))
        self.assertEqual([x["rule"] for x in v], ["E6"])

    def test_empty_expect_is_e6(self):
        code, v = lint(lint_root("new-N1", {"expect": {}}))
        self.assertEqual([x["rule"] for x in v], ["E6"])

    def test_an_assertions_module_satisfies_the_rule(self):
        code, v = lint(lint_root("new-N1", module="N1"))
        self.assertEqual((code, v), (0, []))

    def test_case_without_a_scenario_id_or_module_is_e5(self):
        code, v = lint(lint_root("plain"))
        self.assertEqual([x["rule"] for x in v], ["E5"])

    def test_real_scenario_files_all_pass_e6(self):
        for path in sorted(s.SCENARIOS.glob("*.json")):
            sid = path.stem
            root = lint_root(f"x-{sid}", json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(lint(root)[1], [], sid)


@unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "needs bash")
class EvalLiveShTest(unittest.TestCase):
    def test_dry_run_exports_fixture_env_and_calls_run_live(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("NOT_ME_")}
        out = tempfile.mkdtemp()
        proc = subprocess.run(
            ["bash", str(EVAL_SH), "--owner", "acme", "--repo", "acme/x-fixture", "--skill", "new",
             "--scenario", "N1", "--out", out, "--dry-run"], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("seed_fixture.py", proc.stdout)
        self.assertIn("--repo acme/x-fixture", proc.stdout)
        self.assertIn("claude plugin eval", proc.stdout)

    def test_missing_repo_is_refused(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("NOT_ME_")}
        proc = subprocess.run(["bash", str(EVAL_SH), "--skill", "new", "--scenario", "N1"],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
