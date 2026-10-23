from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notme import guard, runlog, writes
from notme.guard import Block, GuardContext
from tests import PLUGIN_ROOT
from tests import guard_cases as gc
from tests import winsim

HOOK = PLUGIN_ROOT / "hooks" / "repo_guard.py"


class _Fixtures(unittest.TestCase):
    root: Path
    env: dict

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

    def ctx(self, variant: str = "app") -> GuardContext:
        cwd, home = gc.variant_paths(self.root, variant)
        return GuardContext.from_project(cwd, home=home, env=dict(self.env, NOT_ME_HOME=str(home)))

    def hook(self, stdin: str, variant: str = "app") -> subprocess.CompletedProcess:
        _, home = gc.variant_paths(self.root, variant)
        return subprocess.run([sys.executable, str(HOOK)], input=stdin.encode("utf-8"), capture_output=True,
                              env=dict(self.env, NOT_ME_HOME=str(home)), timeout=60)


class GuardTableTests(_Fixtures):
    """Every case in guard_cases, in-process through decide()."""

    def test_cases(self):
        self.assertGreaterEqual(len(gc.CASES), 110)
        ctxs = {}
        for case in gc.CASES:
            cid, want, rule, tool, tin, variant = case
            with self.subTest(cid=cid, tool=tool, input=tin, variant=variant):
                if variant not in ctxs:
                    ctxs[variant] = self.ctx(variant)
                    if not guard.is_active(ctxs[variant]):
                        ctxs[variant].self_only()  # as the hook does
                ctx = ctxs[variant]
                result = guard.decide(gc.event_for(case, self.root), ctx)
                self.assertEqual("block" if isinstance(result, Block) else "allow", want,
                                 getattr(result, "message", ""))
                if isinstance(result, Block):
                    self.assertEqual(result.rule, rule)
                    for text in gc.MESSAGE_CHECKS.get(cid, []):
                        self.assertIn(text, result.message)


class WindowsTypedPathTableTests(_Fixtures):
    """Table cases that name a fixture path, with the path typed the Windows way (C:\\Users\\x\\home). Bash drops
    unquoted backslashes, so before the guard judged words in both spellings these self-protection blocks came back
    allowed on Windows (fail open). Each case must decide as the table says both as written and, where it holds no
    quotes, with every path quoted (the shell keeps the backslashes). On Windows this runs on the real disk; elsewhere
    tests/winsim.py shows the fixture tree to the guard under Windows names with ntpath as its path module."""

    def setUp(self):
        self.ctxs = {}
        self.sim = None if os.name == "nt" else winsim.fixture_sim(self.root, self.env)
        if self.sim is not None:
            self.sim.start()
            self.addCleanup(self.sim.stop)

    def win(self, path) -> str:
        return str(path) if self.sim is None else self.sim.to_win(path)

    def typed(self, value, home, quote=False):
        if isinstance(value, str):
            for key, path in (("{F}", self.root), ("{H}", home), ("{P}", gc.PLUGIN)):
                win = self.win(path).replace("/", "\\")
                value = value.replace(key, f"'{win}'" if quote else win)
            return value
        if isinstance(value, dict):
            return {k: self.typed(v, home, quote and k == "command") for k, v in value.items()}
        return [self.typed(v, home) for v in value] if isinstance(value, list) else value

    def decide(self, variant, tool, tin):
        if variant not in self.ctxs:
            if self.sim is not None:
                self.sim.stop()  # the context is built on the real tree, then shown the Windows way
            ctx = self.ctx(variant)
            if not guard.is_active(ctx):
                ctx.self_only()  # as the hook does
            ctx.write_ctx  # config is read on the real tree
            if self.sim is not None:
                self.sim.start()
                ctx = self.sim.context(ctx)
            self.ctxs[variant] = ctx
        cwd, _ = gc.variant_paths(self.root, variant)
        return guard.decide({"tool_name": tool, "tool_input": tin, "cwd": self.win(cwd), "session_id": gc.SESSION},
                            self.ctxs[variant])

    def test_cases_with_backslash_paths(self):
        checked = 0
        for cid, want, rule, tool, tin, variant in gc.CASES:
            if not any(k in json.dumps(tin) for k in ("{F}", "{H}", "{P}")):
                continue
            checked += 1
            _, home = gc.variant_paths(self.root, variant)
            forms = [("as in the table", self.typed(tin, home))]  # as the Windows CI runner sees the table
            if tool == "Bash" and not any(q in tin["command"] for q in "'\""):  # quotable: no quotes of its own
                forms.append(("quoted", self.typed(tin, home, quote=True)))
            for form, typed in forms:
                with self.subTest(cid=cid, form=form, input=typed, variant=variant):
                    result = self.decide(variant, tool, typed)
                    self.assertEqual("block" if isinstance(result, Block) else "allow", want,
                                     getattr(result, "message", ""))
                    if isinstance(result, Block):
                        self.assertEqual(result.rule, rule)
        self.assertGreater(checked, 100)



