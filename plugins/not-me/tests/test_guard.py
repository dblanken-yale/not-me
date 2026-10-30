from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notme import guard, paths, runlog, writes
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

    def ctx(self, variant: str = "app", git=paths.run_git) -> GuardContext:
        cwd, home = gc.variant_paths(self.root, variant)
        return GuardContext.from_project(cwd, home=home, env=dict(self.env, NOT_ME_HOME=str(home)), git=git)

    def hook(self, stdin: str, variant: str = "app") -> subprocess.CompletedProcess:
        _, home = gc.variant_paths(self.root, variant)
        return subprocess.run([sys.executable, str(HOOK)], input=stdin.encode("utf-8"), capture_output=True,
                              env=dict(self.env, NOT_ME_HOME=str(home)), timeout=60)


# The two table tests below are the slowest in the suite, so their cases are split over several classes (shards) that a
# parallel runner can spread over workers. Each shard owns gc.CASES[shard::TABLE_SHARDS]; TableShardTests checks that
# the shards together cover every case exactly once.
TABLE_SHARDS = 2


def _shard(shard: int) -> list:
    return gc.CASES[shard::TABLE_SHARDS]


def _memo_git(git):
    """git with every answer cached by (args, cwd). The guard only reads git (config, remotes, branch) and the table
    never changes the fixture tree, so each answer is the one real git gives; it just forks once instead of per case."""
    cache = {}

    def run(args, cwd):
        key = (tuple(args), str(cwd))
        if key not in cache:
            cache[key] = git(args, cwd)
        return cache[key]
    return run


class TableShardTests(unittest.TestCase):
    def test_shards_cover_every_case_once(self):
        self.assertGreaterEqual(len(gc.CASES), 110)
        shards = [_shard(i) for i in range(TABLE_SHARDS)]
        self.assertEqual(sum(len(s) for s in shards), len(gc.CASES))
        self.assertEqual(sorted(c[0] for s in shards for c in s), sorted(c[0] for c in gc.CASES))
        for name, base in (("GuardTableShard", _GuardTable), ("WindowsTypedPathTableShard", _WindowsTypedPathTable)):
            found = sorted(cls.shard for cls in globals().values()
                           if isinstance(cls, type) and issubclass(cls, base) and cls.__name__.startswith(name))
            self.assertEqual(found, list(range(TABLE_SHARDS)), name)


class _GuardTable:
    """Every case in guard_cases, in-process through decide() (mixed into one class per shard)."""
    shard: int

    def test_cases(self):
        ctxs = {}
        git = _memo_git(paths.run_git)
        for case in _shard(self.shard):
            cid, want, rule, tool, tin, variant = case
            with self.subTest(cid=cid, tool=tool, input=tin, variant=variant):
                if variant not in ctxs:
                    ctxs[variant] = self.ctx(variant, git=git)
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


class GuardTableShard0Tests(_GuardTable, _Fixtures):
    shard = 0


class GuardTableShard1Tests(_GuardTable, _Fixtures):
    shard = 1


