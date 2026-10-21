from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from notme import runlog
from notme.runlog import RunLog

NOW = datetime(2026, 3, 4, 5, 6, 7)


class TmpDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = str(Path(tempfile.mkdtemp(prefix="notme-runlog-")).resolve())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = Path(self.tmp) / "proj" / "not-me" / "logs"
        self.root.mkdir(parents=True)
        self.marker = self.root.parent / "active-run"


class StartTests(TmpDirTestCase):

    def test_start_creates_file_with_template(self):
        log = RunLog.start(self.root, "review", "PR 12", now=NOW)
        text = log.path.read_text(encoding="utf-8")
        self.assertTrue(log.path.name.startswith("20260304-050607-pr-12-"))
        self.assertEqual(log.path.parent, self.root / "review")
        self.assertEqual(log.run_id, log.path.stem)
        self.assertTrue(text.startswith(f"# not-me:review PR 12   run={log.run_id}\n"))
        self.assertIn("## Summary\n\n(run in progress)\n\n## Timeline", text)

    def test_collision_retries_with_new_hex(self):
        with mock.patch("notme.runlog.secrets.token_hex", side_effect=["aaaaaa", "aaaaaa", "bbbbbb"]):
            first = RunLog.start(self.root, "s", "r", now=NOW)
            second = RunLog.start(self.root, "s", "r", now=NOW, force=True)
        self.assertTrue(first.path.name.endswith("-aaaaaa.md"))
        self.assertTrue(second.path.name.endswith("-bbbbbb.md"))
        self.assertIn(first.run_id, first.path.read_text(encoding="utf-8"))

    def test_marker_written_on_start(self):
        log = RunLog.start(self.root, "review", "PR 12", now=NOW)
        data = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"run_id", "skill", "ref", "started", "pid"})
        self.assertEqual(data["run_id"], log.run_id)
        self.assertEqual(data["skill"], "review")
        self.assertEqual(data["ref"], "PR 12")

    def test_marker_path_override(self):
        other = Path(self.tmp) / "elsewhere" / "marker.json"
        RunLog.start(self.root, "s", "r", now=NOW, marker_path=other)
        self.assertTrue(other.is_file())
        self.assertFalse(self.marker.exists())


class EntryTests(TmpDirTestCase):
    def setUp(self):
        super().setUp()
        self.log = RunLog.start(self.root, "review", "PR 1", now=NOW)

    def test_action_single_line_with_offset(self):
        self.log.action("first\nsecond", "WARN")
        line = self.log.path.read_text(encoding="utf-8").splitlines()[-1]
        self.assertRegex(line, r"^- \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d  WARN  first / second$")

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.log.action("x", "NOPE")

    def test_finish_puts_failures_first_atomically(self):
        self.log.action("did a thing")
        self.log.question("Which base?", "main", "rebase onto develop")
        with mock.patch("notme.runlog.os.replace", wraps=runlog.os.replace) as replace:
            summary = self.log.finish("partial", failures=["tests red"], refused=["push to main"])
        self.assertEqual(replace.call_count, 1)  # the summary rewrite; the marker is unlinked
        text = self.log.path.read_text(encoding="utf-8")
        self.assertNotIn("(run in progress)", text)
        body = text.split("## Summary\n\n", 1)[1]
        self.assertTrue(body.startswith("Status: partial\n"))
        self.assertLess(body.index("Refused / failures first:"), body.index("Questions and decisions:"))
        self.assertLess(body.index("- push to main"), body.index("- tests red"))
        self.assertLess(body.index("Questions and decisions:"), body.index("Counts:"))
        self.assertIn("1. Which base?\n   - Decision: main\n   - Undo: rebase onto develop", body)
        self.assertIn("ACTION 1", summary)
        self.assertIn("did a thing", text.split("## Timeline", 1)[1])
        self.assertEqual(list(self.log.path.parent.glob("*.tmp")), [])

    def test_finish_with_nothing_says_none(self):
        summary = self.log.finish("ok")
        self.assertIn("Refused / failures first:\nnone", summary)
        self.assertIn("Questions and decisions:\nnone", summary)

    def test_question_round_trip(self):
        self.log.question("Ship it?", "yes", "revert the merge")
        self.assertEqual(self.log.questions, [{"question": "Ship it?", "decision": "yes", "undo": "revert the merge"}])
        reopened = RunLog.open(self.log.path)
        self.assertEqual(reopened.questions, self.log.questions)

    def test_open_parses_entries_and_questions(self):
        self.log.action("a1")
        self.log.action("no thanks", "REFUSED")
        self.log.question("Q?", "D", "U")
        self.log.finish("done")
        reopened = RunLog.open(self.log.path)
        self.assertEqual((reopened.skill, reopened.ref, reopened.run_id), ("review", "PR 1", self.log.run_id))
        self.assertEqual(reopened.status, "done")
        self.assertEqual([(e["kind"], e["message"]) for e in reopened.entries],
                         [("ACTION", "a1"), ("REFUSED", "no thanks"), ("QUESTION", "Q?")])
        self.assertEqual(reopened.questions[0]["undo"], "U")

    def test_open_in_progress_has_no_status(self):
        self.assertIsNone(RunLog.open(self.log.path).status)

    def test_finish_on_reopened_log(self):
        self.log.action("x")
        reopened = RunLog.open(self.log.path)
        reopened.action("y")
        reopened.finish("ok")
        again = RunLog.open(self.log.path)
        self.assertEqual([e["message"] for e in again.entries], ["x", "y"])
        self.assertEqual(again.status, "ok")


