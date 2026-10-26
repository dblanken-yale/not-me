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


if __name__ == "__main__":
    unittest.main()
