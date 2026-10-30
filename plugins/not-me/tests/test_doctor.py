"""`notme doctor` probes: the `fix` and per-OS `hint` each check carries, per check and state."""
from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from notme import doctor
from notme.gh import GhError

PLATFORMS = ("darwin", "linux", "win32")
FILES = {"default": ["project"], "global": ["project"], "repo": []}


class StubGh:
    def __init__(self, who="octocat", header="x-oauth-scopes: repo, read:org\n", fail_me=False):
        self.who, self.header, self.fail_me = who, header, fail_me

    def me(self):
        if self.fail_me:
            raise GhError(["api", "user"], 1, "no token")
        return self.who

    def read(self, argv, json=True):
        return self.header


def ok_runner(argv):
    return SimpleNamespace(returncode=0, stdout="gh version 2.60.0 (2024-10-01)\n", stderr="")


def no_gh(argv):
    raise FileNotFoundError(2, "no", "gh")


def on(platform):
    return mock.patch.object(doctor.sys, "platform", platform)


def by_name(checks):
    return {c["name"]: c for c in checks}


class FieldsTests(unittest.TestCase):
    def test_check_is_backward_compatible_and_carries_fix_and_hint(self):
        self.assertEqual(doctor.check("x", False, "bad"),
                         {"name": "x", "ok": False, "detail": "bad", "required": True, "fix": None, "hint": None})
        c = doctor.check("x", False, "bad", False, fix="do it", hint="install it")
        self.assertEqual((c["required"], c["fix"], c["hint"]), (False, "do it", "install it"))

    def test_a_passing_check_drops_fix_and_hint(self):
        c = doctor.check("x", True, "fine", fix="do it", hint="install it")
        self.assertEqual((c["fix"], c["hint"]), (None, None))

    def test_every_check_in_a_full_run_has_fix_and_hint(self):
        for plat in PLATFORMS:
            with on(plat):
                checks = doctor.run(ok_runner, [], lambda: StubGh(), lambda: {"active": True, "selftest": True},
                                    {doctor.SUBAGENT_DEPTH_ENV: "3"}, doctor.mode_check("live", None),
                                    config_files=FILES)
            self.assertIn("config files", by_name(checks))
            for c in checks:
                with self.subTest(platform=plat, check=c["name"]):
                    self.assertTrue(c["ok"])
                    self.assertIsNone(c["fix"], "a passing check offers no fix")
                    self.assertIsNone(c["hint"])

    def test_no_config_files_argument_means_no_probe(self):
        checks = doctor.run(ok_runner, [], lambda: StubGh(), lambda: {}, {})
        self.assertNotIn("config files", by_name(checks))


class InstallHintTests(unittest.TestCase):
    """python, git and gh missing: never a fix (the operator installs them), an install hint per OS."""

    def test_gh_missing(self):
        expected = {"darwin": "brew install gh", "linux": "https://github.com/cli/cli#installation",
                    "win32": "winget install --id GitHub.cli", "freebsd13": "https://cli.github.com"}
        for plat, needle in expected.items():
            with self.subTest(platform=plat), on(plat):
                c = doctor.gh_check(no_gh)
                self.assertFalse(c["ok"])
                self.assertIsNone(c["fix"])
                self.assertIn(needle, c["hint"])

    def test_git_missing_or_failing(self):
        expected = {"darwin": "xcode-select --install", "linux": "apt install git",
                    "win32": "winget install --id Git.Git"}
        failing = subprocess.CompletedProcess(["git"], 1, stdout="", stderr="boom")
        effects = {"missing": {"side_effect": FileNotFoundError(2, "no", "git")}, "failing": {"return_value": failing}}
        for plat, needle in expected.items():
            for state, effect in effects.items():
                with self.subTest(platform=plat, state=state), on(plat), \
                        mock.patch.object(doctor.subprocess, "run", **effect):
                    c = doctor.git_check()
                    self.assertFalse(c["ok"])
                    self.assertIsNone(c["fix"])
                    self.assertIn(needle, c["hint"])

    def test_python_too_old(self):
        expected = {"darwin": "brew install python", "linux": "apt install python3",
                    "win32": "winget install --id Python.Python"}
        for plat, needle in expected.items():
            with self.subTest(platform=plat), on(plat), mock.patch.object(doctor.sys, "version_info", (3, 8, 10)):
                c = doctor.python_check()
                self.assertFalse(c["ok"])
                self.assertEqual(c["detail"], "3.8.10")
                self.assertIsNone(c["fix"])
                self.assertIn(needle, c["hint"])
                self.assertIn("3.9", c["hint"])

    def test_python_current_passes(self):
        self.assertTrue(doctor.python_check()["ok"])


