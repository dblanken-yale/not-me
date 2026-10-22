"""The eval cases under evals-harness/cases/ and the scaffold that stages them.

Checks the shape of each case (prompt.md frontmatter keys, grader keys per type), that every live case maps to
a scenario the harness can grade, and that scripts/lint-evals.py passes. See evals-harness/cases/README.md.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from notme import frontmatter

HARNESS = Path(__file__).resolve().parent.parent
ROOT = HARNESS.parent
PLUGIN = ROOT / "plugins" / "not-me"
sys.path.insert(0, str(PLUGIN))
EVALS = HARNESS / "cases"
SCENARIOS = ROOT / "fixture" / "scenarios"
SCAFFOLD = HARNESS / "scaffold" / "scaffold.py"

PROMPT_KEYS = {"schema_version", "name", "description", "tags", "plugins", "runs", "expected_outcome",
               "model", "max_turns", "timeout_seconds", "allowed_tools", "artifact_publish",
               "growthbook_overrides", "append_system_prompt", "env"}
REQUIRED = {"name", "description", "tags", "runs", "max_turns", "timeout_seconds", "allowed_tools", "expected_outcome"}
COMMON = {"type", "weight", "arm"}
GRADER_KEYS = {
    "regex": COMMON | {"target", "pattern", "flags", "match"},
    "tool_used": COMMON | {"tool", "input_match", "min", "max"},
    "tool_order": COMMON | {"before", "after"},
    "file_exists": COMMON | {"path", "exists"},
    "llm": COMMON | {"criteria", "focus"},
}
CLASSES = {"safety", "outcome", "quality"}
SKILLS = {"new", "update", "review", "prove", "not-me", "setup", "config", "status", "ticket", "retro", "doctor"}
VERBS = {"NONE", "PR", "PUSHED", "PARKED", "HELD", "REVIEWED", "PROVED", "FAILED", "STOP", "TICKET", "RETRO"}
CASE_ID = re.compile(r"(?:^|-)([A-Z]\d+)$")
ABSOLUTE = re.compile(r"(?:/Users/|/home/)[A-Za-z0-9._-]+/|\b[A-Z]:\\{1,2}Users\b")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


scaffold = load_module("_eval_scaffold", SCAFFOLD)
keys = load_module("_eval_keys", HARNESS / "assertions" / "keys.py")


def cases():
    return sorted(p.parent for p in EVALS.glob("*/prompt.md"))


def split(path: Path):
    return frontmatter.parse(path.read_text(encoding="utf-8"), source=path.name)


def denylist():
    words = []
    for line in (ROOT / "scripts" / "lint-denylist.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line.lower())
    return words


class CaseShapeTest(unittest.TestCase):
    def test_there_are_cases(self):
        self.assertGreaterEqual(len(cases()), 40)

    def test_prompt_frontmatter(self):
        for case in cases():
            with self.subTest(case=case.name):
                meta, body = split(case / "prompt.md")
                self.assertEqual(set(meta) - PROMPT_KEYS, set(), "unknown prompt.md keys")
                self.assertEqual(REQUIRED - set(meta), set(), "missing prompt.md keys")
                self.assertEqual(meta["name"], case.name)
                self.assertTrue(body.strip(), "empty prompt")
                self.assertTrue(1 <= meta["max_turns"] <= 200)
                self.assertTrue(1 <= meta["timeout_seconds"] <= 3600)
                self.assertEqual(meta["runs"], 1, "a live case mutates the fixture; one run per seed")
                self.assertIsInstance(meta["allowed_tools"], list)
                for key in (meta.get("env") or {}):
                    self.assertRegex(key, r"^EVAL_[A-Z0-9_]*$", "only EVAL_* keys may be set from a case")

    def test_tags(self):
        for case in cases():
            with self.subTest(case=case.name):
                tags = split(case / "prompt.md")[0]["tags"]
                skills = [t for t in tags if t.startswith("skill:")]
                classes = [t for t in tags if t.startswith("class:")]
                self.assertEqual(len(skills), 1)
                self.assertEqual(len(classes), 1)
                self.assertIn(classes[0][6:], CLASSES)
                self.assertIn(skills[0][6:], SKILLS)
                self.assertTrue(case.name.startswith(skills[0][6:] + "-"), "case is named <skill>-<ID>")
                self.assertIn("live", tags)

    def test_graders(self):
        for case in cases():
            graders = sorted((case / "graders").glob("*.md"))
            self.assertTrue(graders, case.name)
            for grader in graders:
                with self.subTest(grader=f"{case.name}/{grader.name}"):
                    meta, body = split(grader)
                    kind = meta.get("type")
                    self.assertIn(kind, GRADER_KEYS)
                    self.assertEqual(set(meta) - GRADER_KEYS[kind], set(), "keys the graders do not read")
                    self.assertIn(meta["weight"], (1, 2, 3))
                    self.assertIn(meta.get("arm", "both"), ("with-only", "both"))
                    if kind == "regex":
                        pattern = meta.get("pattern") or body.strip()
                        self.assertTrue(pattern)
                        re.compile(pattern)
                        self.assertRegex(meta.get("flags", ""), r"^[dgimsuvy]*$")
                        self.assertRegex(meta.get("match", "contains"), r"^(contains|not_contains|count:\d+)$")
                        target = meta.get("target", "last_message")
                        if isinstance(target, dict):
                            self.assertEqual(set(target), {"source", "path"})
                            self.assertFalse(Path(target["path"]).is_absolute())
                        else:
                            self.assertIn(target, ("trace", "last_message", "files", "mock_calls"))
                    elif kind == "tool_used":
                        self.assertIn("tool", meta)
                        if "input_match" in meta:
                            re.compile(meta["input_match"])
                    elif kind == "tool_order":
                        for side in ("before", "after"):
                            self.assertIn(side, meta)
                    elif kind == "file_exists":
                        self.assertFalse(Path(meta["path"]).is_absolute())
                    elif kind == "llm":
                        self.assertTrue(meta.get("criteria") or body.strip())

    def test_safety_graders_are_weighted_three(self):
        for case in cases():
            for grader in (case / "graders").glob("*.md"):
                meta, _ = split(grader)
                if meta.get("match") == "not_contains" and meta.get("target") == "trace" or meta.get("max") == 0 \
                        and grader.stem.startswith("no-"):
                    with self.subTest(grader=f"{case.name}/{grader.name}"):
                        self.assertEqual(meta["weight"], 3)

    def test_no_absolute_paths_or_forbidden_strings(self):
        words = denylist()
        files = [p for case in cases() for p in case.rglob("*") if p.is_file()]
        files += [SCAFFOLD, EVALS / "README.md"]
        files += [SCENARIOS / f"{CASE_ID.search(c.name).group(1)}.json" for c in cases()]
        for path in files:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertNotIn("—", text, "no em dashes")
                self.assertIsNone(ABSOLUTE.search(text), "no absolute paths")
                low = text.lower()
                for word in words:
                    self.assertNotIn(word, low)


class ScenarioMappingTest(unittest.TestCase):
    def test_every_live_case_has_a_gradable_scenario(self):
        seen = {}
        for case in cases():
            meta, _ = split(case / "prompt.md")
            sid = CASE_ID.search(case.name).group(1)
            with self.subTest(case=case.name):
                self.assertNotIn(sid, seen, f"scenario {sid} already used by {seen.get(sid)}")
                seen[sid] = case.name
                path = SCENARIOS / f"{sid}.json"
                self.assertTrue(path.is_file(), f"no scenario {path.name}")
                spec = json.loads(path.read_text(encoding="utf-8"))
                expect = spec["expect"]
                self.assertEqual(set(expect) - set(keys.KNOWN_KEYS), set())
                self.assertTrue(set(expect) & set(keys.GRADED_KEYS), "nothing for grade_state.py to check")
                skill = [t[6:] for t in meta["tags"] if t.startswith("skill:")][0]
                self.assertEqual(expect.get("skill"), skill)
                self.assertEqual(scaffold.check_config(spec.get("config")), [])

    def test_result_word_matches_the_verb_grader(self):
        for case in cases():
            sid = CASE_ID.search(case.name).group(1)
            result = json.loads((SCENARIOS / f"{sid}.json").read_text(encoding="utf-8"))["expect"].get("result")
            grader = case / "graders" / "result-verb.md"
            with self.subTest(case=case.name):
                if result is None:
                    self.assertFalse(grader.exists())
                    continue
                self.assertIn(result, VERBS)
                if grader.exists():
                    self.assertTrue(split(grader)[1].strip().startswith("^" + result), "verb grader disagrees")
                else:
                    self.assertTrue(list((case / "graders").glob("*.md")))

    def test_config_docs_parse_after_substitution(self):
        values = {"repo": "acme/x-fixture", "companion": "acme/x-fixture-companion",
                  "companion_name": "x-fixture-companion", "companion_path": ".not-me-companion/x-fixture-companion",
                  "operator": "octocat", "owner": "acme"}
        for path in sorted(SCENARIOS.glob("*.json")):
            config = json.loads(path.read_text(encoding="utf-8")).get("config")
            for backend in ("github", "ledger"):
                for layer, docs in scaffold.layer_files(config, backend, values).items():
                    for name, text in docs.items():
                        with self.subTest(scenario=path.stem, layer=layer, doc=name):
                            meta, _ = frontmatter.parse(text, source=name)
                            self.assertIsInstance(meta, dict)


class LintTest(unittest.TestCase):
    def test_lint_evals_passes(self):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint-evals.py")],
                              capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"({len(cases())} cases, 0 violations)", proc.stdout)


class LayerFilesTest(unittest.TestCase):
    V = {"repo": "acme/x-fixture", "operator": "octocat"}

    def test_default_scenario_gets_live_mode_in_the_global_layer(self):
        files = scaffold.layer_files(None, "github", self.V)
        self.assertIn("mode: live", files["global"]["project.md"])
        self.assertEqual(files["repo"], {})

    def test_ledger_backend_is_dry_run(self):
        self.assertIn("mode: dry-run", scaffold.layer_files({}, "ledger", self.V)["global"]["project.md"])

    def test_none_writes_no_layer(self):
        self.assertEqual(scaffold.layer_files({"none": True}, "github", self.V), {"global": {}, "repo": {}})

    def test_scenario_mode_is_kept_and_mode_is_added_to_other_global_docs(self):
        kept = scaffold.layer_files({"global": {"project.md": "---\nmode: dry-run\n---\n"}}, "github", self.V)
        self.assertNotIn("mode: live", kept["global"]["project.md"])
        added = scaffold.layer_files({"global": {"project.md": "---\nenabled: false\n---\n"}}, "github", self.V)
        meta, _ = frontmatter.parse(added["global"]["project.md"])
        self.assertEqual((meta["mode"], meta["enabled"]), ("live", False))

    def test_placeholders_are_filled(self):
        files = scaffold.layer_files({"repo": {"project.md": "---\ntracker: \"{repo}\"\n---\n| {operator} |\n"}},
                                     "github", self.V)
        self.assertIn('tracker: "acme/x-fixture"', files["repo"]["project.md"])
        self.assertIn("| octocat |", files["repo"]["project.md"])

    def test_check_config_rejects_bad_blocks(self):
        self.assertTrue(scaffold.check_config({"pause": "always"}))
        self.assertTrue(scaffold.check_config({"files": {"../x": "y"}}))
        self.assertTrue(scaffold.check_config({"global": {"other.md": ""}}))
        self.assertTrue(scaffold.check_config({"none": True, "repo": {"project.md": ""}}))
        self.assertTrue(scaffold.check_config({"surprise": 1}))

    def test_scenario_id_from_case_name(self):
        self.assertEqual(scaffold.scenario_id("not-me-X1"), "X1")
        self.assertEqual(scaffold.scenario_id("new-N12"), "N12")
        with self.assertRaises(scaffold.ScaffoldError):
            scaffold.scenario_id("plain")


@unittest.skipUnless(shutil.which("git"), "needs git")
class StageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.state = self.tmp / "state"
        self.scen = self.tmp / "scenarios"
        self.scen.mkdir()

    def git(self, *args, cwd):
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)

    def make_stage(self, sid, config, companion=False):
        stage = self.state / "rid1" / f"{sid}.stage"
        repos = [("repo", stage / "repo")] + ([("companion", stage / "companion")] if companion else [])
        for _, path in repos:
            path.mkdir(parents=True)
            (path / "README.md").write_text("fixture\n", encoding="utf-8")
            self.git("init", "-q", "-b", "main", cwd=path)
            self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", ".", cwd=path)
            self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init", cwd=path)
        (stage / "gh-token").write_text("not-a-token\n", encoding="utf-8")
        (stage / "stage.json").write_text(json.dumps({
            "run_id": "rid1", "scenario": sid, "backend": "github", "repo": "acme/x-fixture",
            "companion": "acme/x-fixture-companion" if companion else None, "owner": "acme",
            "operator": "octocat", "operator_id": "1"}), encoding="utf-8")
        (self.state / scaffold.POINTER).write_text(json.dumps({"run_id": "rid1", "scenario": sid,
                                                                "stage": f"rid1/{sid}.stage"}), encoding="utf-8")
        (self.scen / f"{sid}.json").write_text(json.dumps({"config": config, "expect": {"skill": "new"}}),
                                               encoding="utf-8")

    def stage(self, case):
        ws, home = self.tmp / "ws", self.tmp / "home"
        record = scaffold.stage_workspace(case, ws, home, state=self.state, scenarios=self.scen)
        return ws, home, record

    def test_layers_pause_files_and_excludes(self):
        self.make_stage("K9", {"repo": {"verify.md": "---\nschema: 1\n---\n"}, "pause": "project",
                               "files": {".not-me-eval/answers.json": "{\"tracker\": \"{repo}\"}"},
                               "git_files": {"not-me/logs/new/x.md": "# log\n"}})
        ws, home, record = self.stage("new-K9")
        self.assertTrue((ws / "README.md").is_file())
        self.assertIn("mode: live", (home / ".claude" / "not-me" / "project.md").read_text(encoding="utf-8"))
        self.assertTrue((ws / ".claude" / "not-me" / "verify.md").is_file())
        self.assertTrue((ws / ".git" / "not-me" / "PAUSED").is_file())
        self.assertTrue((ws / ".git" / "not-me" / "logs" / "new" / "x.md").is_file())
        self.assertIn("acme/x-fixture", (ws / ".not-me-eval" / "answers.json").read_text(encoding="utf-8"))
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(ws), capture_output=True, text=True).stdout
        self.assertEqual(status.strip(), "", "scaffold files must not make the checkout dirty")
        self.assertEqual(record["env"]["NOT_ME_FIXTURE_REPO"], "acme/x-fixture")
        self.assertTrue((self.state / "rid1" / "K9.stage" / ".not-me-eval.json").is_file())
        self.assertFalse((home / ".not-me-eval.json").exists(), "the record must not land in the home")
        self.assertFalse((home / ".config").exists(), "gh is not signed in by the stage step")

    def test_companion_is_nested_and_allowed(self):
        self.make_stage("M9", {"allow_companions": True}, companion=True)
        ws, home, _ = self.stage("new-M9")
        self.assertTrue((ws / ".not-me-companion" / "x-fixture-companion" / "README.md").is_file())
        allow = json.loads((home / ".claude" / "not-me" / "allowed-repos.json").read_text(encoding="utf-8"))
        entries = allow["projects"]["acme/x-fixture"]["allow"]
        self.assertEqual([e["repo"] for e in entries], ["acme/x-fixture-companion"])

    def test_commit_repo_layer_tracks_the_docs(self):
        self.make_stage("E9", {"repo": {"verify.md": "---\nschema: 1\n---\n"}, "commit_repo_layer": True})
        ws, _, _ = self.stage("retro-E9")
        tracked = subprocess.run(["git", "ls-files", ".claude/not-me"], cwd=str(ws), capture_output=True, text=True)
        self.assertEqual(tracked.stdout.strip(), ".claude/not-me/verify.md")

    def test_stage_for_another_scenario_is_refused(self):
        self.make_stage("N9", {})
        with self.assertRaises(scaffold.ScaffoldError):
            self.stage("new-N8")

    def test_missing_stage_is_refused(self):
        with self.assertRaises(scaffold.ScaffoldError):
            self.stage("new-N1")


if __name__ == "__main__":
    unittest.main()
