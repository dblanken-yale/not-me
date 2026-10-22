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

    def __init__(self, sees_plugin=False, sees_fixture=True, plugin_error="HTTP 404", push=True):
        self.calls, self.sees_plugin, self.sees_fixture = [], sees_plugin, sees_fixture
        self.plugin_error, self.push = plugin_error, push
        # `gh api user/repos` entries as (full_name, private); default: the fixture private, plugin public.
        self.listed = None
        # Reply to the no-op write probe on the plugin repo: 403 not granted by default.
        self.probe_error = f"HTTP 403: {scaffold.NOT_GRANTED}"

    def __call__(self, argv, cwd=None, env=None, input_text=None):
        self.calls.append((list(argv), dict(env) if env is not None else None))
        if argv[:3] == ["gh", "api", "user"]:
            return type("P", (), {"stdout": json.dumps({"login": "fixture-bot", "id": 7})})()
        if argv[:2] == ["gh", "api"] and argv[-1].startswith("user/repos"):
            listed = [(FIX, True), (PLUGIN, False)] if self.listed is None else self.listed
            if not isinstance(listed, list):
                return type("P", (), {"stdout": listed})()
            return type("P", (), {"stdout": json.dumps([{"full_name": n, "private": p} for n, p in listed])})()
        if argv[:4] == ["gh", "api", "-X", "PATCH"] and argv[4].startswith(f"repos/{PLUGIN}/labels/"):
            if self.probe_error is None:
                return type("P", (), {"stdout": "{}"})()
            raise scaffold.ScaffoldError(f"gh api failed (1): {self.probe_error}")
        if argv[:2] == ["gh", "api"] and argv[2].startswith("repos/"):
            repo = argv[2][len("repos/"):]
            if repo == PLUGIN and not self.sees_plugin:
                raise scaffold.ScaffoldError(f"gh api {repo} failed (1): {self.plugin_error}")
            if repo != PLUGIN and not self.sees_fixture:
                raise scaffold.ScaffoldError(f"gh api {repo} failed (1): HTTP 404")
            if repo == PLUGIN:
                # A public plugin repo reports the account's role, which the owner's token also shows.
                return type("P", (), {"stdout": json.dumps({"permissions": {"admin": True, "push": True}})})()
            return type("P", (), {"stdout": json.dumps({"permissions": {"push": self.push}})})()
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
        self.env = {scaffold.TOKEN_ENV: "github_pat_tok", scaffold.PLUGIN_REPO_ENV: PLUGIN, "GH_TOKEN": "operator"}

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

    def test_refuses_a_token_granted_private_repos_outside_the_fixture_set(self):
        for listed in ([(FIX, True), ("acme/other", True)], [(FIX, True), (PLUGIN, True)], [("ACME/Other", True)]):
            fake = FakeRun()
            fake.listed = listed
            with self.assertRaisesRegex(scaffold.ScaffoldError, "private repositories outside", msg=str(listed)):
                self.prepare(fake)
            self.assertFalse(self.stage.exists())
            self.assertFalse(any(a[:3] == ["gh", "repo", "clone"] for a, _ in fake.calls))

    def test_a_granted_companion_is_allowed_when_the_manifest_names_it(self):
        self.manifest.write_text(json.dumps({"scenario": "N1", "run_id": "rid", "repo": FIX, "companion": COMP,
                                             "shas": {"main": "a" * 40}}), encoding="utf-8")
        fake = FakeRun()
        fake.listed = [(FIX, True), (COMP, True), (PLUGIN, False)]
        self.prepare(fake)
        self.assertTrue(self.stage.exists())

    def test_public_repos_in_the_listing_are_not_held_against_the_token(self):
        fake = FakeRun()
        fake.listed = [(FIX, True), ("acme/other", False), ("someone/else", False)]
        self.prepare(fake)
        self.assertTrue(self.stage.exists())

    def test_an_unlistable_grant_refuses(self):
        fake = FakeRun()
        fake.listed = "<html>"
        with self.assertRaisesRegex(scaffold.ScaffoldError, "cannot list"):
            self.prepare(fake)

    def test_a_public_plugin_repo_passes_only_when_the_write_probe_is_refused(self):
        fake = FakeRun(sees_plugin=True)
        self.prepare(fake)
        self.assertTrue(self.stage.exists())
        probes = [a for a, _ in fake.calls if a[:4] == ["gh", "api", "-X", "PATCH"]]
        self.assertEqual(len(probes), 1)
        self.assertTrue(probes[0][4].startswith(f"repos/{PLUGIN}/labels/not-me-probe-"))
        self.assertNotIn("github_pat_tok", " ".join(probes[0]))

    def test_a_write_probe_the_plugin_repo_accepts_refuses(self):
        for err in ("HTTP 404: Not Found", None, "HTTP 403: Must have admin rights", "HTTP 422: Validation Failed",
                    "HTTP 502", "error connecting to api.github.com"):
            fake = FakeRun(sees_plugin=True)
            fake.probe_error = err
            with self.assertRaisesRegex(scaffold.ScaffoldError, "can write to acme/plugin|cannot confirm", msg=str(err)):
                self.prepare(fake)
            self.assertFalse(self.stage.exists())

    def test_a_private_plugin_repo_404_skips_the_write_probe(self):
        fake = FakeRun()
        self.prepare(fake)
        self.assertFalse(any(a[:4] == ["gh", "api", "-X", "PATCH"] for a, _ in fake.calls))

    def test_only_an_explicit_404_counts_as_not_visible(self):
        self.prepare(FakeRun(plugin_error="gh: Not Found (HTTP 404)"))

    def test_a_403_on_the_plugin_repo_refuses(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "expected HTTP 404 or 200"):
            self.prepare(FakeRun(plugin_error="gh: Resource not accessible (HTTP 403)"))
        self.assertFalse(self.stage.exists())

    def test_401_and_5xx_on_the_plugin_repo_refuse(self):
        for err in ("HTTP 401", "HTTP 502"):
            with self.assertRaises(scaffold.ScaffoldError, msg=err):
                self.prepare(FakeRun(plugin_error=err))

    def test_a_network_error_on_the_plugin_repo_refuses(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "expected HTTP 404 or 200"):
            self.prepare(FakeRun(plugin_error="error connecting to api.github.com"))

    def test_gh_missing_refuses(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "could not run"):
            scaffold.run(["definitely-not-a-real-gh-binary", "api", "user"])

    def test_classic_and_oauth_tokens_are_refused_before_any_call(self):
        for tok in ("ghp_abc", "gho_abc", "abc123"):
            fake = FakeRun()
            with self.assertRaisesRegex(scaffold.ScaffoldError, "fine-grained"):
                self.prepare(fake, dict(self.env, **{scaffold.TOKEN_ENV: tok}))
            self.assertEqual(fake.calls, [])

    def test_a_token_without_push_on_the_fixture_is_refused(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "cannot push"):
            self.prepare(FakeRun(push=False))

    def test_network_env_passes_only_proxy_and_ca_settings(self):
        src = {"HTTPS_PROXY": "http://p:1", "no_proxy": "localhost", "SSL_CERT_FILE": "/ca.pem", "GH_TOKEN": "x",
               "HOME": "/h", "HTTP_PROXY": "", "AWS_SECRET_ACCESS_KEY": "y"}
        self.assertEqual(scaffold.network_env(src),
                         {"HTTPS_PROXY": "http://p:1", "no_proxy": "localhost", "SSL_CERT_FILE": "/ca.pem"})

    def test_refuses_a_token_that_cannot_see_the_fixture(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "cannot read the fixture repo"):
            self.prepare(FakeRun(sees_fixture=False))

    def test_verifies_with_the_token_header_and_never_asks_gh_for_a_login(self):
        fake = FakeRun()
        self.prepare(fake)
        api = [a for a, _ in fake.calls if a[:2] == ["gh", "api"]]
        self.assertEqual([a[-1] for a in api], ["user", "user/repos?per_page=100", f"repos/{FIX}", f"repos/{PLUGIN}"])
        for a in api:
            self.assertNotIn("-H", a)  # no header flag: the token is not in argv
            self.assertFalse(any("github_pat_tok" in part for part in a))
        self.assertFalse(any(a[:3] == ["gh", "auth", "token"] for a, _ in fake.calls))
        for a, env in fake.calls:
            if a[0] == "gh":
                self.assertEqual(env["GH_TOKEN"], "github_pat_tok")
                self.assertNotIn("hosts.yml", json.dumps(env))

    def test_the_token_is_in_the_env_of_every_gh_call_and_in_no_argv(self):
        fake = FakeRun()
        self.prepare(fake)
        self.assertTrue(any(a[:3] == ["gh", "repo", "clone"] for a, _ in fake.calls))
        for argv, env in fake.calls:
            self.assertNotIn("github_pat_tok", " ".join(argv))
            if argv[0] == "gh":
                self.assertEqual(env["GH_TOKEN"], "github_pat_tok")
                self.assertTrue(env["GH_CONFIG_DIR"])

    def test_errors_never_carry_a_token(self):
        err = scaffold.ScaffoldError("gh failed: Authorization: token github_pat_ABC123 bad; also ghp_XYZ")
        self.assertNotIn("github_pat_ABC123", str(err))
        self.assertNotIn("ghp_XYZ", str(err))
        fake = FakeRun(plugin_error="HTTP 403 for token github_pat_tok")
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            self.prepare(fake)
        self.assertNotIn("github_pat_tok", str(ctx.exception))

    def test_stages_the_token_0600_and_clean_removes_it(self):
        self.prepare(FakeRun())
        token = self.stage / "gh-token"
        self.assertEqual(token.read_text(encoding="utf-8").strip(), "github_pat_tok")
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
