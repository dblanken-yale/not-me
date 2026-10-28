"""queue.sync: the side queue.source does not read (labels or the board Status) is kept in step as a mirror."""
from __future__ import annotations

import copy
import unittest
from unittest import mock

from notme import claims
from notme.dryrun import Ledger
from notme.gh import GhClient, GhError, ReadbackMismatch, WriteBlocked
from notme.queue import describe
from notme.queue.board import UnmappedState
from notme.writes import Decision, GuardContext, WriteTarget
from tests.test_queue_board import BOARD, BoardTestCase
from tests.test_queue_labels import PROJECT_META, REPO, QueueTestCase, add_issue


def board_writes(fake):
    return [c for c in fake.writes_made() if any("updateProjectV2ItemFieldValue" in a for a in c)]


def label_writes(fake):
    return [c for c in fake.writes_made() if any(a.endswith("/labels") for a in c)]


def board_reads(fake):
    return [c for c in fake.calls if "graphql" in c]


def paused(target):
    return WriteBlocked(Decision(False, "writes are paused", None, "pause"), target)


def item_status(fake, n):
    return [i for i in fake.state["board"]["items"] if i["number"] == n][0]["status"]


class LabelSourceSyncTests(QueueTestCase):
    """source: labels, sync: true, and the board is on the allow list (GuardContext board=acme/6)."""
    cfg_overrides = {"sync": True, "board": BOARD}

    def setUp(self):
        super().setUp()
        self.fake.state["board"]["status_field"]["options"].append({"id": "opt_blocked", "name": "Blocked"})

    def test_claim_sets_the_in_progress_label_and_column(self):
        self.q.claim(1)
        self.assertEqual(self.thing(1)["labels"], ["in progress"])
        self.assertEqual(item_status(self.fake, 1), "In progress")
        self.assertEqual(self.q.sync_warnings, [])

    def test_move_to_needs_review_sets_the_label_and_the_in_review_column(self):
        self.q.claim(1)
        self.q.move(1, "needs_review", require_current="in_progress")
        self.assertEqual(self.thing(1)["labels"], ["needs review"])
        self.assertEqual(item_status(self.fake, 1), "In review")

    def test_move_to_a_state_with_no_column_leaves_the_status_alone(self):
        self.q.claim(1)
        self.q.move(1, "needs_review")
        self.fake.calls.clear()
        self.q.move(1, "question", require_current="needs_review")
        self.assertEqual(self.thing(1)["labels"], ["question"])
        self.assertEqual(item_status(self.fake, 1), "In review")
        self.assertEqual(board_writes(self.fake), [])
        self.assertEqual(self.q.sync_warnings, [])

    def test_release_back_to_todo_sets_both(self):
        self.q.claim(1)
        self.q.release(1)
        self.assertEqual(self.thing(1)["labels"], ["to do"])
        self.assertEqual(item_status(self.fake, 1), "To Do")

    def test_board_not_on_the_allow_list_is_one_warning_and_the_claim_stands(self):
        self.ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), board=None, mode="live",
                                project_dir=self.project)
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=False, ledger=Ledger(self.dir / "ledger.jsonl"))
        self.q = self.queue("run-1")
        got = self.q.claim(1)
        self.assertEqual(got.now, "in_progress")
        self.assertEqual(self.thing(1)["labels"], ["in progress"])
        self.assertEqual(item_status(self.fake, 1), "To Do")
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("/not-me:allow-repo --board acme/6", self.q.sync_warnings[0])

    def test_issue_not_on_the_board_is_one_warning_and_nothing_is_added(self):
        self.fake.state["board"]["items"] = []
        self.q.claim(1)
        self.assertEqual(self.thing(1)["labels"], ["in progress"])
        self.assertEqual(self.fake.state["board"]["items"], [])
        self.assertEqual(board_writes(self.fake), [])
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("add the issue to the board", self.q.sync_warnings[0])

    def test_column_missing_on_the_board_is_one_warning(self):
        self.fake.state["board"]["status_field"]["options"] = [
            o for o in self.fake.state["board"]["status_field"]["options"] if o["name"] != "In review"]
        self.q.claim(1)
        self.q.move(1, "needs_review")
        self.assertEqual(self.thing(1)["labels"], ["needs review"])
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("queue.board_states.in_review", self.q.sync_warnings[0])

    def test_unmapped_board_state_key_is_one_warning(self):
        self.cfg.meta["project"]["queue"]["board_states"].pop("in_progress")
        self.q = self.queue("run-1")
        self.q.claim(1)
        self.assertEqual(self.thing(1)["labels"], ["in progress"])
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("map queue.board_states.in_progress", self.q.sync_warnings[0])

    def test_board_not_configured_is_one_warning(self):
        self.cfg.meta["project"]["queue"]["board"] = {"owner": None, "number": None, "status_field": "Status"}
        self.q = self.queue("run-1")
        self.q.claim(1)
        self.assertEqual(self.thing(1)["labels"], ["in progress"])
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("queue.board.owner", self.q.sync_warnings[0])

    def test_board_write_that_fails_is_one_warning(self):
        with mock.patch.object(self.gh, "board_set_status", side_effect=GhError(["api", "graphql"], 1, "nope")):
            self.q.claim(1)
        self.assertEqual(self.thing(1)["labels"], ["in progress"])
        self.assertEqual(len(self.q.sync_warnings), 1)

    def test_board_not_on_the_allow_list_is_refused_before_any_board_read(self):
        self.ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), board=None, mode="live",
                                project_dir=self.project)
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=False, ledger=Ledger(self.dir / "ledger.jsonl"))
        self.q = self.queue("run-1")
        self.q.claim(1)
        self.assertEqual(board_reads(self.fake), [])
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("not on the global allow list", self.q.sync_warnings[0])

    def test_bad_status_field_name_is_named_as_such(self):
        self.cfg.meta["project"]["queue"]["board"] = {**BOARD, "status_field": 'Status"){x}'}
        self.q = self.queue("run-1")
        self.q.claim(1)
        self.assertEqual(self.thing(1)["labels"], ["in progress"])
        self.assertEqual(len(self.q.sync_warnings), 1)
        warning = self.q.sync_warnings[0]
        self.assertNotIn("are not set", warning)
        self.assertIn("unsupported board field name", warning)
        self.assertIn("queue.board", warning)

    def test_board_write_refused_for_another_reason_names_that_reason(self):
        with mock.patch.object(self.gh, "board_set_status", side_effect=paused(WriteTarget.board("acme", 6))):
            self.q.claim(1)
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("writes are paused", self.q.sync_warnings[0])
        self.assertNotIn("allow-repo", self.q.sync_warnings[0])

    def test_dry_run_records_both_writes(self):
        ledger = Ledger(self.dir / "dry.jsonl")
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=ledger)
        self.q = self.queue("run-1")
        before = self.fake.snapshot()
        self.q.claim(1)
        self.q.move(1, "needs_review", require_current="in_progress")
        self.assertEqual(self.fake.snapshot()["repos"], before["repos"])
        self.assertEqual(self.fake.snapshot()["board"], before["board"])
        descriptions = [e["description"] for e in ledger.read()]
        self.assertEqual(len([d for d in descriptions if d.startswith("set labels")]), 2)
        self.assertEqual(len([d for d in descriptions if d.startswith("set board status")]), 2)
        self.assertEqual(self.q.sync_warnings, [])


