"""The notme command line contract: one JSON object on stdout, the documented exit codes, dry-run behaviour."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

from notme import claims, cli
from tests import PLUGIN_ROOT, TempCase
from tests.fakes.fakegh import FakeRunner

REPO = "acme/app"
ROOT = PLUGIN_ROOT.parent.parent
GLOBAL_PROJECT = """---
schema: 1
mode: live
tracker: acme/app
operator: octocat
allow_repos: [acme/app-ui, acme/other]
---
# Project

## Roster

| GitHub login | Name | Tier | Assignable | Reviewer |
|--------------|------|------|------------|----------|
| octocat | Octo | lead | yes | yes |
| hubot | Hub | lead | yes | yes |
"""
NO_OTHERS_PROJECT = """---
schema: 1
mode: live
tracker: acme/app
operator: octocat
---
# Project
"""


class TtyIO(io.StringIO):
    """stdin that reports a terminal: a person running notme, which relaxes the trust anchor."""

    def isatty(self):
        return True


def things(fake, repo=REPO):
    return fake.state["repos"][repo]["things"]


class CliCase(unittest.TestCase):
    """A temp project, a global config in live mode, and a fake GitHub behind the injected runner. Runs as a person
    at a terminal (a tty stdin, no Claude Code ancestor), so --project-dir and NOT_ME_HOME are honoured."""

    global_project = GLOBAL_PROJECT

    def setUp(self):
        self.tc = TempCase()
        self.addCleanup(self.tc.cleanup)
        self.tc.write("global", "project", self.global_project)
        self.fake = FakeRunner()
        self.env = {"NOT_ME_HOME": str(self.tc.home), "PATH": os.environ.get("PATH", ""),
                    "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)}

    @property
    def project(self) -> Path:
        return self.tc.project

    def write_file(self, name: str, text: str) -> str:
        path = self.project / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    @property
    def reason(self) -> str:
        """A --reason-file for the commands that change a state."""
        return self.write_file("reason.txt", "Because the test says so\n")

    def cli(self, *argv, stdin="", runner=None, hook_cmd=None, env=None, repo=True, project=True):
        """(exit code, parsed stdout object, stderr text). Fails the test unless stdout is exactly one object."""
        out, err = io.StringIO(), io.StringIO()
        args = list(argv)
        if project:
            args = ["--project-dir", str(self.project)] + args
        if repo:
            args = ["--repo", REPO] + args
        code = cli.main(args, runner=runner or self.fake, env=env or self.env, stdin=TtyIO(stdin), stdout=out,
                        stderr=err, hook_cmd=hook_cmd, process_check=lambda: False)
        text = out.getvalue()
        self.assertEqual(text.count("\n"), 1, f"stdout must be one line, got {text!r}")
        payload = json.loads(text)
        self.assertIsInstance(payload, dict)
        return code, payload, err.getvalue()

    def ok(self, *argv, **kw):
        code, payload, err = self.cli(*argv, **kw)
        self.assertEqual(code, 0, f"{argv}: {payload} {err}")
        return payload

    def ledger(self):
        from notme.dryrun import Ledger
        return Ledger.default_for(self.project).read()


class ParserTests(unittest.TestCase):
    def test_build_parser_lists_every_known_command(self):
        import argparse
        parser = cli.build_parser()
        found = set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                found.update(action.choices)
        known = {ln.strip() for ln in (ROOT / "scripts" / "known-commands.txt").read_text().splitlines() if ln.strip()}
        self.assertEqual({" ".join(path) for path in cli.leaf_parsers(parser)}, known)
        self.assertEqual({k.split()[0] for k in known}, found)
        for name in ("me", "config", "preflight", "pick-issue", "claim-issue", "release-issue", "move-issue", "issue",
                     "pick-needs-work", "pr-children", "pr-by-head", "wait-checks", "verify-plan", "issue-body", "retro", "claim-pr", "release-pr", "bundle", "feedback", "transition", "labels-sync",
                     "labels-ensure", "reply-thread", "comment-upsert", "request-review", "pr-body", "pr-open",
                     "companion-prs", "testing-steps", "review-post", "resolve-threads", "pick-reviews",
                     "prove-target", "followup", "check-text", "log", "allow-repo", "guard", "git", "status",
                     "pause", "doctor"):
            self.assertIn(name, found)

    def test_generated_docs_match_the_committed_files(self):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-cli-docs.py"), "--check"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotRegex((ROOT / "docs" / "cli.md").read_text(encoding="utf-8"), r"/Users/|/home/|[A-Za-z]:\\")

    def test_help_exits_zero(self):
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            self.assertEqual(cli.main(["--help"]), 0)
        finally:
            sys.stdout = old
        self.assertIn("claim-issue", out.getvalue())

    def test_redact_hides_credentials_and_tokens(self):
        text = "fatal: https://user:s3cret@github.com/acme/app.git and ghp_abcdefghijklmnopqrstuvwxyz0123"
        out = cli.redact(text)
        self.assertNotIn("s3cret", out)
        self.assertNotIn("ghp_abcdefghijkl", out)
        self.assertIn("https://***@github.com", out)


class OutputShapeTests(CliCase):
    def test_every_read_command_emits_one_json_object(self):
        add_pr_state(self.fake)
        cases = [["me"], ["config"], ["config", "--explain"], ["config", "--check"], ["pick-issue"],
                 ["issue", "1"], ["pick-needs-work"], ["pick-needs-work", "--include-answered", "--exclude", "3"], ["bundle", "3"],
                 ["pr-children", "3"], ["pr-by-head", "feat/widget"], ["wait-checks", "3", "--minutes", "0"],
                 ["verify-plan"], ["verify-plan", "--base", "main"],
                 ["feedback", "3"], ["pick-reviews"],
                 ["prove-target", "3"], ["labels-ensure", "--check"], ["labels-sync", "1", "--set", "to do", "--check"],
                 ["check-text", "--file", self.write_file("t.md", "fine\n")], ["status"], ["allow-repo", "list"],
                 ["log", "last"], ["log", "list"],
                 ["guard", "selftest"], ["doctor"]]
        for argv in cases:
            with self.subTest(argv=argv):
                code, payload, _ = self.cli(*argv)
                self.assertEqual(code, 0, payload)

    def test_errors_are_json_with_error_and_kind(self):
        code, payload, err = self.cli("issue", "99")
        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], "gh_error")
        self.assertIn("error", payload)
        self.assertIn("404", err)  # the gh stderr is chatter, not part of stdout

    def test_global_flags_are_accepted_after_the_subcommand(self):
        code, payload, _ = self.cli("issue", "1", "--repo", REPO, "--json", repo=False)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["number"], 1)


class ExitCodeTests(CliCase):
    def test_usage_errors_exit_2(self):
        for argv in ([], ["nonsense"], ["issue"], ["issue", "not-a-ref"], ["labels-sync", "1"],
                     ["labels-sync", "1", "--set", "a", "--add", "b"],
                     ["labels-sync", "1", "--set", "a", "--remove", "b"], ["claim-issue", "1", "--bogus"],
                     ["move-issue", "1", "--to", "nope"], ["pr-by-head"], ["pr-by-head", "a..b"],
                     ["pr-by-head", "-x"], ["pr-children"], ["wait-checks", "3", "--minutes", "-1"],
                     ["wait-checks", "3", "--minutes", "soon"], ["retro", "save"], ["retro", "save", "--run-id", "r"],
                     ["retro", "is-reviewed"], ["log", "list", "--limit", "x"], ["issue-body", "1"], ["git", "remote-branch-exists"],
                     ["git", "remote-branch-exists", "a..b"], ["pick-needs-work", "--exclude", "x"]):
            with self.subTest(argv=argv):
                code, payload, _ = self.cli(*argv)
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["kind"], "usage")

    def test_config_invalid_exits_4_with_errors(self):
        self.tc.write("repo", "project", "---\nschema: 1\nlimits: {fix_loop: abc}\n---\n# Project\n")
        code, payload, _ = self.cli("config", "--check")
        self.assertEqual(code, 4)
        self.assertTrue(payload["errors"])
        self.assertIn("fix_loop", payload["errors"][0])
        code, payload, _ = self.cli("issue", "1")  # any command that builds the guard refuses too
        self.assertEqual(code, 4)
        self.assertEqual(self.fake.calls, [])

    def test_blocked_write_exits_6_with_allow_cmd(self):
        self.tc.write("global", "project", NO_OTHERS_PROJECT)
        code, payload, _ = self.cli("request-review", "acme/other#1", "--reviewers", "hubot")
        self.assertEqual(code, 6)
        self.assertTrue(payload["blocked"])
        self.assertEqual(payload["repo"], "acme/other")
        self.assertEqual(payload["allow_cmd"], "/not-me:allow-repo acme/other")
        self.assertEqual(self.fake.writes_made(), [])

    def test_claim_lost_exits_3(self):
        self.fake.add_assignee(REPO, 1, "hubot")
        code, payload, _ = self.cli("claim-issue", "1")
        self.assertEqual(code, 3)
        self.assertTrue(payload["claim_lost"])
        self.assertEqual(things(self.fake)["1"]["labels"], ["to do"])

    def test_readback_mismatch_exits_5(self):
        self.fake.drop_adds = True
        code, payload, _ = self.cli("labels-sync", "1", "--set", "to do,extra")
        self.assertEqual(code, 5)
        self.assertEqual(payload["kind"], "readback_mismatch")
        self.assertEqual(payload["expected"], ["extra", "to do"])

    def test_guard_selftest_failure_exits_8(self):
        (self.project / ".claude" / "not-me").mkdir(parents=True)
        code, payload, _ = self.cli("guard", "selftest", hook_cmd=[sys.executable, "-c", "pass"])
        self.assertEqual(code, 8)
        self.assertEqual(payload["kind"], "guard_broken")
        code, payload, _ = self.cli("preflight", hook_cmd=[sys.executable, "-c", "pass"])
        self.assertEqual(code, 8)

    def test_failed_selftest_reports_the_hook_stderr(self):
        (self.project / ".claude" / "not-me").mkdir(parents=True)
        hook = [sys.executable, "-c", "import sys; sys.stderr.write('boom ghp_abcdefghijklmnopqrstuvwxyz0123'); "
                                      "sys.exit(1)"]
        code, payload, _ = self.cli("guard", "selftest", hook_cmd=hook)
        self.assertEqual(code, 8)
        self.assertIn("exit 1: boom", payload["selftest_stderr"])
        self.assertNotIn("ghp_abcdefghijkl", payload["selftest_stderr"])
        code, payload, _ = self.cli("preflight", hook_cmd=hook)
        self.assertEqual(code, 8)
        self.assertIn("boom", payload["guard"]["selftest_stderr"])

    @unittest.skipIf(os.name == "nt", "a fake SYSTEMROOT would stop the child interpreter on Windows")
    def test_selftest_passes_windows_startup_variables_through(self):
        from notme import guard
        hook = [sys.executable, "-c", "import os, sys; sys.exit(2 if os.environ.get('SYSTEMROOT') == 'X' else 1)"]
        ctx = guard.GuardContext.from_project(str(self.project), home=str(self.tc.home), env=self.env)
        with mock.patch.dict(os.environ, {"SYSTEMROOT": "X"}):
            self.assertEqual(guard.selftest_run(hook, ctx, env={"PATH": os.environ.get("PATH", "")}), (True, ""))

    def test_default_hook_command_runs_the_hook_with_this_interpreter(self):
        (self.project / ".claude" / "not-me").mkdir(parents=True)
        self.assertEqual(self.ok("guard", "selftest"), {"ok": True, "active": True})

    def test_guard_selftest_passes_with_the_real_hook(self):
        (self.project / ".claude" / "not-me").mkdir(parents=True)
        hook = [sys.executable, str(PLUGIN_ROOT / "hooks" / "repo_guard.py")]
        payload = self.ok("guard", "selftest", hook_cmd=hook)
        self.assertEqual(payload, {"ok": True, "active": True})

    def test_guard_selftest_skips_an_inactive_project(self):
        self.assertEqual(self.ok("guard", "selftest", hook_cmd=[sys.executable, "-c", "pass"]),
                         {"ok": True, "active": False})

    def test_missing_gh_exits_7(self):
        def no_gh(argv):
            raise FileNotFoundError(2, "No such file", "gh")

        code, payload, _ = self.cli("issue", "1", runner=no_gh)
        self.assertEqual(code, 7)
        self.assertEqual(payload["kind"], "missing_dependency")
        self.assertEqual(payload["dependency"], "gh")

    def test_missing_auth_exits_7(self):
        import subprocess as sp

        def logged_out(argv):
            return sp.CompletedProcess(argv, 1, "", "To get started with GitHub CLI, please run:  gh auth login")

        code, payload, _ = self.cli("issue", "1", runner=logged_out)
        self.assertEqual(code, 7)
        self.assertEqual(payload["kind"], "auth")

    def test_stderr_never_carries_credentials(self):
        import subprocess as sp

        def leaky(argv):
            return sp.CompletedProcess(argv, 1, "", "fatal: https://x-access-token:abc123@github.com/acme/app.git")

        code, payload, err = self.cli("issue", "1", runner=leaky)
        self.assertEqual(code, 1)
        self.assertNotIn("abc123", err)
        self.assertNotIn("abc123", json.dumps(payload))

    def test_unsafe_input_file_is_refused(self):
        outside = TempCase()
        self.addCleanup(outside.cleanup)
        path = outside.root / "body.md"
        path.write_text("hello", encoding="utf-8")
        code, payload, _ = self.cli("comment-upsert", "3", "--marker", "status", "--body-file", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], "unsafe_path")


def add_pr_state(fake, **overrides):
    pr = things(fake)["3"]
    pr.update({"author": "hubot", "labels": ["needs review"], "reviewRequests": ["octocat"]})
    pr.update(overrides)
    return pr


class DryRunTests(CliCase):
    """--dry-run on every write subcommand: ledger entries, no writes reach GitHub."""

    def setUp(self):
        super().setUp()
        add_pr_state(self.fake)
        issue = things(self.fake)["1"]
        issue.update({"labels": ["in progress"], "assignees": ["octocat"]})
        things(self.fake)["4"] = {"number": 4, "title": "Fourth", "body": "", "state": "OPEN", "labels": ["to do"],
                                  "assignees": [], "author": "octocat", "comments": [], "kind": "issue"}
        self.fake.post_comment(REPO, 3, "octocat", claims.marker_body("octocat", "r-1", "2026-01-01T00:00:00Z"))
        body = self.write_file("body.md", "Body text\n")
        title = self.write_file("title.txt", "A title\n")
        self.write_file("companions.json", json.dumps([{"repo": "acme/app-ui", "number": 9}]))
        self.write_file("steps.txt", "Open the page\nClick the button\n")
        self.write_file("ids.json", json.dumps(["PRRT_1"]))
        # queue-resync needs a confirmed board; issue 1 (labeled in progress, Status To Do) is out of step on it
        self.tc.write("repo", "project", "---\nschema: 1\nqueue: {board: {owner: acme, number: 6}}\n---\n# Project\n")
        self.ok("allow-repo", "add", "--board", "acme/6", "--everywhere")
        reason = self.write_file("reason.txt", "Because the test says so\n")
        self.cases = {
            "claim-issue": ["claim-issue", "4"],
            "assign-issue": ["assign-issue", "4"],
            "release-issue": ["release-issue", "1", "--reason-file", reason],
            "move-issue": ["move-issue", "1", "--to", "needs_review", "--reason-file", reason],
            "transition": ["transition", "1", "--from", "in_progress", "--to", "needs_review",
                           "--reason-file", reason],
            "labels-sync": ["labels-sync", "1", "--set", "in progress,extra", "--reason-file", reason],
            "labels-ensure": ["labels-ensure"],
            "queue-resync": ["queue-resync"],
            "reply-thread": ["reply-thread", "3", "--to", "501", "--body-file", body],
            "comment-upsert": ["comment-upsert", "3", "--marker", "status", "--body-file", body],
            "request-review": ["request-review", "3", "--reviewers", "hubot"],
            "pr-body": ["pr-body", "3", "--body-file", body],
            "pr-open": ["pr-open", "--head", "feat/x", "--base", "main", "--title-file", title, "--body-file", body,
                        "--labels", "needs review", "--reviewers", "hubot"],
            "companion-prs": ["companion-prs", "3", "--companions-file", str(self.project / "companions.json")],
            "testing-steps": ["testing-steps", "3", "--steps-file", str(self.project / "steps.txt")],
            "review-post": ["review-post", "3", "--event", "COMMENT", "--body-file", body],
            "resolve-threads": ["resolve-threads", "3", "--ids-file", str(self.project / "ids.json")],
            "followup": ["followup", "--title-file", title, "--body-file", body, "--parent", "1"],
            "issue-body": ["issue-body", "1", "--body-file", body],
            "claim-pr": ["claim-pr", "3"],
            "release-pr": ["release-pr", "3"],
        }

    def test_every_write_command_is_covered(self):
        import argparse
        writers = set()
        parser = cli.build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    if sub.get_default("writes"):
                        writers.add(name)
        self.assertEqual(writers - {"git"}, set(self.cases))

    def test_dry_run_flag_records_to_the_ledger_and_writes_nothing(self):
        from notme.dryrun import Ledger
        ledger = Ledger.default_for(self.project)  # found once (a git lookup): the project stays put for the loop
        for name, argv in self.cases.items():
            with self.subTest(name):
                before = len(ledger.read())
                self.fake.calls.clear()
                before_state = self.fake.snapshot()
                payload = self.ok(*argv, "--dry-run")
                self.assertTrue(payload.get("dry_run"), payload)
                self.assertEqual(self.fake.writes_made(), [])
                self.assertEqual(self.fake.snapshot(), before_state)
                self.assertGreater(len(ledger.read()), before, f"{name} left no ledger entry")

    def test_mode_dry_run_and_env_do_the_same(self):
        self.tc.write("global", "project", GLOBAL_PROJECT.replace("mode: live", "mode: dry-run"))
        self.assertTrue(self.ok("request-review", "3", "--reviewers", "hubot")["dry_run"])
        self.tc.write("global", "project", GLOBAL_PROJECT)  # `mode: live` in the global layer is enough
        self.assertNotIn("dry_run", self.ok("request-review", "3", "--reviewers", "hubot"))
        env = dict(self.env, NOT_ME_DRY_RUN="1")
        self.assertTrue(self.ok("request-review", "3", "--reviewers", "octocat", env=env)["dry_run"])
        self.assertEqual(things(self.fake)["3"]["reviewRequests"], ["octocat", "hubot"])

    def test_log_option_appends_an_action_line(self):
        start = self.ok("log", "start", "--skill", "new", "--ref", "1")
        payload = self.ok("request-review", "3", "--reviewers", "hubot", "--dry-run", "--log", start["path"])
        self.assertTrue(payload["dry_run"])
        text = Path(start["path"]).read_text(encoding="utf-8")
        self.assertRegex(text, r"ACTION  notme request-review 3 \(dry run\)")


class NoneResultTests(CliCase):
    def test_none_result_is_exit_zero(self):
        for argv in (["pick-needs-work"], ["pick-needs-work", "--include-answered"], ["pick-reviews"], ["log", "last"], ["prove-target", "3"]):
            with self.subTest(argv=argv):
                self.assertTrue(self.ok(*argv)["none"])


if __name__ == "__main__":
    unittest.main()