class _WindowsTypedPathTable:
    """Table cases that name a fixture path, with the path typed the Windows way (C:\\Users\\x\\home). Bash drops
    unquoted backslashes, so before the guard judged words in both spellings these self-protection blocks came back
    allowed on Windows (fail open). Each case must decide as the table says both as written and, where it holds no
    quotes, with every path quoted (the shell keeps the backslashes). On Windows this runs on the real disk; elsewhere
    tests/winsim.py shows the fixture tree to the guard under Windows names with ntpath as its path module.
    Mixed into one class per shard."""
    shard: int

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
            if not ctx.only_self:
                ctx.git = _memo_git(ctx.git)
            self.ctxs[variant] = ctx
        cwd, _ = gc.variant_paths(self.root, variant)
        return guard.decide({"tool_name": tool, "tool_input": tin, "cwd": self.win(cwd), "session_id": gc.SESSION},
                            self.ctxs[variant])

    @staticmethod
    def names_a_path(tin) -> bool:
        return any(k in json.dumps(tin) for k in ("{F}", "{H}", "{P}"))

    def test_cases_with_backslash_paths(self):
        checked = 0
        for cid, want, rule, tool, tin, variant in _shard(self.shard):
            if not self.names_a_path(tin):
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
        self.assertGreater(checked, 100 // TABLE_SHARDS)


class WindowsTypedPathTableShardTests(unittest.TestCase):
    def test_the_table_names_enough_paths(self):
        self.assertGreater(sum(1 for c in gc.CASES if _WindowsTypedPathTable.names_a_path(c[4])), 100)


class WindowsTypedPathTableShard0Tests(_WindowsTypedPathTable, _Fixtures):
    shard = 0


class WindowsTypedPathTableShard1Tests(_WindowsTypedPathTable, _Fixtures):
    shard = 1


@unittest.skipIf(os.name == "nt", "the pattern exemption is never given on Windows")
class PatternProgramTests(_Fixtures):
    """The grep, rg or sed pattern exemption holds only when the program resolves on PATH to a file the user cannot
    change. A program planted in a writable PATH directory (in this call or an earlier one) loses it."""

    def decide(self, cmd: str, path: str):
        cwd, home = gc.variant_paths(self.root, "app")
        ctx = GuardContext.from_project(cwd, home=home, env=dict(self.env, NOT_ME_HOME=str(home), PATH=path))
        if not guard.is_active(ctx):
            ctx.self_only()
        return guard.decide({"tool_name": "Bash", "tool_input": {"command": cmd.format(H=home)}, "cwd": str(cwd),
                             "session_id": gc.SESSION}, ctx)

    def planted(self, name: str) -> str:
        d = Path(tempfile.mkdtemp(prefix="notme-path-")).resolve()
        self.addCleanup(shutil.rmtree, str(d), True)
        (d / name).write_text("#!/bin/sh\ncat \"$1\"\n", encoding="utf-8")
        os.chmod(str(d / name), 0o755)
        return str(d)

    def system_dir(self, name: str) -> str:
        found = shutil.which(name, path="/usr/bin" + os.pathsep + "/bin")
        if not found or guard._host_writable(found) or guard._host_writable(os.path.realpath(found)):
            self.skipTest(f"no {name} the user cannot write")
        return os.path.dirname(found)

    def test_writable_program_on_path_blocks(self):
        for name in ("grep", "rg", "sed"):
            with self.subTest(name=name):
                cmd = "sed -n '/{H}/operator-token/p' README.md" if name == "sed" else name + " '{H}/operator-token'"
                self.assertEqual(self.decide(cmd, self.planted(name)).rule, "self_protect")

    def test_system_program_allows(self):
        sysdir = self.system_dir("grep")
        self.assertIsNone(self.decide("grep -n 'x/operator-token' README.md", sysdir))
        self.assertIsNone(self.decide("sed -n '/operator-token/p' README.md", self.system_dir("sed")))

    def test_planted_program_before_system_dir_blocks(self):
        sysdir = self.system_dir("grep")
        self.assertEqual(self.decide("grep '{H}/operator-token'", self.planted("grep") + os.pathsep + sysdir).rule,
                         "self_protect")

    def test_link_in_writable_dir_to_system_program_blocks(self):
        sysdir = self.system_dir("grep")
        d = self.planted("other")
        os.symlink(os.path.join(sysdir, "grep"), os.path.join(d, "grep"))
        self.assertEqual(self.decide("grep '{H}/operator-token'", d).rule, "self_protect")

    def test_relative_or_empty_entry_before_program_blocks(self):
        sysdir = self.system_dir("grep")
        for path in ("bin" + os.pathsep + sysdir, os.pathsep + sysdir, sysdir + os.pathsep):
            with self.subTest(path=path):
                result = self.decide("grep 'x/operator-token' README.md", path)
                if path.endswith(os.pathsep):  # an empty entry after the program's directory is never reached
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result.rule, "self_protect")

    def test_program_not_on_path_blocks(self):
        self.assertEqual(self.decide("grep 'x/operator-token' README.md", self.planted("other")).rule,
                         "self_protect")

    def test_system_rg_allows(self):
        sysdir = self.system_dir("rg")  # a system package (Linux); Homebrew and cargo installs are user-writable
        self.assertIsNone(self.decide("rg '~/.claude/not-me/operator-token' README.md", sysdir))
        self.assertIsNone(self.decide("rg -g '*.py' -t py -- ~/.claude/not-me/operator-token scripts", sysdir))


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
                    "cat operator-token", "nohup make &", "script -q /dev/null make",
                    "cp -r C:\\Users\\op\\homes\\h0 backup"):  # only backslashes: a Windows path
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

    def test_variable_git_path_says_spell_it_out(self):
        ctx = self.ctx()
        ctx.git = lambda args, cwd: None
        for cmd, opt in (("git -C $W commit -m x", "-C"), ('git -C "$W" push origin HEAD:refs/heads/x', "-C"),
                         ("git --git-dir=$G commit -m x", "--git-dir"), ("git --work-tree `pwd` commit", "--work-tree"),
                         ("git -C $W merge feature/y", "-C"), ("git -C $W reset --hard", "-C")):
            with self.subTest(cmd=cmd):
                r = guard.decide({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "."}, ctx)
                self.assertIsInstance(r, Block)
                self.assertEqual(r.rule, "other_repo")
                self.assertIn(f"Why: the guard does not expand shell variables in a `git {opt}` path.", r.message)
                if opt == "-C":
                    self.assertIn("Instead: write the path out in full (`git -C /abs/path <sub> ...`); a rewritten "
                                  "command is checked afresh, so this is not a retry.", r.message)
                else:
                    self.assertIn(f"Instead: drop {opt} and run `git -C /abs/path <sub> ...` with the path written "
                                  "out in full; a rewritten command is checked afresh, so this is not a retry.",
                                  r.message)
                self.assertNotIn("Relax this rule", r.message)
                self.assertNotIn("no github origin", r.message)
        r = guard.decide({"tool_name": "Bash", "tool_input": {"command": "git -C /nowhere commit -m x"}, "cwd": "."},
                         ctx)
        self.assertIn("no github origin", r.message)
        self.assertIn("Relax this rule (not recommended): set guard.other_repo: off", r.message)
        # with other_repo off the unknown path is still a possible protected branch; with both off it passes
        def off(*names: str) -> GuardContext:
            c = GuardContext(write_ctx=writes.GuardContext(
                origin_repo="acme/app", allowed_repos=("acme/app",), protected_branches=("main",),
                rules={r: r not in names for r in guard.RULES}))
            c.git = lambda args, cwd: None
            return c
        ev = {"tool_name": "Bash", "tool_input": {"command": "git -C $W merge x"}, "cwd": "."}
        r = guard.decide(ev, off("other_repo"))
        self.assertIsInstance(r, Block)
        self.assertEqual(r.rule, "protected_branches")
        self.assertIn("shell variables in a `git -C` path", r.message)
        self.assertNotIn("Relax this rule", r.message)
        self.assertIsNone(guard.decide(ev, off("other_repo", "protected_branches")))

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
        self.assertEqual(set(rows[1]), {"ts", "tool", "rule", "reason", "target", "input"})
        self.assertEqual((rows[1]["tool"], rows[1]["target"]), ("Write", "acme/x"))
        self.assertIsNone(rows[1]["input"])
        self.assertTrue(rows[0]["ts"].endswith("Z"))
        blocker = d / "file"
        blocker.write_text("x")
        runlog.guard_log_append(blocker / "guard.jsonl", {"rule": "force_push"})  # unwritable: no exception


