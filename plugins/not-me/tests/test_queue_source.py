"""Where the queue looks, in plain words (queue.describe), the board's Status options, and the doctor's queue
check."""
from __future__ import annotations

import copy
import unittest

from notme import doctor
from notme.gh import GhClient
from notme.queue import describe
from notme.queue.board import status_options
from notme.writes import GuardContext
from tests.fakes.fakegh import FakeRunner
from tests.test_queue_labels import PROJECT_META, REPO


def meta(**queue):
    out = copy.deepcopy(PROJECT_META)
    out["queue"].update(queue)
    return out


BOARD = {"owner": "acme", "number": 6, "status_field": "Status", "priority_fields": []}


class DescribeTests(unittest.TestCase):
    def test_labels_source_names_the_todo_label(self):
        out = describe(meta())
        self.assertEqual(out["source"], "labels")
        self.assertEqual(out["label"], "to do")
        self.assertTrue(out["ok"])
        self.assertEqual(out["summary"], "picks issues labeled `to do`, assigned to you")

    def test_labels_source_follows_the_label_mapping_and_opt_in(self):
        m = meta(opt_in_label="claude", include_unassigned=True)
        m["labels"]["todo"] = "ready"
        self.assertEqual(describe(m)["summary"],
                         "picks issues labeled `ready` that also carry `claude`, assigned to you or to nobody")

    def test_board_source_names_the_column_and_board(self):
        out = describe(meta(source="board", board=BOARD))
        self.assertEqual((out["source"], out["board"], out["column"]), ("board", "acme/6", "To Do"))
        self.assertEqual(out["summary"], "picks issues in the `To Do` column of board acme/6, assigned to you")
        self.assertNotIn("label", out)

    def test_board_source_without_a_board_is_not_ok(self):
        out = describe(meta(source="board"))
        self.assertFalse(out["ok"])
        self.assertIn("queue.board.owner", out["summary"])

    def test_board_source_without_a_todo_column_is_not_ok(self):
        out = describe(meta(source="board", board=BOARD, board_states={"in_progress": "Doing"}))
        self.assertFalse(out["ok"])
        self.assertIn("queue.board_states.todo", out["summary"])

    def test_unknown_source_is_named(self):
        out = describe(meta(source="either"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["summary"], "queue.source `either` is not supported; use labels or board")

    def test_describe_never_calls_github(self):
        describe(meta(source="board", board=BOARD))  # takes no client at all


class StatusOptionsTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRunner()
        ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), mode="live")
        self.gh = GhClient(ctx, runner=self.fake, dry_run=False)

    def test_reads_the_option_names(self):
        self.assertEqual(status_options(self.gh, "acme", 6, "Status"), ["To Do", "In progress", "In review"])
        self.assertEqual(self.fake.writes_made(), [])

    def test_missing_board_is_none(self):
        self.assertIsNone(status_options(self.gh, "acme", 99, "Status"))

    def test_rejects_odd_field_names(self):
        with self.assertRaises(ValueError):
            status_options(self.gh, "acme", 6, 'Status"){x}')


class DoctorQueueTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRunner()
        self.fake.state["repos"][REPO]["label_defs"] = {"to do": {}, "in progress": {}}
        self.ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), board=("acme", 6), mode="live")
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=False)

    def run_check(self, m, board=("acme", 6), gh=None):
        return doctor.queue_check(m, gh or self.gh, REPO, board, origin=REPO)

    def test_labels_source_with_the_label_present(self):
        c = self.run_check(meta())
        self.assertEqual((c["name"], c["ok"], c["required"]), ("queue", True, False))
        self.assertIn("picks issues labeled `to do`", c["detail"])
        self.assertEqual(self.fake.writes_made(), [])

    def test_labels_source_warns_when_the_label_is_missing(self):
        m = meta()
        m["labels"]["todo"] = "ready"
        m["queue"]["pick_label"] = "ready"
        c = self.run_check(m)
        self.assertFalse(c["ok"])
        self.assertFalse(c["required"])
        self.assertIn("no `ready` label", c["detail"])
        self.assertIn("/not-me:setup project --queue", c["detail"])

    def test_labels_source_warns_about_a_missing_opt_in_label(self):
        c = self.run_check(meta(opt_in_label="claude"))
        self.assertFalse(c["ok"])
        self.assertIn("no `claude` label", c["detail"])

    def test_labels_source_warns_when_pick_label_disagrees(self):
        c = self.run_check(meta(pick_label="ready"))
        self.assertFalse(c["ok"])
        self.assertIn("queue.pick_label", c["detail"])
        self.assertIn("`labels.todo`", c["detail"])

    def test_board_source_ok(self):
        c = self.run_check(meta(source="board", board=BOARD,
                                board_states={"todo": "To Do", "in_progress": "In progress", "in_review": "In review"}))
        self.assertTrue(c["ok"], c)
        self.assertIn("column of board acme/6", c["detail"])
        self.assertEqual(self.fake.writes_made(), [])

    def test_board_source_warns_about_a_missing_column(self):
        c = self.run_check(meta(source="board", board=BOARD))  # default board_states has Blocked, the fake does not
        self.assertFalse(c["ok"])
        self.assertIn("no `Blocked` option", c["detail"])
        self.assertIn("queue.board_states.blocked", c["detail"])
        self.assertIn("To Do, In progress, In review", c["detail"])

    def test_board_source_warns_when_the_board_is_not_allowed(self):
        c = self.run_check(meta(source="board", board=BOARD,
                                board_states={"todo": "To Do"}), board=None)
        self.assertFalse(c["ok"])
        self.assertIn("/not-me:allow-repo acme/app --board acme/6", c["detail"])

    def test_board_source_warns_when_the_board_is_not_found(self):
        c = self.run_check(meta(source="board", board={**BOARD, "number": 99}), board=("acme", 99))
        self.assertFalse(c["ok"])
        self.assertIn("not found", c["detail"])

    def test_unsupported_source(self):
        c = self.run_check(meta(source="either"))
        self.assertFalse(c["ok"])
        self.assertIn("not supported", c["detail"])

    def test_github_errors_mean_not_run(self):
        def broken(argv):
            raise FileNotFoundError(2, "no", "gh")

        gh = GhClient(self.ctx, runner=broken, dry_run=False)
        for m in (meta(), meta(source="board", board=BOARD)):
            c = self.run_check(m, gh=gh)
            self.assertTrue(c["ok"])
            self.assertFalse(c["required"])
            self.assertIn("not run", c["detail"])

    def test_run_reports_not_run_when_gh_cannot_be_used(self):
        def runner(argv):
            raise OSError("no gh")

        checks = doctor.run(runner, [], lambda: self.gh, lambda: {"active": False}, {},
                            queue=lambda: self.fail("queue check must not run without gh"))
        q = next(c for c in checks if c["name"] == "queue")
        self.assertTrue(q["ok"])
        self.assertIn("not run", q["detail"])


if __name__ == "__main__":
    unittest.main()
