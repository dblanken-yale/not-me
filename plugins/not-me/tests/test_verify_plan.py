"""notme verify-plan: targeted tests vs the full set, the skippable baseline, and the optional CI wait."""
from __future__ import annotations

import unittest

from notme import verifyplan
from tests import PLUGIN_ROOT
from tests import test_cli_contract as base
from tests.test_cli_commands import git, init_repo
from tests.test_cli_contract import REPO

SHA = "b" * 40
SHIPPED = (PLUGIN_ROOT / "defaults" / "verify.md").read_text(encoding="utf-8")

PINNED = """# Verify

## Targeted tests

Run with the changed test files as trailing arguments.

```bash
# the unit runner
python3 -m unittest

./run-one.sh
```

## Tests

```bash
make test
```
"""

COMMENTS_ONLY = """# Verify

## Targeted tests

```bash
# nothing yet

```
"""


def plan(setting="always", ci="success", body=SHIPPED, minutes=20, sha=SHA, base_name="main", checked=None):
    return verifyplan.plan(setting=setting, base=base_name, base_sha=sha,
                           checked_sha=sha if checked is None else checked, base_ci=ci, verify_body=body,
                           ci_wait_minutes=minutes)


class TargetedTests(unittest.TestCase):
    def test_the_shipped_section_configures_nothing(self):
        self.assertIn("## Targeted tests", SHIPPED)
        self.assertEqual(plan()["targeted"], {"configured": False, "commands": []})

    def test_a_pinned_block_gives_its_commands_without_comments_or_blanks(self):
        self.assertEqual(plan(body=PINNED)["targeted"],
                         {"configured": True, "commands": ["python3 -m unittest", "./run-one.sh"]})

    def test_a_block_of_comments_only_configures_nothing(self):
        self.assertEqual(plan(body=COMMENTS_ONLY)["targeted"], {"configured": False, "commands": []})

    def test_no_section_and_commands_in_other_sections_configure_nothing(self):
        self.assertFalse(plan(body="# Verify\n\n## Tests\n\n```\nmake test\n```\n")["targeted"]["configured"])

    def test_the_heading_matches_without_regard_to_case(self):
        body = "## targeted TESTS\n\n```\nrun-it\n```\n"
        self.assertEqual(plan(body=body)["targeted"]["commands"], ["run-it"])


class BaselineTests(unittest.TestCase):
    def test_always_runs_whatever_the_ci(self):
        for ci in ("success", "failure", "pending", "none", "unknown"):
            with self.subTest(ci=ci):
                got = plan(setting="always", ci=ci)["baseline"]
                self.assertEqual((got["setting"], got["run"]), ("always", True))
                self.assertIn("always", got["reason"])

    def test_auto_with_green_base_ci_skips_and_names_the_sha(self):
        got = plan(setting="auto", ci="success")["baseline"]
        self.assertEqual((got["setting"], got["run"]), ("auto", False))
        self.assertIn(SHA, got["reason"])
        self.assertIn("main", got["reason"])

    def test_auto_runs_on_any_other_ci_state(self):
        for ci in ("failure", "pending", "none", "unknown"):
            with self.subTest(ci=ci):
                got = plan(setting="auto", ci=ci)["baseline"]
                self.assertTrue(got["run"])
                self.assertIn(ci, got["reason"])

    def test_auto_without_a_base_sha_runs(self):
        self.assertTrue(plan(setting="auto", ci="success", sha=None)["baseline"]["run"])

    def test_auto_names_the_checked_commit_when_it_is_not_the_base_head(self):
        head = "c" * 40
        got = plan(setting="auto", ci="success", checked=head)
        self.assertEqual((got["base_sha"], got["checked_sha"]), (SHA, head))
        self.assertFalse(got["baseline"]["run"])
        self.assertIn(head, got["baseline"]["reason"])
        self.assertNotIn(SHA, got["baseline"]["reason"])
        got = plan(setting="auto", ci="failure", checked=head, sha=None)
        self.assertTrue(got["baseline"]["run"])
        self.assertIn(head, got["baseline"]["reason"])

    def test_an_unknown_setting_runs_the_baseline(self):
        self.assertTrue(plan(setting="sometimes", ci="success")["baseline"]["run"])