class BoardSourceSyncTests(BoardTestCase):
    cfg_overrides = {**BoardTestCase.cfg_overrides, "sync": True}

    def setUp(self):
        super().setUp()
        self.put(5, labels=["to do", "bug"])

    def test_claim_sets_the_status_and_the_in_progress_label_keeping_others(self):
        self.q.claim(5)
        self.assertEqual(self.status(5), "In progress")
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "in progress"])
        self.assertEqual(self.q.sync_warnings, [])

    def test_move_to_needs_review_sets_the_column_and_the_label(self):
        self.q.claim(5)
        self.q.move(5, "needs_review", require_current="in_progress")
        self.assertEqual(self.status(5), "In review")
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "needs review"])

    def test_move_to_states_with_no_column_sets_the_label_and_leaves_the_status(self):
        self.q.claim(5)
        self.q.move(5, "needs_review")
        self.fake.calls.clear()
        self.q.move(5, "needs_work", require_current="needs_review")
        self.assertEqual(self.status(5), "In review")
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "needs work"])
        self.q.move(5, "question")
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "question"])
        self.assertEqual(board_writes(self.fake), [])
        self.assertEqual(self.q.sync_warnings, [])

    def test_require_current_for_a_state_with_no_column_reads_the_label(self):
        self.q.claim(5)
        self.q.move(5, "needs_review")
        self.q.move(5, "question")
        self.q.move(5, "needs_review", require_current="question")
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "needs review"])

    def test_release_back_to_todo_sets_both(self):
        self.q.claim(5)
        self.q.release(5)
        self.assertEqual(self.status(5), "To Do")
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "to do"])

    def test_label_only_move_refused_raises_the_write_blocked(self):
        self.q.claim(5)
        self.q.move(5, "needs_review")
        with mock.patch.object(self.gh, "labels_set", side_effect=paused(WriteTarget.repo(REPO))):
            with self.assertRaises(WriteBlocked):
                self.q.move(5, "needs_work")

    def test_label_only_move_with_a_readback_mismatch_or_gh_error_raises_it(self):
        self.q.claim(5)
        with mock.patch.object(self.gh, "labels_set", side_effect=ReadbackMismatch(["needs work"], ["to do"])):
            with self.assertRaises(ReadbackMismatch):
                self.q.move(5, "needs_work")
        with mock.patch.object(self.gh, "labels_set", side_effect=GhError(["api"], 1, "nope")):
            with self.assertRaises(GhError):
                self.q.move(5, "needs_work")

    def test_label_write_refused_for_another_reason_names_that_reason(self):
        with mock.patch.object(self.gh, "labels_set", side_effect=paused(WriteTarget.repo(REPO))):
            self.q.claim(5)
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("writes are paused", self.q.sync_warnings[0])
        self.assertNotIn("allow-repo", self.q.sync_warnings[0])

    def test_require_current_column_state_fails_after_a_label_only_move(self):
        self.q.claim(5)
        self.q.move(5, "needs_review")
        self.q.move(5, "needs_work", require_current="needs_review")
        self.assertEqual(self.status(5), "In review")
        with self.assertRaises(claims.ClaimLost) as raised:
            self.q.move(5, "in_progress", require_current="needs_review")
        self.assertIn("needs_work", str(raised.exception))
        self.assertEqual(self.status(5), "In review")
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "needs work"])

    def test_require_current_column_state_passes_with_needs_human_next_to_its_label(self):
        self.q.claim(5)
        self.thing(5)["labels"] = ["bug", "in progress", "needs human"]  # a held issue: labels-sync --add
        self.q.move(5, "needs_review", require_current="in_progress")
        self.assertEqual(self.status(5), "In review")
        self.assertIn("needs review", self.thing(5)["labels"])

    def test_require_current_column_state_fails_when_needs_human_replaced_its_label(self):
        self.q.claim(5)
        self.thing(5)["labels"] = ["bug", "needs human"]
        with self.assertRaises(claims.ClaimLost) as raised:
            self.q.move(5, "needs_review", require_current="in_progress")
        self.assertIn("needs_human", str(raised.exception))
        self.assertEqual(self.status(5), "In progress")
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "needs human"])

    def test_label_write_that_fails_is_one_warning_and_the_claim_stands(self):
        with mock.patch.object(self.gh, "labels_set", side_effect=GhError(["api"], 1, "nope")):
            got = self.q.claim(5)
        self.assertEqual(got.now, "in_progress")
        self.assertEqual(self.status(5), "In progress")
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("in progress", self.q.sync_warnings[0])

    def test_dry_run_records_both_writes(self):
        ledger = Ledger(self.dir / "dry.jsonl")
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=ledger)
        self.q = self.queue("run-1")
        self.q.claim(5)
        self.q.move(5, "needs_review", require_current="in_progress")
        self.q.move(5, "needs_work", require_current="needs_review")
        self.assertEqual(self.status(5), "To Do")
        self.assertEqual(sorted(self.thing(5)["labels"]), ["bug", "to do"])
        descriptions = [e["description"] for e in ledger.read()]
        self.assertEqual(len([d for d in descriptions if d.startswith("set board status")]), 2)
        self.assertEqual(len([d for d in descriptions if d.startswith("set labels")]), 3)
        self.assertEqual(self.q.sync_warnings, [])


