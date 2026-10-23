"""Merge grants: the UserPromptSubmit hook that writes and ends them, and the guard that spends them."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notme import guard, humans, merge_grant, runlog
from notme.guard import Block, GuardContext
from tests import PLUGIN_ROOT
from tests import guard_cases as gc

PROMPT_HOOK = PLUGIN_ROOT / "hooks" / "merge_grant.py"


class _Home(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="notme-grant-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.home), True)
        humans.create_token(self.home)  # grants are signed with the operator token

    def prompt(self, text, sid="s1", event="UserPromptSubmit"):
        """Run the real hook as Claude Code does: JSON on stdin, NOT_ME_HOME in the environment."""
        payload = {"hook_event_name": event, "session_id": sid, "prompt": text, "cwd": str(self.home),
                   "transcript_path": "/dev/null"}
        env = {k: v for k, v in os.environ.items() if not k.startswith("NOT_ME_")}
        proc = subprocess.run([sys.executable, str(PROMPT_HOOK)], input=json.dumps(payload).encode("utf-8"),
                              capture_output=True, env=dict(env, NOT_ME_HOME=str(self.home)), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def grant(self, sid="s1"):
        return merge_grant.read(self.home, sid)


class PromptHookTests(_Home):
    def test_a_valid_prompt_writes_this_sessions_grant(self):
        self.prompt("/not-me:allow-merge acme/app#12")
        g = self.grant()
        self.assertEqual((g["repo"], g["number"], g["method"], g["session"]), ("acme/app", 12, None, "s1"))
        self.assertLessEqual(abs(time.time() - g["created"]), 60)
        self.prompt("  /not-me:allow-merge Acme-Co/my.repo_x#7 --rebase\n")
        g = self.grant()
        self.assertEqual((g["repo"], g["number"], g["method"]), ("Acme-Co/my.repo_x", 7, "rebase"))

    def test_a_malformed_argument_writes_nothing(self):
        for text in ("/not-me:allow-merge", "/not-me:allow-merge acme/app", "/not-me:allow-merge acme/app#0",
                     "/not-me:allow-merge acme/app#012", "/not-me:allow-merge #5", "/not-me:allow-merge acme#5",
                     "/not-me:allow-merge acme/app#5 acme/app#6", "/not-me:allow-merge acme/app#5 --admin",
                     "/not-me:allow-merge acme/app#5 and merge the rest", "/not-me:allow-merge acme/app#5\nmore",
                     "/not-me:allow-merge acme/../app#5", "/not-me:allow-merge acme/..#5",
                     "/not-me:allow-merge -acme/app#5", "/not-me:allow-merge https://github.com/acme/app/pull/5",
                     "/not-me:allow-merge acme/app#5;rm", "/not-me:allow-merge acme/app#99999999999",
                     "/not-me:allow-mergex acme/app#5", "/not-me:allow-merge:x acme/app#5"):
            with self.subTest(text=text):
                self.prompt(text)
                self.assertIsNone(self.grant())
                self.assertFalse(merge_grant.grant_path(self.home, "s1").exists())

    def test_the_command_mid_prompt_grants_nothing(self):
        for text in ("please run /not-me:allow-merge acme/app#5", "x\n/not-me:allow-merge acme/app#5",
                     "`/not-me:allow-merge acme/app#5`", "/allow-merge acme/app#5"):
            with self.subTest(text=text):
                self.prompt(text)
                self.assertIsNone(self.grant())

    def test_a_malformed_command_says_so(self):
        out = self.prompt("/not-me:allow-merge acme/app").stdout.decode()
        self.assertIn("no merge was granted", out)
        self.assertEqual(self.prompt("hello").stdout, b"")

    def test_the_next_prompt_ends_the_grant(self):
        self.prompt("/not-me:allow-merge acme/app#5")
        self.assertIsNotNone(self.grant())
        self.prompt("thanks")
        self.assertIsNone(self.grant())
        self.prompt("/not-me:allow-merge acme/app#5")
        self.prompt("/not-me:allow-merge acme/app")  # a malformed grant also ends the earlier one
        self.assertIsNone(self.grant())

    def test_a_new_grant_replaces_the_old_one(self):
        self.prompt("/not-me:allow-merge acme/app#5")
        self.prompt("/not-me:allow-merge acme/app#6")
        self.assertEqual(self.grant()["number"], 6)

    def test_sessions_are_isolated(self):
        self.prompt("/not-me:allow-merge acme/app#5", sid="s1")
        self.prompt("/not-me:allow-merge acme/app#9", sid="s2")
        self.prompt("unrelated", sid="s2")
        self.assertEqual(self.grant("s1")["number"], 5)
        self.assertIsNone(self.grant("s2"))

    def test_session_ids_are_sanitised_and_required(self):
        self.prompt("/not-me:allow-merge acme/app#5", sid="../../x y")
        self.assertEqual(sorted(p.name for p in merge_grant.grant_dir(self.home).iterdir()), ["xy.json"])
        self.assertIsNone(merge_grant.session_key(""))
        self.assertIsNone(merge_grant.session_key("../"))
        self.assertIsNone(merge_grant.session_key(None))
        shutil.rmtree(str(merge_grant.grant_dir(self.home)))
        self.prompt("/not-me:allow-merge acme/app#5", sid="")
        self.assertFalse(merge_grant.grant_dir(self.home).exists())

    def test_other_events_and_bad_input_do_nothing(self):
        self.prompt("/not-me:allow-merge acme/app#5", event="PreToolUse")
        self.assertIsNone(self.grant())
        proc = subprocess.run([sys.executable, str(PROMPT_HOOK)], input=b"{not json", capture_output=True,
                              env=dict(os.environ, NOT_ME_HOME=str(self.home)), timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(merge_grant.grant_dir(self.home).exists())

    def test_expiry(self):
        merge_grant.write(self.home, "s1", {"repo": "acme/app", "number": 5}, now=time.time() - 1799)
        self.assertIsNotNone(self.grant())
        merge_grant.write(self.home, "s1", {"repo": "acme/app", "number": 5}, now=time.time() - 1801)
        self.assertIsNone(self.grant())
        merge_grant.write(self.home, "s1", {"repo": "acme/app", "number": 5}, now=time.time() + 600)
        self.assertIsNone(self.grant())  # from the future
        path = merge_grant.write(self.home, "s1", {"repo": "acme/app", "number": 5})
        old = time.time() - 3600
        os.utime(str(path), (old, old))
        self.assertIsNone(self.grant())  # an old file with a fresh-looking time inside

    def test_a_grant_for_another_session_or_malformed_is_ignored(self):
        path = merge_grant.write(self.home, "s1", {"repo": "acme/app", "number": 5})
        data = json.loads(path.read_text())
        for bad in (dict(data, session="s2"), dict(data, number="5"), dict(data, number=True),
                    dict(data, repo=None), dict(data, created="now"), [data], dict(data, method="octopus"),
                    dict(data, number=6), dict(data, repo="acme/other"), dict(data, method="rebase"),
                    {k: v for k, v in data.items() if k != "mac"}, dict(data, mac="0" * 64), dict(data, mac="é")):
            with self.subTest(bad=bad):
                path.write_text(json.dumps(bad))
                self.assertIsNone(self.grant())

    @unittest.skipIf(sys.platform == "win32", "symlinks need privileges on Windows")
    def test_a_symlinked_grant_is_ignored(self):
        real = merge_grant.write(self.home, "real", {"repo": "acme/app", "number": 5})
        link = merge_grant.grant_path(self.home, "s1")
        os.symlink(str(real), str(link))
        self.assertIsNone(self.grant())

    def test_a_grant_is_signed_with_the_operator_token(self):
        merge_grant.write(self.home, "s1", {"repo": "acme/app", "number": 5})
        self.assertIsNotNone(self.grant())
        token = humans.token_path(self.home)
        token.write_text("a-new-token\n")  # a rotated token invalidates an earlier grant
        self.assertIsNone(self.grant())
        token.unlink()
        self.assertIsNone(self.grant())
        with self.assertRaises(merge_grant.NoOperatorToken):
            merge_grant.write(self.home, "s1", {"repo": "acme/app", "number": 5})

    def test_without_a_token_the_hook_grants_nothing_and_says_why(self):
        humans.token_path(self.home).unlink()
        out = self.prompt("/not-me:allow-merge acme/app#5").stdout.decode()
        self.assertIn("operator token", out)
        self.assertFalse(merge_grant.grant_path(self.home, "s1").exists())

    def test_consume_is_single_use(self):
        merge_grant.write(self.home, "s1", {"repo": "acme/app", "number": 5})
        self.assertTrue(merge_grant.consume(self.home, "s1"))
        self.assertFalse(merge_grant.consume(self.home, "s1"))
        self.assertEqual(list(merge_grant.grant_dir(self.home).iterdir()), [])


class GuardGrantTests(unittest.TestCase):
    """decide() with a grant on disk: exactly the granted merge, once, outside a run."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="notme-guard-")).resolve()
        cls.env = gc.build_fixtures(cls.root)
        cls._patch = mock.patch.dict(os.environ, cls.env, clear=True)
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.home = self.root / "homes" / "grant-unit"
        shutil.rmtree(str(self.home), ignore_errors=True)
        self.home.mkdir(parents=True)
        humans.create_token(self.home)

    def decide(self, cmd, variant="app", sid="s1"):
        cwd, _ = gc.variant_paths(self.root, variant)
        ctx = GuardContext.from_project(cwd, home=self.home, env=dict(self.env, NOT_ME_HOME=str(self.home)))
        if not guard.is_active(ctx):
            ctx.self_only()
        event = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd), "session_id": sid}
        return guard.decide(event, ctx)

    def grant(self, repo="acme/app", number=5, sid="s1", method=None):
        merge_grant.write(self.home, sid, {"repo": repo, "number": number, "method": method})

    def test_the_granted_merge_is_allowed_once_and_logged(self):
        self.grant()
        self.assertIsNone(self.decide("gh pr merge 5 -R acme/app --squash"))
        self.assertFalse(merge_grant.grant_path(self.home, "s1").exists())
        again = self.decide("gh pr merge 5 -R acme/app --squash")
        self.assertIsInstance(again, Block)
        self.assertEqual(again.rule, "protected_branches")
        log = runlog.guard_log_path(guard.log_root(self.root / "app" / ".git"))
        entries = [json.loads(x) for x in log.read_text().splitlines()]
        self.assertTrue(any(e.get("rule") == "merge_grant" and e.get("target") == "acme/app#5"
                            and e.get("session") == "s1" for e in entries))

    def test_allowed_forms(self):
        for method, cmd in ((None, "gh pr merge 5 -R acme/app --squash"),
                            ("squash", "gh pr merge 5 -R acme/app --squash"),
                            ("merge", "gh pr merge 5 --repo acme/app --merge"),
                            ("rebase", "gh pr merge --rebase -R Acme/APP 5"),
                            (None, "gh pr merge 5 -R acme/app --squash --subject 'fix: x (#5)'"),
                            (None, 'gh pr merge 5 -R acme/app --squash --body-file README.md --subject "a; b | c"')):
            with self.subTest(cmd=cmd, method=method):
                self.grant(method=method)
                self.assertIsNone(self.decide(cmd))
                self.assertFalse(merge_grant.grant_path(self.home, "s1").exists())

    def test_the_method_is_bound(self):
        for method, cmd in ((None, "gh pr merge 5 -R acme/app"), (None, "gh pr merge 5 -R acme/app --rebase"),
                            (None, "gh pr merge 5 -R acme/app --merge"), ("rebase", "gh pr merge 5 -R acme/app"),
                            ("rebase", "gh pr merge 5 -R acme/app --squash"), ("merge", "gh pr merge 5 -R acme/app --rebase"),
                            ("squash", "gh pr merge 5 -R acme/app --merge")):
            with self.subTest(cmd=cmd, method=method):
                self.grant(method=method)
                self.assertIsInstance(self.decide(cmd), Block)
                self.assertTrue(merge_grant.grant_path(self.home, "s1").exists())

    def test_a_forged_grant_is_ignored(self):
        self.grant()
        path = merge_grant.grant_path(self.home, "s1")
        data = json.loads(path.read_text())
        path.write_text(json.dumps(dict(data, number=6)))  # the mac no longer matches
        self.assertIsInstance(self.decide("gh pr merge 6 -R acme/app --squash"), Block)

    def test_blocked_forms_keep_the_grant(self):
        for cmd in ("gh pr merge 6 -R acme/app --squash", "gh pr merge 5 -R acme/other --squash",
                    "gh pr merge 5 -R acme/app --admin --squash", "gh pr merge 5 -R acme/app --auto --squash",
                    "gh pr merge 5 -R acme/app -d", "gh pr merge 5 -R acme/app --squash --rebase",
                    "gh pr merge 5 -R acme/app -s", "gh pr merge 5", "gh pr merge -R acme/app",
                    "gh pr merge 5 5 -R acme/app", "gh pr merge 5 -R acme/app -R acme/app",
                    "gh pr merge 05 -R acme/app", "gh pr merge 5 -R=acme/app", "gh pr merge 5 --repo=acme/app",
                    "gh pr merge 5 -Racme/app", "gh pr merge 5 -R acme/app --subject --admin",
                    "gh pr merge 5 -R acme/app --body-file -", "gh pr merge 5 -R https://github.com/acme/app",
                    "gh api -X PUT repos/acme/app/pulls/5/merge", "gh pr merge 5 -R acme/app; git push",
                    "gh pr merge 5 -R acme/app && true", "gh pr merge 5 -R acme/app | cat",
                    "gh pr merge 5 -R acme/app &", "(gh pr merge 5 -R acme/app)", "gh pr merge 5 -R acme/app > x",
                    "gh pr merge 5 -R acme/app\ngit push", "gh pr merge 5 -R $REPO", "gh pr merge $(echo 5) -R acme/app",
                    "gh pr merge 5 -R acme/app --subject \"$(id)\"", "GH_HOST=x gh pr merge 5 -R acme/app",
                    "env gh pr merge 5 -R acme/app", "command gh pr merge 5 -R acme/app",
                    "bash -c 'gh pr merge 5 -R acme/app'", "/usr/bin/gh pr merge 5 -R acme/app",
                    "gh pr merge 5 -R acme/app --body-file " + str(self.home / "operator-token"),
                    "gh pr merge 5 -R acme/app #x", "gh pr  merge 5 -R acme/app \\\n --squash"):
            with self.subTest(cmd=cmd):
                self.grant()
                self.assertIsInstance(self.decide(cmd), Block)
                self.assertTrue(merge_grant.grant_path(self.home, "s1").exists())

    def test_no_grant_another_session_or_a_stale_grant(self):
        self.assertIsInstance(self.decide("gh pr merge 5 -R acme/app"), Block)
        self.grant(sid="s2")
        self.assertIsInstance(self.decide("gh pr merge 5 -R acme/app"), Block)
        self.assertIsInstance(self.decide("gh pr merge 5 -R acme/app", sid=""), Block)
        self.assertIsInstance(self.decide("gh pr merge 5 -R acme/app", sid=None), Block)
        merge_grant.write(self.home, "s1", {"repo": "acme/app", "number": 5}, now=time.time() - 1801)
        self.assertIsInstance(self.decide("gh pr merge 5 -R acme/app"), Block)

    def test_an_active_run_never_merges(self):
        for variant in ("running", "run"):
            with self.subTest(variant=variant):
                self.grant()
                self.assertIsInstance(self.decide("gh pr merge 5 -R acme/app --squash", variant), Block)
                self.assertTrue(merge_grant.grant_path(self.home, "s1").exists())

    def test_the_repo_must_be_allowed(self):
        self.grant(repo="acme/zzz")
        result = self.decide("gh pr merge 5 -R acme/zzz --squash")
        self.assertIsInstance(result, Block)
        self.assertTrue(merge_grant.grant_path(self.home, "s1").exists())

    def test_a_grant_spent_by_another_call_blocks(self):
        self.grant()
        with mock.patch.object(merge_grant, "consume", return_value=False):
            result = self.decide("gh pr merge 5 -R acme/app --squash")
        self.assertIsInstance(result, Block)
        self.assertIn("already used", result.reason)

    def test_outside_a_project_the_grant_is_not_spent(self):
        # the merge rule is off outside a not-me project (self-protection only), so the grant is left alone there
        self.grant()
        self.assertIsNone(self.decide("gh pr merge 5 -R acme/app --squash", "bare"))
        self.assertTrue(merge_grant.grant_path(self.home, "s1").exists())


if __name__ == "__main__":
    unittest.main()
