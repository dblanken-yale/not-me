import io
import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
import graders as g
import transcript as tx
from test_transcript import synthetic, write_jsonl, tool_use, say, result

CASES = g.CASES


def grader(**meta):
    body = meta.pop("body", "")
    return g.Grader(meta.pop("name", "x"), meta["type"], meta.get("weight", 1), meta, body)


class GraderTypesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, str(self.tmp), True)
        path = self.tmp / "s.jsonl"
        write_jsonl(path, synthetic())
        self.tr = tx.load(path)
        self.ws = self.tmp / "ws"
        (self.ws / ".git" / "not-me" / "logs").mkdir(parents=True)
        (self.ws / ".git" / "not-me" / "logs" / "r.md").write_text("log\n")
        (self.ws / "project.md").write_text("make lint\n")

    def ok(self, **meta):
        return g.run_grader(grader(**meta), self.tr, self.ws).ok

    def test_regex_last_message(self):
        self.assertTrue(self.ok(type="regex", target="last_message", match="contains", body="^NONE - idle"))
        self.assertFalse(self.ok(type="regex", target="last_message", match="contains", body="^PR\\b"))
        self.assertTrue(self.ok(type="regex", target="last_message", match="contains", flags="i", body="^none"))

    def test_regex_ignores_sidechain_for_last_message_only(self):
        self.assertTrue(self.ok(type="regex", target="trace", match="contains", body="sidechain chatter"))
        self.assertFalse(self.ok(type="regex", target="last_message", match="contains", body="sidechain"))

    def test_regex_trace_not_contains(self):
        self.assertTrue(self.ok(type="regex", target="trace", match="not_contains", body="gh pr merge"))
        self.assertFalse(self.ok(type="regex", target="trace", match="not_contains", body="notme pick-issue"))

    def test_regex_count_and_file_target(self):
        self.assertTrue(self.ok(type="regex", target="trace", match="count:1", body="notme pick-issue"))
        self.assertFalse(self.ok(type="regex", target="trace", match="count:2", body="notme pick-issue"))
        self.assertTrue(self.ok(type="regex", target={"source": "file", "path": "project.md"},
                                match="contains", body="make lint"))
        self.assertFalse(self.ok(type="regex", target={"source": "file", "path": "gone.md"},
                                 match="contains", body="make lint"))

    def test_regex_bad_pattern_fails_not_raises(self):
        r = g.run_grader(grader(type="regex", target="trace", match="contains", body="("), self.tr, self.ws)
        self.assertFalse(r.ok)
        self.assertIn("bad regex", r.detail)

    def test_tool_used(self):
        self.assertTrue(self.ok(type="tool_used", tool="Bash", input_match="notme\\s+pick-issue", min=1))
        self.assertFalse(self.ok(type="tool_used", tool="Bash", input_match="notme\\s+claim-issue", min=1))
        self.assertTrue(self.ok(type="tool_used", tool="Bash", input_match="notme\\s+claim-issue", max=0))
        self.assertFalse(self.ok(type="tool_used", tool="AskUserQuestion", max=0))
        self.assertTrue(self.ok(type="tool_used", tool="Write"))
        self.assertFalse(self.ok(type="tool_used", tool="Bash", min=2))

    def test_tool_order(self):
        self.assertTrue(self.ok(type="tool_order", before="Bash:pick-issue", after="Write"))
        self.assertFalse(self.ok(type="tool_order", before="Write", after="Bash"))
        self.assertTrue(self.ok(type="tool_order", before={"tool": "Bash", "input_match": "pick"},
                                after={"tool": "AskUserQuestion"}))
        self.assertFalse(self.ok(type="tool_order", before="Bash", after="Glob"))

    def test_file_exists(self):
        self.assertTrue(self.ok(type="file_exists", path=".git/not-me/logs/*.md"))
        self.assertFalse(self.ok(type="file_exists", path=".git/not-me/logs/*.json"))
        self.assertTrue(self.ok(type="file_exists", path=".git/not-me/logs/*.json", exists=False))
        self.assertFalse(self.ok(type="file_exists", path=".git/not-me/logs/*.md", exists=False))
        self.assertFalse(self.ok(type="file_exists", path="/etc/passwd"))

    def test_llm_is_skipped_and_excluded(self):
        case = g.Case("c", self.tmp, {}, "", [
            grader(name="a", type="regex", weight=2, target="last_message", match="contains", body="^NONE"),
            grader(name="b", type="regex", weight=2, target="last_message", match="contains", body="^PR"),
            grader(name="j", type="llm", weight=3, criteria="is it good", focus="trace")])
        rep = g.grade(case, self.tr, self.ws)
        self.assertEqual((rep.skipped_llm, rep.score, rep.passed), (1, 0.5, False))
        judge = [r for r in rep.graders if r.name == "j"][0]
        self.assertTrue(judge.skipped)
        self.assertEqual(judge.detail, "llm graders need a judge; advisory only")

    def test_weighted_score_and_pass(self):
        case = g.Case("c", self.tmp, {}, "", [
            grader(name="a", type="tool_used", weight=3, tool="Write"),
            grader(name="b", type="tool_used", weight=1, tool="Glob")])
        rep = g.grade(case, self.tr, self.ws)
        self.assertEqual((rep.score, rep.passed), (0.75, False))
        only_llm = g.grade(g.Case("c", self.tmp, {}, "", [grader(type="llm", criteria="x")]), self.tr, self.ws)
        self.assertFalse(only_llm.passed)

    def test_report_json(self):
        case = g.Case("c", self.tmp, {}, "", [grader(name="a", type="tool_used", tool="Write")])
        d = json.loads(json.dumps(g.grade(case, self.tr, self.ws).to_dict()))
        self.assertEqual(set(d), {"case", "score", "passed", "graders", "skipped_llm"})
        self.assertEqual(set(d["graders"][0]), {"name", "type", "weight", "ok", "detail", "skipped"})


