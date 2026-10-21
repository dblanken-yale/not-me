"""Shape checks for the shared reference docs under references/."""
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
REFS = PLUGIN / "references"
PROJECT_DEFAULTS = PLUGIN / "defaults" / "project.md"

FILES = ["authority.md", "prohibitions.md", "parking.md", "write-back.md"]
MAX_LINES = 180
FORBIDDEN = [
    "/Users/", "/home/", "yalesites", "pantheon", "lando",
    "miketullo", "dblanken", "alan-", "Dave", "Mike",
]
EM_DASH = "—"



def defined_labels():
    """Abstract label keys from the `labels:` map in defaults/project.md."""
    text = PROJECT_DEFAULTS.read_text(encoding="utf-8")
    keys, inside = set(), False
    for line in text.splitlines():
        if re.match(r"^labels:", line):
            inside = True
            continue
        if inside:
            m = re.match(r"^\s+([a-z_]+):", line)
            if not m:
                break
            keys.add(m.group(1))
    return keys


def backticked(text):
    return re.findall(r"`([^`\n]+)`", text)


def label_mentions(text, labels):
    """Backticked words that read as abstract label names.

    A mention is a backticked snake_case word whose first segment starts a
    defined label (so `needs_humans` is caught but `test_sync` is not), or any
    backticked lowercase word followed by the word "label".
    """
    prefixes = {l.split("_")[0] for l in labels if "_" in l}
    found = set()
    for span in backticked(text):
        if re.fullmatch(r"[a-z]+(_[a-z]+)+", span) and span.split("_")[0] in prefixes:
            found.add(span)
    for m in re.finditer(r"`([a-z_]+)` label", text):
        found.add(m.group(1))
    return found


class ReferenceDocsTest(unittest.TestCase):
    def setUp(self):
        self.labels = defined_labels()

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
        last = lines[h2[-1]]
        self.assertEqual(last, "## Checklist", f"{name} must end with Checklist")
        items = [l for l in lines[h2[-1]:] if l.startswith("- [ ] ")]
        self.assertGreaterEqual(len(items), 4, f"{name} checklist too short")

        for word in FORBIDDEN:
            self.assertNotIn(word, text, f"{name} contains {word!r}")
        self.assertNotIn(EM_DASH, text, f"{name} contains an em dash")

        unknown = label_mentions(text, self.labels) - self.labels
        self.assertFalse(unknown, f"{name} mentions unknown labels {sorted(unknown)}")

    def test_authority(self):
        self._check("authority.md")

    def test_prohibitions(self):
        self._check("prohibitions.md")

    def test_parking(self):
        self._check("parking.md")

    def test_write_back(self):
        self._check("write-back.md")

    def test_label_detector_catches_typos(self):
        sample = "Add `needs_humans` and the `review` label; run `test_sync`."
        self.assertEqual(label_mentions(sample, self.labels) - self.labels, {"needs_humans", "review"})


if __name__ == "__main__":
    unittest.main()
