from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from notme import writes
from notme.writes import Decision, GuardContext, WriteTarget, check_write
from tests import TempCase

GLOBAL_MD = """---
schema: 1
mode: live
allow_repos: [acme/extra]
---
"""

PROJECT_MD = """---
schema: 1
repos:
  - {name: acme/app, path: auto, default_branch: auto, role: primary}
  - {name: acme/app-ui, path: auto, default_branch: auto, role: companion}
queue:
  board: {owner: acme, number: 6}
---
"""


def make_ctx(**kw) -> GuardContext:
    base = dict(origin_repo="acme/app", allowed_repos=("acme/app", "acme/app-ui"), board=("acme", 6),
                protected_branches=("main",), mode="live")
    base.update(kw)
    return GuardContext(**base)


class OriginParseTests(unittest.TestCase):
    def test_four_forms(self):
        cases = {
            "https://github.com/acme/app.git": "acme/app",
            "https://github.com/acme/app": "acme/app",
            "git@github.com:acme/app.git": "acme/app",
            "ssh://git@github.com/acme/app": "acme/app",
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(writes.parse_origin(url), want)

    def test_other_forms(self):
        self.assertEqual(writes.parse_origin("https://user:tok@github.com/acme/app-ui.git/"), "acme/app-ui")
        self.assertEqual(writes.parse_origin("ssh://git@github.com:22/acme/app.git"), "acme/app")

    def test_only_github_hosts(self):
        for url in ("https://evil.example/acme/app", "foo@anyhost:acme/app", "git@gitlab.com:acme/app.git",
                    "ssh://git@evil.example/acme/app", "https://github.com.evil.example/acme/app",
                    "https://notgithub.com/acme/app"):
            with self.subTest(url=url):
                self.assertIsNone(writes.parse_origin(url))
        self.assertEqual(writes.parse_origin("https://GitHub.com/acme/app"), "acme/app")

    def test_gh_host_env_allows_an_enterprise_host(self):
        from unittest import mock
        import os
        with mock.patch.dict(os.environ, {"GH_HOST": "ghe.example.com"}):
            self.assertEqual(writes.parse_origin("git@ghe.example.com:acme/app.git"), "acme/app")
            self.assertEqual(writes.parse_origin("https://github.com/acme/app"), "acme/app")
            self.assertIsNone(writes.parse_origin("git@evil.example:acme/app.git"))

    def test_not_a_remote(self):
        for url in (None, "", "not a url", "/local/path/repo", "https://github.com/onlyowner"):
            with self.subTest(url=url):
                self.assertIsNone(writes.parse_origin(url))


class CheckWriteTests(unittest.TestCase):
    def test_blocked_names_target_and_allow_cmd(self):
        d = check_write(WriteTarget.repo("acme/other"), make_ctx())
        self.assertFalse(d.ok)
        self.assertEqual(d.allow_cmd, "/not-me:allow-repo acme/other")
        self.assertEqual(d.rule, "other_repo")
        self.assertIn("acme/other", d.reason)
        self.assertIn("acme/app-ui", d.reason)

    def test_near_match_blocked(self):
        self.assertFalse(check_write(WriteTarget.repo("acme/app-fork"), make_ctx()).ok)
        self.assertFalse(check_write(WriteTarget.repo("acme/ap"), make_ctx()).ok)

    def test_allowed_is_case_insensitive(self):
        self.assertTrue(check_write(WriteTarget.repo("ACME/App"), make_ctx()).ok)

    def test_rule_off_allows_any_repo(self):
        ctx = make_ctx(rules={"other_repo": False})
        self.assertTrue(check_write(WriteTarget.repo("acme/other"), ctx).ok)

    def test_board_only_the_configured_one(self):
        ctx = make_ctx()
        self.assertTrue(check_write(WriteTarget.board("acme", 6), ctx).ok)
        for target in (WriteTarget.board("acme", 7), WriteTarget.board("evil", 6)):
            d = check_write(target, ctx)
            self.assertFalse(d.ok)
            self.assertIsNone(d.allow_cmd)
        self.assertFalse(check_write(WriteTarget.board("acme", 6), make_ctx(board=None)).ok)

    def test_local_always_ok(self):
        self.assertEqual(check_write(WriteTarget.local("notes.md"), make_ctx(allowed_repos=())), Decision(True))


class FromConfigTests(unittest.TestCase):
    def setUp(self):
        self.tc = TempCase()
        self.addCleanup(self.tc.cleanup)
        self.tc.write("repo", "project", PROJECT_MD)
        self.tc.write("global", "project", GLOBAL_MD)

    def ctx(self, url="git@github.com:acme/app.git"):
        return GuardContext.from_config(self.tc.load(), origin_url=url)

    def test_allowed_set_from_config(self):
        writes.allowlist_add(self.tc.home, "acme/app", "acme/app-ui")  # confirms the repo-layer companion
        ctx = self.ctx()
        self.assertEqual(ctx.origin_repo, "acme/app")
        self.assertEqual(set(ctx.allowed_repos), {"acme/app", "acme/app-ui", "acme/extra"})
        self.assertEqual(ctx.allowed_repos_lower, frozenset(a.lower() for a in ctx.allowed_repos))
        self.assertTrue(check_write(WriteTarget.repo("acme/extra"), ctx).ok)
        self.assertTrue(check_write(WriteTarget.repo("acme/app-ui"), ctx).ok)
        self.assertFalse(check_write(WriteTarget.repo("acme/app-fork"), ctx).ok)
        self.assertEqual(ctx.board, ("acme", 6))
        self.assertEqual(ctx.mode, "live")
        self.assertTrue(all(ctx.rules.values()))
        self.assertIn("main", ctx.protected_branches)

    def test_tracker_auto_resolves_to_origin_and_explicit_tracker_counts(self):
        self.tc.write("repo", "project", "---\nschema: 1\ntracker: acme/tracker\n---\n")
        writes.allowlist_add(self.tc.home, None, "acme/tracker", everywhere=True)
        ctx = self.ctx()
        self.assertIn("acme/tracker", ctx.allowed_repos)
        self.assertIn("acme/app", ctx.allowed_repos)

    def test_unconfirmed_repo_layer_companion_is_blocked_with_a_warning(self):
        ctx = self.ctx()
        self.assertNotIn("acme/app-ui", ctx.allowed_repos)
        d = check_write(WriteTarget.repo("acme/app-ui"), ctx)
        self.assertFalse(d.ok)
        self.assertEqual(d.allow_cmd, "/not-me:allow-repo acme/app-ui")
        self.assertIn("acme/app-ui is listed in .claude/not-me/project.md but not confirmed; "
                      "run /not-me:allow-repo acme/app-ui (setup does this for you)", ctx.warnings)
        self.assertTrue(check_write(WriteTarget.repo("acme/app"), ctx).ok)

    def test_unconfirmed_repo_layer_tracker_is_blocked(self):
        self.tc.write("repo", "project", "---\nschema: 1\ntracker: acme/tracker\n---\n")
        ctx = self.ctx()
        self.assertFalse(check_write(WriteTarget.repo("acme/tracker"), ctx).ok)
        self.assertTrue(any(w.startswith("acme/tracker is listed in") for w in ctx.warnings))

    def test_repo_layer_companion_confirmed_by_global_allow_list(self):
        writes.allowlist_add(self.tc.home, "acme/app", "acme/app-ui")
        ctx = self.ctx()
        self.assertTrue(check_write(WriteTarget.repo("acme/app-ui"), ctx).ok)
        self.assertFalse(any("acme/app-ui is listed" in w for w in ctx.warnings))

    def test_companion_confirmed_by_everywhere_entry_and_global_layer_trusted(self):
        writes.allowlist_add(self.tc.home, None, "acme/app-ui", everywhere=True)
        self.assertTrue(check_write(WriteTarget.repo("acme/app-ui"), self.ctx()).ok)
        self.tc.write("global", "project", "---\nschema: 1\nrepos: [{name: acme/glob}]\n---\n")
        self.tc.repo_dir.joinpath("project.md").unlink()
        ctx = self.ctx()
        self.assertTrue(check_write(WriteTarget.repo("acme/glob"), ctx).ok)
        self.assertFalse(any("not confirmed" in w for w in ctx.warnings))

    def test_global_allow_list_per_project_and_everywhere(self):
        writes.allowlist_add(self.tc.home, "acme/app", "acme/per-project")
        writes.allowlist_add(self.tc.home, None, "acme/everywhere", everywhere=True)
        writes.allowlist_add(self.tc.home, "acme/unrelated", "acme/not-mine")
        ctx = self.ctx()
        self.assertTrue(check_write(WriteTarget.repo("acme/per-project"), ctx).ok)
        self.assertTrue(check_write(WriteTarget.repo("acme/everywhere"), ctx).ok)
        self.assertFalse(check_write(WriteTarget.repo("acme/not-mine"), ctx).ok)

    def test_malformed_allow_list_warns_not_crashes(self):
        self.tc.home.mkdir(parents=True, exist_ok=True)
        (self.tc.home / "allowed-repos.json").write_text("{not json", encoding="utf-8")
        ctx = self.ctx()
        self.assertTrue(any("allowed-repos.json" in w for w in ctx.warnings))
        self.assertFalse(check_write(WriteTarget.repo("acme/other"), ctx).ok)

    def test_wrong_shape_allow_list_warns(self):
        self.tc.home.mkdir(parents=True, exist_ok=True)
        (self.tc.home / "allowed-repos.json").write_text('{"projects": []}', encoding="utf-8")
        self.assertTrue(any("allowed-repos.json" in w for w in self.ctx().warnings))

    def test_missing_allow_list_is_empty_without_warning(self):
        self.assertFalse(any("allowed-repos.json" in w for w in self.ctx().warnings))

    def test_guard_off_is_read(self):
        self.tc.write("global", "project", "---\nschema: 1\nguard: {other_repo: off}\nconfirm_guard_off: [other_repo]\n---\n")
        self.assertFalse(self.ctx().rules["other_repo"])


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="notme-allow-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))

    def test_add_list_remove_roundtrip(self):
        self.assertTrue(writes.allowlist_add(self.home, "acme/app", "acme/other"))
        self.assertFalse(writes.allowlist_add(self.home, "acme/app", "ACME/Other"))
        rows = writes.allowlist_list(self.home, "acme/app")
        self.assertEqual([(r["scope"], r["repo"]) for r in rows], [("acme/app", "acme/other")])
        self.assertTrue(rows[0]["added"].endswith("Z"))
        self.assertTrue(writes.allowlist_remove(self.home, "acme/app", "acme/other"))
        self.assertFalse(writes.allowlist_remove(self.home, "acme/app", "acme/other"))
        self.assertEqual(writes.allowlist_list(self.home), [])

    def test_file_shape_and_no_temp_left(self):
        writes.allowlist_add(self.home, None, "acme/x", everywhere=True)
        data = json.loads((self.home / "allowed-repos.json").read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["projects"]["*"]["allow"][0]["repo"], "acme/x")
        self.assertEqual([p.name for p in self.home.iterdir()], ["allowed-repos.json"])

    def test_list_for_project_includes_everywhere(self):
        writes.allowlist_add(self.home, "acme/app", "acme/a")
        writes.allowlist_add(self.home, None, "acme/b", everywhere=True)
        writes.allowlist_add(self.home, "acme/zzz", "acme/c")
        self.assertEqual({r["repo"] for r in writes.allowlist_list(self.home, "acme/app")}, {"acme/a", "acme/b"})
        self.assertEqual(len(writes.allowlist_list(self.home)), 3)

    def test_rejects_bad_slugs(self):
        for bad in ("https://github.com/acme/other", "acme/*", "*", "acme", "acme/other/extra", "a b/c",
                    "../x", "acme/..", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    writes.allowlist_add(self.home, "acme/app", bad)
        with self.assertRaises(ValueError):
            writes.allowlist_add(self.home, "not a slug", "acme/ok")

    def test_slug_must_start_alphanumeric_and_not_end_in_newline(self):
        for bad in ("-acme/app", "acme/-app", "acme/app\n", "_acme/app", ".acme/app", "acme/app "):
            with self.subTest(bad=bad):
                self.assertFalse(writes.valid_slug(bad))
        for good in ("acme/app", "acme/.github", "a1/b_c.d-e", "Acme/App2"):
            with self.subTest(good=good):
                self.assertTrue(writes.valid_slug(good))

    def test_malformed_file_is_not_overwritten(self):
        (self.home / "allowed-repos.json").write_text("{oops", encoding="utf-8")
        with self.assertRaises(ValueError):
            writes.allowlist_add(self.home, "acme/app", "acme/other")
        self.assertEqual((self.home / "allowed-repos.json").read_text(), "{oops")
        self.assertEqual(writes.allowlist_list(self.home), [])


if __name__ == "__main__":
    unittest.main()
