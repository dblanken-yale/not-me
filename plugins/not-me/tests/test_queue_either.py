"""queue.source: either picks from the to-do label or the board's To Do column, claims on the label side, writes both
sides on every state change, logs why, and comments only on a move the timeline cannot explain."""
from __future__ import annotations

import copy
import unittest
from pathlib import Path
from unittest import mock

from notme import claims, doctor
from notme.dryrun import Ledger
from notme.gh import GhClient, GhError, WriteBlocked
from notme.queue import describe, make_queue
from notme.queue.either import EitherQueue
from notme.writes import GuardContext
from tests import test_cli_contract as base
from tests.test_cli_contract import REPO as CLI_REPO, things as cli_things
from tests.test_queue_board import BOARD
from tests.test_queue_labels import PROJECT_META, REPO, QueueTestCase, add_issue
from tests.test_queue_sync import board_reads, board_writes, item_status


class EitherTestCase(QueueTestCase):
    cfg_overrides = {"source": "either", "board": BOARD, "include_unassigned": True}

    def queue(self, run_id, cls=None):
        return super().queue(run_id, cls or EitherQueue)

    def setUp(self):
        super().setUp()
        self.fake.state["repos"][REPO]["things"].pop("1")
        self.fake.state["repos"][REPO]["things"].pop("2")
        self.board = self.fake.state["board"]
        self.board["status_field"]["options"].append({"id": "opt_blocked", "name": "Blocked"})
        self.board["items"] = []

    def put(self, number, status="To Do", labels=(), **issue):
        add_issue(self.fake, number, labels=list(labels), **issue)
        if status is not None:
            self.board["items"].append({"id": f"PVTI_{number}", "repo": REPO, "number": number, "status": status,
                                        "fields": {}})

    def numbers(self):
        return [c.number for c in self.q.candidates()]


class PickingTests(EitherTestCase):
    def test_label_only_issue_is_picked(self):
        self.put(5, status=None, labels=["to do"])
        self.assertEqual(self.numbers(), [5])

    def test_board_only_issue_is_picked(self):
        self.put(6, status="To Do", labels=[])
        self.assertEqual(self.numbers(), [6])

    def test_both_sides_agreeing_is_one_candidate(self):
        self.put(7, status="To Do", labels=["to do"])
        self.assertEqual(self.numbers(), [7])

    def test_other_rules_still_apply_to_a_board_only_issue(self):
        self.put(5, labels=["paused"])
        self.put(6, author="mallory")
        self.put(7, assignees=["hubot"])
        self.put(8, state="CLOSED")
        self.put(9)
        self.assertEqual(self.numbers(), [9])

    def test_label_to_do_but_board_past_to_do_is_not_picked_and_names_both(self):
        self.put(5, status="In review", labels=["to do"])
        self.put(6, status=None, labels=["to do"])
        self.assertEqual(self.numbers(), [6])
        self.assertEqual(len(self.q.disagreements), 1)
        found = self.q.disagreements[0]
        self.assertEqual((found["number"], found["label"], found["status"]), (5, "to do", "In review"))
        self.assertIn("`to do`", found["reason"])
        self.assertIn("`In review`", found["reason"])

    def test_board_to_do_but_label_past_to_do_is_not_picked_and_names_both(self):
        self.put(5, status="To Do", labels=["in progress"])
        self.assertEqual(self.numbers(), [])
        found = self.q.disagreements[0]
        self.assertEqual((found["label"], found["status"]), ("in progress", "To Do"))
        self.assertIn("`in progress`", found["reason"])
        self.assertIn("`To Do`", found["reason"])

    def test_a_board_item_past_to_do_with_no_label_is_not_a_disagreement(self):
        self.put(5, status="In progress", labels=[])
        self.assertEqual(self.numbers(), [])
        self.assertEqual(self.q.disagreements, [])

    def test_board_not_on_the_allow_list_is_refused_before_any_board_read(self):
        self.put(5)
        self.ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), board=None, mode="live",
                                project_dir=self.project)
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=False, ledger=Ledger(self.dir / "ledger.jsonl"))
        self.q = self.queue("run-1")
        with self.assertRaises(WriteBlocked) as caught:
            self.q.candidates()
        self.assertIn("acme/6", str(caught.exception))
        self.assertEqual(board_reads(self.fake), [])

    def test_without_a_board_the_queue_cannot_be_made(self):
        self.cfg.meta["project"]["queue"]["board"] = {"owner": None, "number": None, "status_field": "Status"}
        with self.assertRaises(ValueError):
            self.queue("run-1")

    def test_make_queue_builds_it(self):
        made = make_queue(self.cfg, self.gh, self.roster, "octocat", "run-1", repo=REPO)
        self.assertIsInstance(made, EitherQueue)
        self.assertTrue(made.sync)  # either implies sync, whatever queue.sync says