class BlocklogInputTests(unittest.TestCase):
    """The guard log keeps what was blocked, redacted and capped, so a false positive can be reproduced."""

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="notme-log-"))
        self.addCleanup(shutil.rmtree, d, True)
        self.home = d / "home"
        self.home.mkdir()
        self.ctx = GuardContext(common_dir=d / "git", home=self.home, env={})
        self.log = d / "git" / "not-me" / "logs" / "guard.jsonl"

    def secret_file(self, name, value):
        (self.home / name).write_text(value + "\n", encoding="utf-8")

    def logged(self, tool, tin):
        guard.log_block(self.ctx, Block("force_push", "r", tool=tool), tin)
        return json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])["input"]

    def test_bash_logs_the_command(self):
        self.assertEqual(self.logged("Bash", {"command": "git push --force origin main"}),
                         "git push --force origin main")

    def test_file_tools_log_the_path_never_the_body(self):
        for tool, tin in (("Write", {"file_path": "/x/a.py", "content": "BODY"}),
                          ("Edit", {"file_path": "/x/a.py", "old_string": "BODY", "new_string": "BODY"}),
                          ("MultiEdit", {"file_path": "/x/a.py", "edits": [{"old_string": "BODY"}]}),
                          ("Read", {"file_path": "/x/a.py"})):
            with self.subTest(tool=tool):
                self.assertEqual(self.logged(tool, tin), "/x/a.py")
        self.assertEqual(self.logged("NotebookEdit", {"notebook_path": "/x/n.ipynb", "new_source": "BODY"}),
                         "/x/n.ipynb")
        self.assertNotIn("BODY", self.log.read_text(encoding="utf-8"))

    def test_other_tools_log_compact_json_without_bodies(self):
        got = self.logged("mcp__github__add_comment", {"owner": "acme", "repo": "x", "body": "BODY"})
        self.assertEqual(json.loads(got), {"owner": "acme", "repo": "x"})
        self.assertEqual(self.logged("Write", {"content": "BODY"}), "{}")

    def test_missing_input_falls_back_to_block_command(self):
        guard.log_block(self.ctx, Block("force_push", "r", tool="Bash", command="git push -f"))
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8"))["input"], "git push -f")

    def test_operator_token_and_live_files_are_redacted(self):
        self.secret_file(guard.OPERATOR_TOKEN, "tok3n-abcdef123456")
        self.secret_file(guard.LIVE_MARKER_NAME, "s1gnature-0011223344")
        self.secret_file(guard.LIVE_EPOCH_NAME, "ep0ch-99887766")
        got = self.logged("Bash", {"command": "echo tok3n-abcdef123456 s1gnature-0011223344 ep0ch-99887766 x"})
        self.assertEqual(got, "echo [redacted] [redacted] [redacted] x")

    def test_short_secret_file_values_are_not_used(self):
        self.secret_file(guard.OPERATOR_TOKEN, "a")
        self.assertEqual(self.logged("Bash", {"command": "cat a.txt"}), "cat a.txt")

    def test_missing_secret_files_are_skipped(self):
        self.assertEqual(self.logged("Bash", {"command": "git status"}), "git status")

    def test_confirm_and_secret_named_assignments_keep_only_the_name(self):
        cases = {
            "NOT_ME_OPERATOR_CONFIRM=abc notme x": "NOT_ME_OPERATOR_CONFIRM=[redacted] notme x",
            "GH_TOKEN=x gh pr list": "GH_TOKEN=[redacted] gh pr list",
            "export GITHUB_TOKEN='a b c'; gh api": "export GITHUB_TOKEN=[redacted]; gh api",
            'env api_key="q w" cmd': "env api_key=[redacted] cmd",
            "AWS_SECRET_ACCESS_KEY=zz aws s3 ls": "AWS_SECRET_ACCESS_KEY=[redacted] aws s3 ls",
            "DB_PASSWORD=hunter2 run": "DB_PASSWORD=[redacted] run",
            "MY_SECRET=1&&x": "MY_SECRET=[redacted]&&x",
            "tool --token=abc": "tool --token=[redacted]",
            "FOO=bar BRANCH=main x": "FOO=bar BRANCH=main x",
            "export GH_TOKEN+=more x": "export GH_TOKEN+=[redacted] x",
            "TOKENS[0]=abc x": "TOKENS[0]=[redacted] x",
            'TOKENS[$i]+="a b" x': "TOKENS[$i]+=[redacted] x",
            "FOO[1]=bar BAR+=baz x": "FOO[1]=bar BAR+=baz x",
            "NOT_ME_OPERATOR_CONFIRM=$(cat f) notme x": "NOT_ME_OPERATOR_CONFIRM=[redacted] notme x",
            "GH_TOKEN=`cat f` gh x": "GH_TOKEN=[redacted] gh x",
            "GH_TOKEN=a$(cat f)b gh x": "GH_TOKEN=[redacted] gh x",
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.logged("Bash", {"command": cmd}), want)

    def test_unclosed_quoted_secret_values_are_redacted(self):
        cases = {
            'TOKEN="abc def': "TOKEN=[redacted]",
            "TOKEN='abc def": "TOKEN=[redacted]",
            "TOKEN=$'abc def": "TOKEN=[redacted]",
            "TOKEN=$(cat f": "TOKEN=[redacted]",
            'TOKEN="multi\nline" x': "TOKEN=[redacted] x",
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.logged("Bash", {"command": cmd}), want)

    def test_quoted_secret_longer_than_both_cuts_is_redacted(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                cmd = "TOKEN=" + quote + "S" * (guard._LOG_SCAN + guard._LOG_SCAN_MARGIN + 100) + quote + " ; echo"
                got = self.logged("Bash", {"command": cmd})
                self.assertNotIn("SSSS", got)
                self.assertTrue(got.startswith("TOKEN=[redacted]"))

    def test_space_separated_secret_flags_are_redacted(self):
        cases = {
            "mysql --password hunter2hunter2 db": "mysql --password [redacted] db",
            "tool --api-token 'a b' x": "tool --api-token [redacted] x",
            "gh secret set FOO --body s3cr3tVALUE123": "gh secret set FOO --body [redacted]",
            "gh secret set FOO -b s3cr3t -R acme/app": "gh secret set FOO -b [redacted] -R acme/app",
            "gh secret set FOO --body=s3cr3t": "gh secret set FOO --body=[redacted]",
            "curl -u user:hunter2hunter2 https://x": "curl -u user:[redacted] https://x",
            "curl --user 'user:pw x' https://x": "curl --user 'user:[redacted] https://x",
            "gh pr comment 1 -b hello": "gh pr comment 1 -b hello",
            "curl -u user https://x": "curl -u user https://x",
            "git commit --author me -m msg": "git commit --author me -m msg",
            # a flag with no value before the secret one must not swallow it
            "curl -sS -u alice:hunter2pw https://x": "curl -sS -u alice:[redacted] https://x",
            "curl -k --password hunter2pw https://x": "curl -k --password [redacted] https://x",
            "tool --verbose --api-token abcdef123 x": "tool --verbose --api-token [redacted] x",
            "gh secret set NAME --no-x -b VALUESECRET": "gh secret set NAME --no-x -b [redacted]",
            # hyphenated key flags, values glued to the short flag, here-strings
            "tool --api-key abcdef123 x": "tool --api-key [redacted] x",
            "tool --private-key abcdef123 x": "tool --private-key [redacted] x",
            "gh secret set NAME -bVALUESECRET": "gh secret set NAME -b[redacted]",
            "curl -ualice:hunter2pw https://x": "curl -ualice:[redacted] https://x",
            "gh secret set NAME <<< VALUESECRET": "gh secret set NAME <<< [redacted]",
            "gh secret set NAME<<<VALUESECRET": "gh secret set NAME<<<[redacted]",
            # a secret inside the quoted value of a harmless flag or assignment
            'bash -c "curl --password hunter2 https://x"': 'bash -c "curl --password [redacted] https://x"',
            "sh -c 'curl -u alice:hunter2 https://x'": "sh -c 'curl -u alice:[redacted] https://x'",
            'X="a GH_TOKEN=hunter2 b"': 'X="a GH_TOKEN=[redacted] b"',
            # repo flag before the subcommand, combined short flags, a -u value without a colon
            "gh secret -R o/r set X -b VALUESECRET": "gh secret -R o/r set X -b [redacted]",
            "gh secret --repo o/r set X --body VALUESECRET": "gh secret --repo o/r set X --body [redacted]",
            "curl -sSu alice:hunter2pw https://x": "curl -sSu alice:[redacted] https://x",
            'curl -u "alice --password hunter2pw': 'curl -u "alice --password [redacted]',
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.logged("Bash", {"command": cmd}), want)

    def test_hyphenated_secret_json_keys_are_redacted(self):
        got = json.loads(self.logged("mcp__x__call", {"api-key": "abcdef123", "private-key": "x", "name": "n"}))
        self.assertEqual(got, {"api-key": "[redacted]", "private-key": "[redacted]", "name": "n"})

    def test_auth_headers_and_github_token_shapes(self):
        tok = "ghp_" + "A1" * 18
        cases = {
            'curl -H "Authorization: Bearer abc.def" u': 'curl -H "Authorization: Bearer [redacted]" u',
            "curl -H 'authorization: token xyz' u": "curl -H 'authorization: token [redacted]' u",
            f"echo {tok} done": "echo [redacted] done",
            "echo github_pat_" + "x" * 30 + " done": "echo [redacted] done",
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.logged("Bash", {"command": cmd}), want)

    def test_long_input_is_capped_with_a_marker(self):
        got = self.logged("Bash", {"command": "x" * (guard.LOG_INPUT_MAX + 50)})
        self.assertEqual(len(got), guard.LOG_INPUT_MAX + len(guard.LOG_TRUNCATED))
        self.assertTrue(got.endswith(guard.LOG_TRUNCATED))
        self.assertEqual(self.logged("Bash", {"command": "x" * guard.LOG_INPUT_MAX}), "x" * guard.LOG_INPUT_MAX)

    def test_no_secret_survives_the_cut(self):
        secret = "tok3n-abcdef123456"
        self.secret_file(guard.OPERATOR_TOKEN, secret)
        cmd = "x" * (guard.LOG_INPUT_MAX - 6) + secret + " GH_TOKEN=supersecretvalue"
        got = self.logged("Bash", {"command": cmd})
        self.assertTrue(got.endswith(guard.LOG_TRUNCATED))
        for part in (secret[:6], "supersecret"):
            self.assertNotIn(part, got)

    def test_nested_bodies_are_dropped_at_every_depth(self):
        got = self.logged("mcp__github__push_files", {
            "owner": "acme", "repo": "x", "files": [{"path": "a.py", "content": "BODY"}],
            "review": {"comments": [{"path": "b.py", "line": 3, "body": "BODY"}]}})
        self.assertEqual(json.loads(got), {"owner": "acme", "repo": "x", "files": [{"path": "a.py"}],
                                           "review": {"comments": [{"line": 3, "path": "b.py"}]}})
        got = self.logged("mcp__github__create_pull_request_review",
                          {"pullNumber": 5, "comments": [{"path": "b.py", "body": "BODY"}]})
        self.assertEqual(json.loads(got), {"pullNumber": 5, "comments": [{"path": "b.py"}]})

    def test_secret_named_json_keys_are_redacted(self):
        got = self.logged("mcp__x__call", {"token": "abc", "nested": [{"api_key": "def", "name": "n"}],
                                           "password": {"inner": "v"}})
        self.assertEqual(json.loads(got), {"token": guard.REDACTED, "password": guard.REDACTED,
                                           "nested": [{"api_key": guard.REDACTED, "name": "n"}]})

    def test_url_credentials_are_redacted(self):
        long_tok = "x" * 20
        cases = {
            "git clone https://user:s3cret@gitlab.example/x": "git clone https://user:[redacted]@gitlab.example/x",
            "curl ftp://u:p@h/f": "curl ftp://u:[redacted]@h/f",
            f"git clone https://{long_tok}@github.com/a/b": "git clone https://[redacted]@github.com/a/b",
            "git clone ssh://git@github.com/a/b": "git clone ssh://git@github.com/a/b",
            "curl https://example.com/a@b": "curl https://example.com/a@b",
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.logged("Bash", {"command": cmd}), want)

    def test_mixed_quote_assignment_values_are_redacted_whole(self):
        cases = {
            'TOKEN=abc"def ghi" run': "TOKEN=[redacted] run",
            "TOKEN=$'a b' run": "TOKEN=[redacted] run",
            "TOKEN='a b'\"c d\"e run": "TOKEN=[redacted] run",
            'X=1 GH_TOKEN="a"b\'c d\' cmd': "X=1 GH_TOKEN=[redacted] cmd",
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.logged("Bash", {"command": cmd}), want)

    def test_long_padded_command_is_logged_fast(self):
        # a run like a.a.a. once made the URL scheme patterns quadratic: 100k chars took minutes, past the hook
        # timeout, and a hook that times out does not block
        for pad in ("a.", "a-", "a+"):
            with self.subTest(pad=pad):
                cmd = "git push --force origin main # " + pad * 50000
                start = time.monotonic()
                got = self.logged("Bash", {"command": cmd})
                self.assertLess(time.monotonic() - start, 2.0)
                self.assertTrue(got.startswith("git push --force origin main # "))
                self.assertTrue(got.endswith(guard.LOG_TRUNCATED))
                self.assertEqual(len(got), guard.LOG_INPUT_MAX + len(guard.LOG_TRUNCATED))

    def test_redact_is_linear_on_scheme_like_runs(self):
        for pad in ("a.", "a-", "a+", "a1", "a[", "a=$(", "a=`", "a='", 'a="'):
            with self.subTest(pad=pad):
                text = pad * 20000 + "://u:p"
                start = time.monotonic()
                guard._redact(text, [])
                self.assertLess(time.monotonic() - start, 2.0)

    def test_secret_straddling_the_scan_cut_never_shows_in_part(self):
        # redacting shrinks the text, so the kept part can reach the place where the input was cut before redacting
        secret = "tok3n-abcdef123456"
        self.secret_file(guard.OPERATOR_TOKEN, secret)
        for cut in (guard._LOG_SCAN, guard._LOG_SCAN + guard._LOG_SCAN_MARGIN):
            for split in (1, 5, len(secret) - 1):
                with self.subTest(cut=cut, split=split):
                    head = "TOKEN=" + "v" * (cut - split - 7) + " "
                    got = self.logged("Bash", {"command": head + secret + " tail"})
                    self.assertIn(got, ("TOKEN=[redacted]" + guard.LOG_TRUNCATED,
                                        "TOKEN=[redacted] " + guard.LOG_TRUNCATED))
        tok = "ghp_" + "A1" * 18
        head = "TOKEN=" + "v" * (guard._LOG_SCAN - 10 - 7) + " "
        got = self.logged("Bash", {"command": head + tok + " tail"})
        self.assertNotIn("ghp_", got)

    def test_extraction_failure_never_raises(self):
        with mock.patch.object(guard, "_redact", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.logged("Bash", {"command": "git push -f"}))


class ModuleNamesTests(unittest.TestCase):
    """A second module-level binding of a name silently replaces the first for every caller (a log regex once
    shadowed the parser's assignment regex), so guard.py binds each top-level name once."""

    def test_no_top_level_name_is_bound_twice(self):
        import ast
        tree = ast.parse(Path(guard.__file__).read_text(encoding="utf-8"))
        seen, dupes = set(), []
        for node in tree.body:
            names = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            for name in names:
                if name in seen:
                    dupes.append(name)
                seen.add(name)
        self.assertEqual(dupes, [])


class DoctorFixBoundaryTests(_Fixtures):
    """The one write /not-me:doctor makes (the Claude Code settings `env` key) passes the guard, while the
    not-me global config stays blocked during an active run, so a blocked config fix is reported, not retried."""

    def decide(self, tool, tin, variant="running"):
        ctx = self.ctx(variant)
        self.assertTrue(guard.is_active(ctx))
        cwd, _ = gc.variant_paths(self.root, variant)
        return guard.decide({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tin,
                             "cwd": str(cwd), "session_id": gc.SESSION}, ctx)

    def test_settings_json_edit_is_allowed_during_a_run(self):
        _, home = gc.variant_paths(self.root, "running")
        settings = str(home.parent / "settings.json")  # NOT_ME_HOME is ~/.claude/not-me; settings sit beside it
        key = '"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "3"'
        for tool, tin in (("Edit", {"file_path": settings, "old_string": '"env": {', "new_string": '"env": {' + key}),
                          ("Write", {"file_path": settings, "content": '{"env": {' + key + "}}\n"})):
            with self.subTest(tool=tool):
                self.assertIsNone(self.decide(tool, tin))

    def test_global_config_edit_is_blocked_during_a_run(self):
        _, home = gc.variant_paths(self.root, "running")
        for tool, tin in (("Edit", {"file_path": str(home / "project.md"), "old_string": "# Project",
                                    "new_string": "# Project notes"}),
                          ("Write", {"file_path": str(home / "project.md"), "content": "---\nschema: 1\n---\n"})):
            with self.subTest(tool=tool):
                result = self.decide(tool, tin)
                self.assertIsInstance(result, Block)
                self.assertEqual(result.rule, "self_protect")


class WorktreeRunTests(unittest.TestCase):
    """A run in any worktree of a checkout makes the guard active, and the config read-only, in every worktree;
    found from the git common dir alone, without reading config."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="notme-guard-wt-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.main = self.tmp / "main"
        self.main.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "i"],
                     ["worktree", "add", "-q", "-b", "a", str(self.tmp / "wt-a")],
                     ["worktree", "add", "-q", "-b", "b", str(self.tmp / "wt-b")]):
            subprocess.run(git + args, cwd=str(self.main), check=True, capture_output=True)
        self.home = self.tmp / "home"
        self.home.mkdir()

    def ctx(self, where):
        def no_git(args, cwd):
            raise AssertionError(f"git {args} called")
        return GuardContext.from_project(where, home=self.home, env={}, git=no_git)

    def test_a_run_in_one_worktree_activates_the_guard_in_all(self):
        for where in (self.main, self.tmp / "wt-a", self.tmp / "wt-b"):
            self.assertFalse(guard.is_active(self.ctx(where)))
        logs = runlog.log_root(self.main, None, {})
        log = runlog.RunLog.start(logs, "new", "r", project_dir=self.tmp / "wt-b")
        for where in (self.main, self.tmp / "wt-a", self.tmp / "wt-b"):
            with self.subTest(where=where.name):
                ctx = self.ctx(where)
                self.assertTrue(ctx.active_run)
                self.assertTrue(guard.is_active(ctx))
        log.finish("done")
        self.assertFalse(guard.is_active(self.ctx(self.tmp / "wt-a")))

    def test_a_stale_run_in_a_linked_worktree_does_not_activate(self):
        marker = runlog.default_marker_path(self.tmp / "wt-a")
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"run_id": "old", "started": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
        self.assertFalse(guard.is_active(self.ctx(self.main)))

    def test_main_checkout_config_is_read_only_from_a_worktree_with_its_own_copy(self):
        for where in (self.main, self.tmp / "wt-a"):
            cfg = where / ".claude" / "not-me"
            cfg.mkdir(parents=True)
            (cfg / "project.md").write_text("---\nschema: 1\n---\n# Project\n", encoding="utf-8")
        runlog.RunLog.start(runlog.log_root(self.main, None, {}), "new", "r", project_dir=self.tmp / "wt-b")
        ctx = self.ctx(self.tmp / "wt-a")
        for target in (self.main, self.tmp / "wt-a"):
            with self.subTest(target=target.name):
                event = {"tool_name": "Edit", "cwd": str(self.tmp / "wt-a"),
                         "tool_input": {"file_path": str(target / ".claude" / "not-me" / "project.md"),
                                        "old_string": "# Project", "new_string": "# Project x"}}
                block = guard.decide(event, ctx)
                self.assertIsInstance(block, Block)
                self.assertEqual(block.rule, "self_protect")


class HookTests(_Fixtures):
    def event(self, tool, tin, variant="app"):
        cwd, _ = gc.variant_paths(self.root, variant)
        return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tin, "cwd": str(cwd)})

    def test_block_logs_and_exits_2(self):
        proc = self.hook(self.event("Bash", {"command": "gh pr comment 1 -R acme/other -b x"}))
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"/not-me:allow-repo acme/other", proc.stderr)
        log = self.root / "app" / ".git" / "not-me" / "logs" / "guard.jsonl"
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["target"], "acme/other")
        self.assertEqual(row["input"], "gh pr comment 1 -R acme/other -b x")

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
