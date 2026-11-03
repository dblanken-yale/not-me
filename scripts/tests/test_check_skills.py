import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = SCRIPTS / "check-skills.py"


def run(root, *extra):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(FIXTURES / root), *extra],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


class CheckSkillsTest(unittest.TestCase):
    def test_clean_tree_passes(self):
        code, out, _ = run("clean")
        self.assertEqual(code, 0, out)
        self.assertIn("0 violations", out)

    def test_dirty_tree_reports_every_rule_with_a_line(self):
        code, out, _ = run("dirty", "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        rules = {v["rule"] for v in data["violations"]}
        self.assertEqual(rules, {f"R{i}" for i in range(1, 16)})
        for v in data["violations"]:
            self.assertGreaterEqual(v["line"], 1)

    def test_text_line_format(self):
        code, out, _ = run("dirty")
        self.assertEqual(code, 1)
        self.assertIn("plugins/not-me/skills/kitchen/SKILL.md:17: R14:", out)
        self.assertIn("plugins/not-me/skills/kitchen/SKILL.md:10: R3:", out)

    def test_only_filters_rules(self):
        code, out, _ = run("dirty", "--only", "R1,R5", "--json")
        self.assertEqual(code, 1)
        rules = {v["rule"] for v in json.loads(out)["violations"]}
        self.assertEqual(rules, {"R1", "R5"})

    def test_only_with_clean_rule_set_exits_zero(self):
        code, _, _ = run("clean", "--only", "R5")
        self.assertEqual(code, 0)

    def test_unknown_rule_is_rejected(self):
        code, _, err = run("clean", "--only", "R99")
        self.assertEqual(code, 2)
        self.assertIn("R99", err)

    def test_json_shape(self):
        code, out, _ = run("clean", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"violations", "notices", "count"})
        self.assertEqual(data["count"], 0)
        _, out, _ = run("dirty", "--json")
        first = json.loads(out)["violations"][0]
        self.assertEqual(set(first), {"path", "line", "rule", "message"})

    def test_duplicate_marker_flagged(self):
        _, out, _ = run("dirty", "--only", "R9", "--json")
        messages = [v["message"] for v in json.loads(out)["violations"]]
        self.assertTrue(any("also used in" in m for m in messages), messages)

    def test_allowed_raw_write_block_is_exempt(self):
        _, out, _ = run("clean", "--only", "R6", "--json")
        self.assertEqual(json.loads(out)["count"], 0)


class ReasonRuleTest(unittest.TestCase):
    """R15: a call that changes a state names --reason-file, and only its own call counts."""

    @staticmethod
    def found(text):
        sys.path.insert(0, str(SCRIPTS))
        import importlib.util
        spec = importlib.util.spec_from_file_location("check_skills", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("check_skills", mod)
        spec.loader.exec_module(mod)
        return [cmd for _, cmd in mod.reason_violations(text)]

    def test_a_call_with_a_reason_file_passes(self):
        self.assertEqual(self.found("`notme move-issue 5 --to blocked --reason-file <r> --log <p>`"), [])

    def test_a_call_without_one_is_flagged(self):
        self.assertEqual(self.found("Run `notme release-issue 5 --log <p>` now."), ["notme release-issue"])

    def test_a_later_call_in_the_same_block_does_not_cover_an_earlier_one(self):
        block = "```bash\nnotme move-issue 5 --to blocked\nnotme release-issue 5 --reason-file r\n```\n"
        self.assertEqual(self.found(block), ["notme move-issue"])

    def test_the_flag_on_a_continuation_line_counts(self):
        self.assertEqual(self.found("notme transition 3 --from a --to b \\\n  --reason-file r\n"), [])

    def test_the_wrapper_and_variable_forms_are_caught(self):
        text = ('"${CLAUDE_PLUGIN_ROOT}/bin/notme" move-issue 5 --to blocked\n\n'
                '"$NOTME" transition 5 --from a --to b\n\n'
                'notme labels-sync 5 --add "needs human"\n')
        self.assertEqual(self.found(text), ["notme\" move-issue", "$NOTME\" transition", "notme labels-sync"])

    def test_release_issue_with_no_comment_needs_no_reason(self):
        self.assertEqual(self.found("`notme release-issue 5 --no-comment --log <p>`"), [])
        self.assertEqual(self.found("`notme move-issue 5 --to todo --no-comment`"), ["notme move-issue"])

    def test_labels_sync_check_needs_no_reason(self):
        self.assertEqual(self.found("`notme labels-sync 5 --set a --check`"), [])


if __name__ == "__main__":
    unittest.main()
