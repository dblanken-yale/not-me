"""Shape checks for the /not-me:update skill and its step references."""
import re
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
ROOT = PLUGIN.parent.parent
SKILL_DIR = PLUGIN / "skills" / "update"
SKILL = SKILL_DIR / "SKILL.md"
DEFAULTS = PLUGIN / "defaults"
KNOWN = ROOT / "scripts" / "known-commands.txt"
DENYLIST = ROOT / "scripts" / "lint-denylist.txt"

if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))
from notme import frontmatter  # noqa: E402

VERBS = "NONE | PR | PUSHED | PARKED | HELD | REVIEWED | PROVED | FAILED | STOP"
MARKER = "<!-- not-me:update -->"
PROHIBITIONS = "${CLAUDE_PLUGIN_ROOT}/references/prohibitions.md"
EM_DASH = "\u2014"
GROUPS = {"log", "git", "guard", "allow-repo"}
FORBIDDEN = ["/Users/", "/home/", "C:\\", "~/code", "gh pr create", "gh pr comment",
             "gh pr edit", "gh issue edit", "gh issue comment", "gh pr review", "pytest", "npm test", "ruff",
             "phpunit", "composer"]
FILE_EXT = (".md", ".json", ".txt", ".py", ".yml", ".yaml", ".sh")


def docs():
    return [SKILL] + sorted((SKILL_DIR / "references").glob("*.md"))


def text(path):
    return path.read_text(encoding="utf-8")


def known_commands():
    return {ln.strip() for ln in text(KNOWN).splitlines() if ln.strip() and not ln.startswith("#")}


def default_meta():
    """Frontmatter of each shipped default doc, keyed by doc name."""
    return {p.stem: frontmatter.parse(text(p))[0] for p in sorted(DEFAULTS.glob("*.md"))}


def defined_labels():
    return set(default_meta()["project"]["labels"])


def named_commands(body):
    """Leaf command names after `notme`, two words for the command groups."""
    found = []
    for m in re.finditer(r"(?:^|(?<=[`(|&;$\s\"]))notme\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?", body, re.M):
        first, second = m.group(1), m.group(2)
        found.append(f"{first} {second}" if first in GROUPS else first)
    return found


