"""Shape checks for the five helper skills: not-me, status, doctor, config, allow-repo."""
import re
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
from notme import frontmatter  # noqa: E402

SKILLS = ["not-me", "status", "doctor", "config", "allow-repo"]
FORBIDDEN = ["/Users/", "/home/", "yalesites", "pantheon", "lando", "miketullo",
             "dblanken", "alan-", "Dave", "Mike"]
EM_DASH = "—"
VERBS = "NONE | PR | PUSHED | PARKED | HELD | REVIEWED | PROVED | FAILED | STOP"
BARE = re.compile(r'(?:^|(?<=[`(|&;$\s]))notme\s+([A-Za-z][A-Za-z-]*)')
LONG = re.compile(r'\$\{CLAUDE_PLUGIN_ROOT\}/bin/notme"?\s+([A-Za-z][A-Za-z-]*)')
WRITES = re.compile(r"notme\W+(?:claim-|release-|move-issue|transition|labels-|reply-thread|"
                    r"comment-upsert|request-review|pr-|review-post|resolve-threads|followup|"
                    r"log (?:start|action|question|finish)|pause|git push)|\bgh\s+(?:pr|issue|api)\b")


def known_commands():
    path = PLUGIN.parent.parent / "scripts" / "known-commands.txt"
    return {ln.split()[0] for ln in path.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")}


def load(name):
    text = (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    meta, _body = frontmatter.parse(text, source=name)
    return text, meta


class HelperSkillsTest(unittest.TestCase):
    def each(self):
        for name in SKILLS:
            text, meta = load(name)
            with self.subTest(skill=name):
                yield name, text, meta

    def test_frontmatter_and_description(self):
        for name, _text, meta in self.each():
            self.assertEqual(meta.get("name"), name)
            self.assertIn("NOT for", meta.get("description", ""))
            self.assertLessEqual(len(meta["description"]), 1024)

    def test_commands_exist(self):
        known = known_commands()
        for name, text, _meta in self.each():
            words = []
            for line in text.splitlines():
                words += LONG.findall(line)
                words += BARE.findall(LONG.sub("", line))
            for word in words:
                self.assertIn(word, known, f"{name}: unknown command {word}")

    def test_no_forbidden_or_em_dash(self):
        for name, text, _meta in self.each():
            for token in FORBIDDEN:
                self.assertNotIn(token, text, f"{name}: {token}")
            self.assertNotIn(EM_DASH, text)

    def test_plugin_root_is_explained(self):
        for name, text, _meta in self.each():
            if "CLAUDE_PLUGIN_ROOT" in text:
                self.assertIn("substituted at load time", text, name)

    def test_router(self):
        text, meta = load("not-me")
        self.assertIn(VERBS, text)
        self.assertIn("--bg", meta["argument-hint"])
        for leg in ("/not-me:update", "/not-me:new", "/not-me:review"):
            self.assertIn(leg, text)
        for phrase in ("STOP - disabled", "STOP - paused", "STOP - run in progress",
                       "NONE - nothing to do"):
            self.assertIn(phrase, text)
        self.assertLess(len(text.splitlines()), 90)

    def test_allow_repo_is_human_only(self):
        text, meta = load("allow-repo")
        self.assertIs(meta.get("disable-model-invocation"), True)
        self.assertIn("notme allow-repo list", text)
        self.assertNotIn("allow-repo list --", text)
        self.assertIn("hidden", text)
        for line in text.splitlines():
            if re.search(r"allow-repo (add|remove) ", line):
                self.assertIn('"<notme>"', line)
                self.assertNotIn("CLAUDE_PLUGIN_ROOT", line)
        self.assertLess(len(text.splitlines()), 50)

    def test_read_only_skills_have_no_writes(self):
        for name in ("status", "config", "doctor"):
            text, _meta = load(name)
            self.assertIsNone(WRITES.search(text), f"{name}: write command")
            self.assertNotIn("allow-repo add", text)

    def test_config_and_status_show_the_picking_rules(self):
        config, _meta = load("config")
        for phrase in ("`picking`", "`rules`", "`layer`", "Picking work"):
            self.assertIn(phrase, config, phrase)
        status, _meta = load("status")
        self.assertIn("`picking`", status)

    def test_line_budgets(self):
        budgets = {"status": 60, "doctor": 60, "config": 70}
        for name, cap in budgets.items():
            text, _meta = load(name)
            self.assertLess(len(text.splitlines()), cap + 1, name)


if __name__ == "__main__":
    unittest.main()