class LongBoardTests(EitherTestCase):
    """A board of more than one page of items (100): the Status of an issue on a later page is still read."""

    def fill(self, count=120):
        for i in range(count):  # items that are not issues of this repo's list: they only fill the first pages
            self.board["items"].append({"id": f"PVTI_x{i}", "repo": REPO, "number": 1000 + i, "status": "Done",
                                        "fields": {}})
        for i in range(count):
            add_issue(self.fake, 1000 + i, labels=[])

    def test_an_issue_past_the_first_page_is_found_with_its_real_status(self):
        self.fill()
        self.put(5, status="In review", labels=["to do"])  # item 121, on the second page
        self.assertEqual(self.numbers(), [])
        self.assertEqual([(d["number"], d["status"]) for d in self.q.disagreements], [(5, "In review")])
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(5)

    def test_a_board_only_issue_past_the_first_page_is_picked(self):
        self.fill()
        self.put(6, status="To Do", labels=[])
        self.assertEqual(self.numbers(), [6])

    def test_the_mirror_finds_an_item_past_the_first_page(self):
        self.fill()
        self.put(5, status="To Do", labels=["to do"])
        self.q.claim(5)
        self.assertEqual(item_status(self.fake, 5), "In progress")
        self.assertEqual(self.q.sync_warnings, [])

    def test_a_board_longer_than_the_page_cap_leaves_label_issues_unverified(self):
        self.fill()
        self.put(5, status="In review", labels=["to do"])
        with mock.patch("notme.queue.board.MAX_BOARD_PAGES", 1):
            self.assertEqual(self.numbers(), [])
            self.assertEqual(len(self.q.disagreements), 1)
            self.assertIn("unknown", self.q.disagreements[0]["reason"])
            with self.assertRaises(claims.ClaimLost):
                self.q.claim(5)
        self.assertEqual(self.thing(5)["labels"], ["to do"])

    def test_a_shorter_board_is_not_marked_truncated(self):
        self.put(5, labels=["to do"])
        self.assertFalse(self.q._board()["truncated"])


