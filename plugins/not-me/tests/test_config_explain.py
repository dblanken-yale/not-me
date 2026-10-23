from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from notme import config, frontmatter, paths
from tests import PLUGIN_ROOT, TempCase

DOCS = ("project", "standards", "verify", "review", "prove")


def line_of(path: Path, prefix: str) -> int:
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith(prefix):
            return i
    raise AssertionError(f"{prefix!r} not in {path}")


class DefaultsTests(unittest.TestCase):
    def test_defaults_come_only_from_default_files(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        cfg = tc.load()
        for name in DOCS:
            with self.subTest(name):
                meta, body = frontmatter.parse((PLUGIN_ROOT / "defaults" / f"{name}.md").read_text(encoding="utf-8"), source=name)
                self.assertEqual(cfg.meta[name], meta)
                self.assertEqual(cfg.bodies[name], body)
        self.assertEqual(cfg.check(), [])

    def test_meta_access_by_doc_name(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        self.assertEqual(tc.load().meta["project"]["labels"]["needs_review"], "needs review")

    def test_nonexistent_project_dir_is_absent_repo_layer(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        cfg = config.load(project_dir=str(tc.root / "missing"), home=str(tc.home), plugin_root=str(PLUGIN_ROOT))
        self.assertIsNone(cfg.layers.dirs["repo"])
        json.dumps(cfg.explain())


class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.tc = TempCase("L7")
        self.addCleanup(self.tc.cleanup)
        self.tc.write("repo", "standards", "---\nextends: global\n---\n## Extra\n\nx\n")
        self.tc.write("global", "standards", "---\nbranch: \"{issue}\"\n---\n## Branch\n\ng\n")
        self.cfg = self.tc.load()
        self.out = self.cfg.explain()

    def test_no_absolute_paths(self):
        text = json.dumps(self.out)
        # Built from parts so the repo-wide absolute-path grep stays clean.
        absolute_roots = ["/" + "Users/", "/" + "home/", "C:" + "\\\\"]
        for needle in absolute_roots + [str(self.tc.root), str(PLUGIN_ROOT)]:
            self.assertNotIn(needle, text)

    def test_paths_are_display_forms(self):
        self.assertEqual(self.out["keys"]["project.allow_repos"]["source"]["path"], ".claude/not-me/project.md")
        self.assertEqual(self.out["keys"]["project.mode"]["source"]["path"], "${CLAUDE_PLUGIN_ROOT}/defaults/project.md")
        self.assertEqual(self.out["keys"]["standards.branch"]["source"]["path"], "$NOT_ME_HOME/standards.md")

    def test_line_numbers(self):
        default_project = PLUGIN_ROOT / "defaults" / "project.md"
        keys = self.out["keys"]
        self.assertEqual(keys["project.mode"]["source"]["line"], line_of(default_project, "mode:"))
        self.assertEqual(keys["project.queue.pick_label"]["source"]["line"], line_of(default_project, "  pick_label:"))
        self.assertEqual(keys["project.guard.force_push"]["source"]["line"], line_of(default_project, "guard:"))
        self.assertEqual(keys["project.allow_repos"]["source"]["line"], 4)
        self.assertEqual(keys["standards.branch"]["source"]["line"], 2)

    def test_union_lists_contributors(self):
        entry = self.out["keys"]["project.protected_branches"]
        self.assertEqual([c["layer"] for c in entry["contributors"]], ["default", "global", "repo"])
        self.assertEqual(entry["value"], ["main", "master", "develop", "release", "trunk", "hotfix"])

    def test_bodies_section(self):
        self.assertEqual(self.out["bodies"]["standards"], {"layers_used": ["global", "repo"],
                                                           "sections": {"Branch": "global", "Extra": "repo"}})

    def test_shadowed_values(self):
        self.tc.write("global", "project", "---\nmode: live\n---\n")
        self.tc.write("repo", "project", "---\nmode: dry-run\n---\n")
        entry = self.tc.load().explain()["keys"]["project.mode"]
        self.assertEqual(entry["source"]["layer"], "repo")
        self.assertEqual(entry["shadowed"], [{"layer": "default", "value": "dry-run"}, {"layer": "global", "value": "live"}])


class CheckTests(unittest.TestCase):
    def run_check(self, text, layer="repo", name="project"):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        tc.write(layer, name, text)
        return tc.load().check()

    def test_type_errors(self):
        cases = [
            ("mode: maybe\n", "mode"),
            ("models: {strategy: solo}\n", "models.strategy"),
            ("labels: {todo: 3}\n", "labels.todo"),
            ("repos: [{path: x}]\n", "repos"),
            ("enabled: yes\n", "enabled"),
            ("limits: {fix_loop: three}\n", "limits.fix_loop"),
            ("protected_branches: main\n", "protected_branches"),
            ("schema: 7\n", "schema"),
            ("guard: {force_push: maybe}\n", "guard.force_push"),
        ]
        for text, key in cases:
            with self.subTest(key):
                errors = self.run_check("---\n" + text + "---\n")
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(key, errors[0])
                self.assertTrue(errors[0].startswith("repo .claude/not-me/project.md:2: "), errors[0])

    def test_valid_overrides_pass(self):
        errors = self.run_check("---\nmode: live\nrepos: [{name: acme/app}, {name: acme/app-ui, role: companion}]\n"
                                "models: {strategy: single}\nlimits: {fix_loop: 2}\n---\n")
        self.assertEqual(errors, [])

    def test_unknown_schema_ignores_file(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        tc.write("repo", "project", "---\nschema: 2\nmode: live\n---\n")
        cfg = tc.load()
        self.assertEqual(cfg.meta["project"]["mode"], "dry-run")
        self.assertTrue(any("schema" in w for w in cfg.warnings))


class PathsTests(unittest.TestCase):
    def test_home_dir(self):
        self.assertEqual(paths.home_dir({"NOT_ME_HOME": "/x/y"}), Path("/x/y"))
        self.assertEqual(paths.home_dir({}).parts[-2:], (".claude", "not-me"))

    def test_plugin_root(self):
        self.assertEqual(paths.plugin_root({"CLAUDE_PLUGIN_ROOT": "/p"}), Path("/p"))
        self.assertEqual(paths.plugin_root({}), PLUGIN_ROOT)

    def test_project_dir_precedence(self):
        self.assertEqual(paths.project_dir("/a", {"CLAUDE_PROJECT_DIR": "/b"}), Path("/a"))
        self.assertEqual(paths.project_dir(None, {"CLAUDE_PROJECT_DIR": "/b"}), Path("/b"))

    def test_display_path(self):
        home = Path.home() / ".claude" / "not-me"
        self.assertEqual(paths.display_path(home / "project.md", Path("/r"), home), "~/.claude/not-me/project.md")
        self.assertEqual(paths.display_path(Path("/r/.claude/not-me/a.md"), Path("/r"), home), ".claude/not-me/a.md")
        self.assertEqual(paths.display_path(Path("/elsewhere/q/a.md"), Path("/r"), Path("/h")), ".../a.md")


@unittest.skipUnless(shutil.which("git"), "git not installed")
class WorktreeTrustTests(unittest.TestCase):
    def git(self, cwd, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        subprocess.run(["git", "-c", "user.name=octocat", "-c", "user.email=octocat@example.com",
                        "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                       cwd=cwd, env=env, check=True, capture_output=True)

    def test_repo_layer_read_from_main_checkout(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        self.git(tc.project, "init", "-q")
        tc.write("repo", "project", "---\ntracker: acme/real\n---\n")
        self.git(tc.project, "add", ".")
        self.git(tc.project, "commit", "-q", "-m", "init")
        wt = tc.root / "wt"
        self.git(tc.project, "worktree", "add", "-q", str(wt))
        (wt / ".claude" / "not-me" / "project.md").write_text("---\ntracker: acme/evil\n---\n")
        self.assertTrue((wt / ".git").is_file())
        cfg = config.load(project_dir=str(wt), home=str(tc.home), plugin_root=str(PLUGIN_ROOT))
        self.assertEqual(cfg.meta["project"]["tracker"], "acme/real")
        self.assertEqual(paths.git_common_dir(wt), (tc.project / ".git").resolve())

    def test_submodule_repo_layer_read_from_its_own_checkout(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        lib = tc.root / "lib"
        lib.mkdir()
        self.git(lib, "init", "-q")
        (lib / ".claude" / "not-me").mkdir(parents=True)
        (lib / ".claude" / "not-me" / "project.md").write_text("---\ntracker: acme/sub\n---\n")
        self.git(lib, "add", ".")
        self.git(lib, "commit", "-q", "-m", "init")
        self.git(tc.project, "init", "-q")
        self.git(tc.project, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "sub")
        sub = tc.project / "sub"
        self.assertTrue((sub / ".git").is_file())
        self.assertEqual(paths.main_checkout(sub), sub.resolve())
        cfg = config.load(project_dir=str(sub), home=str(tc.home), plugin_root=str(PLUGIN_ROOT))
        self.assertEqual(cfg.meta["project"]["tracker"], "acme/sub")
        self.assertEqual(cfg.sources["project.tracker"]["layer"], "repo")
        # A linked worktree of the submodule maps back to the submodule checkout.
        sub_wt = tc.root / "sub-wt"
        self.git(sub, "worktree", "add", "-q", str(sub_wt))
        self.assertEqual(paths.main_checkout(sub_wt), sub.resolve())


if __name__ == "__main__":
    unittest.main()


class ReviewBlockOnTests(unittest.TestCase):
    def setUp(self):
        self.tc = TempCase("block_on")
        self.addCleanup(self.tc.cleanup)

    def review(self, layer, value):
        self.tc.write(layer, "review", f"---\nschema: 1\nblock_on: {value}\n---\n# Review\n")

    def test_default_matches_the_old_fixed_rule(self):
        cfg = self.tc.load()
        self.assertEqual(cfg.meta["review"]["block_on"], ["blocker", "major"])
        self.assertEqual(cfg.check(), [])

    def test_global_list_may_add_minor(self):
        self.review("global", "[blocker, major, minor]")
        cfg = self.tc.load()
        self.assertEqual(cfg.meta["review"]["block_on"], ["blocker", "major", "minor"])
        self.assertEqual(cfg.check(), [])

    def test_repo_cannot_remove_major(self):
        self.review("repo", "[blocker]")
        cfg = self.tc.load()
        self.assertEqual(cfg.meta["review"]["block_on"], ["blocker", "major"])
        self.assertTrue(any("block_on" in w and "only tighten" in w for w in cfg.warnings), cfg.warnings)
        ignored = cfg.explain()["keys"]["review.block_on"]["ignored"]
        self.assertEqual(ignored[0]["layer"], "repo")

    def test_repo_cannot_remove_what_global_added(self):
        self.review("global", "[blocker, major, minor]")
        self.review("repo", "[blocker, major]")
        cfg = self.tc.load()
        self.assertEqual(cfg.meta["review"]["block_on"], ["blocker", "major", "minor"])
        self.assertTrue(any("only tighten" in w for w in cfg.warnings), cfg.warnings)

    def test_repo_may_add_minor(self):
        self.review("repo", "[blocker, major, minor]")
        cfg = self.tc.load()
        self.assertEqual(cfg.meta["review"]["block_on"], ["blocker", "major", "minor"])
        self.assertFalse(any("block_on" in w for w in cfg.warnings), cfg.warnings)

    def test_unknown_severity_is_an_error(self):
        self.review("global", "[blocker, major, nit]")
        errors = self.tc.load().check()
        self.assertTrue(any("block_on" in e and "nit" in e for e in errors), errors)

    def test_list_without_blocker_is_an_error(self):
        self.review("global", "[major]")
        errors = self.tc.load().check()
        self.assertTrue(any("block_on must include blocker" in e for e in errors), errors)
