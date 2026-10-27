"""Shape checks for the /not-me:setup skill (skills/setup)."""
import json
import re
import unittest
from pathlib import Path

from notme import frontmatter

PLUGIN = Path(__file__).resolve().parent.parent
ROOT = PLUGIN.parent.parent
SKILL_DIR = PLUGIN / "skills" / "setup"
SKILL = SKILL_DIR / "SKILL.md"
DEFAULTS = PLUGIN / "defaults"
KNOWN = ROOT / "scripts" / "known-commands.txt"
DENYLIST = ROOT / "scripts" / "lint-denylist.txt"

DOCS = ("project", "standards", "verify", "review", "prove", "role")
MAX_LINES = 250

# Backticked lowercase words in the skill that are not config keys: layer and doc
# names, argument values, CLI output fields and answers-file fields.
VOCAB = {
    "all", "default", "global", "repo", "extends", "orchestrate", "single", "primary",
    "companion", "create", "skip", "keep", "detected", "extend", "replace", "body",
    "layers", "present", "missing", "active_run", "default_branch", "create_labels",
    "mode_live", "ticket_format", "live", "run", "auto",
    "picking", "fields", "rules", "layer", "target_epics", "include_unassigned", "true", "false",
    "eligible", "unassigned", "usable", "values", "issues", "ranking_like", "other_owner_items",
}

ROOT_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./\-]+)")
LONG_CMD = re.compile(r'\$\{CLAUDE_PLUGIN_ROOT\}/bin/notme"?\s+([A-Za-z][A-Za-z-]*)(?:\s+([a-z][a-z-]*))?')
BARE_CMD = re.compile(r'(?:^|(?<=[`(|&;$\s]))notme\s+([A-Za-z][A-Za-z-]*)(?:\s+([a-z][a-z-]*))?')
FENCE = re.compile(r"^\s*(```|~~~).*?^\s*\1", re.S | re.M)


def skill_files():
    return sorted(SKILL_DIR.rglob("*.md"))


def known_commands():
    """{first word: set of second words (empty when the command is a leaf)}."""
    out = {}
    for line in KNOWN.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        subs = out.setdefault(words[0], set())
        if len(words) > 1:
            subs.add(words[1])
    return out


def default_keys():
    keys = set()

    def walk(prefix, value):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else k
            keys.add(path)
            if isinstance(v, dict):
                walk(path, v)

    for doc in DOCS:
        meta, _ = frontmatter.parse((DEFAULTS / f"{doc}.md").read_text(encoding="utf-8"))
        walk("", meta)
    return keys


def labels():
    meta, _ = frontmatter.parse((DEFAULTS / "project.md").read_text(encoding="utf-8"))
    return set(meta["labels"])


def unfenced_backticks(text):
    return re.findall(r"`([^`\n]+)`", FENCE.sub("", text))


