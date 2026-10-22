import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401  (puts evals-harness on sys.path)
import transcript as tx


def line(kind, content, **extra):
    obj = {"type": kind, "message": {"role": kind, "content": content}, "sessionId": "s1", "cwd": "/w",
           "isSidechain": False, "timestamp": "2026-10-21T10:00:00.000Z"}
    obj.update(extra)
    return obj


def tool_use(name, args, id_="t1", **extra):
    return line("assistant", [{"type": "tool_use", "id": id_, "name": name, "input": args}], **extra)


def result(text, id_="t1", **extra):
    return line("user", [{"type": "tool_result", "tool_use_id": id_, "content": text}], **extra)


def say(text, **extra):
    return line("assistant", [{"type": "text", "text": text}], **extra)


def write_jsonl(path, objects, raw=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([json.dumps(o) for o in objects] + list(raw)) + "\n", encoding="utf-8")


def synthetic():
    return [
        line("user", "/not-me:new --bg"),
        say("Checking the board."),
        tool_use("Bash", {"command": "notme pick-issue --json"}, "t1"),
        result("{\"issues\": []}", "t1"),
        tool_use("AskUserQuestion", {"questions": []}, "t2"),
        result([{"type": "text", "text": "no answer"}], "t2"),
        tool_use("Write", {"file_path": "/w/notes.md", "content": "x"}, "t3"),
        say("sidechain chatter", isSidechain=True),
        {"type": "queue-operation", "operation": "enqueue"},
        say("NONE - idle; nothing to do"),
    ]


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)


class ProjectDirTest(unittest.TestCase):
    def test_every_non_alphanumeric_becomes_a_dash(self):
        home = Path("/h")
        self.assertEqual(tx.project_dir_for(Path("/Users/x/code/not-me"), home),
                         home / ".claude/projects/-Users-x-code-not-me")
        self.assertEqual(tx.project_dir_for(Path("/Users/x/.claude/a b"), home).name, "-Users-x--claude-a-b")
        self.assertEqual(tx.project_dir_for(Path("/Users/x/a_b.c"), home).name, "-Users-x-a-b-c")

    def test_default_home(self):
        self.assertEqual(tx.project_dir_for(Path("/a")).parent, Path.home() / ".claude" / "projects")


class FindTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.cwd = Path("/w/x")
        self.pdir = tx.project_dir_for(self.cwd, self.tmp)
        self.pdir.mkdir(parents=True)

    def make(self, name, mtime):
        p = self.pdir / name
        p.write_text("{}\n", encoding="utf-8")
        os.utime(str(p), (mtime, mtime))
        return p

    def test_newest_first_and_since(self):
        old, mid, new = self.make("a.jsonl", 100), self.make("b.jsonl", 200), self.make("c.jsonl", 300)
        (self.pdir / "notes.txt").write_text("x")
        self.assertEqual(tx.find_transcripts(self.cwd, 0, self.tmp), [new, mid, old])
        self.assertEqual(tx.find_transcripts(self.cwd, 150, self.tmp), [new, mid])
        self.assertEqual(tx.find_transcripts(self.cwd, 300, self.tmp), [])

    def test_session_id_and_missing_dir(self):
        one = self.make("abc.jsonl", 100)
        self.assertEqual(tx.find_transcripts(self.cwd, 9999, self.tmp, session_id="abc"), [one])
        self.assertEqual(tx.find_transcripts(self.cwd, 0, self.tmp, session_id="nope"), [])
        self.assertEqual(tx.find_transcripts(Path("/other"), 0, self.tmp), [])


class LoadTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "s1.jsonl"
        write_jsonl(self.path, synthetic(), raw=["not json", "[1, 2]", ""])

    def test_fields(self):
        t = tx.load(self.path)
        self.assertEqual([(n, i) for n, _, i in t.tool_uses], [("Bash", 0), ("AskUserQuestion", 1), ("Write", 2)])
        self.assertEqual(t.tool_uses[0][1], {"command": "notme pick-issue --json"})
        self.assertEqual(t.questions_asked, 1)
        self.assertEqual(t.files_written, ["/w/notes.md"])
        self.assertEqual((t.session_id, t.cwd), ("s1", "/w"))

    def test_last_message_ignores_sidechains(self):
        write_jsonl(self.path, synthetic() + [say("late sidechain", isSidechain=True)])
        self.assertEqual(tx.load(self.path).last_message, "NONE - idle; nothing to do")

    def test_trace(self):
        trace = tx.load(self.path).trace.splitlines()
        self.assertIn('TOOL Bash {"command": "notme pick-issue --json"}', trace)
        self.assertIn('{"issues": []}', trace)
        self.assertIn("no answer", trace)
        self.assertIn("Checking the board.", trace)
        self.assertIn("sidechain chatter", trace)

    def test_subagent_files_are_merged_in_time_order(self):
        write_jsonl(self.path, [
            tool_use("Bash", {"command": "first"}, timestamp="2026-10-21T10:00:01Z"),
            tool_use("Bash", {"command": "third"}, timestamp="2026-10-21T10:00:03Z"),
            say("done"),
        ])
        write_jsonl(self.tmp / "s1" / "subagents" / "agent-a.jsonl", [
            tool_use("Bash", {"command": "second"}, isSidechain=True, timestamp="2026-10-21T10:00:02Z"),
            say("sub says hi", isSidechain=True, timestamp="2026-10-21T10:00:02Z"),
        ])
        t = tx.load(self.path)
        self.assertEqual([a["command"] for _, a, _ in t.tool_uses], ["first", "second", "third"])
        self.assertEqual(t.last_message, "done")

    def test_edit_and_notebook_paths(self):
        write_jsonl(self.path, [tool_use("Edit", {"file_path": "/w/a"}),
                                tool_use("NotebookEdit", {"notebook_path": "/w/b.ipynb"}),
                                tool_use("Read", {"file_path": "/w/c"})])
        self.assertEqual(tx.load(self.path).files_written, ["/w/a", "/w/b.ipynb"])

    def test_missing_file_is_empty(self):
        t = tx.load(self.tmp / "nope.jsonl")
        self.assertEqual((t.tool_uses, t.last_message, t.trace), ([], "", ""))


if __name__ == "__main__":
    unittest.main()