class WritingBothSidesTests(EitherTestCase):
    def test_claim_of_a_label_only_issue_writes_the_label_and_warns_it_is_not_on_the_board(self):
        self.put(5, status=None, labels=["to do"])
        got = self.q.claim(5)
        self.assertEqual(got.now, "in_progress")
        self.assertEqual(self.thing(5)["labels"], ["in progress"])
        self.assertEqual(self.board["items"], [])  # never added
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertIn("add the issue to the board", self.q.sync_warnings[0])

    def test_claim_of_a_board_only_issue_adds_the_label_and_sets_the_column(self):
        self.put(6, status="To Do", labels=["bug"])
        self.q.claim(6)
        self.assertEqual(sorted(self.thing(6)["labels"]), ["bug", "in progress"])
        self.assertEqual(item_status(self.fake, 6), "In progress")
        self.assertEqual(self.q.sync_warnings, [])
        self.assertEqual(len(self.marker_comments(6)), 1)

    def test_claim_refuses_when_the_sides_disagree(self):
        self.put(5, status="In review", labels=["to do"])
        with self.assertRaises(claims.ClaimLost) as caught:
            self.q.claim(5)
        self.assertIn("`to do`", str(caught.exception))
        self.assertIn("`In review`", str(caught.exception))
        self.assertEqual(self.thing(5)["labels"], ["to do"])

    def test_claim_move_to_needs_review_and_release_write_both_sides(self):
        self.put(5, labels=["to do"])
        self.q.claim(5)
        self.assertEqual((self.thing(5)["labels"], item_status(self.fake, 5)), (["in progress"], "In progress"))
        self.q.move(5, "needs_review", require_current="in_progress")
        self.assertEqual((self.thing(5)["labels"], item_status(self.fake, 5)), (["needs review"], "In review"))
        self.assertEqual(self.q.change, {"from": "in_progress", "to": "needs_review", "column": "In review"})
        self.assertEqual(self.q.sync_warnings, [])

    def test_release_back_to_to_do_writes_both_sides(self):
        self.put(5, labels=["to do"])
        self.q.claim(5)
        self.q.release(5)
        self.assertEqual((self.thing(5)["labels"], item_status(self.fake, 5)), (["to do"], "To Do"))
        self.assertEqual(self.q.sync_warnings, [])

    def test_release_to_blocked_writes_both_sides(self):
        self.put(5, labels=["to do"])
        self.q.claim(5)
        self.q.release(5, "blocked")
        self.assertEqual((self.thing(5)["labels"], item_status(self.fake, 5)), (["blocked"], "Blocked"))

    def test_a_failed_board_write_is_one_warning_and_the_label_claim_stands(self):
        self.put(5, labels=["to do"])
        with mock.patch.object(self.gh, "board_set_status", side_effect=GhError(["api", "graphql"], 1, "nope")):
            got = self.q.claim(5)
        self.assertEqual(got.now, "in_progress")
        self.assertEqual(self.thing(5)["labels"], ["in progress"])
        self.assertEqual(item_status(self.fake, 5), "To Do")
        self.assertEqual(len(self.q.sync_warnings), 1)
        self.assertEqual(len(self.marker_comments(5)), 1)

    def test_the_claim_still_reads_back_the_timeline(self):
        self.put(5, labels=["to do"])
        self.q.claim(5)
        self.assertTrue([c for c in self.fake.calls if any("/timeline" in a for a in c)])
        self.assertEqual(len(board_writes(self.fake)), 1)


class DescribeTests(unittest.TestCase):
    def meta(self, **queue):
        meta = copy.deepcopy(PROJECT_META)
        meta["queue"].update(source="either", board=BOARD, **queue)
        meta["queue"]["board_states"]["todo"] = "Todo"
        return meta

    def test_summary_names_both_sides_in_plain_words(self):
        out = describe(self.meta())
        self.assertTrue(out["ok"])
        self.assertEqual(out["summary"], "picks issues labeled `to do` or in the `Todo` column of board acme/6, "
                                         "assigned to you; keeps both in step")

    def test_summary_says_why_it_cannot_work(self):
        meta = self.meta()
        meta["queue"]["board"] = {"owner": None, "number": None, "status_field": "Status"}
        out = describe(meta)
        self.assertFalse(out["ok"])
        self.assertIn("queue.board", out["summary"])


class DoctorTests(unittest.TestCase):
    def test_checks_both_the_label_and_the_board(self):
        from tests.fakes.fakegh import FakeRunner
        fake = FakeRunner()
        fake.state["repos"][REPO]["label_defs"] = {"to do": {}, "in progress": {}}
        ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), board=("acme", 6), mode="live")
        gh = GhClient(ctx, runner=fake, dry_run=False)
        meta = copy.deepcopy(PROJECT_META)
        meta["queue"].update(source="either", board=BOARD)
        fake.state["board"]["status_field"]["options"].append({"id": "opt_blocked", "name": "Blocked"})
        ok = doctor.queue_check(meta, gh, REPO, ("acme", 6), origin=REPO)
        self.assertTrue(ok["ok"], ok)
        self.assertIn("or in the `To Do` column of board acme/6", ok["detail"])
        bad = doctor.queue_check(meta, gh, REPO, None, origin=REPO)  # board not confirmed
        self.assertFalse(bad["ok"])
        self.assertIn("allow-repo", bad["detail"])


