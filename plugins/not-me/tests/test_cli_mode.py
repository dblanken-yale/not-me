"""`notme mode show|dry-run|live` and the line-level mode editor."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from notme import cli, modeswitch
from notme.frontmatter import parse
from tests import TempCase
from tests import test_cli_contract as base
from tests.test_cli_security import UnderClaudeCase

GLOBAL_DRY = base.GLOBAL_PROJECT.replace("mode: live", "mode: dry-run   # dry-run | live")
REPO_LAYER = "---\nschema: 1\n# team settings\ntracker: acme/app   # issues live here\n---\n# Project\n\nBody text.\n"


class SetModeTextTests(unittest.TestCase):
    def test_replaces_only_the_value_and_keeps_the_comment_column(self):
        text = "---\nschema: 1\nmode: dry-run   # dry-run | live\nenabled: true\n---\n# Body\n"
        out = modeswitch.set_mode_text(text, "live")
        self.assertEqual(out, "---\nschema: 1\nmode: live      # dry-run | live\nenabled: true\n---\n# Body\n")
        self.assertEqual(modeswitch.set_mode_text(out, "dry-run"), text)

    def test_quoted_values_and_no_comment(self):
        self.assertEqual(modeswitch.set_mode_text('---\nmode: "live"\n---\n', "dry-run"), "---\nmode: dry-run\n---\n")

    def test_inserts_after_schema_when_missing(self):
        text = "---\nschema: 1\ntracker: acme/app\n---\nbody\n"
        self.assertEqual(modeswitch.set_mode_text(text, "dry-run"),
                         "---\nschema: 1\nmode: dry-run\ntracker: acme/app\n---\nbody\n")

    def test_inserts_first_without_schema(self):
        self.assertEqual(modeswitch.set_mode_text("---\ntracker: x\n---\n", "live"), "---\nmode: live\ntracker: x\n---\n")

    def test_a_nested_mode_key_is_not_the_top_level_one(self):
        text = "---\nschema: 1\nother:\n  mode: live\n---\n"
        out = modeswitch.set_mode_text(text, "dry-run")
        self.assertEqual(out, "---\nschema: 1\nmode: dry-run\nother:\n  mode: live\n---\n")

    def test_empty_file_gets_minimal_frontmatter(self):
        out = modeswitch.set_mode_text("", "dry-run")
        self.assertEqual(parse(out)[0], {"schema": 1, "mode": "dry-run"})

    def test_text_without_frontmatter_is_refused(self):
        with self.assertRaises(modeswitch.ModeEditError):
            modeswitch.set_mode_text("# Project\n\nhello\n", "dry-run")

    def test_quoted_keys_are_the_mode_key(self):
        for key in ('"mode"', "'mode'"):
            with self.subTest(key=key):
                out = modeswitch.set_mode_text(f"---\nschema: 1\n{key}: live  # c\n---\n", "dry-run")
                self.assertEqual(out, f"---\nschema: 1\n{key}: dry-run # c\n---\n")
                self.assertEqual(parse(out)[0]["mode"], "dry-run")

    def test_crlf_and_bom_survive(self):
        text = "﻿---\r\nschema: 1\r\nmode: live\r\n---\r\nbody\r\n"
        self.assertEqual(modeswitch.set_mode_text(text, "dry-run"), "﻿---\r\nschema: 1\r\nmode: dry-run\r\n---\r\nbody\r\n")
        inserted = modeswitch.set_mode_text("---\r\nschema: 1\r\n---\r\n", "live")
        self.assertEqual(inserted, "---\r\nschema: 1\r\nmode: live\r\n---\r\n")

    def test_unclosed_frontmatter_is_refused(self):
        with self.assertRaises(modeswitch.ModeEditError):
            modeswitch.set_mode_text("---\nmode: live\n", "dry-run")


class WriteModeTests(unittest.TestCase):
    def setUp(self):
        self.tc = TempCase()
        self.addCleanup(self.tc.cleanup)

    def test_bytes_outside_the_mode_line_are_untouched(self):
        path = self.tc.root / "project.md"
        path.write_bytes(REPO_LAYER.encode("utf-8"))
        modeswitch.write_mode(path, "dry-run")
        self.assertEqual(path.read_bytes().decode("utf-8"), REPO_LAYER.replace("schema: 1\n", "schema: 1\nmode: dry-run\n"))

    def test_unparseable_frontmatter_is_refused_and_left_alone(self):
        path = self.tc.root / "project.md"
        bad = "---\nmode: live\nlist: [unclosed\n---\n"
        path.write_text(bad, encoding="utf-8")
        with self.assertRaises(modeswitch.ModeEditError):
            modeswitch.write_mode(path, "dry-run")
        self.assertEqual(path.read_text(encoding="utf-8"), bad)

    @unittest.skipUnless(os.name == "posix", "symlinks")
    def test_a_symlinked_file_stays_a_symlink(self):
        real = self.tc.root / "dotfiles" / "project.md"
        real.parent.mkdir()
        real.write_text("---\nschema: 1\nmode: live\n---\n", encoding="utf-8")
        link = self.tc.root / "project.md"
        link.symlink_to(real)
        modeswitch.write_mode(link, "dry-run", follow_links=True)
        self.assertTrue(link.is_symlink())
        self.assertIn("mode: dry-run", real.read_text(encoding="utf-8"))

    def assert_refused(self, path: Path, follow_links: bool, untouched: Path):
        before = untouched.read_bytes()
        with self.assertRaises(modeswitch.ModeEditError):
            modeswitch.write_mode(path, "dry-run", follow_links=follow_links)
        self.assertEqual(untouched.read_bytes(), before)

    def victim(self, name: str = "repo_guard.py", text: str = "import sys\nprint('hook')\n") -> Path:
        path = self.tc.root / "plugin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @unittest.skipUnless(os.name == "posix", "symlinks")
    def test_repo_layer_never_writes_through_a_symlink(self):
        for text in ("import sys\n", "---\nschema: 1\n---\n"):  # with or without frontmatter
            with self.subTest(text=text):
                victim = self.victim("project.md", text)
                layer = self.tc.root / "repo" / ".claude" / "not-me"
                layer.mkdir(parents=True, exist_ok=True)
                link = layer / "project.md"
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(victim)
                self.assert_refused(link, False, victim)

    @unittest.skipUnless(os.name == "posix", "symlinks")
    def test_repo_layer_refuses_a_symlinked_directory(self):
        victim = self.victim("project.md", "---\nschema: 1\n---\n")
        claude = self.tc.root / "repo2" / ".claude"
        claude.mkdir(parents=True)
        (claude / "not-me").symlink_to(victim.parent)
        self.assert_refused(claude / "not-me" / "project.md", False, victim)

    def test_a_hard_linked_file_is_refused(self):
        victim = self.victim("SKILL.md", "---\nname: x\n---\nbody\n")
        link = self.tc.root / "project.md"
        try:
            os.link(str(victim), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("hard links unavailable")
        for follow in (False, True):
            with self.subTest(follow_links=follow):
                self.assert_refused(link, follow, victim)

    def test_a_directory_is_refused(self):
        (self.tc.root / "project.md").mkdir()
        with self.assertRaises(modeswitch.ModeEditError):
            modeswitch.write_mode(self.tc.root / "project.md", "dry-run")

    @unittest.skipUnless(os.name == "posix", "symlinks")
    def test_a_global_symlink_must_lead_to_a_regular_project_md(self):
        victim = self.victim()
        link = self.tc.root / "home" / "project.md"
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(victim)
        self.assert_refused(link, True, victim)
        dangling = self.tc.root / "home2" / "project.md"
        dangling.parent.mkdir()
        dangling.symlink_to(self.tc.root / "nowhere" / "project.md")
        with self.assertRaises(modeswitch.ModeEditError):
            modeswitch.write_mode(dangling, "dry-run", follow_links=True)

    def test_text_without_frontmatter_is_left_alone(self):
        path = self.victim("project.md")
        self.assert_refused(path, False, path)

    def test_an_empty_file_gets_fresh_frontmatter(self):
        path = self.tc.root / "project.md"
        path.write_text("", encoding="utf-8")
        modeswitch.write_mode(path, "dry-run")
        self.assertEqual(parse(path.read_text(encoding="utf-8"))[0]["mode"], "dry-run")


class ModeCliCase(base.CliCase):
    """A project with a dry-run global layer."""

    global_project = GLOBAL_DRY

    @property
    def global_file(self) -> Path:
        return self.tc.home / "project.md"

    def settings_ledger(self):
        from notme.dryrun import Ledger
        return Ledger(self.tc.home / cli.SETTINGS_LEDGER).read()


class ModeShowTests(ModeCliCase):
    def test_reports_mode_and_source(self):
        out = self.ok("mode", "show")
        self.assertEqual(out["mode"], "dry-run")
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["source"]["layer"], "global")
        self.assertEqual(out["source"]["line"], 3)
        self.assertEqual(out["layers"]["global"]["mode"], "dry-run")
        self.assertIsNone(out["layers"]["repo"]["mode"])
        self.assertIsNone(out["repo_pin"])
        for gone in ("live_confirmed", "unconfirmed", "token_present", "launcher", "shell", "live_cmd"):
            self.assertNotIn(gone, out)

    def test_a_global_live_is_enough(self):
        self.global_file.write_text(base.GLOBAL_PROJECT, encoding="utf-8")  # mode: live, set by hand
        out = self.ok("mode", "show")
        self.assertEqual((out["mode"], out["dry_run"]), ("live", False))
        self.assertNotIn("downgraded", out["source"])

    def test_reports_a_repo_layer_dry_run_pin(self):
        self.tc.write("repo", "project", "---\nschema: 1\nmode: dry-run\n---\n")
        out = self.ok("mode", "show")
        self.assertEqual(out["repo_pin"]["line"], 3)
        self.assertTrue(out["repo_pin"]["file"].endswith(os.path.join(".claude", "not-me", "project.md")))
        self.assertEqual(out["source"]["layer"], "repo")

    def test_repo_layer_live_is_never_honoured(self):
        self.tc.write("repo", "project", "---\nschema: 1\nmode: live\n---\n")
        out = self.ok("mode", "show")
        self.assertEqual(out["mode"], "dry-run")
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["source"]["layer"], "global")
        self.assertEqual(out["source"]["ignored"][0]["layer"], "repo")


class ModeDryRunTests(ModeCliCase):
    global_project = base.GLOBAL_PROJECT.replace("mode: live", "mode: live   # dry-run | live")

    def test_repo_layer_by_default_changing_only_the_mode_line(self):
        path = self.tc.write("repo", "project", REPO_LAYER)
        before_global = self.global_file.read_bytes()
        out = self.ok("mode", "dry-run")
        self.assertEqual(out["written"]["layer"], "repo")
        self.assertEqual(path.read_text(encoding="utf-8"),
                         REPO_LAYER.replace("schema: 1\n", "schema: 1\nmode: dry-run\n"))
        self.assertEqual(self.global_file.read_bytes(), before_global)
        self.assertEqual(out["mode"], "dry-run")
        self.assertEqual(out["repo_pin"]["line"], 3)
        self.assertEqual(out["summary"], "mode set to dry-run in the repo layer (this project only)")
        self.assertEqual([x["op"] for x in self.settings_ledger()], ["mode dry-run"])

    def test_creates_the_repo_layer_file_when_missing(self):
        self.ok("mode", "dry-run")
        meta, _ = parse((self.tc.repo_dir / "project.md").read_text(encoding="utf-8"))
        self.assertEqual(meta["mode"], "dry-run")

    def test_global_changes_only_the_mode_line(self):
        out = self.ok("mode", "dry-run", "--global")
        self.assertEqual(out["written"]["layer"], "global")
        self.assertEqual(out["summary"], "mode set to dry-run in the global layer (every project)")
        self.assertEqual(self.global_file.read_text(encoding="utf-8"),
                         base.GLOBAL_PROJECT.replace("mode: live", "mode: dry-run # dry-run | live"))
        self.assertFalse((self.tc.repo_dir / "project.md").exists())
        self.assertEqual(out["mode"], "dry-run")

    def test_global_quoted_mode_key_is_rewritten(self):
        self.global_file.write_text(GLOBAL_DRY.replace("mode: dry-run", '"mode": live'), encoding="utf-8")
        self.assertEqual(self.ok("mode", "show")["mode"], "live")
        out = self.ok("mode", "dry-run", "--global")
        self.assertEqual(out["mode"], "dry-run")
        self.assertEqual(self.global_file.read_text(encoding="utf-8"),
                         GLOBAL_DRY.replace("mode: dry-run   #", '"mode": dry-run #'))

    @unittest.skipUnless(os.name == "posix", "symlinks")
    def test_a_repo_layer_symlink_is_refused(self):
        victim = self.tc.root / "victim.py"
        victim.write_text("print('hook')\n", encoding="utf-8")
        self.tc.repo_dir.mkdir(parents=True)
        (self.tc.repo_dir / "project.md").symlink_to(victim)
        code, out, _ = self.cli("mode", "dry-run")
        self.assertEqual((code, out["kind"]), (1, "mode_edit_refused"))
        self.assertEqual(victim.read_text(encoding="utf-8"), "print('hook')\n")
        self.assertEqual(self.settings_ledger(), [])


class ModeLiveTests(ModeCliCase):
    def test_sets_the_global_layer_only_and_says_so(self):
        repo = self.tc.write("repo", "project", REPO_LAYER)
        out = self.ok("mode", "live")
        self.assertEqual(out["written"], {"layer": "global", "file": str(self.global_file.resolve()), "mode": "live"})
        self.assertEqual(self.global_file.read_text(encoding="utf-8"),
                         GLOBAL_DRY.replace("mode: dry-run   #", "mode: live      #"))
        self.assertEqual(repo.read_text(encoding="utf-8"), REPO_LAYER)  # never the repo layer
        self.assertEqual((out["mode"], out["dry_run"]), ("live", False))
        self.assertEqual(out["summary"], "mode set to live in the global layer (every project)")
        self.assertNotIn("note", out)
        line = self.settings_ledger()[-1]
        self.assertEqual((line["op"], line["target"]), ("mode live", "global layer (every project)"))

    def test_back_to_dry_run_and_live_again(self):
        self.ok("mode", "live")
        self.assertEqual(self.ok("mode", "dry-run", "--global")["mode"], "dry-run")
        self.assertEqual(self.ok("mode", "live")["mode"], "live")
        self.assertEqual([x["op"] for x in self.settings_ledger()], ["mode live", "mode dry-run", "mode live"])

    def test_reports_a_repo_layer_dry_run_pin(self):
        self.tc.write("repo", "project", "---\nschema: 1\nmode: dry-run\n---\n")
        out = self.ok("mode", "live")
        self.assertIn("mode: live", self.global_file.read_text(encoding="utf-8"))
        self.assertEqual(out["mode"], "dry-run")
        self.assertEqual(out["repo_pin"]["line"], 3)
        self.assertIn(":3", out["note"])


class ModeLiveUnderClaudeTests(UnderClaudeCase):
    """The operator's /not-me:mode live runs the CLI inside the session (dblanken-yale/not-me#118)."""

    def setUp(self):
        super().setUp()
        self.global_file = self.home / "project.md"
        self.global_file.write_text(GLOBAL_DRY, encoding="utf-8")

    def test_runs_under_claude_code_without_a_terminal(self):
        payload = self.ok("mode", "live")
        self.assertEqual((payload["mode"], payload["dry_run"]), ("live", False))
        self.assertIn("mode: live", self.global_file.read_text(encoding="utf-8"))

    def test_dry_run_too(self):
        self.global_file.write_text(base.GLOBAL_PROJECT, encoding="utf-8")  # live
        payload = self.ok("mode", "dry-run", "--global")
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(self.global_file.read_text(encoding="utf-8"),
                         base.GLOBAL_PROJECT.replace("mode: live", "mode: dry-run"))
        self.ok("mode", "dry-run")
        self.assertTrue((self.session / ".claude" / "not-me" / "project.md").is_file())


if __name__ == "__main__":
    unittest.main()
