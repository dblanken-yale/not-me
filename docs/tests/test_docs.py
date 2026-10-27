"""Checks on the user guides: requirements, install, config, guards, workflow and testing, and the READMEs.

Run through scripts/run-tests.py, which puts plugins/not-me on sys.path.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "not-me"
DOCS = ROOT / "docs"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from notme import frontmatter  # noqa: E402

GUIDES = ("requirements.md", "install.md", "config.md", "guards.md")
DOC_NAMES = ("project", "standards", "verify", "review", "prove", "role")
RULES = ("other_repo", "protected_branches", "force_push", "destructive_git")
NEW_DOCS = ("workflow.md", "testing.md")
READMES = (ROOT / "README.md", PLUGIN / "README.md")
VERBS = ("NONE", "PR", "PUSHED", "PARKED", "HELD", "REVIEWED", "PROVED", "FAILED", "STOP")
SKILLS = ("not-me", "new", "update", "review", "prove", "ticket", "suggest", "retro", "status", "setup", "config",
          "doctor", "allow-repo", "mode")

# Built from pieces so this file does not trip the repo's own denylist lint.
FORBIDDEN = (
    "/Users" + "/",
    "/ho" + "me/",
    "ya" + "lesites",
    "pan" + "theon",
    "lan" + "do",
    "mike" + "tullo",
    "al" + "an-",
)
EM_DASH = "—"


def read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def default_meta(doc: str) -> dict:
    text = (PLUGIN / "defaults" / f"{doc}.md").read_text(encoding="utf-8")
    meta, _ = frontmatter.parse(text, source=f"{doc}.md")
    return meta


def leaf_keys(meta: dict, prefix: str = "") -> set:
    """Dotted names of every leaf; a non-empty map is descended into, anything else is a leaf."""
    out = set()
    for key, value in meta.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out |= leaf_keys(value, name + ".")
        else:
            out.add(name)
    return out


def key_tables(text: str) -> dict:
    """{doc name: set of keys} from the first column of the tables under '## Key reference'."""
    section = text.split("## Key reference", 1)[1]
    tables: dict = {}
    current = None
    for line in section.splitlines():
        heading = re.match(r"^### (\w+)\.md\s*$", line)
        if heading:
            current = heading.group(1)
            tables[current] = set()
            continue
        if current and line.startswith("|"):
            cell = line.split("|")[1].strip()
            m = re.fullmatch(r"`([^`]+)`", cell)
            if m:
                tables[current].add(m.group(1))
    return tables


class TestGuides(unittest.TestCase):
    def test_each_doc_exists_and_is_not_empty(self):
        for name in GUIDES:
            path = DOCS / name
            self.assertTrue(path.is_file(), f"{name} is missing")
            self.assertGreater(len(path.read_text(encoding="utf-8").strip()), 500, f"{name} is too short")

    def test_no_forbidden_strings(self):
        for name in GUIDES:
            low = read(name).lower()
            for token in FORBIDDEN:
                self.assertNotIn(token.lower(), low, f"{name} contains {token!r}")

    def test_no_em_dashes(self):
        for name in GUIDES:
            self.assertNotIn(EM_DASH, read(name), f"{name} contains an em dash")


class TestConfigDoc(unittest.TestCase):
    def setUp(self):
        self.text = read("config.md")
        self.tables = key_tables(self.text)

    def test_every_doc_has_a_key_table(self):
        self.assertEqual(set(self.tables), set(DOC_NAMES))

    def test_documented_keys_exist_in_the_defaults(self):
        for doc in DOC_NAMES:
            known = leaf_keys(default_meta(doc))
            for key in self.tables[doc]:
                self.assertIn(key, known, f"config.md documents {doc}.{key}, which is not a default key")

    def test_every_default_key_is_documented(self):
        for doc in DOC_NAMES:
            missing = leaf_keys(default_meta(doc)) - self.tables[doc]
            self.assertFalse(missing, f"config.md does not document {doc}: {sorted(missing)}")

    def test_every_top_level_default_key_appears(self):
        for doc in DOC_NAMES:
            documented = self.tables[doc]
            for key in default_meta(doc):
                self.assertTrue(
                    key in documented or any(k.startswith(key + ".") for k in documented),
                    f"top-level key {doc}.{key} is not in config.md",
                )

    def test_backticked_doc_prefixed_keys_exist(self):
        """A backticked `project.some.key` style token anywhere must name a real key."""
        pattern = re.compile(r"`(%s)\.([A-Za-z0-9_.]+)`" % "|".join(DOC_NAMES))
        for doc, key in pattern.findall(self.text):
            if key == "md":
                continue
            known = leaf_keys(default_meta(doc))
            ok = key in known or any(k.startswith(key + ".") for k in known)
            self.assertTrue(ok, f"config.md names `{doc}.{key}`, which is not a default key")

    def test_mentions_core_topics(self):
        for phrase in ("extends", "confirm_guard_off", "needs help", "models.strategy", "intended-writes.jsonl",
                       "PAUSED", "schema"):
            self.assertIn(phrase, self.text, f"config.md never mentions {phrase!r}")


class TestGuardsDoc(unittest.TestCase):
    def setUp(self):
        self.text = read("guards.md")

    def test_guard_rule_names_are_real(self):
        for name in re.findall(r"`guard\.([a-z_]+)`", self.text):
            self.assertIn(name, RULES, f"guards.md names guard.{name}, which is not a rule")

    def test_all_four_rules_are_covered(self):
        for rule in RULES:
            self.assertIn(f"### {rule}", self.text, f"guards.md has no section for {rule}")
            self.assertIn(f"`guard.{rule}`", self.text)

    def test_rule_sections_are_only_the_four(self):
        section = self.text.split("## The rule groups", 1)[1].split("## The block message", 1)[0]
        headings = re.findall(r"^### (\S+)", section, re.M)
        self.assertEqual(headings, list(RULES))

    def test_block_message_and_allow_command(self):
        for phrase in ("not-me guard [other_repo]", "/not-me:allow-repo", "--everywhere", "--board",
                       "confirm_guard_off"):
            self.assertIn(phrase, self.text)


class TestInstallDoc(unittest.TestCase):
    def setUp(self):
        self.text = read("install.md").lower()

    def test_all_os_families(self):
        for word in ("macos", "debian", "ubuntu", "fedora", "windows"):
            self.assertIn(word, self.text)

    def test_both_clients(self):
        self.assertIn("claude code cli", self.text)
        self.assertIn("claude code desktop", self.text)
        self.assertIn("auto mode", self.text)

    def test_core_commands(self):
        for phrase in ("/plugin marketplace add dblanken-" + "ya" + "le/not-me", "/plugin install not-me@not-me",
                       "/plugin update not-me@not-me", "/not-me:doctor", "/not-me:setup", "gh auth refresh -s project"):
            self.assertIn(phrase.lower(), self.text)


def skill_command(name: str) -> str:
    return "/not-me" if name == "not-me" else f"/not-me:{name}"


class TestWorkflowAndTestingDocs(unittest.TestCase):
    def test_docs_exist_and_are_not_empty(self):
        for name in NEW_DOCS:
            path = DOCS / name
            self.assertTrue(path.is_file(), f"{name} is missing")
            self.assertGreater(len(path.read_text(encoding="utf-8").strip()), 2000, f"{name} is too short")

    def test_no_forbidden_strings_or_em_dashes(self):
        for name in NEW_DOCS:
            text = read(name)
            self.assertNotIn(EM_DASH, text, f"{name} contains an em dash")
            for token in FORBIDDEN:
                self.assertNotIn(token.lower(), text.lower(), f"{name} contains {token!r}")

    def test_workflow_mentions_every_label_name(self):
        text = read("workflow.md")
        labels = default_meta("project")["labels"]
        self.assertEqual(len(labels), 11)
        for key, name in labels.items():
            self.assertIn(f"`{name}`", text, f"workflow.md never names the label {name!r} ({key})")

    def test_workflow_mentions_every_verb(self):
        text = read("workflow.md")
        for verb in VERBS:
            self.assertIn(f"`{verb}`", text, f"workflow.md never explains the verb {verb}")

    def test_workflow_has_a_state_diagram_and_core_topics(self):
        text = read("workflow.md")
        self.assertIn("```mermaid", text)
        self.assertIn("stateDiagram-v2", text)
        for phrase in ("needs human", "PAUSED", "/loop 2h /not-me --bg", "dry-run", "First week", "Companion",
                       "Delegation"):
            self.assertIn(phrase, text, f"workflow.md never mentions {phrase!r}")

    def test_testing_names_every_script(self):
        text = read("testing.md")
        scripts = sorted(p.name for p in (ROOT / "scripts").iterdir() if p.is_file() and p.suffix in (".py", ".sh"))
        self.assertTrue(scripts)
        for name in scripts:
            self.assertIn(name, text, f"testing.md never names scripts/{name}")
        for name in ("run_guard_cases.py", "seed_fixture.py", "eval-live.sh"):
            self.assertIn(name, text)

    def test_testing_explains_both_fixture_repos(self):
        text = read("testing.md").lower()
        for phrase in ("fixture repo", "companion repo", "eval-lock", "_github", "post-as-bot", "github", "ledger",
                       "safety", "outcome", "quality"):
            self.assertIn(phrase, text, f"testing.md never mentions {phrase!r}")

    def test_docs_are_linked_from_the_readmes(self):
        for readme in READMES:
            text = readme.read_text(encoding="utf-8")
            for doc in ("requirements", "install", "config", "guards", "workflow", "cli", "testing", "release"):
                self.assertIn(f"docs/{doc}.md", text, f"{readme.name} does not link docs/{doc}.md")


class TestReadmes(unittest.TestCase):
    def table_names(self, text: str) -> set:
        rows = (line for line in text.splitlines() if line.startswith("| `/not-me"))
        out = set()
        for row in rows:
            m = re.match(r"\| `(/not-me(?::[a-z-]+)?)", row)
            if m:
                out.add(m.group(1))
        return out

    def test_command_tables_list_all_skills(self):
        wanted = {skill_command(n) for n in SKILLS}
        self.assertEqual(len(wanted), 14)
        for readme in READMES:
            found = self.table_names(readme.read_text(encoding="utf-8"))
            self.assertEqual(found, wanted, f"{readme.relative_to(ROOT)} command table differs")

    def test_table_matches_the_skills_directory_when_present(self):
        skills = PLUGIN / "skills"
        if not skills.is_dir():
            self.skipTest("skills are not on this branch yet")
        self.assertEqual({p.name for p in skills.iterdir() if p.is_dir()}, set(SKILLS))

    def test_readmes_have_no_forbidden_strings_or_em_dashes(self):
        for readme in READMES:
            text = readme.read_text(encoding="utf-8")
            self.assertNotIn(EM_DASH, text, f"{readme.name} contains an em dash")
            for token in FORBIDDEN:
                self.assertNotIn(token.lower(), text.lower(), f"{readme.name} contains {token!r}")


if __name__ == "__main__":
    unittest.main()
