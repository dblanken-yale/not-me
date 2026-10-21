import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import _support as s

sys.path.insert(0, str(s.HARNESS / "scaffold"))
import scaffold  # noqa: E402

FIX, COMP, PLUGIN = "acme/x-fixture", "acme/x-fixture-companion", "acme/plugin"


class FakeRun:
    """A scaffold runner: records calls; the token can see the fixtures and optionally the plugin repo."""

    def __init__(self, sees_plugin=False, sees_fixture=True):
        self.calls, self.sees_plugin, self.sees_fixture = [], sees_plugin, sees_fixture

    def __call__(self, argv, cwd=None, env=None, input_text=None):
        self.calls.append((list(argv), dict(env) if env is not None else None))
        if argv[:3] == ["gh", "api", "user"]:
            return type("P", (), {"stdout": json.dumps({"login": "fixture-bot", "id": 7})})()
        if argv[:2] == ["gh", "api"] and argv[2].startswith("repos/"):
            repo = argv[2][len("repos/"):]
            if repo == PLUGIN and not self.sees_plugin or repo != PLUGIN and not self.sees_fixture:
                raise scaffold.ScaffoldError(f"gh api {repo} failed (1): HTTP 404")
            return type("P", (), {"stdout": "{}"})()
        if argv[:3] == ["gh", "repo", "clone"]:
            Path(argv[4]).mkdir(parents=True)
            return type("P", (), {"stdout": ""})()
        return type("P", (), {"stdout": ""})()

    def names(self):
        return [" ".join(a[:3]) for a, _ in self.calls]


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = self.dir / "state"
        self.manifest = self.dir / "N1.json"
        self.manifest.write_text(json.dumps({"scenario": "N1", "run_id": "rid", "repo": FIX, "companion": None,
                                             "shas": {"main": "a" * 40}}), encoding="utf-8")
        self.stage = self.state / "rid" / "N1.stage"
        self.env = {scaffold.TOKEN_ENV: "tok", scaffold.PLUGIN_REPO_ENV: PLUGIN, "GH_TOKEN": "operator"}

    def prepare(self, runner, env=None):
        return scaffold.prepare(self.manifest, self.stage, self.env if env is None else env, runner=runner,
                                state=self.state)

    def test_refuses_without_the_fixture_token(self):
        fake = FakeRun()
        env = {k: v for k, v in self.env.items() if k != scaffold.TOKEN_ENV}
        with self.assertRaisesRegex(scaffold.ScaffoldError, "NOT_ME_FIXTURE_TOKEN is not set"):
            self.prepare(fake, env)
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.stage.exists())

    def test_the_operators_gh_token_is_not_a_substitute(self):
        with self.assertRaises(scaffold.ScaffoldError):
            self.prepare(FakeRun(), {"GH_TOKEN": "operator", scaffold.PLUGIN_REPO_ENV: PLUGIN})

    def test_refuses_a_token_that_can_see_the_plugin_repo(self):
        fake = FakeRun(sees_plugin=True)
        with self.assertRaisesRegex(scaffold.ScaffoldError, "can see acme/plugin"):
            self.prepare(fake)
        self.assertFalse(self.stage.exists())
        self.assertFalse(any(a[:3] == ["gh", "repo", "clone"] for a, _ in fake.calls))

    def test_refuses_a_token_that_cannot_see_the_fixture(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "cannot see the fixture repo"):
            self.prepare(FakeRun(sees_fixture=False))

    def test_verifies_with_the_token_header_and_never_asks_gh_for_a_login(self):
        fake = FakeRun()
        self.prepare(fake)
        api = [a for a, _ in fake.calls if a[:2] == ["gh", "api"]]
        self.assertEqual([a[2] for a in api], ["user", f"repos/{FIX}", f"repos/{PLUGIN}"])
        for a in api:
            self.assertEqual(a[3:], ["-H", "Authorization: token tok"])
        self.assertFalse(any(a[:3] == ["gh", "auth", "token"] for a, _ in fake.calls))
        for a, env in fake.calls:
            if a[0] == "gh":
                self.assertEqual(env["GH_TOKEN"], "tok")
                self.assertNotIn("hosts.yml", json.dumps(env))

    def test_stages_the_token_0600_and_clean_removes_it(self):
        self.prepare(FakeRun())
        token = self.stage / "gh-token"
        self.assertEqual(token.read_text(encoding="utf-8").strip(), "tok")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertTrue((self.state / scaffold.POINTER).is_file())
        removed = scaffold.clean(self.state, self.stage)
        self.assertFalse(self.stage.exists())
        self.assertFalse((self.state / scaffold.POINTER).exists())
        self.assertTrue(removed)

    def test_clean_is_safe_when_nothing_was_prepared(self):
        self.assertEqual(scaffold.clean(self.state, self.stage), [])

    def test_clean_finds_the_stage_from_the_pointer(self):
        self.prepare(FakeRun())
        scaffold.clean(self.state)
        self.assertFalse(self.stage.exists())

    def test_stage_info_names_the_fixture_bot_as_operator(self):
        self.prepare(FakeRun())
        info = json.loads((self.stage / "stage.json").read_text(encoding="utf-8"))
        self.assertEqual((info["operator"], info["repo"]), ("fixture-bot", FIX))


if __name__ == "__main__":
    unittest.main()