class CiWaitTests(unittest.TestCase):
    def test_zero_minutes_means_no_wait(self):
        got = plan(minutes=0)
        self.assertEqual((got["ci_wait_minutes"], got["ci_wait"]), (0, False))

    def test_the_default_waits(self):
        got = plan(minutes=20)
        self.assertEqual((got["ci_wait_minutes"], got["ci_wait"]), (20, True))


class BaseCiStateTests(unittest.TestCase):
    def test_reduces_check_runs_and_statuses(self):
        green = {"conclusion": "success", "status": "completed"}
        self.assertEqual(verifyplan.ci_state([], []), "none")
        self.assertEqual(verifyplan.ci_state([green], []), "success")
        self.assertEqual(verifyplan.ci_state([green], [{"state": "failure"}]), "failure")
        self.assertEqual(verifyplan.ci_state([{"status": "in_progress", "conclusion": None}], []), "pending")
        self.assertEqual(verifyplan.ci_state([], [{"state": "success"}]), "success")

    def test_a_list_cut_by_paging_is_unknown(self):
        green = {"conclusion": "success", "status": "completed"}
        self.assertEqual(verifyplan.ci_state([green], [], runs_total=2), "unknown")
        self.assertEqual(verifyplan.ci_state([green], [{"state": "success"}], statuses_total=3), "unknown")
        self.assertEqual(verifyplan.ci_state([green], [{"state": "success"}], runs_total=1, statuses_total=1),
                         "success")
        self.assertEqual(verifyplan.ci_state([green], [], runs_total=None, statuses_total=None), "success")


