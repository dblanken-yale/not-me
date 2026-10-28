"""Drift between state labels and board Status: the report (doctor, status) and `queue-resync`."""
from __future__ import annotations

import unittest
from unittest import mock

from notme import doctor
from notme.dryrun import Ledger
from notme.gh import GhClient, GhError
from notme.queue import resync
from notme.queue.sync import MirrorFailed
from notme.writes import GuardContext
from tests.test_queue_board import BOARD, BoardTestCase
from tests.test_queue_labels import REPO, QueueTestCase, add_issue
from tests.test_queue_sync import board_reads, board_writes, item_status, label_writes


class LabelsSourceCase(QueueTestCase):
    """source: labels with queue.board set: the board Status is the side resync writes."""
    cfg_overrides = {"board": BOARD}

    def setUp(self):
        super().setUp()
        self.fake.state["repos"][REPO]["things"].pop("1")
        self.fake.state["repos"][REPO]["things"].pop("2")
        self.board = self.fake.state["board"]
        self.board["status_field"]["options"].append({"id": "opt_blocked", "name": "Blocked"})
        self.board["items"] = []

    def put(self, number, status, labels, **issue):
        add_issue(self.fake, number, labels=labels, **issue)
        self.board["items"].append({"id": f"PVTI_{number}", "repo": REPO, "number": number, "status": status})

    def dry(self):
        ledger = Ledger(self.dir / "dry.jsonl")
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=ledger)
        self.q = self.queue("run-1")
        return ledger

    def unallowed(self):
        self.ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), board=None, mode="live",
                                project_dir=self.project)
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=False, ledger=Ledger(self.dir / "ledger.jsonl"))
        self.q = self.queue("run-1")


class LabelsSourceReportTests(LabelsSourceCase):
    def test_drift_is_listed_with_url_and_both_values(self):
        self.put(5, "To Do", ["needs review"])
        self.put(6, "In progress", ["in progress"])
        found = resync.read(self.q)
        self.assertEqual([e["number"] for e in found["drift"]], [5])
        entry = found["drift"][0]
        self.assertEqual((entry["label"], entry["status"], entry["to"], entry["side"]),
                         ("needs review", "To Do", "In review", "board"))
        self.assertTrue(entry["url"].endswith("/5"), entry["url"])
        self.assertEqual(resync.describe(entry), f"{entry['url']}: label `needs review`, Status `To Do`")

    def test_no_drift_when_label_and_status_agree(self):
        self.put(5, "To Do", ["to do"])
        self.put(6, "In review", ["needs review"])
        self.assertEqual(resync.read(self.q)["drift"], [])
        self.assertTrue(doctor.drift_check([], "acme/6")["ok"])

    def test_states_with_no_column_and_unlabelled_issues_are_not_drift(self):
        self.put(5, "In review", ["needs work"])
        self.put(6, "To Do", ["question"])
        self.put(7, "In progress", [])
        self.assertEqual(resync.read(self.q)["drift"], [])

    def test_closed_issues_are_not_listed(self):
        self.put(5, "To Do", ["in progress"], state="CLOSED")
        self.assertEqual(resync.read(self.q)["drift"], [])

    def test_issue_with_no_status_is_drift(self):
        self.put(5, None, ["in progress"])
        entry = resync.read(self.q)["drift"][0]
        self.assertEqual(entry["status"], resync.NO_STATUS)

    def test_report_is_read_only(self):
        self.put(5, "To Do", ["needs review"])
        resync.read(self.q, include_missing=True)
        self.assertEqual(self.fake.writes_made(), [])

    def test_unallowed_board_is_refused_before_any_read(self):
        self.put(5, "To Do", ["needs review"])
        self.unallowed()
        with self.assertRaises(MirrorFailed) as cm:
            resync.read(self.q)
        self.assertIn("/not-me:allow-repo --board acme/6", str(cm.exception))
        self.assertEqual(board_reads(self.fake), [])

    def test_board_not_set_is_named(self):
        self.cfg = type(self.cfg)(meta=self.cfg.meta, project_dir=None)
        self.cfg.meta["project"]["queue"]["board"] = {"owner": None, "number": None, "status_field": "Status"}
        self.q = self.queue("run-1")
        with self.assertRaises(MirrorFailed) as cm:
            resync.read(self.q)
        self.assertIn("queue.board.owner", str(cm.exception))
        self.assertNotIn("queue.sync off", str(cm.exception))


