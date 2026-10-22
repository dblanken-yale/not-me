"""Attacks a prompt-injected model could try through the notme command line, and the CLI's refusals.

The model controls argv, env prefixes, the working directory and any file it writes. Under Claude Code (CLAUDECODE
or CLAUDE_PROJECT_DIR set) none of those may move the trust anchor: the session project, the global layer, the
log root, the allow list or the pause switch.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from notme import claims, cli, humans, runlog, states
from notme.redact import redact
from tests import PLUGIN_ROOT, TempCase
from tests import test_cli_contract as base
from tests.fakes.fakegh import FakeRunner
from tests.test_cli_contract import REPO, add_pr_state


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env, check=True).stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "init")
    return path


from tests.test_cli_contract import TtyIO  # noqa: E402


class UnderClaudeCase(unittest.TestCase):
    """A session project and a second checkout, a default global layer under a scratch HOME, CLAUDECODE=1."""

    def setUp(self):
        self.tc = TempCase()
        self.addCleanup(self.tc.cleanup)
        self.session = init_repo(self.tc.root / "session")
        self.other = init_repo(self.tc.root / "other")
        user = self.tc.root / "user"
        patcher = mock.patch.dict(os.environ, {"HOME": str(user), "USERPROFILE": str(user)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = user / ".claude" / "not-me"
        self.home.mkdir(parents=True)
        (self.home / "project.md").write_text(base.GLOBAL_PROJECT, encoding="utf-8")
        self.fake = FakeRunner()
        self.env = {"CLAUDECODE": "1", "PATH": os.environ.get("PATH", ""), "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)}
        self.token = humans.create_token(self.home)
        humans.write_live_marker(self.home)  # the global layer says live; signed as `notme mode live` would
        # a person: no Claude Code variable, the operator token, and (with stdin=TtyIO()) a terminal
        self.human_env = {k: v for k, v in self.env.items() if k not in cli.CLAUDE_ENV}
        self.human_env["NOT_ME_OPERATOR_CONFIRM"] = self.token
        self.claude_ancestor = False  # the injected process-tree check

    def cli(self, *argv, env=None, cwd=None, stdin=None, repo=True, prompt=None):
        out, err = io.StringIO(), io.StringIO()
        args = (["--repo", REPO] if repo else []) + list(argv)
        code = cli.main(args, runner=self.fake, env=env or self.env, stdin=stdin or io.StringIO(""), stdout=out,
                        stderr=err, cwd=str(cwd or self.session), process_check=lambda: self.claude_ancestor,
                        prompt=prompt)
        return code, json.loads(out.getvalue()), err.getvalue()

    def ok(self, *argv, **kw):
        code, payload, err = self.cli(*argv, **kw)
        self.assertEqual(code, 0, f"{argv}: {payload} {err}")
        return payload

    def write(self, where: Path, name: str, text: str) -> str:
        path = where / name
        path.write_text(text, encoding="utf-8")
        return str(path)


class TrustAnchorTests(UnderClaudeCase):
    def test_project_dir_to_another_checkout_is_refused(self):
        code, payload, _ = self.cli("--project-dir", str(self.other), "request-review", "3", "--reviewers", "hubot")
        self.assertEqual((code, payload["kind"]), (1, "project_override_refused"))
        self.assertEqual(self.fake.calls, [])

    def test_claude_project_dir_prefix_to_another_checkout_is_refused(self):
        env = dict(self.env, CLAUDE_PROJECT_DIR=str(self.other))
        code, payload, _ = self.cli("me", env=env)
        self.assertEqual((code, payload["kind"]), (1, "project_override_refused"))

    def test_cd_into_another_repo_while_claude_project_dir_names_the_session_is_refused(self):
        env = dict(self.env, CLAUDE_PROJECT_DIR=str(self.session))
        code, payload, _ = self.cli("status", env=env, cwd=self.other)
        self.assertEqual((code, payload["kind"]), (1, "project_override_refused"))

    def test_override_from_outside_any_checkout_is_refused(self):
        outside = self.tc.root / "plain"
        outside.mkdir()
        code, payload, _ = self.cli("--project-dir", str(self.session), "me", cwd=outside)
        self.assertEqual((code, payload["kind"]), (1, "project_override_refused"))

    def test_a_linked_worktree_of_the_session_project_is_allowed(self):
        wt = self.tc.root / "wt"
        git(self.session, "worktree", "add", "-q", "--detach", str(wt))
        self.assertEqual(self.ok("--project-dir", str(wt), "git", "status")["branch"], None)
        env = dict(self.env, CLAUDE_PROJECT_DIR=str(self.session))
        self.assertEqual(self.ok("git", "status", env=env, cwd=wt)["branch"], "main")

    def test_input_file_in_another_checkout_is_refused(self):
        body = self.write(self.other, "body.md", "planted\n")
        code, payload, _ = self.cli("comment-upsert", "3", "--marker", "status", "--body-file", body)
        self.assertEqual((code, payload["kind"]), (1, "unsafe_path"))
        self.assertEqual(self.fake.writes_made(), [])

    def test_overrides_are_honoured_only_for_a_person_at_a_terminal(self):
        env = {k: v for k, v in self.env.items() if k != "CLAUDECODE"}
        self.assertEqual(self.ok("--project-dir", str(self.other), "git", "status", env=env,
                                 stdin=TtyIO())["branch"], "main")
        code, payload, _ = self.cli("--project-dir", str(self.other), "git", "status", stdin=TtyIO())
        self.assertEqual((code, payload["kind"]), (1, "project_override_refused"))  # tty, but CLAUDECODE set


class EnvironmentTests(UnderClaudeCase):
    def test_not_me_home_is_ignored_with_a_warning(self):
        evil = self.tc.root / "evil"
        evil.mkdir()
        (evil / "project.md").write_text(base.GLOBAL_PROJECT.replace("operator: octocat", "operator: mallory"),
                                         encoding="utf-8")
        payload = self.ok("me", env=dict(self.env, NOT_ME_HOME=str(evil)))
        self.assertEqual(payload["operator"], "octocat")
        self.assertTrue(any("NOT_ME_HOME" in w for w in payload["warnings"]))

    def test_not_me_home_is_honoured_only_for_a_person_at_a_terminal(self):
        evil = self.tc.root / "evil"
        evil.mkdir()
        (evil / "project.md").write_text(base.GLOBAL_PROJECT.replace("operator: octocat", "operator: mallory"),
                                         encoding="utf-8")
        env = {"PATH": self.env["PATH"], "NOT_ME_HOME": str(evil)}
        self.assertEqual(self.ok("me", env=env, stdin=TtyIO())["operator"], "mallory")
        self.assertEqual(self.ok("me", env=env)["operator"], "octocat")  # no terminal: ignored

    def test_not_me_dry_run_cannot_turn_dry_run_off(self):
        (self.home / "project.md").write_text(base.GLOBAL_PROJECT.replace("mode: live", "mode: dry-run"),
                                              encoding="utf-8")
        add_pr_state(self.fake)
        for value in ("0", "false", ""):
            with self.subTest(value=value):
                payload = self.ok("request-review", "3", "--reviewers", "hubot",
                                  env=dict(self.env, NOT_ME_DRY_RUN=value))
                self.assertTrue(payload["dry_run"])
        self.assertEqual(self.fake.writes_made(), [])

    def test_not_me_log_root_cannot_redirect_the_run_log(self):
        evil = self.tc.root / "evil-logs"
        payload = self.ok("log", "start", "--skill", "new", "--ref", "1",
                          env=dict(self.env, NOT_ME_LOG_ROOT=str(evil)))
        self.assertFalse(evil.exists())
        self.assertIn(os.path.realpath(str(self.session / ".git" / "not-me" / "logs")),
                      os.path.realpath(payload["path"]))


class HumanOnlyTests(UnderClaudeCase):
    def entries(self):
        return self.ok("allow-repo", "list")["entries"]

    def test_allow_repo_add_without_confirmation_is_refused(self):
        code, payload, _ = self.cli("allow-repo", "add", "evil/repo", "--everywhere")
        self.assertEqual((code, payload["kind"]), (1, "human_only"))
        self.assertIn("allow-repo add evil/repo --everywhere", payload["terminal_cmd"])
        self.assertEqual(self.entries(), [])

    def test_allow_repo_confirmation_needs_a_terminal(self):
        code, payload, _ = self.cli("allow-repo", "add", "evil/repo", "--everywhere", env=self.human_env)
        self.assertEqual((code, payload["kind"]), (1, "human_only"))
        self.assertEqual(self.entries(), [])

    def test_refusals_say_the_guard_is_the_enforcement(self):
        code, payload, _ = self.cli("allow-repo", "add", "evil/repo")
        self.assertEqual(payload["note"], humans.SECOND_LAYER)
        self.assertIn("the PreToolUse guard is the enforcement; this check is a second layer", payload["error"])

    def test_confirmation_must_be_the_operator_token_not_the_gh_login(self):
        for wrong in ("octocat", "mallory", self.token + "x", ""):
            with self.subTest(wrong=wrong):
                env = dict(self.human_env, NOT_ME_OPERATOR_CONFIRM=wrong)
                code, payload, _ = self.cli("allow-repo", "add", "evil/repo", "--everywhere", env=env, stdin=TtyIO())
                self.assertEqual((code, payload["kind"]), (1, "human_only"))
        self.assertEqual(self.entries(), [])
        self.assertTrue(self.ok("allow-repo", "add", "acme/extra", "--everywhere", env=self.human_env,
                                stdin=TtyIO())["added"])
        self.assertEqual([e["repo"] for e in self.entries()], ["acme/extra"])

    def test_a_token_readable_by_others_does_not_count(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions")
        humans.token_path(self.home).chmod(0o644)
        code, payload, _ = self.cli("allow-repo", "add", "acme/extra", "--everywhere", env=self.human_env,
                                    stdin=TtyIO())
        self.assertEqual((code, payload["kind"]), (1, "human_only"))

    def test_pty_spoof_under_a_claude_ancestor_is_refused(self):
        """A pty wrapper makes stdin a tty and the token may have leaked; the Claude Code ancestor still refuses."""
        self.claude_ancestor = True
        code, payload, _ = self.cli("allow-repo", "add", "evil/repo", "--everywhere", env=self.human_env,
                                    stdin=TtyIO())
        self.assertEqual((code, payload["kind"]), (1, "human_only"))
        self.ok("pause", "on")
        code, payload, _ = self.cli("pause", "off", env=self.human_env, stdin=TtyIO())
        self.assertEqual((code, payload["kind"]), (1, "human_only"))
        code, payload, _ = self.cli("--project-dir", str(self.other), "git", "status", env=self.human_env,
                                    stdin=TtyIO())
        self.assertEqual((code, payload["kind"]), (1, "project_override_refused"))
        code, payload, _ = self.cli("doctor", "--init-token", env=self.human_env, stdin=TtyIO())
        self.assertEqual((code, payload["kind"]), (1, "human_only"))
        self.assertEqual(self.entries(), [])

    def test_init_token_is_for_a_person_and_never_replaces_a_token(self):
        humans.token_path(self.home).unlink()
        code, payload, _ = self.cli("doctor", "--init-token")
        self.assertEqual((code, payload["kind"]), (1, "human_only"))
        self.assertFalse(humans.token_path(self.home).exists())
        made = self.ok("doctor", "--init-token", env=self.human_env, stdin=TtyIO())
        self.assertEqual(humans.read_token(self.home), made["token"])
        if os.name == "posix":
            self.assertEqual(humans.token_path(self.home).stat().st_mode & 0o777, 0o600)
        code, payload, _ = self.cli("doctor", "--init-token", env=self.human_env, stdin=TtyIO())
        self.assertEqual((code, payload["kind"]), (1, "token_exists"))
        self.assertEqual(humans.read_token(self.home), made["token"])

    def test_pause_off_is_refused_and_the_pause_stays(self):
        self.ok("pause", "on")
        code, payload, _ = self.cli("pause", "off")
        self.assertEqual((code, payload["kind"]), (1, "human_only"))
        self.assertIn("pause off", payload["terminal_cmd"])
        self.assertTrue(self.ok("status")["paused"]["project"])
        self.assertFalse(self.ok("pause", "off", env=self.human_env, stdin=TtyIO())["paused"])

    def test_repo_layer_cannot_enable_auto_approve(self):
        add_pr_state(self.fake)
        repo_layer = self.session / ".claude" / "not-me"
        repo_layer.mkdir(parents=True)
        (repo_layer / "review.md").write_text("---\nschema: 1\nauto_approve: true\n---\n# Review\n", encoding="utf-8")
        body = self.write(self.session, "body.md", "LGTM\n")
        code, payload, _ = self.cli("review-post", "3", "--event", "APPROVE", "--body-file", body)
        self.assertEqual((code, payload["kind"]), (1, "approve_refused"))
        code, _, _ = self.cli("review-post", "3", "--event", "APPROVE", "--body-file", body, "--allow-approve")
        self.assertEqual(code, 2)  # the old escape hatch is gone
        self.assertEqual(self.fake.writes_made(), [])


class PathTests(UnderClaudeCase):
    def test_worktree_add_outside_the_worktrees_dir_is_refused(self):
        sha = git(self.session, "rev-parse", "HEAD").strip()
        for target in (self.tc.root / "anywhere", self.session / ".git" / "not-me" / "worktrees" / ".." / "x"):
            with self.subTest(target=str(target)):
                code, payload, _ = self.cli("git", "worktree-add", str(target), sha)
                self.assertEqual((code, payload["kind"]), (1, "unsafe_path"))
                self.assertFalse(target.exists())
        ok = self.session / ".git" / "not-me" / "worktrees" / "review-3"
        self.ok("git", "worktree-add", str(ok), sha)
        self.assertTrue((ok / "a.txt").is_file())

    def test_log_commands_refuse_a_file_that_is_not_a_run_log(self):
        root = self.session / ".git" / "not-me" / "logs" / "new"
        root.mkdir(parents=True)
        planted = root / "planted.md"
        planted.write_text("# just a file\n", encoding="utf-8")
        msg = self.write(self.session, "msg.txt", "x\n")
        code, payload, _ = self.cli("log", "action", str(planted), "--message-file", msg)
        self.assertEqual((code, payload["kind"]), (2, "usage"))
        self.assertEqual(planted.read_text(encoding="utf-8"), "# just a file\n")

    def test_bad_log_option_is_refused_before_the_write(self):
        add_pr_state(self.fake)
        stray = self.write(self.session, "stray.md", "# not a log\n")
        code, payload, _ = self.cli("request-review", "3", "--reviewers", "hubot", "--log", stray)
        self.assertEqual((code, payload["kind"]), (2, "usage"))
        self.assertEqual(self.fake.writes_made(), [])

    def test_log_failure_after_the_write_is_a_warning(self):
        add_pr_state(self.fake)
        start = self.ok("log", "start", "--skill", "new", "--ref", "1")
        with mock.patch.object(runlog.RunLog, "action", side_effect=OSError("disk full")):
            payload = self.ok("request-review", "3", "--reviewers", "hubot", "--log", start["path"])
        self.assertEqual(payload["requested"], ["hubot"])
        self.assertIn("run log not written", payload["warning"])

    def test_input_files_over_one_megabyte_are_refused(self):
        big = self.write(self.session, "big.md", "x" * (cli.MAX_INPUT_BYTES + 1))
        code, payload, _ = self.cli("check-text", "--file", big)
        self.assertEqual((code, payload["kind"]), (1, "input_too_large"))


class ProveTargetTests(UnderClaudeCase):
    def setUp(self):
        super().setUp()
        add_pr_state(self.fake)

    def test_preview_comment_from_an_unlisted_bot_is_skipped(self):
        self.fake.post_comment(REPO, 3, "evil[bot]", "Preview: https://deploy-preview-3--app.netlify.app/")
        payload = self.ok("prove-target", "3")
        self.assertTrue(payload["none"])
        self.assertIn("not from prove.target.bots", payload["skipped"][0]["reason"])

    def test_deployment_on_an_unknown_host_is_skipped(self):
        self.fake.state.setdefault("deployments", []).append(
            {"repo": REPO, "sha": "a" * 40, "id": 11,
             "statuses": [{"state": "success", "environment_url": "https://netlify.app.evil.test/"}]})
        payload = self.ok("prove-target", "3")
        self.assertTrue(payload["none"])
        self.assertEqual(payload["skipped"][0]["strategy"], "deployments")
        self.assertIn("not in prove.target.hosts", payload["skipped"][0]["reason"])

    def test_repo_layer_cannot_add_hosts_or_bots(self):
        repo_layer = self.session / ".claude" / "not-me"
        repo_layer.mkdir(parents=True)
        (repo_layer / "prove.md").write_text('---\nschema: 1\ntarget: {hosts: [evil.test], bots: ["evil[bot]"]}\n'
                                             '---\n# Prove\n', encoding="utf-8")
        self.fake.post_comment(REPO, 3, "evil[bot]", "Preview: https://x.evil.test/")
        self.fake.state.setdefault("deployments", []).append(
            {"repo": REPO, "sha": "a" * 40, "id": 11,
             "statuses": [{"state": "success", "environment_url": "https://x.evil.test/"}]})
        self.assertTrue(self.ok("prove-target", "3")["none"])


    def test_repo_layer_url_template_cannot_bring_its_own_host(self):
        repo_layer = self.session / ".claude" / "not-me"
        repo_layer.mkdir(parents=True)
        (repo_layer / "prove.md").write_text('---\nschema: 1\ntarget: {url_template: "https://pr-{pr}.evil.test/", '
                                             'strategy: [url-template]}\n---\n# Prove\n', encoding="utf-8")
        payload = self.ok("prove-target", "3")
        self.assertTrue(payload["none"])
        self.assertIn("not in prove.target.hosts", payload["skipped"][0]["reason"])
        (repo_layer / "prove.md").write_text('---\nschema: 1\ntarget: '
                                             '{url_template: "https://pr-{pr}--app.netlify.app/"}\n---\n# Prove\n',
                                             encoding="utf-8")
        payload = self.ok("prove-target", "3")  # a repo template on an allowed host still works
        self.assertEqual((payload["strategy"], payload["url"]), ("url-template", "https://pr-3--app.netlify.app/"))


class CorrectnessTests(UnderClaudeCase):
    def lock(self, n):
        return claims.lock_path(self.session / ".git" / "not-me" / "claims", n, "pr")

    def test_claim_pr_dry_run_takes_no_lock(self):
        add_pr_state(self.fake)
        payload = self.ok("claim-pr", "3", "--dry-run")
        self.assertTrue(payload["dry_run"])
        self.assertFalse(self.lock(3).exists())
        self.assertFalse((self.session / ".git" / "not-me" / "claims").exists())

    def test_release_pr_removes_a_lock_held_by_another_run(self):
        add_pr_state(self.fake)
        self.lock(3).parent.mkdir(parents=True)
        self.lock(3).write_text(json.dumps({"run_id": "other-run", "ts": claims.iso(claims.utcnow())}),
                                encoding="utf-8")
        self.ok("release-pr", "3")
        self.assertFalse(self.lock(3).exists())

    def test_read_only_commands_create_no_directories(self):
        add_pr_state(self.fake)
        for argv in (["status"], ["preflight"], ["pick-issue"], ["pick-needs-work"], ["pick-reviews"],
                     ["log", "last"]):
            with self.subTest(argv=argv):
                self.ok(*argv)
                self.assertFalse((self.session / ".git" / "not-me").exists(), argv)

    def test_label_collision_is_a_config_error(self):
        (self.home / "project.md").write_text(base.GLOBAL_PROJECT.replace(
            "operator: octocat", "operator: octocat\nlabels: {needs_review: to do}"), encoding="utf-8")
        code, payload, _ = self.cli("config", "--check")
        self.assertEqual(code, 4)
        self.assertTrue(any("to do" in e for e in payload["errors"]))
        self.assertEqual(self.cli("pick-issue")[0], 4)
        with self.assertRaises(states.LabelCollision):
            states.Labels({"todo": "x", "blocked": "X"})

    def test_doctor_exits_1_when_a_non_gh_check_fails(self):
        (self.home / "project.md").write_text("---\nschema: 1\nlimits: {fix_loop: abc}\n---\n# Project\n",
                                              encoding="utf-8")
        code, payload, _ = self.cli("doctor")
        self.assertEqual((code, payload["kind"]), (1, "doctor"))

        def no_gh(argv):
            raise FileNotFoundError(2, "no", "gh")
        (self.home / "project.md").write_text(base.GLOBAL_PROJECT, encoding="utf-8")
        self.fake = no_gh
        self.assertEqual(self.cli("doctor")[0], 7)


class AncestryTests(unittest.TestCase):
    """humans.under_claude_process_tree over injected process tables: pid -> (ppid, comm, args)."""

    SHELL = {1: (0, "launchd", "/sbin/launchd"), 50: (1, "Terminal", "/System/Applications/Terminal.app/x"),
             60: (50, "-zsh", "-zsh"), 70: (60, "python3", "python3 cli/notme.py pause off")}

    def check(self, table, pid=70):
        return humans.under_claude_process_tree(table=table, pid=pid)

    def test_a_plain_shell_is_human(self):
        self.assertFalse(self.check(self.SHELL))

    def test_claude_ancestors_restrict(self):
        claude_like = [(80, "claude", "claude --resume"),
                       (80, "node", "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"),
                       (80, "node", "node /opt/claude-code/dist/cli.js"),
                       (80, "/Applications/Claude.app/Contents/MacOS/Claude", "/Applications/Claude.app/x"),
                       (80, "claude.exe", "C:\\Users\\me\\AppData\\Local\\AnthropicClaude\\claude.exe")]
        for row in claude_like:
            with self.subTest(row=row):
                table = dict(self.SHELL)
                table[80] = (1, row[1], row[2])
                table[50] = (80, "Terminal", "terminal panel")  # the shell was started by Claude Code
                self.assertTrue(self.check(table))

    def test_unreadable_ancestry_fails_closed(self):
        self.assertTrue(humans.under_claude_process_tree(reader=lambda: None, pid=70))
        self.assertTrue(self.check({}))
        self.assertTrue(self.check({70: (60, "python3", "x")}))  # gap: the parent cannot be read
        loop = {70: (71, "python3", "x"), 71: (70, "sh", "sh")}
        self.assertTrue(self.check(loop))  # a cycle never reaches the root

    def test_names_that_merely_mention_claude_do_not_restrict(self):
        table = dict(self.SHELL)
        table[60] = (50, "vim", "vim CLAUDE.md")
        self.assertFalse(self.check(table))

    def test_windows_explorer_is_a_session_root(self):
        table = {70: (60, "python.exe", "python notme.py"), 60: (40, "cmd.exe", "cmd"),
                 40: (9999, "explorer.exe", "C:\\Windows\\explorer.exe")}
        self.assertFalse(self.check(table))

    def test_the_real_table_reads_or_fails_closed(self):
        self.assertIsInstance(humans.under_claude_process_tree(), bool)


class RedactTests(unittest.TestCase):
    def test_every_credential_shape_is_masked(self):
        cases = {"http://user:pw@example.test/x": "pw", "https://user:s3cret@github.com/acme/app.git": "s3cret",
                 "ssh://deploy@host.example/repo.git": "deploy", "x-access-token:abc123@github.com": "abc123",
                 "token ghp_abcdefghijklmnopqrstuvwxyz0123": "ghp_abcdefghijkl",
                 "github_pat_abcdefghijklmnopqrstuvwxyz": "github_pat_abcdefghij"}
        for text, secret in cases.items():
            with self.subTest(text=text):
                self.assertNotIn(secret, redact(text))

    def test_payloads_are_redacted_once_in_main(self):
        out = io.StringIO()
        me = next(c for c in cli.COMMANDS if c.name == "me")
        leak = {"x": "https://u:hunter2@h.test/", "nested": [{"k": "x-access-token:abc123@github.com"}]}
        with mock.patch.object(me, "handler", return_value=leak):
            cli.main(["me"], env={"PATH": os.environ.get("PATH", "")}, stdout=out, stderr=io.StringIO())
        self.assertNotIn("hunter2", out.getvalue())
        self.assertNotIn("abc123", out.getvalue())
        self.assertIn("https://***@h.test/", out.getvalue())


def _without_claude_env(case):
    """The same attacks with every Claude Code variable unset (`env -u CLAUDECODE ...`) and a non-tty stdin."""

    class Case(case):
        def setUp(self):
            super().setUp()
            self.env = {k: v for k, v in self.env.items() if k not in cli.CLAUDE_ENV}

    Case.__name__ = Case.__qualname__ = "NoClaudeEnv" + case.__name__
    Case.__doc__ = _without_claude_env.__doc__
    return Case


_SKIP_NO_ENV = {"test_overrides_are_honoured_only_for_a_person_at_a_terminal"}
for _case in (TrustAnchorTests, EnvironmentTests, HumanOnlyTests, PathTests, ProveTargetTests, CorrectnessTests):
    _variant = _without_claude_env(_case)
    for _name in _SKIP_NO_ENV & set(dir(_case)):
        setattr(_variant, _name, unittest.skip("needs a Claude Code variable")(getattr(_case, _name)))
    globals()[_variant.__name__] = _variant
del _case, _variant, _name


if __name__ == "__main__":
    unittest.main()
