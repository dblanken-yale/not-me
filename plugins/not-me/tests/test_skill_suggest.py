"""Shape checks for the /not-me:suggest skill: person-only, CLI-only, assigns only the operator."""
import re
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
ROOT = PLUGIN.parent.parent
SKILL = PLUGIN / "skills" / "suggest" / "SKILL.md"
DEFAULTS = PLUGIN / "defaults"
KNOWN = ROOT / "scripts" / "known-commands.txt"
DENYLIST = ROOT / "scripts" / "lint-denylist.txt"

if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))
from notme import frontmatter  # noqa: E402

EM_DASH = "\u2014"
GROUPS = {"log", "git", "guard", "allow-repo", "retro", "mode"}
FORBIDDEN = ["/Users/", "/home/", "C:\\", "~/code", "Dave", "Mike", "gh pr ", "gh issue ", "gh api",
             "git push", "pytest", "npm test", "ruff", "phpunit", "composer"]


def text(path):
    return path.read_text(encoding="utf-8")


def flat(body):
    return " ".join(body.split())


def named_commands(body):
    found = []
    for m in re.finditer(r"(?:^|(?<=[`(|&;$\s\"]))notme\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?", body, re.M):
        first, second = m.group(1), m.group(2)
        found.append(f"{first} {second}" if first in GROUPS else first)
    return found


class SkillSuggestTest(unittest.TestCase):
    def setUp(self):
        self.body = text(SKILL)
        self.meta, _rest = frontmatter.parse(self.body)

    def test_frontmatter_is_person_only(self):
        self.assertEqual(self.meta.get("name"), "suggest")
        self.assertIs(self.meta.get("disable-model-invocation"), True)
        desc = self.meta.get("description")
        self.assertIsInstance(desc, str)
        self.assertLessEqual(len(desc), 1024)
        self.assertIn("Use when", desc)
        self.assertIn("NOT for", desc)
        self.assertIn("never from the /not-me router, a loop or a --bg run", desc)
        self.assertNotIn("--bg", self.meta.get("argument-hint", ""))
        self.assertIn("--dry-run", self.meta.get("argument-hint", ""))

    def test_length(self):
        self.assertLessEqual(len(self.body.splitlines()), 100)

    def test_only_the_suggest_commands(self):
        known = {ln.strip() for ln in text(KNOWN).splitlines() if ln.strip() and not ln.startswith("#")}
        names = set(named_commands(self.body))
        self.assertEqual(names, {"suggest-issues", "assign-issue"})
        self.assertLessEqual(names, known)

    def test_assigns_only_after_the_person_names_the_issue(self):
        body = flat(self.body)
        for phrase in ("Assign exactly the numbers the person named in this answer",
                       "never assigns or unassigns anyone else", "Never assume a yes",
                       "It adds the operator as assignee and nothing else"):
            self.assertIn(phrase, body)

    def test_ranks_by_the_queue_rules_and_hides_untrusted_text(self):
        body = flat(self.body)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/skills/new/references/target.md", self.body)
        self.assertIn("`picking.layer` is `default`: most impact first", body)
        self.assertIn("never one in `held_back`", body)
        self.assertIn("numbers and logins only", body)
        self.assertIn("one short line", body)

    def test_dry_run_writes_nothing(self):
        body = flat(self.body)
        self.assertIn("would assign", body)
        self.assertIn("nothing was written to GitHub", body)

    def test_plugin_root_paths_resolve(self):
        for m in re.finditer(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./\-]+)", self.body):
            rel = m.group(1).rstrip(".,;:)")
            self.assertTrue((PLUGIN / rel).exists(), rel)
        self.assertIn("substituted at load time", self.body)

    def test_no_forbidden_strings(self):
        deny = [ln.strip().lower() for ln in text(DENYLIST).splitlines() if ln.strip() and not ln.startswith("#")]
        low = self.body.lower()
        for token in FORBIDDEN:
            self.assertNotIn(token, self.body, token)
        for token in deny:
            self.assertNotIn(token, low, token)
        self.assertNotIn(EM_DASH, self.body)

    def test_router_and_bg_mode_never_call_it(self):
        for path in (PLUGIN / "skills" / "not-me" / "SKILL.md", PLUGIN / "references" / "bg-mode.md"):
            self.assertNotIn("not-me:suggest", text(path), path.name)
            self.assertNotIn("assign-issue", text(path), path.name)

    def test_no_other_skill_or_reference_runs_assign_issue(self):
        allowed = {PLUGIN / "references" / "prohibitions.md",
                   PLUGIN / "skills" / "new" / "references" / "target.md"}
        suggest = PLUGIN / "skills" / "suggest"
        files = list((PLUGIN / "skills").rglob("*.md")) + list((PLUGIN / "references").rglob("*.md"))
        self.assertTrue(files)
        for path in files:
            if suggest in path.parents or path in allowed:
                continue
            self.assertNotIn("assign-issue", text(path), str(path.relative_to(PLUGIN)))

    def test_runs_are_told_never_to_assign(self):
        prohibitions = flat(text(PLUGIN / "references" / "prohibitions.md"))
        self.assertIn("never runs `notme assign-issue`", prohibitions)
        self.assertIn("Checked every request against the ten prohibitions", prohibitions)
        target = flat(text(PLUGIN / "skills" / "new" / "references" / "target.md"))
        self.assertIn("Never run `/not-me:suggest` or `notme assign-issue` yourself", target)

    def test_reports_a_run_in_progress_without_retrying(self):
        body = flat(self.body)
        self.assertIn("kind `active_run`", body)
        self.assertIn("a not-me run is in progress", body)

    def test_empty_queue_points_to_suggest(self):
        target = text(PLUGIN / "skills" / "new" / "references" / "target.md")
        self.assertIn("/not-me:suggest", target)


if __name__ == "__main__":
    unittest.main()