class SkillUpdateTest(unittest.TestCase):
    def setUp(self):
        self.body = text(SKILL)
        self.meta, _rest = frontmatter.parse(self.body)

    def test_frontmatter(self):
        self.assertEqual(self.meta.get("name"), "update")
        desc = self.meta.get("description")
        self.assertIsInstance(desc, str)
        self.assertLessEqual(len(desc), 1024)
        self.assertIn("Use when", desc)
        self.assertIn("NOT for", desc)
        self.assertIn("--bg", self.meta.get("argument-hint", ""))

    def test_length(self):
        self.assertLessEqual(len(self.body.splitlines()), 260)

    def test_commands_exist(self):
        known = known_commands()
        for path in docs():
            names = named_commands(text(path))
            if path == SKILL:
                self.assertTrue(names, "SKILL.md names no notme commands")
            for name in names:
                self.assertIn(name, known, f"{path.name}: unknown command 'notme {name}'")

    def test_plugin_root_paths_resolve(self):
        for path in docs():
            for m in re.finditer(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./\-]+)", text(path)):
                rel = m.group(1).rstrip(".,;:)")
                self.assertTrue((PLUGIN / rel).exists(), f"{path.name}: {rel} does not exist")
        self.assertIn("substituted at load time", self.body)

    def test_verb_list_marker_prohibitions(self):
        self.assertIn(VERBS, self.body)
        self.assertIn(MARKER, self.body)
        self.assertIn(PROHIBITIONS, self.body)

    def test_labels_exist(self):
        labels = defined_labels()
        self.assertEqual(len(labels), 11)
        prefixes = {l.split("_")[0] for l in labels if "_" in l}
        for path in docs():
            for span in re.findall(r"`([^`\n]+)`", text(path)):
                if re.fullmatch(r"[a-z]+(_[a-z]+)+", span) and span.split("_")[0] in prefixes:
                    self.assertIn(span, labels, f"{path.name}: unknown label `{span}`")

    def test_dotted_config_keys_exist(self):
        meta = default_meta()
        project = meta["project"]
        for path in docs():
            for span in re.findall(r"`([a-z_]+(?:\.[a-z_]+)+)`", text(path)):
                if span.endswith(FILE_EXT):
                    continue
                parts = span.split(".")
                if parts[0] in meta and parts[0] != "project":
                    node = meta[parts[0]]
                    rest = parts[1:]
                elif parts[0] in project:
                    node = project
                    rest = parts
                else:
                    self.fail(f"{path.name}: `{span}` is not a config key")
                for part in rest:
                    self.assertIsInstance(node, dict, f"{path.name}: `{span}`")
                    self.assertIn(part, node, f"{path.name}: `{span}` is not in the defaults")
                    node = node[part]

    def test_no_forbidden_strings(self):
        deny = [ln.strip().lower() for ln in text(DENYLIST).splitlines()
                if ln.strip() and not ln.startswith("#")]
        for path in docs():
            body = text(path)
            low = body.lower()
            for token in FORBIDDEN:
                self.assertNotIn(token, body, f"{path.name}: forbidden '{token}'")
            for token in deny:
                self.assertNotIn(token, low, f"{path.name}: denylisted '{token}'")
            self.assertNotIn(EM_DASH, body, f"{path.name}: em dash")
            self.assertIsNone(re.search(r"(?<![\w$}])/tmp\b", body), f"{path.name}: bare temp dir")
            self.assertNotIn("$CLAUDE_PLUGIN_ROOT/", body, f"{path.name}: shell-style plugin root")

    def test_raw_writes_only_in_allowed_blocks(self):
        for path in docs():
            prev, in_fence, allowed = "", False, False
            for line in text(path).splitlines():
                if line.lstrip().startswith("```"):
                    if not in_fence:
                        allowed = prev.strip() == "<!-- allowed-raw-write -->"
                    in_fence = not in_fence
                elif re.search(r"\bgit\s+(?:push|commit)\b", line):
                    self.assertTrue(in_fence and allowed, f"{path.name}: raw write outside an allowed block: {line}")
                prev = line

    def test_ask_user_question_only_with_bg(self):
        for path in docs():
            for line in text(path).splitlines():
                if "AskUserQuestion" in line:
                    self.assertIn("--bg", line, f"{path.name}: AskUserQuestion without --bg guidance")

    def test_reply_thread_never_resolves(self):
        low = self.body.lower()
        starts = [m.start() for m in re.finditer(r"notme reply-thread", low)]
        self.assertTrue(starts, "SKILL.md never names notme reply-thread")
        for at in starts:
            window = low[max(0, at - 400):at + 400]
            self.assertTrue("never resolve" in window or "do not resolve" in window,
                            "reply-thread without a nearby 'never resolve'")

    def test_ignored_logged(self):
        self.assertIn("IGNORED", self.body)
        self.assertIn("ignored_count", self.body)

    def test_decision_order(self):
        step = self.body.split("## Step 4", 1)[1].split("## Step 5", 1)[0].lower()
        at = [step.find(word) for word in ("refuse", "defer", "implement", "disagree")]
        self.assertNotIn(-1, at, f"decision words missing from step 4: {at}")
        self.assertEqual(at, sorted(at), "step 4 must order refuse, defer, implement, disagree")
        body = self.body.lower()
        self.assertRegex(body, r"refuse.*defer.*implement.*disagree")

    def test_step_references_exist(self):
        for name in ("findings.md", "write-back-assembly.md", "failures.md"):
            self.assertTrue((SKILL_DIR / "references" / name).is_file(), name)
            self.assertIn(f"skills/update/references/{name}", self.body)

    def test_every_park_adds_needs_human(self):
        findings = text(SKILL_DIR / "references" / "findings.md")
        table = findings.split("## Park labels", 1)[1].split("## Checklist", 1)[0]
        rows = [r for r in table.splitlines() if r.startswith("| ") and "`needs_review`" in r]
        self.assertEqual(len(rows), 4, "park label table must have four rows")
        for row in rows:
            self.assertIn("`needs_human`", row, f"park row without needs_human: {row}")
        deferred = next(r for r in rows if r.startswith("| Only deferrals"))
        self.assertIn("`question`", deferred)
        refused = next(r for r in rows if r.startswith("| Only refusals"))
        self.assertNotIn("`question`", refused)
        assembly = text(SKILL_DIR / "references" / "write-back-assembly.md")
        for row in assembly.splitlines():
            if row.startswith("| ") and "`PARKED" in row:
                self.assertIn("`needs_human`", row, f"PARKED outcome without needs_human: {row}")
        self.assertIn("needs_human", self.body.split("## Step 4", 1)[1].split("## Step 5", 1)[0])

    def test_ci_red_at_cap_still_needs_work(self):
        failures = text(SKILL_DIR / "references" / "failures.md")
        row = next(r for r in failures.splitlines() if r.startswith("| CI red at"))
        for part in ("`needs_work`", "`needs_human`", "ci-red"):
            self.assertIn(part, row)


if __name__ == "__main__":
    unittest.main()