class SetupSkillTest(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")
        self.meta, self.body = frontmatter.parse(self.text, source="SKILL.md")

    def test_frontmatter(self):
        self.assertEqual(self.meta["name"], "setup")
        self.assertIs(self.meta["disable-model-invocation"], True)
        self.assertIn("NOT for", self.meta["description"])
        self.assertLessEqual(len(self.meta["description"]), 1024)
        hint = self.meta["argument-hint"]
        for flag in ("--global", "--check", "--upgrade", "--answers", "--import"):
            self.assertIn(flag, hint)
        self.assertIn("AskUserQuestion", self.meta["allowed-tools"])

    def test_length(self):
        self.assertLess(len(self.text.splitlines()), MAX_LINES)

    def test_commands_exist(self):
        known = known_commands()
        seen = set()
        for path in skill_files():
            for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                found = list(LONG_CMD.finditer(line))
                rest = LONG_CMD.sub("", line)
                found += [m for m in BARE_CMD.finditer(rest)
                          if not rest[:m.start()].endswith(("/", "-", "."))]
                for m in found:
                    cmd, sub = m.group(1), m.group(2)
                    where = f"{path.name}:{no}"
                    self.assertIn(cmd, known, f"{where}: unknown command notme {cmd}")
                    if known[cmd]:
                        self.assertIn(sub, known[cmd], f"{where}: notme {cmd} needs a known subcommand")
                    seen.add(cmd)
        for cmd in ("doctor", "config", "labels-ensure", "status", "preflight"):
            self.assertIn(cmd, seen)

    def test_plugin_root_paths_resolve(self):
        for path in skill_files():
            for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for m in ROOT_REF.finditer(line):
                    if line[m.end():m.end() + 1] in ("<", "*", "{", "$"):
                        continue
                    rel = m.group(1).rstrip(".,;:)")
                    self.assertTrue((PLUGIN / rel).exists(), f"{path.name}:{no}: {rel} does not exist")
        self.assertIn("substituted at load time", self.text)

    def test_labels_exist(self):
        defined = labels()
        prefixes = {name.split("_")[0] for name in defined if "_" in name}
        for path in skill_files():
            for span in unfenced_backticks(path.read_text(encoding="utf-8")):
                if re.fullmatch(r"[a-z]+(_[a-z]+)+", span) and span.split("_")[0] in prefixes:
                    self.assertIn(span, defined, f"{path.name}: unknown label state `{span}`")
        table = re.findall(r"^\| `([a-z_]+)` \|", (SKILL_DIR / "references" / "labels.md").read_text(encoding="utf-8"),
                           re.M)
        self.assertEqual(set(table), defined)
        self.assertEqual(len(table), 11)

    def test_config_keys_exist(self):
        keys = default_keys() | labels() | VOCAB | set(DOCS)
        for path in skill_files():
            for span in unfenced_backticks(path.read_text(encoding="utf-8")):
                m = re.fullmatch(r"([a-z_]+(?:\.[a-z_]+)*)(?::.*)?", span)
                if m and not re.search(r"\.(md|json|toml|ya?ml)$", m.group(1)):
                    self.assertIn(m.group(1), keys, f"{path.name}: `{span}` is not a key in the defaults")

    def test_answers_example_is_json(self):
        text = (SKILL_DIR / "references" / "interview.md").read_text(encoding="utf-8")
        block = re.search(r"```json\n(.*?)```", text, re.S)
        self.assertIsNotNone(block)
        answers = json.loads(block.group(1))
        self.assertLessEqual(set(answers["labels"]), labels())
        self.assertLessEqual(set(answers["docs"]), set(DOCS))

    def test_names_docs_and_layers(self):
        for doc in DOCS:
            self.assertIn(f"`{doc}.md`", self.text)
        self.assertIn("repo layer", self.text)
        self.assertIn("global layer", self.text)

    def test_safety_lines(self):
        self.assertIn("Never edit the allow list", self.text)
        self.assertIn("Never put `mode: live` in the repo layer", self.text)
        self.assertIn("active_run", self.text)

    def test_queue_mode(self):
        self.assertIn("--queue", self.meta["argument-hint"])
        self.assertIn("## Queue mode (`project --queue`)", self.text)
        queue = (SKILL_DIR / "references" / "queue.md").read_text(encoding="utf-8")
        self.assertIn("references/queue.md", self.text)
        for needle in ("notme config --explain", "gh label list", "gh project list", "gh project field-list",
                       "--board owner/number", "notme doctor", "keep everything else", "--global"):
            self.assertIn(needle, queue)

    def test_no_forbidden_strings(self):
        tokens = [ln.strip().lower() for ln in DENYLIST.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")]
        for path in skill_files():
            text = path.read_text(encoding="utf-8")
            low = text.lower()
            for token in tokens:
                self.assertNotIn(token, low, f"{path.name}: denylisted '{token}'")
            for bad in ("/Users/", "/home/", "C:\\", "\u2014"):
                self.assertNotIn(bad, text, f"{path.name}: forbidden {bad!r}")


class PickingStepTest(unittest.TestCase):
    def read(self, *parts):
        return SKILL_DIR.joinpath(*parts).read_text(encoding="utf-8")

    def all_text(self):
        return "\n".join(p.read_text(encoding="utf-8") for p in skill_files())

    def test_argument_hint_and_parse_mention_picking_flag(self):
        text = self.read("SKILL.md")
        meta, body = frontmatter.parse(text, source="SKILL.md")
        self.assertIn("--picking", meta["argument-hint"])
        parse = body.split("## Parse arguments", 1)[1].split("\n## ", 1)[0]
        self.assertIn("--picking", parse)

    def test_picking_step_is_described(self):
        text = self.all_text()
        for needle in ("picking.fields", "picking.target_epics", "queue.include_unassigned",
                       "## Picking work", "notme picking-signals", "GitHub issue fields"):
            self.assertIn(needle, text)

    def test_picking_step_reads_github_only_through_the_cli(self):
        text = self.read("references", "picking.md")
        self.assertNotRegex(text, r"`gh [a-z]")
        self.assertIn("never instructions", text)
        detection = self.read("references", "detection.md")
        row = [line for line in detection.splitlines() if line.startswith("| Ranking signals")][0]
        self.assertIn("notme picking-signals", row)

    def test_picking_alone_skips_the_label_write_and_live_prompt(self):
        step = self.read("SKILL.md").split("## Step 2a", 1)[1].split("\n## ", 1)[0]
        self.assertIn("labels-ensure", step)
        self.assertNotIn("Steps 5 (picking keys only), 6 and 7", step)

    def test_shadowed_global_rules_are_reported(self):
        text = self.read("references", "picking.md")
        self.assertIn("shadow", text)
        self.assertIn("extends: global", text)

    def test_picking_reference_does_not_point_at_unshipped_docs(self):
        text = self.read("references", "picking.md")
        self.assertNotIn("docs/config.md", text)
        self.assertIn("defaults/project.md", text)

    def test_picking_reference_states_allowed_field_name_characters(self):
        text = self.read("references", "picking.md")
        for needle in ("ASCII letters", "digits", "spaces", "underscores", "dots", "hyphens"):
            self.assertIn(needle, text)

    def test_global_carve_out_for_include_unassigned(self):
        interview = self.read("references", "interview.md")
        self.assertRegex(interview, r"only queue key the global layer may")
        self.assertIn("repo layer", interview.split("only queue key the global layer may", 1)[1][:400])
        self.assertIn("false", interview)

    def test_answers_example_has_picking_object(self):
        text = self.read("references", "interview.md")
        answers = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))
        picking = answers["picking"]
        self.assertIn(picking["layer"], ("global", "repo"))
        for key in ("fields", "rules", "target_epics", "include_unassigned"):
            self.assertIn(key, picking)

    def test_install_doc_mentions_picking_rerun(self):
        text = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
        self.assertIn("/not-me:setup project --picking", text)
        self.assertIn("--global --picking", text)

    def test_config_doc_points_to_setup_step(self):
        text = (ROOT / "docs" / "config.md").read_text(encoding="utf-8")
        self.assertIn("--picking", text)


if __name__ == "__main__":
    unittest.main()