class FixTests(unittest.TestCase):
    def test_spawn_depth_unset_or_low_offers_the_settings_change(self):
        for env in ({}, {doctor.SUBAGENT_DEPTH_ENV: "2"}, {doctor.SUBAGENT_DEPTH_ENV: "x"}):
            with self.subTest(env=env):
                c = doctor.depth_check(env)
                self.assertFalse(c["ok"])
                self.assertIn("`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`", c["fix"])
                self.assertIn('"3"', c["fix"])
                self.assertIn("`env`", c["fix"])
                self.assertIn("~/.claude/settings.json", c["fix"])
                self.assertIsNone(c["hint"])
        passing = doctor.depth_check({doctor.SUBAGENT_DEPTH_ENV: "5"})
        self.assertEqual((passing["ok"], passing["fix"]), (True, None))

    def test_config_invalid_points_to_setup_and_config(self):
        checks = by_name(doctor.run(ok_runner, ["global: bad value"], lambda: StubGh(), lambda: {}, {}))
        c = checks["config"]
        self.assertFalse(c["ok"])
        self.assertIn("/not-me:setup", c["fix"])
        self.assertIn("/not-me:config", c["fix"])
        self.assertTrue(c["detail"].startswith("1 error(s): "))

    def test_missing_config_files_point_to_setup(self):
        for files in ({"default": ["project"], "global": [], "repo": []}, {}):
            with self.subTest(files=files):
                c = doctor.config_files_check(files)
                self.assertEqual(c["name"], "config files")
                self.assertFalse(c["ok"])
                self.assertFalse(c["required"])
                self.assertIn("`/not-me:setup`", c["fix"])
                self.assertIn("no not-me config file", c["detail"])
        checks = doctor.run(ok_runner, [], lambda: StubGh(), lambda: {}, {}, config_files={"global": [], "repo": []})
        self.assertFalse(by_name(checks)["config files"]["ok"])
        self.assertIsNone(doctor.exit_kind([c for c in checks if c["name"] != "spawn depth"]))

    def test_config_files_in_either_layer_pass(self):
        for files, needle in (({"global": ["project", "roster"], "repo": []}, "global: project.md, roster.md"),
                              ({"global": [], "repo": ["verify"]}, "repo: verify.md")):
            with self.subTest(files=files):
                c = doctor.config_files_check(files)
                self.assertTrue(c["ok"])
                self.assertIsNone(c["fix"])
                self.assertIn(needle, c["detail"])

    def test_gh_not_signed_in(self):
        (c,) = doctor.auth_checks(StubGh(fail_me=True))
        self.assertFalse(c["ok"])
        self.assertIn("`gh auth login`", c["fix"])
        self.assertTrue(c["detail"].startswith("not authenticated"))

    def test_gh_missing_repo_scope(self):
        auth, scopes = doctor.auth_checks(StubGh(header="x-oauth-scopes: read:org, gist\n"))
        self.assertTrue(auth["ok"])
        self.assertFalse(scopes["ok"])
        self.assertIn("`gh auth refresh -s repo`", scopes["fix"])

    def test_guard_and_mode_never_offer_a_fix(self):
        guard = doctor.guard_check({"active": True, "selftest": False})
        self.assertFalse(guard["ok"])
        self.assertEqual(guard["detail"], "self-test did not block")
        self.assertIsNone(guard["fix"])
        self.assertIn("/plugin update not-me@not-me", guard["hint"])
        mode = doctor.mode_check("live", "mode: live is set but not confirmed; a person runs /not-me:mode live")
        self.assertFalse(mode["ok"])
        self.assertIsNone(mode["fix"])
        self.assertIsNone(mode["hint"])

    def test_no_failures_means_nothing_to_offer(self):
        with on("linux"):
            checks = doctor.run(ok_runner, [], lambda: StubGh(), lambda: {"active": True, "selftest": True},
                                {doctor.SUBAGENT_DEPTH_ENV: "3"}, config_files=FILES)
        self.assertEqual([c for c in checks if not c["ok"] and c["fix"]], [])
        self.assertIsNone(doctor.exit_kind(checks))