class CrlfEditTests(unittest.TestCase):
    """A file with CRLF line endings (Path.write_text on Windows): Claude Code matches old_string against LF text,
    so the global-mode check applies the edit that way too. Other edits are not read at all."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="notme-crlf-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.ctx = GuardContext(cwd=str(self.dir), root=str(self.dir), home=str(self.dir / "home"), env={})

    def edit(self, text, old, new, name="split.md"):
        path = self.dir / name
        path.write_bytes(text.encode("utf-8"))
        return guard.decide({"tool_name": "Edit", "cwd": str(self.dir),
                             "tool_input": {"file_path": str(path), "old_string": old, "new_string": new}}, self.ctx)

    def test_the_text_of_an_edit_is_not_judged(self):
        """The text an edit leaves is not judged, and an edit the guard cannot apply is Claude Code's to refuse."""
        for nl in ("\n", "\r\n"):
            with self.subTest(newline=repr(nl)):
                self.assertIsNone(self.edit(f"notme pause{nl}X{nl}", "\nX", " off"))
        self.assertIsNone(self.edit("line one\r\nline two\r\n", "\nline two", "\nline 2"))
        self.assertIsNone(self.edit("a\r\nb\r\n", "missing", "x"))

    def test_global_project_mode_with_crlf(self):
        self.ctx = GuardContext(cwd=str(self.dir), root=str(self.dir), home=str(self.dir), env={})
        doc = "---\r\nschema: 1\r\nmode: dry-run\r\n---\r\n"
        self.assertEqual(self.edit(doc, "dry-run\n", "live\n", "project.md").rule, "self_protect")
        self.assertIsNone(self.edit(doc, "schema: 1\n", "schema: 1\nx: 1\n", "project.md"))


class ContextTests(_Fixtures):
    def test_activation(self):
        self.assertTrue(guard.is_active(self.ctx("app")))
        self.assertTrue(guard.is_active(self.ctx("run")))
        self.assertTrue(guard.is_active(self.ctx("runenv")))  # the run logged to $NOT_ME_LOG_ROOT
        self.assertFalse(guard.is_active(self.ctx("stale")))
        self.assertFalse(guard.is_active(self.ctx("bare")))

    def test_allowed_sources(self):
        self.assertEqual(self.ctx("app").allowed_repos, {"acme/app"})
        self.assertEqual(self.ctx("tracker@htracker").allowed_repos, {"acme/app", "acme/issues", "acme/web"})
        self.assertEqual(self.ctx("tracker").allowed_repos, {"acme/app"})  # repo layer, not confirmed
        self.assertIn("acme/friend", self.ctx("cfgallow").allowed_repos)
        self.assertIn("acme/friend", self.ctx("app@hproj").allowed_repos)
        self.assertIn("acme/friend", self.ctx("app@hstar").allowed_repos)
        self.assertNotIn("acme/friend", self.ctx("app@hmismatch").allowed_repos)
        self.assertEqual(self.ctx("run").allowed_repos, {"acme/app"})

    def test_rules_and_branches(self):
        ctx = self.ctx("off")
        self.assertFalse(ctx.rules["other_repo"])
        self.assertTrue(ctx.rules["force_push"])
        self.assertTrue(self.ctx("offnoconfirm").rules["other_repo"])
        self.assertEqual(ctx.protected_branches, {"main", "master", "develop"})

    def test_repo_layer_cannot_loosen(self):
        self.assertTrue(self.ctx("repooff").rules["other_repo"])
        self.assertEqual(self.ctx("repoallow").allowed_repos, {"acme/app"})

    def test_not_a_git_checkout(self):
        empty = self.root / "plain"
        empty.mkdir(exist_ok=True)
        ctx = GuardContext.from_project(empty, home=self.root / "homes" / "h0", env=self.env)
        self.assertFalse(guard.is_active(ctx))
        self.assertIsNone(guard.log_root(ctx.common_dir))
        guard.log_block(ctx, Block("force_push", "r"))  # nowhere to log: no exception, no file

    def test_self_only_never_forks_or_loads_config(self):
        def boom(args, cwd):
            raise AssertionError(f"git {args} called")
        home = self.root / "homes" / "h0"
        for where in (self.root / "nogit", self.root / "app" / "vendor" / "nested"):
            ctx = GuardContext.from_project(where, home=home, env=self.env, git=boom)
            self.assertFalse(guard.is_active(ctx))
            ctx.self_only()
            calls = [("Bash", {"command": "git push origin feature/x && git commit -m x && git pp"}, None),
                     ("Bash", {"command": "gh pr comment 1 --repo acme/other -b x"}, None),
                     ("Bash", {"command": "notme pause off"}, guard.SELF),
                     ("Write", {"file_path": str(home / writes.ALLOWLIST_FILE)}, guard.SELF),
                     ("Write", {"file_path": "notes.txt"}, None),
                     ("Read", {"file_path": str(home / guard.OPERATOR_TOKEN)}, guard.SELF),
                     ("Read", {"file_path": "README.md"}, None)]
            with mock.patch("subprocess.run", boom), mock.patch("subprocess.Popen", boom), \
                    mock.patch("notme.config.load", boom):
                for tool, tin, rule in calls:
                    with self.subTest(where=where.name, tool=tool, input=tin):
                        event = {"tool_name": tool, "tool_input": tin, "cwd": str(where)}
                        self.assertEqual(getattr(guard.decide(event, ctx), "rule", None), rule)
            # write_ctx is the empty stub self_only() put in place, never one built from config
            self.assertEqual(ctx.__dict__["write_ctx"], writes.GuardContext())