class LastTests(TmpDirTestCase):
    def test_last_orders_by_filename(self):
        root = self.root
        older = RunLog.start(root, "review", "a", now=NOW, force=True)
        newer = RunLog.start(root, "retro", "b", now=NOW + timedelta(hours=1), force=True)
        self.assertEqual(runlog.last(root).run_id, newer.run_id)
        self.assertEqual(runlog.last(root, "review").run_id, older.run_id)
        self.assertIsNone(runlog.last(root, "missing"))
        self.assertIsNone(runlog.last(Path(self.tmp) / "nope"))


class MarkerTests(TmpDirTestCase):

    def test_cleared_on_finish(self):
        log = RunLog.start(self.root, "s", "r", now=datetime.now())
        self.assertEqual(runlog.active_run(self.marker)["run_id"], log.run_id)
        log.finish("ok")
        self.assertFalse(self.marker.exists())
        self.assertIsNone(runlog.active_run(self.marker))

    def test_stale_after_six_hours_but_not_deleted(self):
        RunLog.start(self.root, "s", "r", now=NOW)
        self.assertIsNotNone(runlog.active_run(self.marker, now=NOW + timedelta(hours=5, minutes=59)))
        self.assertIsNone(runlog.active_run(self.marker, now=NOW + timedelta(hours=6, minutes=1)))
        self.assertTrue(self.marker.exists())

    def test_not_cleared_by_other_run_id(self):
        log = RunLog.start(self.root, "s", "r", now=NOW)
        self.assertFalse(runlog.marker_clear(self.marker, "someone-else"))
        self.assertTrue(self.marker.exists())
        other = RunLog.start(self.root, "s", "r2", now=NOW, force=True)  # takes over the marker
        log.finish("ok")
        self.assertTrue(self.marker.exists())
        self.assertEqual(runlog.active_run(self.marker, now=NOW)["run_id"], other.run_id)
        self.assertTrue(runlog.marker_clear(self.marker))
        self.assertFalse(self.marker.exists())

    def test_start_refuses_fresh_marker_of_another_run(self):
        first = RunLog.start(self.root, "s", "r", now=NOW)
        with self.assertRaises(runlog.ActiveRunError) as ctx:
            RunLog.start(self.root, "s", "r2", now=NOW + timedelta(hours=1))
        self.assertEqual(ctx.exception.existing["run_id"], first.run_id)
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["run_id"], first.run_id)
        later = RunLog.start(self.root, "s", "r3", now=NOW + timedelta(hours=7))  # stale marker does not block
        self.assertNotEqual(later.run_id, first.run_id)

    def test_missing_or_corrupt_marker_is_none(self):
        self.assertIsNone(runlog.active_run(self.marker))
        self.marker.write_text("not json", encoding="utf-8")
        self.assertIsNone(runlog.active_run(self.marker))
        self.assertFalse(runlog.marker_clear(Path(self.tmp) / "absent"))


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


class RootTests(TmpDirTestCase):
    def test_config_beats_env_beats_git(self):
        repo = Path(self.tmp) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        env = {"NOT_ME_LOG_ROOT": str(Path(self.tmp) / "envroot")}
        cfg = Path(self.tmp) / "cfgroot"
        self.assertEqual(runlog.log_root(repo, str(cfg), env), cfg)
        self.assertTrue(cfg.is_dir())
        self.assertEqual(runlog.log_root(repo, None, env), Path(self.tmp) / "envroot")
        git_root = runlog.log_root(repo, None, {})
        self.assertEqual(git_root, (repo / ".git").resolve() / "not-me" / "logs")
        self.assertTrue(git_root.is_dir())

    def test_config_root_expands_tilde(self):
        with mock.patch.dict("os.environ", {"HOME": self.tmp, "USERPROFILE": self.tmp}):
            root = runlog.log_root(None, "~/lg", {})
        self.assertEqual(root, Path(self.tmp) / "lg")

    def test_linked_worktree_shares_main_root(self):
        repo = Path(self.tmp) / "main"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "i")
        wt = Path(self.tmp) / "linked"
        _git(repo, "worktree", "add", "-q", "-b", "other", str(wt))
        self.assertEqual(runlog.log_root(wt, None, {}), runlog.log_root(repo, None, {}))

    def test_no_root_available_raises(self):
        with self.assertRaises(ValueError):
            runlog.log_root(None, None, {})


class BodyTests(TmpDirTestCase):
    def test_no_absolute_path_in_body(self):
        root = self.root
        log = RunLog.start(root, "review", "PR 1", now=NOW)
        log.action("read config")
        log.question("Q?", "D", "U")
        log.finish("ok", failures=["f"])
        text = log.path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn(str(root), text)


if __name__ == "__main__":
    unittest.main()


class MarkerInMessageTests(TmpDirTestCase):
    def test_section_marker_inside_message_does_not_break_parse(self):
        log = RunLog.start(self.root, "new", "acme/app#1", marker_path=self.marker)
        log.action("saw text: ## Timeline and ## Summary in a comment")
        log.finish("DONE", failures=["body mentioned ## Timeline"])
        again = RunLog.open(log.path, marker_path=self.marker)
        self.assertEqual(again.status, "DONE")
        self.assertEqual(len(again.entries), 1)
        self.assertIn("## Timeline", again.entries[0]["message"])
