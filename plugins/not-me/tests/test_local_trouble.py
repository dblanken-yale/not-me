"""Local trouble a run works around stays in the run log, and verify commands run as the verify doc gives them."""
import re
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))
from notme import writeback as wb  # noqa: E402

REPO = "acme/app"
SKILLS = PLUGIN / "skills"
AUTONOMY = PLUGIN / "references" / "autonomy-and-logging.md"
NO_STORY = "no account of local trouble"
AS_GIVEN = "as the verify doc gives it"


def text(path):
    return path.read_text(encoding="utf-8")


def section(body, heading):
    start = body.index(heading)
    nxt = body.find("\n## ", start + len(heading))
    return body[start:] if nxt < 0 else body[start:nxt]


def checklist(body):
    return section(body, "## Checklist")


class LocalTroubleDocsTest(unittest.TestCase):
    def test_failures_on_our_side_covers_worked_around_problems(self):
        part = section(text(AUTONOMY), "## Failures on our side")
        for phrase in ("works around", "(environment, tooling, worktree, sandbox, guard)", "`WARN`",
                       "never into a review, comment, reply, PR body or proof comment", AS_GIVEN,
                       "config, flags or environment"):
            self.assertIn(phrase, part, phrase)

    def test_post_sections_report_checks_without_local_trouble(self):
        posting = text(SKILLS / "review" / "references" / "posting.md")
        self.assertNotIn("each command run on the head SHA and its result", posting)
        self.assertIn("`**Checks**`: each check by its command and result only", posting)
        for path in (SKILLS / "review" / "references" / "posting.md",
                     SKILLS / "new" / "references" / "pr-assembly.md",
                     SKILLS / "update" / "references" / "write-back-assembly.md",
                     SKILLS / "prove" / "references" / "proof-comment.md"):
            self.assertIn(NO_STORY, text(path), path.name)

    def test_skills_run_verify_commands_as_given(self):
        for name in ("review", "new", "update"):
            body = text(SKILLS / name / "SKILL.md")
            self.assertIn(AS_GIVEN, body, name)
            self.assertIn("config, flags or environment", body, name)
            items = checklist(body)
            self.assertIn(AS_GIVEN, items, name)
            self.assertIn("local trouble", items, name)

    def test_prove_checklist_keeps_local_trouble_out(self):
        self.assertIn("local trouble", checklist(text(SKILLS / "prove" / "SKILL.md")))


class LocalTroubleTextCheckTest(unittest.TestCase):
    def problems(self, body):
        return [p for p in wb.check_text(body, tracker=REPO, repo=REPO) if "local trouble" in p]

    def test_flags_the_rerun_story(self):
        body = ("**Checks**\n- The default run could not start the browser; I reran with the same config and "
                "`server.fs.strict` off.\n")
        found = self.problems(body)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("line 2:"), found)

    def test_flags_common_phrasings(self):
        for line in ("Re-ran with a clean cache.", "I worked around the sandbox.", "Reran it without the lock.",
                     "Worked around a guard block."):
            self.assertTrue(self.problems(line), line)

    def test_leaves_plain_check_results_and_tooling_words_alone(self):
        for line in ("- Tests on abc1234: 212 passed.", "Adds a worktree helper for review runs.",
                     "Runs the sandbox profile in CI.", "The guard now blocks force pushes.",
                     "Rerun with `--verbose` to see the log.", "A workaround for the old parser is removed."):
            self.assertEqual(self.problems(line), [], line)

    def test_quoted_and_code_text_is_not_flagged(self):
        for body in ('The old body said "I reran with the same config".', "> I reran with the same config.",
                     "`reran with`", "```\nreran with\n```"):
            self.assertEqual(self.problems(body), [], body)


if __name__ == "__main__":
    unittest.main()
