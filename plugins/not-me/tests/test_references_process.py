"""Shape checks for the process reference docs under references/."""
import re
import unittest
from pathlib import Path

from notme import frontmatter

PLUGIN = Path(__file__).resolve().parent.parent
REFS = PLUGIN / "references"
DEFAULTS = PLUGIN / "defaults"
DOC_NAMES = ("project", "standards", "verify", "review", "prove", "role")

FILES = [
    "verification.md", "gate.md", "branching.md", "autonomy-and-logging.md",
    "pr-shape.md", "bg-mode.md", "config-and-layers.md", "delegation.md",
    "companions.md",
]
MAX_LINES = 180
FORBIDDEN = [
    "/Users/", "/home/", "yalesites", "pantheon", "lando",
    "miketullo", "dblanken", "alan-", "Dave", "Mike",
]
EM_DASH = "—"
FILE_SUFFIXES = {"md", "py", "yml", "yaml", "json", "jsonl", "txt", "sh", "env", "html", "js", "css"}
DOTTED = re.compile(r"[a-z_]+(\.[a-z_]+)+")


def defaults_meta():
    """Parsed frontmatter of each default doc, keyed by doc name."""
    out = {}
    for name in DOC_NAMES:
        meta, _ = frontmatter.parse((DEFAULTS / f"{name}.md").read_text(encoding="utf-8"), source=name)
        out[name] = meta
    return out


def defined_labels(meta):
    return set(meta["project"]["labels"])


def backticked(text):
    return re.findall(r"`([^`\n]+)`", text)


def label_mentions(text, labels):
    """Backticked snake_case words that start like a label, or any `word` label."""
    prefixes = {l.split("_")[0] for l in labels if "_" in l}
    found = set()
    for span in backticked(text):
        if re.fullmatch(r"[a-z]+(_[a-z]+)+", span) and span.split("_")[0] in prefixes:
            found.add(span)
    for m in re.finditer(r"`([a-z_]+)` label", text):
        found.add(m.group(1))
    return found


def _resolves(meta, parts):
    node = meta
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def key_resolves(key, metas):
    """True when a dotted key exists in a default doc, with or without a doc-name prefix."""
    parts = key.split(".")
    if parts[0] in metas and _resolves(metas[parts[0]], parts[1:]):
        return True
    return any(_resolves(m, parts) for m in metas.values())


def config_mentions(text):
    """Backticked dotted tokens that read as config keys, not file names."""
    return {
        span for span in backticked(text)
        if DOTTED.fullmatch(span) and span.rsplit(".", 1)[1] not in FILE_SUFFIXES
    }


class ProcessReferenceDocsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metas = defaults_meta()
        cls.labels = defined_labels(cls.metas)

    def test_label_set_parsed(self):
        self.assertEqual(len(self.labels), 11, sorted(self.labels))

    def _check(self, name):
        path = REFS / name
        self.assertTrue(path.is_file(), f"{name} missing")
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()

        self.assertLessEqual(len(lines), MAX_LINES, f"{name} has {len(lines)} lines")

        headed = [l for l in lines if l.strip()]
        self.assertTrue(headed[0].startswith("# "), f"{name} lacks an H1")
        self.assertEqual(headed[1], "## When to read this", f"{name} second heading")

        h2 = [i for i, l in enumerate(lines) if l.startswith("## ")]
        self.assertEqual(lines[h2[-1]], "## Checklist", f"{name} must end with Checklist")
        items = [l for l in lines[h2[-1]:] if l.startswith("- [ ] ")]
        self.assertGreaterEqual(len(items), 4, f"{name} checklist too short")

        for word in FORBIDDEN:
            self.assertNotIn(word, text, f"{name} contains {word!r}")
        self.assertNotIn(EM_DASH, text, f"{name} contains an em dash")

        unknown = label_mentions(text, self.labels) - self.labels
        self.assertFalse(unknown, f"{name} mentions unknown labels {sorted(unknown)}")

        bad = sorted(k for k in config_mentions(text) if not key_resolves(k, self.metas))
        self.assertFalse(bad, f"{name} mentions config keys missing from defaults {bad}")

    def test_verification(self):
        self._check("verification.md")

    def test_gate(self):
        self._check("gate.md")

    def test_gate_code_review_fallback(self):
        text = (REFS / "gate.md").read_text(encoding="utf-8")
        start = text.index("3. **Code review.**")
        step = text[start:text.index("4. **Security review.**")]
        self.assertIn("pr-review-toolkit:code-reviewer", step)
        self.assertIn("general-purpose", step)
        self.assertIn("reviewer=", step)
        self.assertIn("no fallback", step)
        cannot = text[text.index("## When a gate step cannot run"):text.index("## Order with the rest")]
        self.assertIn("general-purpose", cannot)

    def test_branching(self):
        self._check("branching.md")

    def test_autonomy_and_logging(self):
        self._check("autonomy-and-logging.md")

    def test_pr_shape(self):
        self._check("pr-shape.md")

    def test_bg_mode(self):
        self._check("bg-mode.md")

    def test_config_and_layers(self):
        self._check("config-and-layers.md")

    def test_delegation(self):
        self._check("delegation.md")

    def test_companions(self):
        self._check("companions.md")

    def test_key_detector(self):
        sample = "Use `models.max_tries`, `standards.branch`, `models.maxtries`, read `verify.md`."
        found = config_mentions(sample)
        self.assertEqual(found, {"models.max_tries", "standards.branch", "models.maxtries"})
        self.assertEqual(sorted(k for k in found if not key_resolves(k, self.metas)), ["models.maxtries"])

    def test_label_detector_catches_typos(self):
        sample = "Add `needs_humans` and the `review` label; run `test_sync`."
        self.assertEqual(label_mentions(sample, self.labels) - self.labels, {"needs_humans", "review"})


if __name__ == "__main__":
    unittest.main()