class SyncOffTests(BoardTestCase):
    """sync off or unset: no mirror reads or writes in either mode."""

    def setUp(self):
        super().setUp()
        self.put(5, labels=["to do"])

    def test_board_source_without_sync_writes_no_labels(self):
        for value in (None, False):
            with self.subTest(sync=value):
                self.cfg.meta["project"]["queue"].pop("sync", None)
                if value is not None:
                    self.cfg.meta["project"]["queue"]["sync"] = value
                self.board["items"][0]["status"] = "To Do"
                self.thing(5)["assignees"] = []
                self.fake.calls.clear()
                q = self.queue(f"run-{value}")
                q.claim(5)
                q.release(5)
                self.assertEqual(label_writes(self.fake), [])
                self.assertFalse([c for c in self.fake.calls if c[:1] == ["api"] and c[-1].endswith("/labels")])
                with self.assertRaises(UnmappedState):
                    q.move(5, "needs_work")

    def test_label_source_without_sync_never_touches_the_board(self):
        for value in (None, False):
            with self.subTest(sync=value):
                self.cfg.meta["project"]["queue"].update(source="labels")
                self.cfg.meta["project"]["queue"].pop("sync", None)
                if value is not None:
                    self.cfg.meta["project"]["queue"]["sync"] = value
                self.fake.calls.clear()
                q = QueueTestCase.queue(self, f"run-{value}")
                q.claim(5)
                q.move(5, "needs_review")
                q.move(5, "needs_work")
                self.assertFalse([c for c in self.fake.calls if "graphql" in c])
                self.assertEqual(item_status(self.fake, 5), "To Do")
                q.unlock(5, force=True)
                self.thing(5)["labels"], self.thing(5)["assignees"] = ["to do"], []


class DescribeSyncTests(unittest.TestCase):
    def test_summary_mentions_sync_only_when_on(self):
        meta = copy.deepcopy(PROJECT_META)
        self.assertNotIn("step", describe(meta)["summary"])
        meta["queue"]["sync"] = True
        self.assertIn("keeps the board Status in step", describe(meta)["summary"])
        meta["queue"].update(source="board", board=BOARD)
        self.assertIn("keeps the labels in step", describe(meta)["summary"])


if __name__ == "__main__":
    unittest.main()
