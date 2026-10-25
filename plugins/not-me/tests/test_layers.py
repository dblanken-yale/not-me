from __future__ import annotations

import os
import shutil
import sys
import unittest

from notme import layers
from notme.layers import Doc
from tests import LAYER_CASES, TempCase

UNION = layers.SCHEMA["project"]


class LayerCaseTests(unittest.TestCase):
    """Table-driven layer cases L1-L18 against the fixtures."""

    def case(self, name):
        tc = TempCase(name)
        self.addCleanup(tc.cleanup)
        return tc

    def test_L1_no_layers_uses_defaults_with_one_hint(self):
        cfg = self.case("L1").load()
        self.assertEqual(cfg.meta["project"]["mode"], "dry-run")
        self.assertEqual(len(cfg.warnings), 1, cfg.warnings)
        self.assertIn("defaults", cfg.warnings[0])
        self.assertIsNone(cfg.layers.dirs["global"])
        self.assertIsNone(cfg.layers.dirs["repo"])

    def test_L2_repo_only(self):
        # The repo layer may not switch to live; the fixture's `mode: live` is ignored with a warning.
        cfg = self.case("L2").load()
        self.assertEqual(cfg.meta["project"]["mode"], "dry-run")
        self.assertEqual(cfg.sources["project.mode"]["layer"], "default")
        self.assertEqual(len(cfg.warnings), 1, cfg.warnings)
        self.assertIn(".claude/not-me/project.md:3", cfg.warnings[0])

    def test_L3_global_only(self):
        tc = self.case("L3")
        tc.sign_live()
        cfg = tc.load()
        self.assertEqual(cfg.meta["project"]["mode"], "live")
        self.assertEqual(cfg.sources["project.mode"]["layer"], "global")

    def test_L4_repo_scalar_beats_global(self):
        cfg = self.case("L4").load()
        self.assertEqual(cfg.meta["project"]["tracker"], "acme/app")
        src = cfg.sources["project.tracker"]
        self.assertEqual(src["layer"], "repo")
        self.assertEqual([s["layer"] for s in src["shadowed"]], ["default", "global"])
        self.assertEqual(src["shadowed"][1]["value"], "acme/app-ui")

    def test_L5_map_merges_per_key(self):
        labels = self.case("L5").load().meta["project"]["labels"]
        self.assertEqual(labels["needs_review"], "review me")
        self.assertEqual(labels["blocked"], "stuck")
        self.assertEqual(labels["todo"], "to do")

    def test_L6_list_replaces(self):
        self.assertEqual(self.case("L6").load().meta["project"]["router"], ["update"])

    def test_L7_union_keys(self):
        cfg = self.case("L7").load()
        self.assertEqual(cfg.meta["project"]["protected_branches"], ["main", "master", "develop", "release", "trunk", "hotfix"])
        self.assertEqual(cfg.meta["project"]["allow_repos"], ["acme/app-ui"])
        contributors = cfg.sources["project.protected_branches"]["contributors"]
        self.assertEqual([c["layer"] for c in contributors], ["default", "global", "repo"])

    def test_L8_repo_body_replaces_global(self):
        cfg = self.case("L8").load()
        self.assertIn("Repo commit rules.", cfg.bodies["standards"])
        self.assertNotIn("Global branch rules.", cfg.bodies["standards"])
        self.assertEqual(cfg.body_sources["standards"], {"Commits": "repo"})

    def test_L9_extends_global_replaces_sections_in_place(self):
        cfg = self.case("L9").load()
        body = cfg.bodies["standards"]
        self.assertLess(body.index("## Branch"), body.index("## Commits"))
        self.assertLess(body.index("## Commits"), body.index("## Extra"))
        self.assertIn("Global branch rules.", body)
        self.assertIn("Repo commit rules.", body)
        self.assertNotIn("Global commit rules.", body)
        self.assertEqual(cfg.body_sources["standards"], {"Branch": "global", "Commits": "repo", "Extra": "repo"})
        self.assertEqual(cfg.body_layers["standards"], ["global", "repo"])
        self.assertEqual(cfg.warnings, [])

    def test_L10_extends_global_without_global_warns(self):
        cfg = self.case("L10").load()
        self.assertIn("Repo commit rules.", cfg.bodies["standards"])
        self.assertNotIn("## Branch", cfg.bodies["standards"])
        self.assertTrue(any("extends" in w for w in cfg.warnings), cfg.warnings)

    def test_L11_extends_in_global_layer_warns(self):
        cfg = self.case("L11").load()
        self.assertIn("Global branch rules.", cfg.bodies["standards"])
        self.assertNotIn("## Commits", cfg.bodies["standards"])
        self.assertTrue(any("extends" in w for w in cfg.warnings), cfg.warnings)

    def test_L12_guard_off_with_confirm_same_layer_ok(self):
        # Confirmed in the same file, but a repo file still cannot loosen the guard.
        cfg = self.case("L12").load()
        self.assertEqual(cfg.meta["project"]["guard"]["other_repo"], "on")
        self.assertEqual(cfg.check(), [])
        self.assertTrue(any("other_repo" in w and "only tighten" in w for w in cfg.warnings), cfg.warnings)

    def test_L13_repo_turns_guard_back_on(self):
        cfg = self.case("L13").load()
        self.assertEqual(cfg.meta["project"]["guard"]["other_repo"], "on")
        self.assertEqual(cfg.check(), [])

    def test_L14_guard_off_without_confirm_is_an_error(self):
        tc = self.case("L14")
        errors = tc.load().check()
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith("repo .claude/not-me/project.md:4: "), errors[0])
        self.assertIn("force_push", errors[0])
        cfg = tc.load()
        self.assertEqual(cfg.meta["project"]["guard"]["force_push"], "on")
        entry = cfg.explain()["keys"]["project.guard.force_push"]
        self.assertEqual(entry["value"], "on")
        self.assertEqual(entry["source"]["layer"], "default")
        self.assertEqual(entry["ignored"], [{"layer": "repo", "value": "off", "reason": "not confirmed"}])

    def test_L14b_confirmed_global_off_beats_unconfirmed_repo_off(self):
        tc = self.case("L14b")
        cfg = tc.load()
        self.assertEqual(cfg.meta["project"]["guard"]["other_repo"], "off")
        entry = cfg.explain()["keys"]["project.guard.other_repo"]
        self.assertEqual(entry["source"]["layer"], "global")
        self.assertEqual(entry["ignored"], [{"layer": "repo", "value": "off", "reason": "not confirmed"}])
        errors = cfg.check()
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith("repo .claude/not-me/project.md:3: "), errors[0])

    def test_L15_malformed_global_falls_back_to_default(self):
        cfg = self.case("L15").load()
        self.assertEqual(cfg.meta["project"]["mode"], "dry-run")
        self.assertEqual(cfg.meta["standards"]["branch"], "feature/{issue}")
        self.assertTrue(any("project.md:3" in w for w in cfg.warnings), cfg.warnings)
        self.assertTrue(any("project.md:3" in e for e in cfg.check()))

    def test_L16_malformed_repo_falls_back_to_global(self):
        tc = self.case("L16")
        tc.sign_live()
        cfg = tc.load()
        self.assertEqual(cfg.meta["project"]["mode"], "live")
        self.assertEqual(cfg.meta["project"]["tracker"], "auto")
        self.assertTrue(any(".claude/not-me/project.md:3" in w for w in cfg.warnings), cfg.warnings)

    @unittest.skipIf(sys.platform.startswith("win") or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "permission bits not enforced")
    def test_L17_unreadable_global_dir_is_absent_with_warning(self):
        tc = self.case("L17")
        os.chmod(tc.home, 0)
        self.addCleanup(os.chmod, tc.home, 0o755)
        cfg = tc.load()
        self.assertEqual(cfg.meta["project"]["mode"], "dry-run")
        self.assertIsNone(cfg.layers.dirs["global"])
        self.assertTrue(any("unreadable" in w for w in cfg.warnings), cfg.warnings)

    @unittest.skipIf(sys.platform.startswith("win"), "symlinks need privileges on Windows")
    def test_L18_symlinked_global_dir_resolves(self):
        tc = self.case("L18")
        real = tc.root / "real-home"
        tc.home.rename(real)
        os.symlink(real, tc.home)
        tc.sign_live()  # signed through the link: bound to the resolved home
        cfg = tc.load()
        self.assertEqual(cfg.meta["project"]["mode"], "live")
        self.assertEqual(cfg.sources["project.mode"]["layer"], "global")

    def global_and_repo(self, global_text, repo_text):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        if global_text is not None:
            tc.write("global", "project", global_text)
        tc.write("repo", "project", repo_text)
        return tc.load()

    def test_L19_repo_guard_may_only_tighten(self):
        cfg = self.global_and_repo("---\nguard: {other_repo: off, force_push: off}\n"
                                   "confirm_guard_off: [other_repo, force_push]\n---\n",
                                   "---\nguard: {other_repo: on, force_push: off, destructive_git: off}\n"
                                   "confirm_guard_off: [force_push, destructive_git]\n---\n")
        guard = cfg.meta["project"]["guard"]
        self.assertEqual(guard["other_repo"], "on")  # tightening is allowed
        self.assertEqual(guard["force_push"], "off")  # stays at the global value
        self.assertEqual(guard["destructive_git"], "on")  # repo off ignored
        warned = [w for w in cfg.warnings if "only tighten" in w]
        self.assertEqual(len(warned), 2, warned)
        self.assertTrue(all(w.startswith("repo .claude/not-me/project.md:") for w in warned), warned)

    def test_L20_repo_allow_repos_and_protected_removals_ignored(self):
        cfg = self.global_and_repo("---\nallow_repos: [acme/app-ui]\nprotected_branches: [release]\n---\n",
                                   "---\nallow_repos: [acme/evil]\nprotected_branches: ['!main', '!release', hotfix]\n---\n")
        meta = cfg.meta["project"]
        self.assertEqual(meta["allow_repos"], ["acme/app-ui"])
        self.assertEqual(meta["protected_branches"], ["main", "master", "develop", "release", "hotfix"])
        warned = [w for w in cfg.warnings if "only tighten" in w]
        self.assertEqual(len(warned), 2, warned)
        self.assertIn("acme/evil", " ".join(warned))

    def test_L21_repo_mode_and_enabled_may_only_tighten(self):
        cfg = self.global_and_repo("---\nmode: live\n---\n", "---\nmode: dry-run\nenabled: false\n---\n")
        self.assertEqual(cfg.meta["project"]["mode"], "dry-run")
        self.assertIs(cfg.meta["project"]["enabled"], False)
        self.assertEqual([w for w in cfg.warnings if "only tighten" in w], [])
        cfg = self.global_and_repo("---\nenabled: false\n---\n", "---\nmode: live\nenabled: true\n---\n")
        self.assertEqual(cfg.meta["project"]["mode"], "dry-run")
        self.assertIs(cfg.meta["project"]["enabled"], False)
        self.assertEqual(len([w for w in cfg.warnings if "only tighten" in w]), 2)
        cfg = self.global_and_repo(None, "---\nenabled: true\n---\n")  # no global false: harmless
        self.assertIs(cfg.meta["project"]["enabled"], True)

    def test_repo_may_not_turn_include_unassigned_on(self):
        cfg = self.global_and_repo(None, "---\nqueue: {include_unassigned: true}\n---\n")
        self.assertIs(cfg.meta["project"]["queue"]["include_unassigned"], False)
        self.assertEqual(len([w for w in cfg.warnings if "only tighten" in w]), 1)
        cfg = self.global_and_repo("---\nqueue: {include_unassigned: true}\n---\n",
                                   "---\nqueue: {include_unassigned: false}\n---\n")
        self.assertIs(cfg.meta["project"]["queue"]["include_unassigned"], False)  # turning it off tightens
        self.assertEqual([w for w in cfg.warnings if "only tighten" in w], [])
        cfg = self.global_and_repo("---\nqueue: {include_unassigned: true}\n---\n", "---\nschema: 1\n---\n")
        self.assertIs(cfg.meta["project"]["queue"]["include_unassigned"], True)

    def test_repo_may_lower_list_limit_but_never_raise_or_clear_it(self):
        cfg = self.global_and_repo(None, "---\nqueue: {list_limit: 3}\n---\n")
        self.assertEqual(cfg.meta["project"]["queue"]["list_limit"], 3)  # lower than the default: tightens
        self.assertEqual([w for w in cfg.warnings if "only tighten" in w], [])
        for loosening in ("50", "null", "true", "'5'"):
            with self.subTest(loosening):
                cfg = self.global_and_repo("---\nqueue: {list_limit: 20}\n---\n",
                                           f"---\nqueue: {{list_limit: {loosening}}}\n---\n")
                self.assertEqual(cfg.meta["project"]["queue"]["list_limit"], 20)
                self.assertEqual(len([w for w in cfg.warnings if "only tighten" in w]), 1)
                ignored = cfg.explain()["keys"]["project.queue.list_limit"]["ignored"]
                self.assertEqual([i["layer"] for i in ignored], ["repo"])
        cfg = self.global_and_repo("---\nqueue: {list_limit: 20}\n---\n", "---\nqueue: {list_limit: 20}\n---\n")
        self.assertEqual(cfg.meta["project"]["queue"]["list_limit"], 20)  # equal is not a loosening
        self.assertEqual([w for w in cfg.warnings if "only tighten" in w], [])

    def test_repo_cannot_clear_the_queue_map_to_drop_list_limit(self):
        for repo in ("null", "5", "[]", "off"):
            for global_ in (None, "---\nqueue: {list_limit: 20}\n---\n"):
                with self.subTest(repo=repo, global_=global_):
                    cfg = self.global_and_repo(global_, f"---\nqueue: {repo}\n---\n")
                    want = 20 if global_ else 10
                    self.assertEqual(cfg.meta["project"]["queue"]["list_limit"], want)
                    warned = [w for w in cfg.warnings if "only tighten" in w]
                    self.assertTrue(warned, cfg.warnings)
                    self.assertIn("queue", " ".join(warned))
                    keys = cfg.explain()["keys"]
                    ignored = keys.get("project.queue.list_limit", {}).get("ignored", []) \
                        + keys.get("project.queue", {}).get("ignored", [])
                    self.assertIn("repo", [i["layer"] for i in ignored])
        # a repo null still clears what is not tighten-only, and a lower true include_unassigned may go
        cfg = self.global_and_repo("---\nqueue: {include_unassigned: true, list_limit: 20}\n---\n",
                                   "---\nqueue: null\n---\n")
        self.assertEqual(cfg.meta["project"]["queue"]["list_limit"], 20)
        self.assertNotEqual(cfg.meta["project"]["queue"].get("include_unassigned"), True)

    def test_global_may_clear_the_queue_map(self):
        cfg = self.global_and_repo("---\nqueue: null\n---\n", "---\nschema: 1\n---\n")
        self.assertNotIn("queue", cfg.meta["project"])

    def test_repo_cannot_clear_guard_map(self):
        cfg = self.global_and_repo(None, "---\nguard: null\n---\n")
        guard = cfg.meta["project"]["guard"]
        self.assertTrue(guard)
        self.assertTrue(all(v == "on" for v in guard.values()), guard)

    def test_repo_may_lower_picking_value_max_chars_but_never_raise_or_clear_it(self):
        # Field values can hold text other people wrote: the repo layer may only shorten what reaches the model.
        default = self.global_and_repo(None, "---\nschema: 1\n---\n").meta["project"]["picking"]["value_max_chars"]
        self.assertTrue(isinstance(default, int) and default > 0, default)
        cfg = self.global_and_repo(None, "---\npicking: {value_max_chars: 5}\n---\n")
        self.assertEqual(cfg.meta["project"]["picking"]["value_max_chars"], 5)
        self.assertEqual([w for w in cfg.warnings if "only tighten" in w], [])
        for loosening in (str(default + 1), "null", "'5'"):
            with self.subTest(loosening):
                cfg = self.global_and_repo(None, f"---\npicking: {{value_max_chars: {loosening}}}\n---\n")
                self.assertEqual(cfg.meta["project"]["picking"]["value_max_chars"], default)
                self.assertEqual(len([w for w in cfg.warnings if "only tighten" in w]), 1)

    def test_global_may_raise_list_limit(self):
        cfg = self.global_and_repo("---\nqueue: {list_limit: 40}\n---\n", "---\nschema: 1\n---\n")
        self.assertEqual(cfg.meta["project"]["queue"]["list_limit"], 40)

    def test_L22_explain_shows_ignored_with_reason(self):
        cfg = self.global_and_repo("---\nmode: dry-run\n---\n", "---\nmode: live\nallow_repos: [acme/evil]\n---\n")
        keys = cfg.explain()["keys"]
        reason = "repo layer may only tighten"
        self.assertEqual(keys["project.mode"]["ignored"], [{"layer": "repo", "value": "live", "reason": reason}])
        self.assertEqual(keys["project.mode"]["source"]["layer"], "global")
        self.assertEqual(keys["project.allow_repos"]["ignored"],
                         [{"layer": "repo", "value": ["acme/evil"], "reason": reason}])
        self.assertEqual(keys["project.allow_repos"]["value"], [])

    def test_every_case_has_a_fixture_dir(self):
        for i in range(1, 19):
            self.assertTrue((LAYER_CASES / f"L{i}").is_dir(), f"L{i}")