EITHER_GLOBAL = base.GLOBAL_PROJECT.replace(
    "allow_repos: [acme/app-ui, acme/other]",
    "allow_repos: [acme/app-ui, acme/other]\nqueue: {source: either, include_unassigned: true, "
    "board: {owner: acme, number: 6, status_field: Status}}")


class ConfigTests(base.CliCase):
    global_project = EITHER_GLOBAL

    def test_either_with_sync_left_at_the_default_is_valid(self):
        self.assertTrue(self.ok("config", "--check")["ok"])

    def test_either_with_sync_true_is_valid(self):
        self.tc.write("repo", "project", "---\nschema: 1\nqueue: {sync: true}\n---\n# Project\n")
        self.assertTrue(self.ok("config", "--check")["ok"])

    def test_either_with_sync_false_written_is_a_config_error_naming_the_fix(self):
        self.tc.write("repo", "project", "---\nschema: 1\nqueue: {sync: false}\n---\n# Project\n")
        code, payload, _ = self.cli("config", "--check")
        self.assertEqual((code, payload["kind"]), (4, "config_invalid"))
        self.assertEqual(len(payload["errors"]), 1, payload)
        self.assertIn("queue.sync is false but queue.source is either", payload["errors"][0])
        self.assertIn("remove the queue.sync line or set queue.sync: true", payload["errors"][0])

    def test_sync_false_without_either_is_not_an_error(self):
        self.tc.write("repo", "project", "---\nschema: 1\nqueue: {source: labels, sync: false}\n---\n# Project\n")
        self.assertTrue(self.ok("config", "--check")["ok"])

    def test_status_and_explain_describe_the_queue_in_plain_words(self):
        want = "picks issues labeled `to do` or in the `To Do` column of board acme/6, assigned to you or to nobody"
        self.assertIn(want, self.ok("config", "--explain")["queue"]["summary"])