class TokenizerTests(unittest.TestCase):
    @staticmethod
    def words(cmd):
        return [c.words for c in guard.lex(cmd).commands]

    def test_commands_and_words(self):
        self.assertEqual(self.words("a && b || c; d | e & f\ng"), [[x] for x in "abcdefg"])
        self.assertEqual(self.words('git commit -m "fix: x" (y'), [["git", "commit", "-m", "fix: x"], ["y"]])
        self.assertEqual(self.words("echo '' \"\""), [["echo", "", ""]])
        # quotes, escapes and $'...' are read the way the shell reads them
        self.assertEqual(self.words("x='a b' y=a\\ b git push"), [["x=a b", "y=a b", "git", "push"]])
        self.assertEqual(self.words("g''i\"t\" $'\\x70ush' 'a;b' \"c && d\""), [["git", "push", "a;b", "c && d"]])
        # backslash-newline joins words, also inside double quotes; single quotes keep it
        self.assertEqual(self.words("gi\\\nt push \"a\\\nb\" 'c\\\nd'"), [["git", "push", "ab", "c\\\nd"]])
        # comments start only at a word start
        self.assertEqual(self.words("a#b c # git push\nd"), [["a#b", "c"], ["d"]])
        self.assertEqual(self.words("f() { cd x; }"), [["f"], ["{", "cd", "x"], ["}"]])
        self.assertTrue(guard.lex("f() { cd x; }").funcdef)
        self.assertFalse(guard.lex("a=() ; b").funcdef)

    def test_expansion_flags(self):
        (cmd,) = guard.lex("echo {a,b} x{1..3} g* [ab] '*' {} [").commands
        flags = [(w.glob, w.brace) for w in cmd.words]
        self.assertEqual(flags, [(False, False), (False, True), (False, True), (True, False), (True, False),
                                 (False, False), (False, False), (False, False)])

    def test_has_gh_git(self):
        self.assertTrue(guard.has_gh_git("x; gh pr list"))
        self.assertTrue(guard.has_gh_git("/usr/bin/git status"))
        self.assertFalse(guard.has_gh_git("github-cli digit legit .gitignore"))

    def test_substitutions(self):
        p = guard.lex("echo $(a $(b) c) `d` <(e) \"$(f)\" ${x:-$(g)} $((1 + (2))) '$(h)'")
        self.assertEqual(sorted(p.subs), sorted(["a $(b) c", "b", "d", "e", "f", "g", " $(g)"]))  # ${ } too
        self.assertEqual([c.words for c in p.commands if c.nested], [["b"], ["a", "$(b)", "c"], ["d"], ["e"], ["f"],
                                                                     ["g"]])
        self.assertEqual(guard.lex("echo $( (a) )").subs, [" (a) "])
        # inside double quotes a ' in ${ } is a plain character, not a quote
        self.assertEqual(guard.lex("echo \"${x:-'}\"; git push").commands[-1].words, ["git", "push"])
        # <<- strips leading tabs from the body and the delimiter line
        self.assertEqual(guard.lex("cat <<-EOF\n\tx\n\tEOF\ngit push").commands[-1].words, ["git", "push"])
        self.assertEqual(self.words("cd\t\tx"), [["cd", "x"]])

    def test_heredocs(self):
        p = guard.lex("cat <<'EOF'\ngit push\nEOF\necho done")
        self.assertEqual(p.code, "cat <<'EOF'\necho done")
        self.assertEqual([c.bodies for c in p.commands], [["git push\n"], []])
        p = guard.lex("sh <<-EOF\n\tgh pr merge 1\n\tEOF\nnext")
        self.assertEqual(p.commands[-1].words, ["next"])
        p = guard.lex("grep x <<< 'y'\ngit status")
        self.assertEqual([c.bodies for c in p.commands], [["y"], []])
        # a quoted <<EOF is data; an unquoted delimiter runs the substitutions in its body
        self.assertEqual(guard.lex("echo '<<EOF'\ngit push").commands[-1].words, ["git", "push"])
        self.assertEqual(guard.lex("cat <<EOF\n$(git push)\nEOF").subs, ["git push"])
        self.assertEqual(guard.lex("cat <<'EOF'\n$(git push)\nEOF").subs, [])
        # a body inside $( ) is not part of the substitution text
        self.assertEqual(guard.lex("x \"$(cat <<'E'\ngit push\nE\n)\"").subs, ["cat <<'E'\n"])

    def test_redirections(self):
        self.assertEqual(self.words("git push 2>&1 >/dev/null"), [["git", "push"]])
        self.assertEqual(self.words("git push > log.txt 2> err.txt"), [["git", "push"]])
        self.assertEqual(self.words("git push &> all.txt {fd}>x 3<&-"), [["git", "push"]])
        self.assertEqual(self.words("a 2>&1 & b >&2"), [["a"], ["b"]])
        self.assertEqual(self.words("echo '2'>x"), [["echo", "2"]])  # a quoted 2 is a word, not an fd
        self.assertEqual(guard.lex("cat < in.txt").commands[0].redirs, [("<", "in.txt")])

    def test_unparsed(self):
        for bad in ("echo 'a", 'echo "a', "echo `a", "echo $(a", "echo ${a", "echo $'a", "cat >", "a <<",
                    "echo `a \\`b\\``", "$(( $(a) ))", "x" * (guard.MAX_COMMAND + 1),
                    "echo " + "$(" * (guard.MAX_NESTING + 1)):
            with self.subTest(bad=bad[:40]):
                with self.assertRaises(guard.Unparsed):
                    guard.lex(bad)

    def test_adversarial_input_is_fast(self):
        """Commands far over the hook's budget are judged quickly: linear scans, a size cap and a nesting cap. Past
        the caps the text is opaque: allowed when it holds no opaque-text word, blocked when it does."""
        import time
        ctx = GuardContext(cwd="/nonexistent/app", root="/nonexistent/app", active_run=True, env={},
                           home="/nonexistent/home", plugin_root="/nonexistent/plugin", git=lambda a, c: None,
                           write_ctx=writes.GuardContext(origin_repo="acme/app", allowed_repos=("acme/app",)))
        # the hook's watchdog is 8 s; Windows runners are slower per call, so they get more of that budget
        limit = 3.0 if os.name == "nt" else 1.0
        n = 100 * 1024
        near = guard.MAX_COMMAND - 16
        for cmd in ("$(" * (n // 2), "${X-" * (n // 4), "cat/" * (n // 4) + " -", "-u " * (n // 3),
                    ("$(" * (near // 2))[:near], ("$(a)" * near)[:near], ("notme;" * near)[:near],
                    ("cat a;" * near)[:near], ("${X-" * near)[:near], ("-u " * near)[:near], ("(" * near),
                    "rm -r " * (n // 6), "x" * n + " git push origin main"):
            event = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/nonexistent/app"}
            start = time.perf_counter()
            guard.needs_check(event)
            block = guard.decide(event, ctx)
            took = time.perf_counter() - start
            with self.subTest(cmd=cmd[:20], size=len(cmd)):
                self.assertLess(took, limit)
                if len(cmd) > guard.MAX_COMMAND:
                    self.assertEqual(getattr(block, "rule", None), "protected_branches" if "push" in cmd else None)

    def test_prefilter(self):
        def bash(cmd):
            return guard.needs_check({"tool_name": "Bash", "tool_input": {"command": cmd}})
        for cmd in ("ls -la", "npm test", "cat <<EOF\ngit push\nEOF", "echo hello > out.txt", "cat -n notes"):
            self.assertFalse(bash(cmd), cmd)
        for cmd in ("git status", "g''h pr list", "g\\it log", "${X} y", "./run", "make.sh", "python3 -c 1",
                    "find . -name x", "cat ~/.claude/not-me/allowed-repos.json", "sh <<EOF\ngh pr merge 1\nEOF",
                    "cp -r /some/home x", "wc < /some/home/op*", "echo 0 > /some/home/m*.sig", "echo 0 >~/x",
                    "cat operator-token", "nohup make &", "script -q /dev/null make"):
            self.assertTrue(bash(cmd), cmd)
        self.assertTrue(guard.needs_check({"tool_name": "Write", "tool_input": {"file_path": "x"}}))
        self.assertTrue(guard.needs_check({"tool_name": "mcp__github__create_issue", "tool_input": {}}))
        self.assertFalse(guard.needs_check({"tool_name": "mcp__terminal__run_in_terminal", "tool_input": {}}))


class CheckoutWalkTests(unittest.TestCase):
    def test_plain_and_linked_worktree(self):
        d = Path(tempfile.mkdtemp(prefix="notme-walk-")).resolve()
        self.addCleanup(shutil.rmtree, d, True)
        (d / "main" / ".git" / "worktrees" / "wt").mkdir(parents=True)
        (d / "main" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (d / "main" / "sub").mkdir()
        wt_git = d / "main" / ".git" / "worktrees" / "wt"
        (wt_git / "commondir").write_text("../..\n")
        (wt_git / "HEAD").write_text("ref: refs/heads/feature/y\n")
        (d / "wt").mkdir()
        (d / "wt" / ".git").write_text(f"gitdir: {wt_git}\n")
        root, git_dir, common = guard.find_checkout(str(d / "main" / "sub"))
        self.assertEqual((root, git_dir, common), (str(d / "main"), d / "main" / ".git", d / "main" / ".git"))
        self.assertEqual(guard.head_branch(git_dir), "main")
        root, git_dir, common = guard.find_checkout(str(d / "wt"))
        self.assertEqual((root, git_dir, common), (str(d / "wt"), wt_git, d / "main" / ".git"))
        self.assertEqual(guard.head_branch(git_dir), "feature/y")
        (d / "main" / ".claude" / "not-me").mkdir(parents=True)
        ctx = GuardContext.from_project(d / "wt", home=d / "home", env={},
                                        git=lambda a, c: self.fail("git called"))
        self.assertEqual(ctx.config_dir, d / "main" / ".claude" / "not-me")
        (d / "bad").mkdir()
        (d / "bad" / ".git").write_text("nonsense")
        with self.assertRaises(guard._Ambiguous):
            guard.find_checkout(str(d / "bad"))


class UrlTests(unittest.TestCase):
    def test_repo_slug(self):
        for value in ("acme/app", "github.com/acme/app", "https://github.com/acme/app.git",
                      "https://github.com/acme/app/pull/3", "git@github.com:acme/app.git"):
            self.assertEqual(guard.repo_slug(value), "acme/app", value)
        for value in ("evil/acme/app", "{owner}/{repo}", "https://example.com/acme/app", "", None):
            self.assertIsNone(guard.repo_slug(value), value)


class WindowsPathTests(unittest.TestCase):
    """Path checks with ntpath standing in for os.path: drive-letter case, backslashes and forward slashes."""

    def setUp(self):
        import ntpath
        for patch in (mock.patch.object(guard, "_path", ntpath), mock.patch.object(ntpath, "realpath", ntpath.abspath),
                      mock.patch.object(ntpath, "exists", lambda p: False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.ctx = GuardContext(cwd="C:\\Work\\App", root="C:\\Work\\App", common_dir="C:\\Work\\App\\.git",
                                config_dir="C:\\Work\\App\\.claude\\not-me", home="C:\\Users\\op\\.claude\\not-me",
                                plugin_root="C:\\Plugins\\not-me", active_run=True, env={},
                                write_ctx=writes.GuardContext(origin_repo="acme/app", allowed_repos=("acme/app",)))

    def decide(self, tool, tin, cwd="C:\\Work\\App"):
        return guard.decide({"tool_name": tool, "tool_input": tin, "cwd": cwd}, self.ctx)

    def test_file_self_protection(self):
        for path in ("c:/work/app/.claude/not-me/project.md", "C:\\Plugins\\NOT-ME\\hooks\\repo_guard.py",
                     "C:/Users/OP/.claude/not-me/allowed-repos.json"):
            with self.subTest(path=path):
                self.assertEqual(self.decide("Edit", {"file_path": path}).rule, "self_protect")
        self.assertIsNone(self.decide("Edit", {"file_path": "C:/Work/App/src/main.py"}))

    def test_norm_and_under_compare_whole_components(self):
        norm = guard._norm
        self.assertEqual(norm("c:/WORK\\app/./x/../", "D:\\"), norm("C:\\Work\\App", "D:\\"))
        self.assertEqual(norm("sub/f", "C:\\Work\\App"), norm("C:/work/app/SUB/F", "D:\\"))
        base = norm("C:\\a", "D:\\")
        for path, under in (("C:\\a", True), ("C:\\a\\b", True), ("c:/A/b/", True), ("C:\\ab", False),
                            ("C:\\a.b\\c", False), ("D:\\a\\b", False), ("C:\\", False)):
            with self.subTest(path=path):
                self.assertIs(guard._under(norm(path, "D:\\"), base), under)
        self.assertTrue(guard._under(norm("C:\\x\\y", "D:\\"), norm("c:/", "D:\\")))

    def test_unquoted_backslash_paths(self):
        """Bash reads C:\\Work\\App as C:WorkApp; the guard judges the word as typed too."""
        words = guard.lex("cat C:\\Users\\op\\x 'C:\\q' \"C:\\d\" e\\ f").commands[0].words
        self.assertEqual([str(w) for w in words], ["cat", "C:Usersopx", "C:\\q", "C:\\d", "e f"])
        self.assertEqual([w.raw for w in words], ["cat", "C:\\Users\\op\\x", "C:\\q", "C:\\d", "e\\ f"])
        self.assertIsNone(self.decide("Bash", {"command": "git -C C:\\Work\\App status"}))
        for cmd in ("cat C:\\Users\\op\\.claude\\not-me\\op*", "cat 'C:\\Users\\op\\.claude\\not-me\\operator-token'",
                    "cp -r C:\\Users\\op\\.claude\\not-me D:\\x"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.decide("Bash", {"command": cmd}).rule, "self_protect")


class MessageTests(unittest.TestCase):
    def ctx(self):
        return GuardContext(write_ctx=writes.GuardContext(origin_repo="acme/app", allowed_repos=("acme/app", "acme/web"),
                                                          protected_branches=("main",)))

    def test_allow_suggestion(self):
        b = Block("other_repo", "acme/other is not an allowed repo", target="acme/other", command="gh pr comment 1",
                  suggest_allow=True)
        msg = guard.render(b, self.ctx())
        self.assertTrue(msg.startswith("not-me guard [other_repo]: blocked `gh pr comment 1`: targeting acme/other."))
        self.assertIn("This repo (acme/app) may write only to: acme/app, acme/web.", msg)
        self.assertIn("the operator runs:  /not-me:allow-repo acme/other\n", msg)
        self.assertIn("(all projects: /not-me:allow-repo acme/other --everywhere; team-wide: list it under repos "
                      "in .claude/not-me/project.md, then each operator confirms it once with "
                      "/not-me:allow-repo acme/other)", msg)
        self.assertIn("Autonomous runs: do not retry; log a QUESTION naming the operator and continue.", msg)
        self.assertIn("set guard.other_repo: off and confirm_guard_off: [other_repo]", msg)
        self.assertTrue(msg.endswith("See effective config: /not-me:config"))

    def test_unresolvable_gives_fix_not_allow(self):
        b = Block("other_repo", "gh/git inside $(...) cannot be checked", command="echo",
                  hint="run gh/git directly, not inside $(...)")
        msg = guard.render(b, self.ctx())
        self.assertNotIn("allow-repo", msg)
        self.assertIn("Instead: run gh/git directly, not inside $(...).", msg)

    def test_other_rules_say_what_to_do(self):
        msg = guard.render(Block("protected_branches", "merging a pull request is a human action",
                                 command="gh pr merge 1", hint="leave the PR ready for review"), self.ctx())
        self.assertIn("Why: merging a pull request is a human action.", msg)
        self.assertIn("Instead: leave the PR ready for review.", msg)
        self.assertNotIn("allow-repo", msg)
        msg = guard.render(Block("self_protect", "x", command="y"), self.ctx())
        self.assertNotIn("Relax this rule", msg)

    def test_decide_fills_message_and_tool(self):
        ctx = self.ctx()
        ctx.git = lambda args, cwd: None
        r = guard.decide({"tool_name": "Bash", "tool_input": {"command": "gh pr comment 1 -R acme/other -b x"},
                          "cwd": "."}, ctx)
        self.assertIsInstance(r, Block)
        self.assertEqual((r.tool, r.target), ("Bash", "acme/other"))
        self.assertIn("/not-me:allow-repo acme/other", r.message)
        self.assertIsNone(guard.decide({"tool_name": "Glob", "tool_input": {}}, ctx))
        with self.assertRaises(TypeError):
            guard.decide({"tool_name": "Bash", "tool_input": "x"}, ctx)


class BlocklogTests(unittest.TestCase):
    def test_append(self):
        d = Path(tempfile.mkdtemp(prefix="notme-log-"))
        self.addCleanup(shutil.rmtree, d, True)
        ctx = GuardContext(common_dir=d)
        guard.log_block(ctx, Block("force_push", "r1", tool="Bash"))
        guard.log_block(ctx, Block("other_repo", "r2", target="acme/x", tool="Write"))
        p = d / "not-me" / "logs" / "guard.jsonl"
        self.assertEqual(runlog.guard_log_path(guard.log_root(d)), p)
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["rule"] for r in rows], ["force_push", "other_repo"])
        self.assertEqual(set(rows[1]), {"ts", "tool", "rule", "reason", "target"})
        self.assertEqual((rows[1]["tool"], rows[1]["target"]), ("Write", "acme/x"))
        self.assertTrue(rows[0]["ts"].endswith("Z"))
        blocker = d / "file"
        blocker.write_text("x")
        runlog.guard_log_append(blocker / "guard.jsonl", {"rule": "force_push"})  # unwritable: no exception


class HookTests(_Fixtures):
    def event(self, tool, tin, variant="app"):
        cwd, _ = gc.variant_paths(self.root, variant)
        return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tin, "cwd": str(cwd)})

    def test_block_logs_and_exits_2(self):
        proc = self.hook(self.event("Bash", {"command": "gh pr comment 1 -R acme/other -b x"}))
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"/not-me:allow-repo acme/other", proc.stderr)
        log = self.root / "app" / ".git" / "not-me" / "logs" / "guard.jsonl"
        self.assertIn('"target": "acme/other"', log.read_text(encoding="utf-8"))

    def test_allow_and_inactive(self):
        self.assertEqual(self.hook(self.event("Bash", {"command": "git status"})).returncode, 0)
        self.assertEqual(self.hook(self.event("Bash", {"command": "git push --force"}, "bare"), "bare").returncode, 0)

    def test_fails_closed_on_git_input(self):
        proc = self.hook('{"tool_name": "Bash", "tool_input": {"command": "git push')
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"guard failed closed", proc.stderr)
        self.assertIn(b"/not-me:doctor", proc.stderr)
        proc = self.hook(self.event("Bash", "git push origin main"))  # tool_input is not an object
        self.assertEqual(proc.returncode, 2)

    def test_fails_open_otherwise(self):
        proc = self.hook('{"tool_name": "Bash", "tool_input": {"command": "ls')
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertEqual(self.hook(self.event("Bash", "ls -la")).returncode, 0)

    def test_inactive_and_read_paths_start_no_process(self):
        spec = importlib.util.spec_from_file_location("notme_repo_guard_hook", str(HOOK))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        def boom(*args, **kwargs):
            raise AssertionError("subprocess started")
        calls = [("Bash", {"command": "ls"}, "bare"),
                 ("Bash", {"command": "git push --force origin main"}, "bare"),
                 ("Bash", {"command": "gh pr comment 1 -R acme/other -b x"}, "bare"),
                 ("Edit", {"file_path": "x/.git/config"}, "bare"),
                 ("Bash", {"command": "ls"}, "app"),
                 ("Bash", {"command": "git status && git log --oneline"}, "app")]
        with mock.patch("subprocess.run", boom), mock.patch("subprocess.Popen", boom):
            for tool, tin, variant in calls:
                with self.subTest(tool=tool, input=tin, variant=variant):
                    self.assertEqual(mod.main(self.event(tool, tin, variant)), 0)

    def test_destructive_label(self):
        proc = self.hook(self.event("Bash", {"command": "gh secret set T --repo acme/app --body x"}))
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"not-me guard [destructive]", proc.stderr)
        self.assertIn(b"set guard.destructive_git: off", proc.stderr)

    def test_selftest(self):
        ctx = self.ctx("app")
        self.assertTrue(guard.selftest([sys.executable, str(HOOK)], ctx, env=self.env))
        self.assertFalse(guard.selftest([sys.executable, "-c", "pass"], ctx, env=self.env))
        # self-protection applies outside a not-me project too, so the canned allow-list write is blocked there
        self.assertTrue(guard.selftest([sys.executable, str(HOOK)], self.ctx("bare"), env=self.env))


class HooksJsonTests(unittest.TestCase):
    def test_declares_pre_tool_use(self):
        data = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        entry = data["hooks"]["PreToolUse"][0]
        self.assertEqual(entry["matcher"],
                         "Bash|Write|Edit|MultiEdit|NotebookEdit|Read|NotebookRead|Grep|Glob|LS|mcp__.*")
        hook = entry["hooks"][0]
        self.assertEqual((hook["type"], hook["timeout"]), ("command", 10))
        self.assertIn('bin/notme-python" "${CLAUDE_PLUGIN_ROOT}/hooks/repo_guard.py"', hook["command"])

    def test_declares_only_the_guard(self):
        data = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(list(data["hooks"]), ["PreToolUse"])


class LiveMarkerTests(unittest.TestCase):
    """The guard protects the live-mode marker by name, the same cheap way as the allow list and the token."""

    def test_the_guard_names_the_marker_the_cli_writes(self):
        from notme import humans
        self.assertEqual(guard.LIVE_MARKER_NAME, humans.LIVE_MARKER)

    def test_a_shell_mention_of_the_marker_is_never_prefiltered_away(self):
        for cmd in ("cp /tmp/x ~/.claude/x/mode-live.sig", "echo 0 > 'mode-live.sig'", "touch mode-l\\ive.sig"):
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.needs_check({"tool_name": "Bash", "tool_input": {"command": cmd}}))


