"""`notme mode show|dry-run|live`, the line-level mode editor, and the hidden operator-token prompt."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from notme import cli, humans, modeswitch
from notme.frontmatter import parse
from tests import TempCase
from tests import test_cli_contract as base
from tests.test_cli_contract import TtyIO
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
    """A person at a terminal (base.CliCase) with a dry-run global layer."""

    global_project = GLOBAL_DRY

    def setUp(self):
        super().setUp()
        humans.clear_live_marker(self.tc.home)  # nobody has run `notme mode live` yet

    @property
    def global_file(self) -> Path:
        return self.tc.home / "project.md"

    @property
    def marker(self) -> Path:
        return humans.live_marker_path(self.tc.home)

    @property
    def token(self) -> str:
        return self.env["NOT_ME_OPERATOR_CONFIRM"]

    def no_env(self):
        return {k: v for k, v in self.env.items() if k != "NOT_ME_OPERATOR_CONFIRM"}


class ModeShowTests(ModeCliCase):
    def test_reports_mode_source_and_token_presence_without_reading_the_token(self):
        with mock.patch.object(humans, "read_token", side_effect=AssertionError("token read")):
            out = self.ok("mode", "show")
        self.assertEqual(out["mode"], "dry-run")
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["source"]["layer"], "global")
        self.assertEqual(out["source"]["line"], 3)
        self.assertEqual(out["layers"]["global"]["mode"], "dry-run")
        self.assertIsNone(out["layers"]["repo"]["mode"])
        self.assertIsNone(out["repo_pin"])
        self.assertFalse(out["live_confirmed"])
        self.assertTrue(out["token_present"])
        self.assertNotIn(self.token, json.dumps(out))
        humans.token_path(self.tc.home).unlink()
        self.assertFalse(self.ok("mode", "show")["token_present"])

    def test_a_hand_set_live_is_not_confirmed(self):
        self.global_file.write_text(base.GLOBAL_PROJECT, encoding="utf-8")  # mode: live, no marker
        out = self.ok("mode", "show")
        self.assertEqual(out["layers"]["global"]["mode"], "live")
        self.assertEqual((out["mode"], out["dry_run"], out["live_confirmed"]), ("dry-run", True, False))
        self.assertIn("not confirmed", out["unconfirmed"])
        self.tc.sign_live()
        out = self.ok("mode", "show")
        self.assertEqual((out["mode"], out["dry_run"], out["live_confirmed"]), ("live", False, True))
        self.assertIsNone(out["unconfirmed"])
        self.assertNotIn(self.marker.read_text(encoding="utf-8").strip(), json.dumps(out))

    def test_launcher_and_live_command_are_absolute_and_runnable(self):
        out = self.ok("mode", "show")
        launcher = Path(out["launcher"])
        self.assertTrue(launcher.is_absolute())
        self.assertTrue(launcher.is_file())
        self.assertEqual(launcher.name, "notme")
        self.assertIn(out["launcher"], out["live_cmd"])
        self.assertTrue(out["live_cmd"].endswith("mode live"))
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", out["live_cmd"])

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
        self.assertFalse(self.marker.exists())
        self.assertEqual(out["repo_pin"]["line"], 3)

    def test_repo_layer_dry_run_keeps_the_marker(self):
        signed = self.tc.sign_live().read_bytes()
        out = self.ok("mode", "dry-run")
        self.assertEqual(self.marker.read_bytes(), signed)
        self.assertTrue(out["live_confirmed"])
        self.assertEqual(out["mode"], "dry-run")  # the repo pin still wins here

    def test_creates_the_repo_layer_file_when_missing(self):
        self.ok("mode", "dry-run")
        meta, _ = parse((self.tc.repo_dir / "project.md").read_text(encoding="utf-8"))
        self.assertEqual(meta["mode"], "dry-run")

    def test_global_changes_only_the_mode_line(self):
        self.tc.sign_live()
        out = self.ok("mode", "dry-run", "--global")
        self.assertFalse(self.marker.exists())  # going back removes the signature too
        self.assertFalse(out["live_confirmed"])
        self.assertEqual(out["written"]["layer"], "global")
        self.assertEqual(self.global_file.read_text(encoding="utf-8"),
                         base.GLOBAL_PROJECT.replace("mode: live", "mode: dry-run # dry-run | live"))
        self.assertFalse((self.tc.repo_dir / "project.md").exists())
        self.assertEqual(out["mode"], "dry-run")

    def test_global_quoted_mode_key_is_rewritten(self):
        self.global_file.write_text(GLOBAL_DRY.replace("mode: dry-run", '"mode": live'), encoding="utf-8")
        self.tc.sign_live()
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


class ModeLiveTests(ModeCliCase):
    def test_via_the_environment_variable(self):
        repo = self.tc.write("repo", "project", REPO_LAYER)
        out = self.ok("mode", "live")
        self.assertEqual(out["written"], {"layer": "global", "file": str(self.global_file.resolve()), "mode": "live"})
        self.assertEqual(self.global_file.read_text(encoding="utf-8"),
                         GLOBAL_DRY.replace("mode: dry-run   #", "mode: live      #"))
        self.assertEqual(repo.read_text(encoding="utf-8"), REPO_LAYER)  # never the repo layer
        self.assertEqual(out["mode"], "live")
        self.assertFalse(out["dry_run"])
        self.assertTrue(out["live_confirmed"])
        self.assertTrue(humans.live_confirmed(self.tc.home))
        self.assertNotIn(self.token, self.marker.read_text(encoding="utf-8"))
        self.assertNotIn("note", out)

    def test_via_the_hidden_prompt(self):
        asked = []
        code, out, err = self.cli("mode", "live", env=self.no_env(), prompt=lambda: asked.append(1) or self.token)
        self.assertEqual(code, 0, out)
        self.assertEqual(asked, [1])
        self.assertEqual(out["mode"], "live")
        self.assertTrue(humans.live_confirmed(self.tc.home))
        self.assertNotIn(self.token, json.dumps(out) + err)

    def test_wrong_or_empty_prompt_answer_is_refused(self):
        for answer in ("wrong", "", None, self.token + "x"):
            with self.subTest(answer=answer):
                code, out, _ = self.cli("mode", "live", env=self.no_env(), prompt=lambda: answer)
                self.assertEqual((code, out["kind"]), (1, "human_only"))
        self.assertEqual(self.global_file.read_text(encoding="utf-8"), GLOBAL_DRY)
        self.assertFalse(self.marker.exists())

    def test_a_set_variable_is_not_overridden_by_the_prompt(self):
        env = dict(self.env, NOT_ME_OPERATOR_CONFIRM="wrong")
        code, out, _ = self.cli("mode", "live", env=env, prompt=lambda: self.fail("prompted"))
        self.assertEqual((code, out["kind"]), (1, "human_only"))

    def test_without_any_token_is_refused(self):
        code, out, _ = self.cli("mode", "live", env=self.no_env())
        self.assertEqual((code, out["kind"]), (1, "human_only"))
        humans.token_path(self.tc.home).unlink()
        code, out, _ = self.cli("mode", "live", env=self.no_env(), prompt=lambda: self.fail("prompted"))
        self.assertEqual((code, out["kind"]), (1, "human_only"))
        self.assertEqual(self.global_file.read_text(encoding="utf-8"), GLOBAL_DRY)

    def test_running_it_again_after_a_token_rotation_re_signs(self):
        self.ok("mode", "live")
        humans.token_path(self.tc.home).unlink()
        new = humans.create_token(self.tc.home)
        self.assertEqual(self.ok("mode", "show")["mode"], "dry-run")
        out = self.ok("mode", "live", env=dict(self.env, NOT_ME_OPERATOR_CONFIRM=new))
        self.assertEqual((out["mode"], out["live_confirmed"]), ("live", True))

    def test_a_marker_saved_while_live_is_dead_after_dry_run_global(self):
        self.ok("mode", "live")
        saved = self.marker.read_bytes()
        self.ok("mode", "dry-run", "--global")
        self.global_file.write_text(base.GLOBAL_PROJECT, encoding="utf-8")  # mode: live again, by hand
        self.marker.write_bytes(saved)  # and the old signature put back
        out = self.ok("mode", "show")
        self.assertEqual((out["mode"], out["live_confirmed"]), ("dry-run", False))
        self.assertEqual(self.ok("mode", "live")["live_confirmed"], True)  # a person can still go live
        self.assertNotEqual(self.marker.read_bytes(), saved)

    def test_reports_a_repo_layer_dry_run_pin(self):
        self.tc.write("repo", "project", "---\nschema: 1\nmode: dry-run\n---\n")
        out = self.ok("mode", "live")
        self.assertIn("mode: live", self.global_file.read_text(encoding="utf-8"))
        self.assertEqual(out["mode"], "dry-run")
        self.assertEqual(out["repo_pin"]["line"], 3)
        self.assertIn(":3", out["note"])


class PromptForOtherPersonCommandsTests(ModeCliCase):
    def test_pause_off_and_allow_repo_add_take_the_token_at_the_prompt(self):
        self.ok("pause", "on")
        code, out, _ = self.cli("pause", "off", env=self.no_env(), prompt=lambda: self.token)
        self.assertEqual(code, 0, out)
        self.assertFalse(out["paused"])
        code, out, _ = self.cli("allow-repo", "add", "acme/extra", "--everywhere", env=self.no_env(),
                                prompt=lambda: self.token)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["added"])
        code, out, _ = self.cli("allow-repo", "add", "acme/more", "--everywhere", env=self.no_env(),
                                prompt=lambda: "nope")
        self.assertEqual((code, out["kind"]), (1, "human_only"))


class ShellCommandTests(unittest.TestCase):
    def test_posix_form(self):
        with mock.patch.object(cli.os, "name", "posix"):
            self.assertEqual(cli.shell_name(), "posix")
            self.assertEqual(cli.shell_command("/opt/p/bin/notme", ["mode", "live"], "/w/my app"),
                             "cd '/w/my app' && /opt/p/bin/notme mode live")

    def test_windows_form_runs_the_launcher_through_git_bash(self):
        with mock.patch.object(cli.os, "name", "nt"):
            self.assertEqual(cli.shell_name(), "git-bash")
            cmd = cli.shell_command("C:\\Users\\op\\plugin\\bin\\notme", ["mode", "live"], "C:\\work\\app")
        self.assertEqual(cmd, "cd 'C:\\work\\app' && bash 'C:\\Users\\op\\plugin\\bin\\notme' mode live")


class ShellFieldTests(ModeCliCase):
    def test_mode_show_and_the_refusal_name_the_shell(self):
        self.assertEqual(self.ok("mode", "show")["shell"], cli.shell_name())
        with mock.patch.object(cli, "shell_name", return_value="git-bash"):
            self.assertEqual(self.ok("mode", "show")["shell"], "git-bash")
            code, out, _ = self.cli("mode", "live", env=self.no_env())
        self.assertEqual((code, out["kind"], out["shell"]), (1, "human_only", "git-bash"))


class ModeLiveUnderClaudeTests(UnderClaudeCase):
    def setUp(self):
        super().setUp()
        self.global_file = self.home / "project.md"
        self.global_file.write_text(GLOBAL_DRY, encoding="utf-8")
        humans.clear_live_marker(self.home)

    def live(self, env, stdin=None, prompt=None):
        code, payload, _ = self.cli("mode", "live", env=env, stdin=stdin, prompt=prompt)
        return code, payload

    def assert_refused(self, code, payload):
        self.assertEqual((code, payload["kind"]), (1, "human_only"))
        self.assertEqual(self.global_file.read_text(encoding="utf-8"), GLOBAL_DRY)
        self.assertFalse(humans.live_confirmed(self.home))

    def test_refused_under_claude_code_without_prompting(self):
        code, payload = self.live(self.env, stdin=TtyIO(), prompt=lambda: self.fail("prompted"))
        self.assert_refused(code, payload)
        cmd = payload["terminal_cmd"]
        self.assertTrue(cmd.startswith("cd "), cmd)
        self.assertIn(cli.launcher_path(), cmd)
        self.assertTrue(cmd.endswith("mode live"), cmd)
        for stale in ("$(cat", "NOT_ME_OPERATOR_CONFIRM", "CLAUDE_PLUGIN_ROOT"):
            self.assertNotIn(stale, cmd)
        self.assertNotIn("NOT_ME_OPERATOR_CONFIRM", payload["error"])

    def test_refused_from_a_non_terminal(self):
        self.assert_refused(*self.live(self.human_env, prompt=lambda: self.fail("prompted")))

    def test_refused_under_a_claude_code_ancestor(self):
        self.claude_ancestor = True
        self.assert_refused(*self.live(self.human_env, stdin=TtyIO(), prompt=lambda: self.fail("prompted")))

    def test_refused_without_or_with_a_wrong_token(self):
        no_token = {k: v for k, v in self.human_env.items() if k != "NOT_ME_OPERATOR_CONFIRM"}
        self.assert_refused(*self.live(no_token, stdin=TtyIO()))
        self.assert_refused(*self.live(dict(self.human_env, NOT_ME_OPERATOR_CONFIRM="octocat"), stdin=TtyIO()))

    def test_dry_run_needs_no_person(self):
        self.global_file.write_text(base.GLOBAL_PROJECT, encoding="utf-8")  # live
        humans.write_live_marker(self.home)
        payload = self.ok("mode", "dry-run", "--global")
        self.assertEqual(payload["mode"], "dry-run")
        self.assertFalse(humans.live_marker_path(self.home).exists())
        self.assertEqual(self.global_file.read_text(encoding="utf-8"),
                         base.GLOBAL_PROJECT.replace("mode: live", "mode: dry-run"))
        payload = self.ok("mode", "dry-run")
        self.assertTrue((self.session / ".claude" / "not-me" / "project.md").is_file())

    def test_a_person_succeeds(self):
        code, payload = self.live(self.human_env, stdin=TtyIO())
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["mode"], "live")
        self.assertTrue(humans.live_confirmed(self.home))


if __name__ == "__main__":
    unittest.main()
