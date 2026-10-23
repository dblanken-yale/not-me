"""Merge grants: the UserPromptSubmit hook that writes and ends them, and the guard that spends them."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from notme import guard, humans, merge_grant, runlog
from notme.guard import Block, GuardContext
from tests import PLUGIN_ROOT
from tests import guard_cases as gc

PROMPT_HOOK = PLUGIN_ROOT / "hooks" / "merge_grant.py"
_SPEC = importlib.util.spec_from_file_location("notme_prompt_hook", str(PROMPT_HOOK))
prompt_hook = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prompt_hook)

MAC_CLI = "/Users/x/Library/Application Support/Claude/claude-code/2.1.0/claude"
MAC_APP = "/Applications/Claude.app/Contents/MacOS/Claude"
MAC_HELPER = "/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper"
TERMINAL = "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"
ITERM = "/Applications/iTerm.app/Contents/MacOS/iTerm2"
NODE_CLI = "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"
# the hook as Claude Code runs it: its python, the launcher, the shell
HOOK = (("/usr/bin/python3", "python3 /p/hooks/merge_grant.py"),
        ("/bin/bash", "bash /p/bin/notme-python /p/hooks/merge_grant.py"),
        ("/bin/sh", "/bin/sh -c bash \"/p/bin/notme-python\" \"/p/hooks/merge_grant.py\""))


def by_path(pid, comm):
    """The test resolver: the command name is the real executable path when it is absolute (as macOS ps gives)."""
    return comm if comm.startswith("/") else None


def table(*chain):
    """A process table for this process and the ancestors given (comm, args), outermost last, then launchd."""
    rows, pid = {}, os.getpid()
    for k, (comm, args) in enumerate(chain):
        parent = 1 if k == len(chain) - 1 else 1000 + k
        rows[pid] = (parent, comm, args)
        pid = parent
    rows[1] = (1, "/sbin/launchd", "/sbin/launchd")
    return rows


def desktop():
    return table(*HOOK, (MAC_CLI, MAC_CLI), (MAC_APP, MAC_APP))


def terminal():
    return table(*HOOK, ("/usr/local/bin/claude", "claude"), ("/bin/zsh", "-zsh"), ("/usr/bin/login", "login -pf x"),
                 (TERMINAL, TERMINAL))


class _Home(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="notme-grant-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.home), True)
        humans.create_token(self.home)  # grants are signed with the operator token

    def prompt(self, text, sid="s1", event="UserPromptSubmit", procs="terminal", env=None):
        """Run the real hook's main() as Claude Code does (JSON in, NOT_ME_HOME set), with a given process table
        (default: a session started in a terminal); procs=None means the table cannot be read."""
        payload = {"hook_event_name": event, "session_id": sid, "prompt": text, "cwd": str(self.home),
                   "transcript_path": "/dev/null"}
        base = {k: v for k, v in os.environ.items() if not k.startswith(("NOT_ME_", "CLAUDE_CODE_ENTRYPOINT"))}
        rows = terminal() if procs == "terminal" else procs
        out = io.StringIO()
        with mock.patch.dict(os.environ, dict(base, NOT_ME_HOME=str(self.home), **(env or {})), clear=True), \
                mock.patch.object(humans, "read_process_table", lambda: rows), \
                mock.patch.object(merge_grant, "real_exe", lambda platform=None: by_path), redirect_stdout(out):
            rc = prompt_hook.main(json.dumps(payload))
        self.assertEqual(rc, 0)
        return SimpleNamespace(returncode=rc, stdout=out.getvalue().encode("utf-8"))

    def grant(self, sid="s1"):
        return merge_grant.read(self.home, sid)


class ProvenanceTests(_Home):
    """A grant is written only from the exact shapes a person starts: the hook's launch chain, one Claude Code
    process, then the desktop app binary or an interactive shell under a terminal, login, sshd, tmux or screen
    binary (real executable paths), and no Claude Code process anywhere above. Everything else gets no grant."""

    def nested(self, rows):
        return merge_grant.nested_session(rows, exe=by_path)

    def test_shapes_a_person_starts(self):
        cases = {
            "desktop app (Code tab, Remote Control)": desktop(),
            "desktop app helper": table(*HOOK, (MAC_CLI, MAC_CLI), (MAC_HELPER, MAC_HELPER), (MAC_APP, MAC_APP)),
            "Terminal": terminal(),
            "iTerm without login": table(*HOOK, ("/usr/local/bin/claude", "claude"), ("/bin/zsh", "-zsh"),
                                         (ITERM, ITERM)),
            "npm CLI over ssh": table(*HOOK, ("/usr/local/bin/node", NODE_CLI), ("/bin/bash", "-bash"),
                                      ("/usr/sbin/sshd", "sshd: x@ttys001"), ("/usr/sbin/sshd", "sshd")),
            "tmux": table(*HOOK, ("/opt/homebrew/bin/claude", "claude"), ("/bin/zsh", "-zsh"),
                          ("/opt/homebrew/bin/tmux", "tmux")),
        }
        for name, rows in cases.items():
            with self.subTest(name):
                self.assertFalse(self.nested(rows))
        self.prompt("/not-me:allow-merge acme/app#5")  # the hook itself, from a Terminal session
        self.assertIsNotNone(self.grant())

    def test_every_other_shape_gets_no_grant(self):
        cases = {
            "claude -p from a desktop session": table(*HOOK, ("/usr/local/bin/claude", "claude -p hi"),
                                                      ("/bin/bash", "bash -c x"), (MAC_CLI, MAC_CLI),
                                                      (MAC_APP, MAC_APP)),
            "claude --resume from a terminal session": table(*HOOK, ("/usr/local/bin/claude", "claude --resume s1 -p"),
                                                             ("/bin/zsh", "zsh -c x"), ("/usr/local/bin/claude", "claude"),
                                                             ("/bin/zsh", "-zsh"), (TERMINAL, TERMINAL)),
            "a resumed session started by a shell under a session's shell": table(
                *HOOK, ("/usr/local/bin/claude", "claude -r s1"), ("/bin/zsh", "-zsh"), (TERMINAL, TERMINAL),
                (MAC_CLI, MAC_CLI), (MAC_APP, MAC_APP)),
            "npx claude-code inside a session": table(*HOOK, ("/usr/local/bin/node", NODE_CLI + " -p x"),
                                                      ("/usr/local/bin/npm", "npm exec"), (MAC_CLI, MAC_CLI),
                                                      (MAC_APP, MAC_APP)),
            "host named only in the arguments": table(*HOOK, (MAC_CLI, MAC_CLI), ("/tmp/x/claude", MAC_APP)),
            "host argv0 forged, real binary elsewhere": table(*HOOK, (MAC_CLI, MAC_CLI), ("/tmp/x/Claude", MAC_APP)),
            "a terminal named only by basename": table(*HOOK, (MAC_CLI, MAC_CLI), ("/bin/zsh", "-zsh"),
                                                       ("/tmp/x/Terminal", "Terminal")),
            "host not the direct parent": table(*HOOK, (MAC_CLI, MAC_CLI), ("/bin/bash", "bash"), (MAC_APP, MAC_APP)),
            "a shell whose parent is not an anchor": table(*HOOK, (MAC_CLI, MAC_CLI), ("/bin/zsh", "-zsh"),
                                                           ("/usr/bin/nohup", "nohup")),
            "an orphan re-parented to launchd": table(*HOOK, ("/usr/local/bin/claude", "claude -p x")),
            "a process the resolver cannot place": table(*HOOK, ("claude", "claude"), ("zsh", "-zsh"),
                                                         ("login", "login")),
            "no hook launch chain": table(("/usr/bin/python3", "python3 x.py"), (MAC_CLI, MAC_CLI), (MAC_APP, MAC_APP)),
            "no Claude Code at all": table(*HOOK, ("/bin/zsh", "-zsh"), (TERMINAL, TERMINAL)),
            "unreadable": None,
            "a gap in the chain": {os.getpid(): (4242, "/usr/bin/python3", "python3 merge_grant.py")},
        }
        for name, rows in cases.items():
            with self.subTest(name):
                self.assertTrue(self.nested(rows))
                out = self.prompt("/not-me:allow-merge acme/app#5", procs=rows).stdout.decode()
                self.assertIsNone(self.grant())
                self.assertIn("no merge was granted", out)

    def test_the_default_resolver_never_reads_arguments(self):
        darwin, windows = merge_grant.real_exe("darwin"), merge_grant.real_exe("win32")
        self.assertEqual(darwin(1, MAC_APP), MAC_APP)
        self.assertIsNone(darwin(1, "Claude"))  # a bare name is not a path
        self.assertIsNone(windows(1, "Claude.exe"))  # no executable path on Windows: no grant

    def test_print_mode_is_refused(self):
        self.assertTrue(merge_grant.started_non_interactively({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}))
        for value in ("claude-desktop", "cli", ""):
            self.assertFalse(merge_grant.started_non_interactively({"CLAUDE_CODE_ENTRYPOINT": value}))
        self.prompt("/not-me:allow-merge acme/app#5", env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
        self.assertIsNone(self.grant())


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

    def test_session_ids_must_be_plain_and_are_never_stripped(self):
        for sid in ("../../x y", "S/1", "s1\n", "x" * 129, "", "é"):
            with self.subTest(sid=sid):
                self.assertIsNone(merge_grant.session_key(sid))
                self.prompt("/not-me:allow-merge acme/app#5", sid=sid)
                self.assertFalse(merge_grant.grant_dir(self.home).exists())
        self.assertIsNone(merge_grant.session_key(None))
        self.assertEqual(merge_grant.session_key("S-1_a"), "S-1_a")

    def test_a_stripped_lookalike_session_gets_no_grant(self):
        self.prompt("/not-me:allow-merge acme/app#5", sid="S1")
        self.assertIsNotNone(self.grant("S1"))
        self.assertIsNone(merge_grant.read(self.home, merge_grant.session_key("S/1")))

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
