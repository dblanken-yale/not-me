"""Shape checks for the /not-me router skill, mainly its --parallel option."""
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
ROOT = PLUGIN.parent.parent
SKILL = PLUGIN / "skills" / "not-me" / "SKILL.md"
BRANCHING = PLUGIN / "references" / "branching.md"
DEFAULTS = PLUGIN / "defaults" / "project.md"

if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))
from notme import frontmatter  # noqa: E402


def text(path):
    return path.read_text(encoding="utf-8")


def flat(body):
    return " ".join(body.split())


class RouterSkillTest(unittest.TestCase):
    def setUp(self):
        self.body = text(SKILL)
        self.meta, _rest = frontmatter.parse(self.body)
        self.flat = flat(self.body)

    def test_frontmatter_offers_parallel(self):
        self.assertEqual(self.meta.get("name"), "not-me")
        self.assertIn("--parallel N", self.meta.get("argument-hint", ""))
        self.assertLessEqual(len(self.meta.get("description", "")), 1024)

    def test_length(self):
        self.assertLessEqual(len(self.body.splitlines()), 100)

    def test_parallel_is_capped_by_a_default_limit(self):
        self.assertIn("Lower N to `limits.parallel_runs` (default 3)", self.flat)
        meta, _ = frontmatter.parse(text(DEFAULTS))
        self.assertEqual(meta["limits"]["parallel_runs"], 3)

    def test_parallel_needs_worktree_mode_and_refuses_bg(self):
        self.assertIn("`workspace.new` must be `worktree`", self.flat)
        self.assertIn("STOP - parallel needs workspace.new: worktree", self.flat)
        self.assertIn("Refuse it with `--bg`", self.flat)

    def test_parallel_dispatches_one_unisolated_new_run_per_distinct_issue(self):
        for phrase in ("`notme pick-issue`", "up to N distinct issues", "never a `held_back` one",
                       "Dispatch one subagent per issue", "all in one message",
                       "without worktree isolation", "`/not-me:new <issue>`"):
            self.assertIn(phrase, self.flat, phrase)

    def test_claims_keep_two_runs_off_one_issue(self):
        self.assertIn("compare-and-set", self.flat)
        self.assertIn("`STOP <ref> claimed elsewhere`", self.flat)

    def test_worktree_rules_for_new_work(self):
        branching = flat(text(BRANCHING))
        for phrase in ("## Worktrees for new and update work", "--name notme-new-<N>", "`notme-update-<N>`",
                       "A lost claim removes the worktree", "pass `--project-dir <path>` to every `notme` call", "`git -C <path> ...`",
                       "`notme git worktree-remove <path>`"):
            self.assertIn(phrase, branching, phrase)


if __name__ == "__main__":
    unittest.main()