class ReasonCommentTests(base.CliCase):
    """Every move, release and transition logs why; one to a state that needs a person or stops work also says
    why in one comment, with the label and the board column."""
    global_project = EITHER_GLOBAL

    def setUp(self):
        super().setUp()
        self.fake.state["board"]["status_field"]["options"].append({"id": "opt_blocked", "name": "Blocked"})

    def comments(self, n=1):
        return [c["body"] for c in cli_things(self.fake)[str(n)]["comments"] if "not-me:run=" in c["body"]]

    def test_missing_reason_file_is_refused_and_changes_nothing(self):
        for argv in (["move-issue", "1", "--to", "in_progress"], ["release-issue", "1"],
                     ["transition", "1", "--from", "todo", "--to", "in_progress"]):
            with self.subTest(argv[0]):
                code, payload, _ = self.cli(*argv)
                self.assertEqual(code, 2, payload)
        self.assertEqual(cli_things(self.fake)["1"]["labels"], ["to do"])
        self.assertEqual(self.fake.writes_made(), [])

    def test_empty_reason_file_is_refused(self):
        empty = self.write_file("empty.txt", "\n")
        code, payload, _ = self.cli("move-issue", "1", "--to", "in_progress", "--reason-file", empty)
        self.assertEqual(code, 2, payload)
        self.assertEqual(cli_things(self.fake)["1"]["labels"], ["to do"])

    def test_a_move_the_timeline_explains_posts_nothing_and_logs_the_reason(self):
        self.ok("claim-issue", "1")
        start = self.ok("log", "start", "--skill", "new", "--ref", "1")
        before = len(self.comments())
        moved = self.ok("move-issue", "1", "--to", "needs_review", "--require-current", "in_progress",
                        "--reason-file", self.reason, "--log", start["path"])
        self.assertFalse(moved["commented"])
        self.assertEqual(self.comments()[before:], [])
        self.assertEqual(self.fake.state["board"]["items"][0]["status"], "In review")
        self.assertEqual(moved.get("warnings", []), [])
        self.assertIn("Moved from `in progress` to `needs review` (board column `In review`): "
                      "Because the test says so", Path(start["path"]).read_text(encoding="utf-8"))

    def test_a_release_to_todo_posts_nothing(self):
        self.ok("claim-issue", "1")
        before = len(self.comments())
        released = self.ok("release-issue", "1", "--reason-file", self.reason)
        self.assertFalse(released["commented"])
        self.assertEqual([c for c in self.comments()[before:] if c.startswith("Moved from")], [])

    def test_a_move_to_blocked_posts_one_comment_naming_both_sides_and_the_reason(self):
        self.ok("claim-issue", "1")
        start = self.ok("log", "start", "--skill", "new", "--ref", "1")
        before = len(self.comments())
        moved = self.ok("move-issue", "1", "--to", "blocked", "--reason-file", self.reason, "--log", start["path"])
        self.assertTrue(moved["commented"])
        new = self.comments()[before:]
        self.assertEqual(len(new), 1, new)
        self.assertIn("Moved from `in progress` to `blocked` (board column `Blocked`): Because the test says so",
                      new[0])
        self.assertIn("Because the test says so", Path(start["path"]).read_text(encoding="utf-8"))

    def test_a_release_to_blocked_posts_one_comment(self):
        self.ok("claim-issue", "1")
        before = len(self.comments())
        released = self.ok("release-issue", "1", "--to", "blocked", "--reason-file", self.reason)
        self.assertTrue(released["commented"])
        new = [c for c in self.comments()[before:] if c.startswith("Moved from")]
        self.assertEqual(len(new), 1, new)
        self.assertIn("Moved from `in progress` to `blocked` (board column `Blocked`)", new[0])

    def test_a_release_with_no_comment_gives_the_claim_back_and_posts_nothing(self):
        issue = cli_things(self.fake)["1"]
        issue["assignees"] = []
        self.ok("claim-issue", "1")
        before = len(self.comments())
        released = self.ok("release-issue", "1", "--no-comment")
        self.assertEqual((released["released"], released["commented"]), (True, False))
        self.assertEqual(len(self.comments()), before, self.comments())  # the claim marker is edited, not added to
        self.assertEqual(issue["labels"], ["to do"])
        self.assertEqual(issue["assignees"], [])
        self.assertEqual(self.fake.state["board"]["items"][0]["status"], "To Do")
        claim = [c["body"] for c in issue["comments"] if "not-me:claim" in c["body"]]
        self.assertEqual(len(claim), 1, claim)
        self.assertIn("state=released", claim[0])

    def test_a_release_to_blocked_with_no_comment_logs_the_reason_and_posts_nothing(self):
        self.ok("claim-issue", "1")
        start = self.ok("log", "start", "--skill", "new", "--ref", "1")
        before = len(self.comments())
        released = self.ok("release-issue", "1", "--to", "blocked", "--no-comment", "--reason-file", self.reason,
                           "--log", start["path"])
        self.assertFalse(released["commented"])
        self.assertEqual(len(self.comments()), before)
        self.assertIn("to `blocked` (board column `Blocked`): Because the test says so",
                      Path(start["path"]).read_text(encoding="utf-8"))

    def test_a_release_with_no_comment_still_checks_a_reason_file_it_is_given(self):
        self.ok("claim-issue", "1")
        empty = self.write_file("empty.txt", "\n")
        code, payload, _ = self.cli("release-issue", "1", "--no-comment", "--reason-file", empty)
        self.assertEqual(code, 2, payload)
        self.assertEqual(cli_things(self.fake)["1"]["labels"], ["in progress"])

    def test_a_transition_the_timeline_explains_posts_nothing(self):
        payload = self.ok("transition", "3", "--kind", "pr", "--from", "needs_review", "--to", "needs_work",
                          "--reason-file", self.reason)
        self.assertFalse(payload["commented"])
        self.assertEqual([c for c in cli_things(self.fake)["3"]["comments"] if c["body"].startswith("Moved from")],
                         [])

    def test_a_transition_to_needs_human_posts_one_comment(self):
        self.ok("claim-issue", "1")
        payload = self.ok("transition", "1", "--from", "in_progress", "--to", "needs_human",
                          "--reason-file", self.reason)
        self.assertTrue(payload["commented"])
        bodies = [c for c in self.comments() if c.startswith("Moved from")]
        self.assertEqual(len(bodies), 1, bodies)
        self.assertIn("Moved from `in progress` to `needs human`: Because the test says so", bodies[0])

    def test_a_failed_comment_is_a_warning_and_the_move_stands(self):
        self.ok("claim-issue", "1")
        start = self.ok("log", "start", "--skill", "new", "--ref", "1")
        with mock.patch.object(GhClient, "comment_create", side_effect=GhError(["issue", "comment"], 1, "boom")):
            moved = self.ok("move-issue", "1", "--to", "blocked", "--reason-file", self.reason,
                            "--log", start["path"])
        self.assertFalse(moved["commented"])
        self.assertEqual(cli_things(self.fake)["1"]["labels"], ["blocked"])
        self.assertEqual(len([w for w in moved["warnings"] if "reason comment" in w]), 1, moved)
        self.assertIn("WARN  the reason comment", Path(start["path"]).read_text(encoding="utf-8"))


