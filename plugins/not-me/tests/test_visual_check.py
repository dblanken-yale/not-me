"""The visual check rules must not tell a run to put a local path on GitHub."""
import re
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))
from notme import writeback  # noqa: E402

VERIFICATION = PLUGIN / "references" / "verification.md"
PR_ASSEMBLY = PLUGIN / "skills" / "new" / "references" / "pr-assembly.md"
REPO = "acme/widgets"


def visual_section():
    text = VERIFICATION.read_text(encoding="utf-8")
    m = re.search(r"^## Visual check\n(.*?)(?=^## )", text, re.S | re.M)
    assert m, "no Visual check section"
    return m.group(1)


def fenced_lines():
    """Visual check lines from each fenced block in the section."""
    blocks = re.findall(r"```\n(.*?)```", visual_section(), re.S)
    return [[ln for ln in b.splitlines() if ln.startswith("Visual check:")] for b in blocks]


class VisualCheckRules(unittest.TestCase):
    def test_log_form_keeps_the_path(self):
        lines = [ln for block in fenced_lines() for ln in block]
        self.assertIn("Visual check: <path of the screenshot you actually read>", lines)

    def test_pr_form_passes_check_text(self):
        pr_lines = [ln for block in fenced_lines() for ln in block
                    if ln.startswith("Visual check: read <")]
        self.assertTrue(pr_lines, "no PR form (words, no path) in the Visual check section")
        pr_lines.append("Visual check: not run (<named reason>)")
        fills = {"what was checked, in words": "the settings page at 375px; the Save button "
                 "sits below the form as the issue asks", "named reason": "no env"}
        for line in pr_lines:
            for key, val in fills.items():
                line = line.replace("<" + key + ">", val)
            self.assertNotIn("<", line)
            body = ("## Summary\nMove the Save button.\n\n## Testing steps\n- [x] Opened the page\n\n"
                    + line + "\n\n## References\nCloses " + REPO + "#43\n")
            self.assertEqual(writeback.check_text(body, tracker=REPO, repo=REPO), [], line)

    def test_pr_assembly_uses_words_form(self):
        text = PR_ASSEMBLY.read_text(encoding="utf-8")
        m = re.search(r"For a UI diff.*?(?=\n   - Any gate finding)", text, re.S)
        self.assertTrue(m)
        item = m.group(0)
        self.assertIn("no local path", item)
        self.assertNotIn("line from the run", item)


if __name__ == "__main__":
    unittest.main()