class VerifyPlanCliTests(base.CliCase):
    def setUp(self):
        super().setUp()
        init_repo(self.project)
        self.sha = git(self.project, "rev-parse", "HEAD").strip()
        git(self.project, "update-ref", "refs/remotes/origin/main", self.sha)

    def set_ci(self, check_runs, statuses=(), sha=None, **totals):
        self.fake.state.setdefault("commit_checks", {})[f"{REPO}@{sha or self.sha}"] = dict(
            {"check_runs": list(check_runs), "statuses": list(statuses)}, **totals)

    def test_always_by_default_and_no_targeted_command(self):
        self.set_ci([{"name": "ci", "status": "completed", "conclusion": "success"}])
        out = self.ok("verify-plan", "--base", "main")
        self.assertEqual((out["base"], out["base_sha"], out["base_ci"]), ("main", self.sha, "success"))
        self.assertEqual(out["baseline"]["setting"], "always")
        self.assertTrue(out["baseline"]["run"])
        self.assertEqual(out["targeted"], {"configured": False, "commands": []})
        self.assertEqual((out["ci_wait_minutes"], out["ci_wait"]), (20, True))
        self.assertEqual(self.fake.writes_made(), [])

    def test_auto_skips_on_green_base_ci_and_reads_targeted_and_ci_wait(self):
        self.tc.write("repo", "verify", "---\nschema: 1\nbaseline: auto\n---\n" + PINNED)
        self.tc.write("repo", "project", "---\nschema: 1\nlimits: {ci_wait_minutes: 0}\n---\n# Project\n")
        self.set_ci([{"name": "ci", "status": "completed", "conclusion": "success"}], [{"state": "success"}])
        out = self.ok("verify-plan", "--base", "main")
        self.assertFalse(out["baseline"]["run"])
        self.assertIn(self.sha, out["baseline"]["reason"])
        self.assertEqual(out["targeted"]["commands"], ["python3 -m unittest", "./run-one.sh"])
        self.assertEqual((out["ci_wait_minutes"], out["ci_wait"]), (0, False))

    def test_auto_runs_when_the_base_ci_is_red_missing_or_unreadable(self):
        self.tc.write("repo", "verify", "---\nschema: 1\nbaseline: auto\n---\n# Verify\n")
        self.set_ci([{"name": "ci", "status": "completed", "conclusion": "failure"}])
        out = self.ok("verify-plan", "--base", "main")
        self.assertEqual((out["base_ci"], out["baseline"]["run"]), ("failure", True))
        self.set_ci([])
        out = self.ok("verify-plan", "--base", "main")
        self.assertEqual((out["base_ci"], out["baseline"]["run"]), ("none", True))
        self.fake.fail_next = "HTTP 502: Bad Gateway"
        out = self.ok("verify-plan", "--base", "main")
        self.assertEqual((out["base_ci"], out["baseline"]["run"]), ("unknown", True))

    def test_more_checks_than_one_page_is_unknown_and_runs_the_baseline(self):
        self.tc.write("repo", "verify", "---\nschema: 1\nbaseline: auto\n---\n# Verify\n")
        green = {"name": "ci", "status": "completed", "conclusion": "success"}
        self.set_ci([green], runs_total=101)
        out = self.ok("verify-plan", "--base", "main")
        self.assertEqual((out["base_ci"], out["baseline"]["run"]), ("unknown", True))
        self.set_ci([green], [{"state": "success"}], statuses_total=101)
        out = self.ok("verify-plan", "--base", "main")
        self.assertEqual((out["base_ci"], out["baseline"]["run"]), ("unknown", True))

    def test_checked_sha_is_the_base_head_without_sha(self):
        self.set_ci([])
        self.assertEqual(self.ok("verify-plan", "--base", "main")["checked_sha"], self.sha)

    def test_sha_reads_the_ci_of_that_commit(self):
        self.tc.write("repo", "verify", "---\nschema: 1\nbaseline: auto\n---\n# Verify\n")
        head = "d" * 40
        self.set_ci([{"name": "ci", "status": "completed", "conclusion": "success"}])
        self.set_ci([{"name": "ci", "status": "completed", "conclusion": "failure"}], sha=head)
        out = self.ok("verify-plan", "--base", "main", "--sha", head)
        self.assertEqual((out["base_sha"], out["checked_sha"], out["base_ci"]), (self.sha, head, "failure"))
        self.assertTrue(out["baseline"]["run"])
        self.assertIn(head, out["baseline"]["reason"])
        self.set_ci([{"name": "ci", "status": "completed", "conclusion": "success"}], sha=head)
        out = self.ok("verify-plan", "--base", "develop", "--sha", head)
        self.assertEqual((out["base_sha"], out["checked_sha"], out["base_ci"]), (None, head, "success"))
        self.assertFalse(out["baseline"]["run"])
        self.assertIn(head, out["baseline"]["reason"])

    def test_a_bad_sha_is_a_usage_error(self):
        for bad in ("abc", "g" * 40, "B" * 40):
            with self.subTest(sha=bad):
                code, payload, _ = self.cli("verify-plan", "--base", "main", "--sha", bad)
                self.assertEqual((code, payload["kind"]), (2, "usage"))

    def test_a_base_without_a_local_ref_is_unknown(self):
        self.tc.write("repo", "verify", "---\nschema: 1\nbaseline: auto\n---\n# Verify\n")
        out = self.ok("verify-plan", "--base", "develop")
        self.assertEqual((out["base_sha"], out["base_ci"], out["baseline"]["run"]), (None, "unknown", True))

    def test_the_base_defaults_to_the_repo_default_branch(self):
        self.set_ci([])
        self.assertEqual(self.ok("verify-plan")["base"], "main")

    def test_a_bad_base_name_is_a_usage_error(self):
        code, payload, _ = self.cli("verify-plan", "--base", "-x")
        self.assertEqual((code, payload["kind"]), (2, "usage"))

    def test_baseline_is_validated_and_explained(self):
        self.tc.write("repo", "verify", "---\nschema: 1\nbaseline: sometimes\n---\n# Verify\n")
        code, payload, _ = self.cli("config", "--check")
        self.assertEqual(code, 4)
        self.assertTrue(any("baseline must be one of always, auto" in e for e in payload["errors"]), payload)
        self.tc.write("repo", "verify", "---\nschema: 1\nbaseline: auto\n---\n# Verify\n")
        keys = self.ok("config", "--explain")["keys"]
        self.assertEqual(keys["verify.baseline"]["value"], "auto")
        self.assertEqual(keys["verify.baseline"]["source"]["layer"], "repo")


if __name__ == "__main__":
    unittest.main()