class MergeMetaTests(unittest.TestCase):
    def test_empty_higher_union_list_removes_nothing(self):
        merged, _ = layers.merge_meta({"protected_branches": ["main"]}, None, {"protected_branches": []}, schema=UNION)
        self.assertEqual(merged["protected_branches"], ["main"])

    def test_bang_removes_lower_value(self):
        merged, src = layers.merge_meta({"protected_branches": ["main", "release"]}, {"protected_branches": ["!release"]},
                                        None, schema=UNION)
        self.assertEqual(merged["protected_branches"], ["main"])
        self.assertEqual([c["layer"] for c in src["protected_branches"]["contributors"]], ["default", "global"])

    def test_nested_union_key(self):
        merged, _ = layers.merge_meta({"markers": {"legacy": ["a"]}}, {"markers": {"legacy": ["b"]}}, None, schema=UNION)
        self.assertEqual(merged["markers"]["legacy"], ["a", "b"])

    def test_null_on_union_key_is_ignored_with_warning(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        tc.write("global", "project", "---\nprotected_branches: [release]\n---\n")
        tc.write("repo", "project", "---\nschema: 1\nprotected_branches: null\n---\n")
        cfg = tc.load()
        self.assertEqual(cfg.meta["project"]["protected_branches"], ["main", "master", "develop", "release"])
        contributors = cfg.sources["project.protected_branches"]["contributors"]
        self.assertEqual([c["layer"] for c in contributors], ["default", "global"])
        self.assertEqual(cfg.sources["project.protected_branches"]["layer"], "global")
        matching = [w for w in cfg.warnings if "null cannot clear a union list" in w]
        self.assertEqual(len(matching), 1, cfg.warnings)
        self.assertTrue(matching[0].startswith("repo .claude/not-me/project.md:3: "), matching[0])
        self.assertIn("!value", matching[0])

    def test_null_removes_lower_scalar(self):
        merged, src = layers.merge_meta({"a": 1, "b": 2}, {"a": None}, None, schema={})
        self.assertNotIn("a", merged)
        self.assertTrue(src["a"]["removed"])

    def test_null_in_default_is_kept(self):
        merged, _ = layers.merge_meta({"a": None}, None, None, schema={})
        self.assertEqual(merged, {"a": None})

    def test_guard_confirm_is_not_a_union_key(self):
        merged, _ = layers.merge_meta({"confirm_guard_off": ["x"]}, {"confirm_guard_off": ["y"]}, None, schema=UNION)
        self.assertEqual(merged["confirm_guard_off"], ["y"])

    def test_sources_carry_path_and_line(self):
        _, src = layers.merge_meta({"a": {"b": 1}}, None, {"a": {"b": 2}}, schema={},
                                   paths={"repo": "r.md"}, lines={"repo": {("a", "b"): 7}})
        self.assertEqual((src["a.b"]["layer"], src["a.b"]["path"], src["a.b"]["line"]), ("repo", "r.md", 7))

    def test_inputs_not_mutated(self):
        low = {"a": {"b": [1]}}
        merged, _ = layers.merge_meta(low, None, {"a": {"c": 2}}, schema={})
        merged["a"]["b"].append(9)
        self.assertEqual(low, {"a": {"b": [1]}})


class ResolveBodyTests(unittest.TestCase):
    def test_extends_in_default_layer_warns(self):
        warnings = []
        body, srcs, _ = layers.resolve_body("x", {"default": Doc("default", "d.md", {}, "## A\n\na\n", extends="global")},
                                         warnings)
        self.assertIn("## A", body)
        self.assertEqual(srcs, {"A": "default"})
        self.assertEqual(len(warnings), 1)

    def test_repo_extends_default_skips_global(self):
        docs = {
            "default": Doc("default", "d.md", {}, "## A\n\nda\n\n## B\n\ndb\n"),
            "global": Doc("global", "g.md", {}, "## A\n\nga\n"),
            "repo": Doc("repo", "r.md", {}, "## B\n\nrb\n", extends="default"),
        }
        body, srcs, _ = layers.resolve_body("x", docs)
        self.assertEqual(srcs, {"A": "default", "B": "repo"})
        self.assertNotIn("ga", body)

    def test_chained_extends(self):
        docs = {
            "default": Doc("default", "d.md", {}, "intro\n\n## A\n\nda\n"),
            "global": Doc("global", "g.md", {}, "## B\n\ngb\n", extends="default"),
            "repo": Doc("repo", "r.md", {}, "## A\n\nra\n", extends="global"),
        }
        body, srcs, _ = layers.resolve_body("x", docs)
        self.assertEqual(srcs, {"A": "repo", "B": "global"})
        self.assertTrue(body.startswith("intro"))

    def test_headings_in_code_fences_are_not_sections(self):
        docs = {"default": Doc("default", "d.md", {}, "## A\n\n```\n## not a heading\n```\n")}
        _, srcs, _ = layers.resolve_body("x", docs)
        self.assertEqual(srcs, {"A": "default"})

    def test_discover_lists_present_files(self):
        tc = TempCase("L9")
        self.addCleanup(tc.cleanup)
        found = layers.discover(tc.project, tc.home)
        self.assertEqual(found.files["global"], ["standards"])
        self.assertEqual(found.files["repo"], ["standards"])
        self.assertEqual(len(found.files["default"]), 5)
        shutil.rmtree(tc.home)
        self.assertIsNone(layers.discover(tc.project, tc.home).dirs["global"])


if __name__ == "__main__":
    unittest.main()