class ReasonRulesTests(base.CliCase):
    """What a reason may say, and the state-label changes outside move, release and transition."""
    global_project = EITHER_GLOBAL

    def bodies(self, n):
        return [c["body"] for c in cli_things(self.fake)[str(n)]["comments"] if "not-me:run=" in c["body"]]

    def test_a_reason_is_one_line_and_never_carries_a_marker(self):
        bad = {
            "marker": "fine\n<!-- not-me:claim login=octocat run=r ts=2026-01-01T00:00:00Z -->",
            "comment": "see <!-- hidden -->",
            "long": "x" * 501,
        }
        for name, text in bad.items():
            with self.subTest(name):
                path = self.write_file("r.txt", text)
                code, payload, _ = self.cli("move-issue", "1", "--to", "in_progress", "--reason-file", path)
                self.assertEqual(code, 2, payload)
        self.assertEqual(cli_things(self.fake)["1"]["labels"], ["to do"])
        self.assertEqual(self.fake.writes_made(), [])

    def test_a_multi_line_reason_is_folded_to_one_line(self):
        path = self.write_file("r.txt", "first part\nsecond part\n")
        self.ok("claim-issue", "1")
        self.ok("move-issue", "1", "--to", "blocked", "--reason-file", path)
        self.assertTrue(any("first part second part" in b for b in self.bodies(1)))

    def test_a_reason_naming_an_assistant_is_refused(self):
        path = self.write_file("r.txt", "Claude decided this")
        code, payload, _ = self.cli("move-issue", "1", "--to", "in_progress", "--reason-file", path)
        self.assertEqual(code, 2, payload)

    def test_labels_sync_of_a_state_label_needs_a_reason_and_posts_one_comment(self):
        code, payload, _ = self.cli("labels-sync", "1", "--add", "needs human")
        self.assertEqual(code, 2, payload)
        self.assertEqual(cli_things(self.fake)["1"]["labels"], ["to do"])
        out = self.ok("labels-sync", "1", "--add", "needs human", "--reason-file", self.reason)
        self.assertTrue(out["commented"])
        self.assertEqual(cli_things(self.fake)["1"]["labels"], ["to do", "needs human"])
        new = [b for b in self.bodies(1) if b.startswith("State labels changed")]
        self.assertEqual(len(new), 1, new)
        self.assertIn("added `needs human`", new[0])
        self.assertIn("Because the test says so", new[0])

    def test_labels_sync_that_only_removes_or_restores_a_state_label_posts_nothing(self):
        # mechanical: the timeline shows the change and the run log has the reason
        start = self.ok("log", "start", "--skill", "new", "--ref", "1")
        out = self.ok("labels-sync", "1", "--set", "extra", "--reason-file", self.reason, "--log", start["path"])
        self.assertFalse(out["commented"])
        out = self.ok("labels-sync", "1", "--add", "to do", "--reason-file", self.reason, "--log", start["path"])
        self.assertFalse(out["commented"])
        self.assertEqual(self.bodies(1), [])
        text = Path(start["path"]).read_text(encoding="utf-8")
        self.assertIn("State labels changed (removed `to do`): Because the test says so", text)
        self.assertIn("State labels changed (added `to do`): Because the test says so", text)

    def test_labels_sync_of_other_labels_needs_no_reason_and_posts_nothing(self):
        out = self.ok("labels-sync", "1", "--add", "extra")
        self.assertNotIn("commented", out)
        self.assertEqual(self.bodies(1), [])
        self.assertTrue(self.ok("labels-sync", "1", "--add", "needs human", "--check")["in_sync"] is False)

    def test_labels_sync_that_changes_nothing_posts_nothing(self):
        out = self.ok("labels-sync", "1", "--add", "to do")  # already there
        self.assertNotIn("commented", out)
        self.assertEqual(self.bodies(1), [])

    def test_a_failed_labels_comment_is_a_warning_and_the_labels_stand(self):
        with mock.patch.object(GhClient, "comment_create", side_effect=GhError(["issue", "comment"], 1, "boom")):
            out = self.ok("labels-sync", "1", "--add", "needs human", "--reason-file", self.reason)
        self.assertFalse(out["commented"])
        self.assertEqual(cli_things(self.fake)["1"]["labels"], ["to do", "needs human"])
        self.assertEqual(len([w for w in out["warnings"] if "reason comment" in w]), 1)

    def test_review_post_that_changes_a_state_label_logs_why_and_posts_no_comment(self):
        # the review is the reason, and it shows in the timeline next to the label change
        body = self.write_file("review.md", "Please rename this.\n")
        start = self.ok("log", "start", "--skill", "review", "--ref", "3")
        out = self.ok("review-post", "3", "--event", "REQUEST_CHANGES", "--body-file", body,
                      "--labels-set", "needs work", "--log", start["path"])
        self.assertFalse(out["commented"])
        self.assertEqual([b for b in self.bodies(3) if b.startswith("State labels")], [])
        self.assertIn("State labels changed (added `needs work`; removed `needs review`): "
                      "a REQUEST_CHANGES review was posted", Path(start["path"]).read_text(encoding="utf-8"))

    def test_review_post_that_changes_no_state_label_does_not_comment(self):
        body = self.write_file("review.md", "Looks fine.\n")
        out = self.ok("review-post", "3", "--event", "COMMENT", "--body-file", body,
                      "--labels-set", "needs review,extra")
        self.assertNotIn("commented", out)
        self.assertEqual([b for b in self.bodies(3) if b.startswith("State labels")], [])

    def test_pr_open_with_a_state_label_logs_why_and_posts_no_comment(self):
        body = self.write_file("body.md", "Body text\n")
        title = self.write_file("title.txt", "A title\n")
        start = self.ok("log", "start", "--skill", "new", "--ref", "1")
        out = self.ok("pr-open", "--head", "feat/x", "--base", "main", "--title-file", title, "--body-file", body,
                      "--labels", "needs review", "--log", start["path"])
        self.assertFalse(out["commented"])
        self.assertEqual(self.bodies(out["number"]), [])
        self.assertIn("(added `needs review`): pull request opened", Path(start["path"]).read_text(encoding="utf-8"))


class DryRunReleaseTests(EitherTestCase):
    def test_a_dry_run_release_of_a_board_only_claim_records_the_comment_side_and_the_board_write(self):
        self.put(6, status="To Do", labels=[])
        ledger = Ledger(self.dir / "dry.jsonl")
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=ledger)
        self.q = self.queue("run-1")
        self.q.claim(6)
        self.q.release(6, "blocked")
        self.assertEqual(self.q.change["from"], "in_progress")
        self.assertEqual(self.q.change["to"], "blocked")
        self.assertEqual(self.q.change.get("column"), "Blocked")  # the mirror recorded the board write
        self.assertEqual(self.fake.writes_made(), [])  # and nothing reached GitHub


if __name__ == "__main__":
    unittest.main()