class CaseLoadTest(unittest.TestCase):
    def test_every_case_loads_with_supported_graders(self):
        dirs = sorted(p.parent for p in CASES.glob("*/prompt.md"))
        self.assertGreaterEqual(len(dirs), 40)
        for d in dirs:
            with self.subTest(case=d.name):
                case = g.load_case(d)
                self.assertTrue(case.graders)
                self.assertTrue(case.prompt)
                for gr in case.graders:
                    self.assertIn(gr.type, g.SUPPORTED_TYPES, gr.name)

    def test_new_n2_passes_an_idle_run(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, str(tmp), True)
        path = tmp / "s.jsonl"
        write_jsonl(path, [tool_use("Bash", {"command": "notme pick-issue"}), result("none"),
                           say("NONE - idle; nothing to do")])
        ws = tmp / "ws"
        (ws / ".git" / "not-me" / "logs" / "new").mkdir(parents=True)
        (ws / ".git" / "not-me" / "logs" / "new" / "r1.md").write_text("x")
        out, js = io.StringIO(), tmp / "out" / "r.json"
        code = g.main(["--case", "new-N2", "--transcript", str(path), "--workspace", str(ws), "--json", str(js)], out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("result-verb", out.getvalue())
        self.assertTrue(json.loads(js.read_text())["passed"])
        # no run log written: file_exists fails, exit 1
        (ws / ".git" / "not-me" / "logs" / "new" / "r1.md").unlink()
        self.assertEqual(g.main(["--case", "new-N2", "--transcript", str(path), "--workspace", str(ws)], io.StringIO()), 1)

    def test_bad_input_exits_2(self):
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.main(["--case", "no-such", "--transcript", "x", "--workspace", "."], io.StringIO()), 2)


if __name__ == "__main__":
    unittest.main()