class InterpreterPrefilterTests(unittest.TestCase):
    def test_every_interpreter_name_reaches_the_check(self):
        import re
        body = guard._INTERP_NAMES[len("(?:"):guard._INTERP_NAMES.index(")")]
        for name in body.split("|"):
            for spelled in (name, name + "3", name + "3.12", name.upper(), name.capitalize()):
                with self.subTest(name=spelled):
                    self.assertTrue(guard._INTERP.match(spelled.lower()))
                    event = {"tool_name": "Bash", "tool_input": {"command": f"{spelled} x.txt"}}
                    self.assertTrue(guard.needs_check(event))
        self.assertTrue(re.search(r"\(\?i:", guard._PREFILTER.pattern))


class OwnScriptsTests(unittest.TestCase):
    """The live-mode heuristics for interpreter code must not block this repository's own check commands."""

    def test_the_acceptance_commands_are_allowed(self):
        repo = PLUGIN_ROOT.parent.parent
        home = Path(tempfile.mkdtemp(prefix="notme-home-"))
        self.addCleanup(shutil.rmtree, str(home), True)
        ctx = GuardContext(cwd=str(repo), root=str(repo), config_dir=str(repo / ".claude" / "not-me"), home=str(home),
                           plugin_root=str(PLUGIN_ROOT), env={},
                           write_ctx=writes.GuardContext(origin_repo="acme/app", allowed_repos=("acme/app",)))
        for cmd in ("python3 scripts/run-tests.py", "python3 plugins/not-me/tests/run_guard_cases.py",
                    "python3 scripts/check-skills.py", "python3 scripts/gen-cli-docs.py --check",
                    "bash scripts/validate-plugins.sh", "claude plugin validate --strict plugins/not-me",
                    "shellcheck $(git ls-files '*.sh')"):
            with self.subTest(cmd=cmd):
                event = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}
                self.assertIsNone(guard.decide(event, ctx))


if __name__ == "__main__":
    unittest.main()