class SyncQueueCheckTests(unittest.TestCase):
    """queue.sync with source labels also needs a usable board: the doctor checks it like a board source."""

    def setUp(self):
        from notme.gh import GhClient
        from notme.writes import GuardContext
        from tests.fakes.fakegh import FakeRunner
        from tests.test_queue_labels import REPO
        self.repo = REPO
        self.fake = FakeRunner()
        self.fake.state["repos"][REPO]["label_defs"] = {"to do": {}, "in progress": {}}
        ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), board=("acme", 6), mode="live")
        self.gh = GhClient(ctx, runner=self.fake, dry_run=False)

    def run_check(self, confirmed=("acme", 6), **queue):
        from tests.test_queue_source import meta
        return doctor.queue_check(meta(**queue), self.gh, self.repo, confirmed, origin=self.repo)

    def test_sync_with_a_good_board_is_ok(self):
        c = self.run_check(sync=True, board={"owner": "acme", "number": 6, "status_field": "Status"},
                           board_states={"todo": "To Do", "in_progress": "In progress", "in_review": "In review"})
        self.assertTrue(c["ok"], c)
        self.assertEqual(self.fake.writes_made(), [])

    def test_sync_checks_the_board_columns(self):
        c = self.run_check(sync=True, board={"owner": "acme", "number": 6, "status_field": "Status"})
        self.assertFalse(c["ok"])
        self.assertIn("no `Blocked` option", c["detail"])

    def test_sync_checks_the_board_is_confirmed(self):
        c = self.run_check(confirmed=None, sync=True, board={"owner": "acme", "number": 6, "status_field": "Status"},
                           board_states={"todo": "To Do"})
        self.assertFalse(c["ok"])
        self.assertIn("--board acme/6", c["detail"])
        self.assertEqual([x for x in self.fake.calls if "graphql" in x], [])  # an unconfirmed board is not read

    def test_sync_checks_the_board_exists(self):
        c = self.run_check(confirmed=("acme", 99), sync=True,
                           board={"owner": "acme", "number": 99, "status_field": "Status"})
        self.assertFalse(c["ok"])
        self.assertIn("not found", c["detail"])

    def test_sync_without_a_board_names_the_fix(self):
        c = self.run_check(sync=True)
        self.assertFalse(c["ok"])
        self.assertIn("queue.sync", c["detail"])
        self.assertIn("queue.board.owner", c["detail"])

    def test_sync_off_does_not_check_the_board(self):
        c = self.run_check(board={"owner": "acme", "number": 99, "status_field": "Status"})
        self.assertTrue(c["ok"], c)


class PermissionsTests(unittest.TestCase):
    """The recommended permissions.allow rules: reported when missing, with the JSON to add; never written."""

    LAUNCHER = "/plugins/not-me/bin/notme"

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def settings(self, name, allow=None, raw=None):
        import json
        path = self.dir / name
        path.write_text(raw if raw is not None else json.dumps({"permissions": {"allow": allow or []}}),
                        encoding="utf-8")
        return path

    def wanted(self):
        return list(doctor.permission_rules(self.LAUNCHER))

    def test_the_rules_include_the_issue_and_pr_housekeeping_and_the_launcher(self):
        rules = self.wanted()
        for rule in ("Bash(gh issue close:*)", "Bash(gh issue comment:*)", "Bash(gh issue edit:*)",
                     "Bash(gh pr comment:*)", "Bash(notme:*)", f"Bash({self.LAUNCHER}:*)"):
            self.assertIn(rule, rules)
        self.assertNotIn("Bash(gh:*)", rules)

    def test_missing_rules_are_listed_with_the_json_to_add_and_no_fix(self):
        import json
        path = self.settings("settings.json", ["Bash(gh issue close:*)"])
        before = path.read_text(encoding="utf-8")
        c = doctor.permissions_check([path, self.dir / "absent.json"], self.LAUNCHER)
        self.assertFalse(c["ok"])
        self.assertFalse(c["required"])
        self.assertIsNone(c["fix"], "doctor never edits settings")
        missing = [r for r in self.wanted() if r != "Bash(gh issue close:*)"]
        start = c["hint"].index("{")
        self.assertEqual(json.loads(c["hint"][start:]), {"permissions": {"allow": missing}})
        self.assertIn("docs/permissions.md", c["hint"])
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_rules_across_files_and_broader_prefixes_count(self):
        a = self.settings("settings.json", ["Bash(gh issue:*)", "Bash(gh pr comment:*)"])
        b = self.settings("settings.local.json", ["Bash(notme:*)", f"Bash({self.LAUNCHER}:*)"])
        self.assertTrue(doctor.permissions_check([a, b], self.LAUNCHER)["ok"])

    def test_a_blanket_gh_rule_counts_but_is_called_out(self):
        a = self.settings("settings.json", ["Bash(gh:*)", "Bash(notme:*)", f"Bash({self.LAUNCHER}:*)"])
        c = doctor.permissions_check([a], self.LAUNCHER)
        self.assertTrue(c["ok"])
        self.assertIn("Bash(gh:*)", c["detail"])

    def test_an_unreadable_settings_file_is_named_and_treated_as_empty(self):
        bad = self.settings("settings.json", raw="{not json")
        c = doctor.permissions_check([bad], self.LAUNCHER)
        self.assertFalse(c["ok"])
        self.assertIn("could not be read", c["detail"])

    def test_the_doc_lists_every_rule_doctor_checks(self):
        from pathlib import Path
        doc = (Path(__file__).resolve().parents[3] / "docs" / "permissions.md").read_text(encoding="utf-8")
        for rule in doctor.permission_rules("<launcher>"):
            self.assertIn(rule, doc)
        self.assertIn("Bash(gh:*)", doc)

    def test_run_adds_the_check_when_asked(self):
        checks = doctor.run(ok_runner, [], lambda: StubGh(), lambda: {}, {},
                            permissions=lambda: doctor.check("permissions", True, "all there", required=False))
        self.assertIn("permissions", by_name(checks))


if __name__ == "__main__":
    unittest.main()