class LabelsSourceResyncTests(LabelsSourceCase):
    def test_live_sets_each_status_and_reads_it_back(self):
        self.put(5, "To Do", ["needs review"])
        self.put(6, "To Do", ["in progress"])
        self.put(7, "To Do", ["to do"])
        out = resync.resync(self.q)
        self.assertFalse(out["dry_run"])
        self.assertEqual([(c["number"], c["from"], c["to"], c["result"]) for c in out["changes"]],
                         [(5, "To Do", "In review", "changed"), (6, "To Do", "In progress", "changed")])
        self.assertEqual([item_status(self.fake, n) for n in (5, 6, 7)], ["In review", "In progress", "To Do"])
        self.assertEqual(len(board_writes(self.fake)), 2)
        self.assertEqual(label_writes(self.fake), [])
        self.assertEqual(resync.read(self.q)["drift"], [])

    def test_a_status_that_does_not_stick_is_a_failure(self):
        self.put(5, "To Do", ["needs review"])
        with mock.patch.object(self.gh, "board_set_status"):  # the write goes nowhere
            out = resync.resync(self.q)
        self.assertEqual(out["changes"][0]["result"], "failed")
        self.assertIn("after the write", out["changes"][0]["reason"])

    def test_dry_run_records_the_writes_in_the_ledger_only(self):
        self.put(5, "To Do", ["needs review"])
        ledger = self.dry()
        before = self.fake.snapshot()
        out = resync.resync(self.q)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["changes"][0]["result"], "planned")
        self.assertEqual(self.fake.snapshot(), before)
        self.assertEqual(self.fake.writes_made(), [])
        self.assertEqual([e["description"] for e in ledger.read() if e["description"].startswith("set board")],
                         ["set board status on acme/6"])

    def test_issue_not_on_the_board_is_skipped_and_reported_never_added(self):
        self.put(5, "To Do", ["needs review"])
        add_issue(self.fake, 8, labels=["in progress"])  # open, labelled, not on the board
        out = resync.resync(self.q)
        self.assertEqual([c["number"] for c in out["changes"]], [5])
        self.assertEqual([s["number"] for s in out["skipped"]], [8])
        self.assertIn("not on board acme/6", out["skipped"][0]["reason"])
        self.assertIn("never adds", out["skipped"][0]["reason"])
        self.assertEqual([i["number"] for i in self.board["items"]], [5])
        self.assertEqual(len(board_writes(self.fake)), 1)

    def test_closed_issues_are_left_alone(self):
        self.put(5, "To Do", ["in progress"], state="CLOSED")
        add_issue(self.fake, 8, labels=["in progress"], state="CLOSED")
        out = resync.resync(self.q)
        self.assertEqual((out["changes"], out["skipped"]), ([], []))
        self.assertEqual(self.fake.writes_made(), [])

    def test_unallowed_board_is_refused_before_any_read_or_write(self):
        self.put(5, "To Do", ["needs review"])
        self.unallowed()
        with self.assertRaises(MirrorFailed):
            resync.resync(self.q)
        self.assertEqual(board_reads(self.fake), [])
        self.assertEqual(self.fake.writes_made(), [])

    def test_a_column_the_board_lacks_is_skipped_with_the_fix(self):
        self.board["status_field"]["options"] = [o for o in self.board["status_field"]["options"]
                                                 if o["name"] != "In review"]
        self.put(5, "To Do", ["needs review"])
        out = resync.resync(self.q)
        self.assertEqual(out["changes"], [])
        self.assertIn("queue.board_states.in_review", out["skipped"][0]["reason"])
        self.assertEqual(board_writes(self.fake), [])

    def test_a_failed_write_is_reported_and_the_rest_go_on(self):
        self.put(5, "To Do", ["needs review"])
        self.put(6, "To Do", ["in progress"])
        calls = []
        real = self.gh.board_set_status

        def flaky(*args, **kw):
            calls.append(args)
            if len(calls) == 1:
                raise GhError(["api", "graphql"], 1, "nope")
            return real(*args, **kw)

        with mock.patch.object(self.gh, "board_set_status", side_effect=flaky):
            out = resync.resync(self.q)
        self.assertEqual([c["result"] for c in out["changes"]], ["failed", "changed"])
        self.assertIn("project scope", out["changes"][0]["reason"])


