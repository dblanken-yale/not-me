from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notme import git
from notme.gh import WriteBlocked
from notme.git import GitError, ProtectedBranchError
from notme.writes import GuardContext


def sh(*args, cwd=None) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class Repo(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="notme-git-")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.remote = self.dir / "origin.git"
        sh("init", "-q", "--bare", "-b", "main", str(self.remote))
        self.work = self.dir / "work"
        sh("init", "-q", "-b", "main", str(self.work))
        sh("config", "user.name", "Test", cwd=self.work)
        sh("config", "user.email", "test@example.com", cwd=self.work)
        sh("config", "commit.gpgsign", "false", cwd=self.work)
        sh("remote", "add", "origin", str(self.remote), cwd=self.work)
        self.commit("a.txt", "one\n", "first")
        sh("push", "-q", "origin", "main", cwd=self.work)

    def commit(self, name, text, msg, cwd=None):
        cwd = cwd or self.work
        (Path(cwd) / name).write_text(text, encoding="utf-8")
        sh("add", name, cwd=cwd)
        sh("commit", "-q", "-m", msg, cwd=cwd)


class BasicTests(Repo):
    def test_branch_clean_origin(self):
        self.assertEqual(git.current_branch(cwd=self.work), "main")
        self.assertTrue(git.is_clean(cwd=self.work))
        (self.work / "new.txt").write_text("x", encoding="utf-8")
        self.assertFalse(git.is_clean(cwd=self.work))
        self.assertEqual(git.origin_url(cwd=self.work), str(self.remote))

    def test_detached_head_has_no_branch(self):
        sh("checkout", "-q", "--detach", cwd=self.work)
        self.assertIsNone(git.current_branch(cwd=self.work))

    def test_origin_url_none_without_origin(self):
        sh("remote", "remove", "origin", cwd=self.work)
        self.assertIsNone(git.origin_url(cwd=self.work))

    def test_git_error_carries_details(self):
        with self.assertRaises(GitError) as cm:
            git.is_clean(cwd=self.dir)
        self.assertNotEqual(cm.exception.returncode, 0)
        self.assertEqual(cm.exception.cmd, ["status", "--porcelain"])
        self.assertTrue(cm.exception.stderr)
        self.assertNotIn(cm.exception.stderr.strip(), str(cm.exception))

    def test_default_branch_prefers_origin_head(self):
        sh("branch", "trunk", cwd=self.work)
        sh("push", "-q", "origin", "trunk", cwd=self.work)
        sh("fetch", "-q", "origin", cwd=self.work)
        sh("remote", "set-head", "origin", "trunk", cwd=self.work)
        self.assertEqual(git.default_branch(cwd=self.work), "trunk")

    def test_default_branch_falls_back_to_main(self):
        self.assertEqual(git.default_branch(cwd=self.work), "main")

    def test_default_branch_falls_back_to_gh(self):
        sh("remote", "set-url", "origin", "git@github.com:acme/app.git", cwd=self.work)
        seen = []

        class Gh:
            def repo_default_branch(self, repo):
                seen.append(repo)
                return "develop"

        self.assertEqual(git.default_branch(gh=Gh(), cwd=self.work), "develop")
        self.assertEqual(seen, ["acme/app"])

    def test_default_branch_origin_slug_skips_remote_lookup(self):
        class Gh:
            def repo_default_branch(self, repo):
                return f"for-{repo}"

        self.assertEqual(git.default_branch(gh=Gh(), origin_slug="acme/app", cwd=self.work), "for-acme/app")

    def test_git_runs_with_c_locale(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            git.is_clean(cwd=self.work)
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_default_branch_gh_failure_falls_to_main(self):
        from notme.gh import GhError
        sh("remote", "set-url", "origin", "https://github.com/acme/app.git", cwd=self.work)

        class Gh:
            def repo_default_branch(self, repo):
                raise GhError(["repo", "view"], 1, "nope")

        self.assertEqual(git.default_branch(gh=Gh(), cwd=self.work), "main")

    def test_fetch(self):
        other = self.dir / "other"
        sh("clone", "-q", str(self.remote), str(other))
        sh("config", "user.name", "T", cwd=other)
        sh("config", "user.email", "t@example.com", cwd=other)
        self.commit("b.txt", "two\n", "second", cwd=other)
        sh("push", "-q", "origin", "main", cwd=other)
        git.fetch(cwd=self.work)
        self.assertEqual(sh("rev-parse", "origin/main", cwd=self.work), sh("rev-parse", "HEAD", cwd=other))

    def test_checkout_no_track(self):
        sh("fetch", "-q", "origin", cwd=self.work)
        git.checkout_no_track("feat/x", "origin/main", cwd=self.work)
        self.assertEqual(git.current_branch(cwd=self.work), "feat/x")
        proc = subprocess.run(["git", "config", "branch.feat/x.remote"], cwd=str(self.work), capture_output=True)
        self.assertNotEqual(proc.returncode, 0)

    def test_stash_roundtrip(self):
        self.assertIsNone(git.stash_by_sha(cwd=self.work))
        (self.work / "a.txt").write_text("changed\n", encoding="utf-8")
        (self.work / "untracked.txt").write_text("u\n", encoding="utf-8")
        sha = git.stash_by_sha(cwd=self.work)
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertTrue(git.is_clean(cwd=self.work))
        git.stash_restore(sha, cwd=self.work)
        self.assertEqual((self.work / "a.txt").read_text(), "changed\n")
        self.assertTrue((self.work / "untracked.txt").exists())
        self.assertEqual(sh("stash", "list", cwd=self.work), "")

    def test_stash_restore_rejects_non_sha(self):
        with self.assertRaises(ValueError):
            git.stash_restore("--all", cwd=self.work)

    def test_worktree_add_and_remove(self):
        sha = sh("rev-parse", "HEAD", cwd=self.work)
        wt = self.dir / "wt"
        git.worktree_add_detached(wt, sha, cwd=self.work)
        self.assertTrue((wt / "a.txt").exists())
        self.assertIsNone(git.current_branch(cwd=wt))
        git.worktree_remove(wt, cwd=self.work)
        self.assertFalse(wt.exists())
        with self.assertRaises(ValueError):
            git.worktree_add_detached(wt, "main; rm", cwd=self.work)


class MergeTests(Repo):
    def test_clean_merge_returns_false(self):
        sh("checkout", "-q", "-b", "feat", cwd=self.work)
        self.commit("f.txt", "f\n", "feat")
        sh("checkout", "-q", "main", cwd=self.work)
        self.commit("m.txt", "m\n", "main work")
        sh("checkout", "-q", "feat", cwd=self.work)
        self.assertFalse(git.merge_base_into_current("main", cwd=self.work))
        self.assertTrue((self.work / "m.txt").exists())
        self.assertEqual(sh("log", "-1", "--format=%P", cwd=self.work).count(" "), 1)  # a merge commit

    def test_conflict_returns_true(self):
        sh("checkout", "-q", "-b", "feat", cwd=self.work)
        self.commit("a.txt", "feat side\n", "feat edit")
        sh("checkout", "-q", "main", cwd=self.work)
        self.commit("a.txt", "main side\n", "main edit")
        sh("checkout", "-q", "feat", cwd=self.work)
        self.assertTrue(git.merge_base_into_current("main", cwd=self.work))
        self.assertIn("<<<<<<<", (self.work / "a.txt").read_text())

    def test_conflict_detected_without_english_text(self):
        sh("checkout", "-q", "-b", "feat", cwd=self.work)
        self.commit("a.txt", "feat side\n", "feat edit")
        sh("checkout", "-q", "main", cwd=self.work)
        self.commit("a.txt", "main side\n", "main edit")
        sh("checkout", "-q", "feat", cwd=self.work)
        real = git._run

        def no_text(args, cwd=None, check=True):
            proc = real(args, cwd, check)
            if args[0] == "merge":
                proc.stdout = proc.stderr = "\u5408\u5e76"
            return proc

        with mock.patch.object(git, "_run", no_text):
            self.assertTrue(git.merge_base_into_current("main", cwd=self.work))

    def test_other_failure_raises(self):
        with self.assertRaises(GitError):
            git.merge_base_into_current("no-such-branch", cwd=self.work)

    def test_rejects_option_like_base(self):
        with self.assertRaises(ValueError):
            git.merge_base_into_current("--abort", cwd=self.work)


class PushTests(Repo):
    def setUp(self):
        super().setUp()
        sh("checkout", "-q", "-b", "feat/x", cwd=self.work)
        self.commit("x.txt", "x\n", "x")

    def remote_refs(self):
        return sh("for-each-ref", "--format=%(refname)", cwd=self.remote).splitlines()

    def test_dry_run_sends_nothing(self):
        ref = git.push_dry_run("feat/x", cwd=self.work)
        self.assertRegex(ref, r"^refs/heads/not-me-preflight-[0-9a-f]+$")
        self.assertEqual(self.remote_refs(), ["refs/heads/main"])

    def test_dry_run_uses_dry_run_flag_and_unique_ref(self):
        seen = []
        real = git._run

        def spy(args, cwd=None, check=True):
            seen.append(list(args))
            return real(args, cwd, check)

        with mock.patch.object(git, "_run", spy):
            a = git.push_dry_run("feat/x", cwd=self.work)
            b = git.push_dry_run("feat/x", cwd=self.work)
        self.assertNotEqual(a, b)
        for args in seen:
            self.assertIn("--dry-run", args)
            self.assertIn(f"feat/x:{a}" if args is seen[0] else f"feat/x:{b}", args)

    def ctx(self, **kw):
        base = dict(origin_repo="acme/app", allowed_repos=("acme/app",), protected_branches=("main", "develop"),
                    mode="live")
        base.update(kw)
        return GuardContext(**base)

    def gated(self, urls=("git@github.com:acme/app.git",)):
        """Pretend origin's push URL is on GitHub while the real remote is a local bare repo."""
        return mock.patch.object(git, "_push_urls", return_value=list(urls))

    def test_push_publishes_branch(self):
        with self.gated():
            git.push("feat/x", self.ctx(), cwd=self.work)
        self.assertIn("refs/heads/feat/x", self.remote_refs())

    def test_push_sends_explicit_full_refspec(self):
        with self.gated(), mock.patch.object(git, "_run") as run:
            git.push("heads/feat/x", self.ctx(), cwd=self.work)
        self.assertEqual(run.call_args.args[0], ["push", "--", "origin", "refs/heads/feat/x:refs/heads/feat/x"])

    def test_push_ignores_remote_push_config(self):
        sh("config", "remote.origin.push", "refs/heads/feat/x:refs/heads/main", cwd=self.work)
        before = sh("rev-parse", "main", cwd=self.remote)
        with self.gated():
            git.push("feat/x", self.ctx(), cwd=self.work)
        self.assertEqual(sh("rev-parse", "main", cwd=self.remote), before)
        self.assertIn("refs/heads/feat/x", self.remote_refs())

    def test_push_refuses_pushurl_to_another_repo(self):
        sh("remote", "set-url", "origin", "https://github.com/acme/app.git", cwd=self.work)
        sh("config", "remote.origin.pushurl", "https://github.com/acme/other.git", cwd=self.work)
        with mock.patch.object(git, "_run", wraps=git._run) as run:
            with self.assertRaises(WriteBlocked) as cm:
                git.push("feat/x", self.ctx(), cwd=self.work)
        self.assertEqual(cm.exception.decision.allow_cmd, "/not-me:allow-repo acme/other")
        self.assertFalse(any(c.args[0][0] == "push" for c in run.call_args_list))

    def test_push_refuses_mixed_or_unparseable_push_urls(self):
        sh("remote", "set-url", "origin", "https://github.com/acme/app.git", cwd=self.work)
        sh("remote", "set-url", "--add", "--push", "origin", "https://github.com/acme/app.git", cwd=self.work)
        sh("remote", "set-url", "--add", "--push", "origin", "https://github.com/acme/other.git", cwd=self.work)
        with self.assertRaises(GitError):
            git.push("feat/x", self.ctx(allowed_repos=("acme/app", "acme/other")), cwd=self.work)
        sh("config", "--unset-all", "remote.origin.pushurl", cwd=self.work)
        sh("config", "remote.origin.pushurl", "https://evil.example/acme/app.git", cwd=self.work)
        with self.assertRaises(GitError):
            git.push("feat/x", self.ctx(), cwd=self.work)
        self.assertEqual(self.remote_refs(), ["refs/heads/main"])

    def test_push_refuses_local_path_origin(self):
        with self.assertRaises(GitError):
            git.push("feat/x", self.ctx(), cwd=self.work)
        self.assertEqual(self.remote_refs(), ["refs/heads/main"])

    def test_push_runs_the_write_gate_itself(self):
        with self.gated(["git@github.com:acme/app.git"]):
            with self.assertRaises(WriteBlocked):
                git.push("feat/x", self.ctx(allowed_repos=("acme/else",)), cwd=self.work)
        self.assertEqual(self.remote_refs(), ["refs/heads/main"])

    def test_push_refuses_protected_branch_without_running_git(self):
        with mock.patch.object(git, "_run") as run:
            for name in ("main", "develop"):
                with self.subTest(name=name), self.assertRaises(ProtectedBranchError):
                    git.push(name, self.ctx(), cwd=self.work)
            run.assert_not_called()

    def test_push_never_passes_force_flags(self):
        with self.gated(), mock.patch.object(git, "_run") as run:
            git.push("feat/x", self.ctx(), cwd=self.work)
            git.push_dry_run("feat/x", cwd=self.work)
        for call in run.call_args_list:
            for token in call.args[0]:
                self.assertNotIn(token, ("-f", "--force", "--force-with-lease", "--force-if-includes", "--delete"))
                self.assertFalse(token.startswith("+"))
                self.assertFalse(token.startswith("--force"))

    def test_push_protected_bypass_spellings(self):
        for spelling in ("refs/heads/main", "heads/main", "HEAD", "@", "refs/heads/refs/heads/main"):
            with self.subTest(spelling=spelling), mock.patch.object(git, "_run") as run:
                with self.assertRaises((ProtectedBranchError, ValueError)):
                    git.push(spelling, self.ctx(), cwd=self.work)
                run.assert_not_called()

    def test_push_bypass_refs_heads_main_is_protected_error(self):
        for spelling in ("refs/heads/main", "heads/main"):
            with self.subTest(spelling=spelling), self.assertRaises(ProtectedBranchError):
                git.push(spelling, self.ctx(), cwd=self.work)

    def test_push_head_and_at_are_refused(self):
        for spelling in ("HEAD", "@"):
            with self.subTest(spelling=spelling), self.assertRaises(ValueError):
                git.push(spelling, self.ctx(), cwd=self.work)

    def test_push_protected_entries_are_patterns(self):
        ctx = self.ctx(protected_branches=("main", "release/*"))
        with self.gated(), mock.patch.object(git, "_run") as run:
            for name in ("release/1.0", "refs/heads/release/2.x"):
                with self.subTest(name=name), self.assertRaises(ProtectedBranchError):
                    git.push(name, ctx, cwd=self.work)
            run.assert_not_called()
            git.push("feat/x", ctx, cwd=self.work)
            run.assert_called_once()

    def test_names_with_trailing_newline_refused(self):
        for bad in ("feat/x\n", "main\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                git.push(bad, self.ctx(), cwd=self.work)
        with self.assertRaises(ValueError):
            git.stash_restore("abcdef1\n", cwd=self.work)

    def test_push_rejects_force_refspecs_and_options(self):
        for bad in ("+main", "--force", "a:b", ":main", "-d", "feat x", "feat/../x", "", "refs/tags/v1",
                    "refs/heads/HEAD"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                git.push(bad, self.ctx(), cwd=self.work)


if __name__ == "__main__":
    unittest.main()