class BoardSourceResyncTests(BoardTestCase):
    """source: board: the labels are the side resync writes."""

    def test_report_lists_labels_that_do_not_follow_the_status(self):
        self.put(5, status="In progress", labels=["to do", "bug"])
        self.put(6, status="To Do", labels=["to do"])
        found = resync.read(self.q)
        self.assertEqual([e["number"] for e in found["drift"]], [5])
        self.assertEqual((found["drift"][0]["label"], found["drift"][0]["status"], found["drift"][0]["side"]),
                         ("to do", "In progress", "labels"))

    def test_a_label_state_with_no_column_is_not_drift(self):
        self.put(5, status="In review", labels=["needs work"])
        self.assertEqual(resync.read(self.q)["drift"], [])

    def test_an_unmapped_column_has_nothing_to_copy(self):
        self.put(5, status="Done", labels=["to do"])
        self.assertEqual(resync.read(self.q)["drift"], [])

    def test_live_swaps_the_state_label_keeping_the_others_and_reads_it_back(self):
        self.put(5, status="In progress", labels=["to do", "bug"])
        out = resync.resync(self.q)
        self.assertEqual([(c["side"], c["to"], c["result"]) for c in out["changes"]],
                         [("labels", "in progress", "changed")])
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "in progress"])
        self.assertEqual(board_writes(self.fake), [])
        self.assertEqual(resync.read(self.q)["drift"], [])

    def test_dry_run_records_the_label_write_in_the_ledger_only(self):
        self.put(5, status="In progress", labels=["to do"])
        ledger = Ledger(self.dir / "dry.jsonl")
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=ledger)
        self.q = self.queue("run-1")
        before = self.fake.snapshot()
        out = resync.resync(self.q)
        self.assertEqual(out["changes"][0]["result"], "planned")
        self.assertEqual(self.fake.snapshot(), before)
        self.assertEqual(len([e for e in ledger.read() if e["description"].startswith("set labels")]), 1)

    def test_closed_issues_are_left_alone(self):
        self.put(5, status="In progress", labels=["to do"], state="CLOSED")
        self.assertEqual(resync.resync(self.q)["changes"], [])
        self.assertEqual(self.fake.writes_made(), [])

    def test_unallowed_board_is_refused_before_any_read(self):
        self.put(5, status="In progress", labels=["to do"])
        self.ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), board=None, mode="live",
                                project_dir=self.project)
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=False, ledger=Ledger(self.dir / "ledger.jsonl"))
        self.q = self.queue("run-1")
        with self.assertRaises(MirrorFailed):
            resync.resync(self.q)
        self.assertEqual(board_reads(self.fake), [])
        self.assertEqual(self.fake.writes_made(), [])


class DriftCheckTests(unittest.TestCase):
    def entry(self, n):
        return {"number": n, "url": f"https://github.com/acme/app/issues/{n}", "label": "needs review",
                "status": "Todo"}

    def test_a_warning_with_each_url_and_both_values_and_the_fix(self):
        c = doctor.drift_check([self.entry(5), self.entry(6)], "acme/6")
        self.assertFalse(c["ok"])
        self.assertFalse(c["required"])
        self.assertIn("https://github.com/acme/app/issues/5 has label `needs review` but Status `Todo`", c["detail"])
        self.assertIn("2 open issue(s)", c["detail"])
        self.assertIn("queue-resync", c["fix"])

    def test_a_long_list_is_cut(self):
        c = doctor.drift_check([self.entry(n) for n in range(1, 15)], "acme/6")
        self.assertIn("and 4 more", c["detail"])


if __name__ == "__main__":
    unittest.main()
