from __future__ import annotations

import unittest
from unittest import mock

from notme import claims
from notme.dryrun import Ledger
from notme.gh import GhClient
from notme.queue.board import BoardQueue, UnmappedState, priority_value
from tests.fakes.fakegh import FakeRunner
from tests.test_queue_labels import REPO, QueueTestCase, add_issue, make_cfg

BOARD = {"owner": "acme", "number": 6, "status_field": "Status"}
FIELDS = ["Priority", "Size"]


class BoardTestCase(QueueTestCase):
    cfg_overrides = {"source": "board", "board": BOARD}
    picking = {"fields": FIELDS}
    ranked: tuple = ()  # what the deprecated queue.board.priority_fields named (Config.ranked_fields)

    def queue(self, run_id, cls=None):
        project = self.cfg.meta["project"]
        project["picking"] = {**project.get("picking", {}), **self.picking}
        self.cfg.ranked_fields = list(self.ranked)
        return super().queue(run_id, cls or BoardQueue)

    def setUp(self):
        super().setUp()
        things = self.fake.state["repos"][REPO]["things"]
        things.pop("2")
        self.board = self.fake.state["board"]
        self.board["status_field"]["options"].append({"id": "opt_blocked", "name": "Blocked"})
        self.board["items"] = []

    def put(self, number, status="To Do", fields=None, **issue):
        add_issue(self.fake, number, labels=issue.pop("labels", []), **issue)
        self.board["items"].append({"id": f"PVTI_{number}", "repo": REPO, "number": number, "status": status,
                                    "fields": fields or {}})

    def status(self, number):
        return [i for i in self.board["items"] if i["number"] == number][0]["status"]

    def numbers(self):
        return [c.number for c in self.q.candidates()]


class CandidateTests(BoardTestCase):
    # These cover the other eligibility rules, so unassigned items are let in; AssignmentTests covers the default.
    cfg_overrides = {**BoardTestCase.cfg_overrides, "include_unassigned": True}

    def test_only_to_do_open_unassigned_items(self):
        self.put(5)
        self.put(6, status="In progress")
        self.put(7, assignees=["hubot"])
        self.put(8, state="CLOSED")
        self.assertEqual(self.numbers(), [5])

    def test_opt_in_label_when_configured(self):
        self.cfg = self.cfg.__class__(meta={"project": {**self.cfg.meta["project"], "queue": {
            **self.cfg.meta["project"]["queue"], "opt_in_label": "claude"}}}, project_dir=None)
        self.q = self.queue("run-1")
        self.put(5, labels=["claude"])
        self.put(6)
        self.assertEqual(self.numbers(), [5])

    def test_untrusted_authors_are_excluded(self):
        self.put(5, author="hubot")
        self.put(6, author="mallory")
        self.put(7, author="monalisa")
        self.assertEqual(self.numbers(), [5])

    def test_blocked_items_are_excluded(self):
        self.put(5)
        self.put(6, labels=["blocked"])
        self.assertEqual(self.numbers(), [5])

    def test_paused_and_epic_items_are_skipped(self):
        self.put(5)
        self.put(6, labels=["paused"])
        self.put(7, labels=["epic"])
        self.assertEqual(self.numbers(), [5])

    def test_items_from_other_repos_and_the_blank_board_item_are_ignored(self):
        self.put(5)
        self.board["items"].append({"id": "PVTI_x", "repo": "acme/other", "number": 5, "status": "To Do"})
        self.assertEqual(self.numbers(), [5])

    def test_migrated_priority_fields_rank_higher_first_nulls_last_then_oldest(self):
        self.ranked = tuple(FIELDS)
        self.q = self.queue("run-1")
        self.put(5, fields={"Priority": "2 - Medium"}, created="2026-02-01T00:00:00Z")
        self.put(6, fields={"Priority": "4 - Critical"}, created="2026-02-09T00:00:00Z")
        self.put(7, created="2026-01-01T00:00:00Z")  # no priority: after every prioritized item
        self.put(8, fields={"Priority": "2 - Medium"}, created="2026-01-15T00:00:00Z")
        self.put(9, fields={"Priority": "2 - Medium", "Size": "3 - Large"}, created="2026-03-01T00:00:00Z")
        self.assertEqual(self.numbers(), [6, 9, 8, 5, 7])

    def test_no_named_fields_means_oldest_first(self):
        self.q.pick_fields = []  # as if picking.fields were empty
        self.q.rank_fields = []
        self.put(5, created="2026-02-02T00:00:00Z")
        self.put(6, created="2026-02-01T00:00:00Z")
        self.assertEqual(self.numbers(), [6, 5])

    def test_priority_value_parsing(self):
        self.assertEqual(priority_value("4 - Critical"), 4)
        self.assertEqual(priority_value("  10 high"), 10)
        self.assertIsNone(priority_value("Critical"))
        self.assertIsNone(priority_value(None))

    def test_the_board_query_names_the_configured_fields(self):
        self.put(5)
        self.q.candidates()
        query = [c for c in self.fake.calls if "graphql" in c][-1][3]
        self.assertIn('p0:fieldValueByName(name:"Priority")', query)
        self.assertIn('p1:fieldValueByName(name:"Size")', query)

    def test_unsafe_field_names_are_refused(self):
        self.picking = {"fields": ['x"){evil}']}
        with self.assertRaises(ValueError):
            self.queue("run-1")
        self.picking = {"fields": FIELDS}
        bad = dict(BOARD, status_field='x"){evil}')
        self.cfg = self.cfg.__class__(meta={"project": {**self.cfg.meta["project"], "queue": {
            **self.cfg.meta["project"]["queue"], "board": bad}}}, project_dir=None)
        with self.assertRaises(ValueError):
            self.queue("run-1")

    def test_number_text_and_iteration_fields_are_read(self):
        self.picking = {"fields": ["Points", "Team"]}
        self.q = self.queue("run-1")
        self.put(5, fields={"Points": 8, "Team": "Web"}, created="2026-02-01T00:00:00Z")
        self.put(6, fields={"Points": 13}, created="2026-02-02T00:00:00Z")
        facts = {f["number"]: f for f in self.q.facts(self.q.candidates())}
        self.assertEqual(facts[5]["fields"], {"Points": 8, "Team": "Web"})
        self.assertEqual(facts[5]["board"]["fields"], {"Points": 8, "Team": "Web"})
        self.assertEqual(facts[6]["fields"], {"Points": 13, "Team": None})

    def test_picking_fields_alone_do_not_reorder_the_board(self):
        # "Prefer the smallest Size" is a valid rule: the order among named fields is the model's reading of the
        # Picking work prose, so the queue keeps its own order (oldest first).
        self.put(5, fields={"Priority": "1 - Low", "Size": "1 - Small"}, created="2026-02-01T00:00:00Z")
        self.put(6, fields={"Priority": "4 - Critical", "Size": "5 - Huge"}, created="2026-02-02T00:00:00Z")
        self.put(7, created="2026-01-01T00:00:00Z")
        self.assertEqual(self.numbers(), [7, 5, 6])

    def test_migrated_priority_fields_keep_their_ranking_beside_new_picking_fields(self):
        # priority_fields: [Priority, Size] merged with picking.fields: [Size, Team] gives fields
        # [Size, Team, Priority]; the ranking stays Priority, then Size, as before.
        self.picking = {"fields": ["Size", "Team", "Priority"]}
        self.ranked = ("Priority", "Size")
        self.q = self.queue("run-1")
        self.put(5, fields={"Priority": "2 - Medium", "Size": "5 - Huge", "Team": "9"}, created="2026-02-01T00:00:00Z")
        self.put(6, fields={"Priority": "4 - Critical", "Size": "1 - Small"}, created="2026-02-02T00:00:00Z")
        self.put(7, fields={"Priority": "2 - Medium", "Size": "1 - Small"}, created="2026-01-01T00:00:00Z")
        self.put(8, fields={"Size": "9 - Giant", "Team": "99"}, created="2025-01-01T00:00:00Z")
        self.assertEqual(self.numbers(), [6, 5, 7, 8])
        facts = {f["number"]: f for f in self.q.facts(self.q.candidates())}
        self.assertEqual(facts[5]["fields"], {"Size": "5 - Huge", "Team": "9", "Priority": "2 - Medium"})

    def test_long_board_text_values_are_cut_to_picking_value_max_chars(self):
        self.picking = {"fields": ["Notes", "Points"], "value_max_chars": 5}
        self.q = self.queue("run-1")
        self.put(5, fields={"Notes": "abcdefghij", "Points": 123456789})
        facts = self.q.facts(self.q.candidates())
        self.assertEqual(facts[0]["fields"], {"Notes": "abcde", "Points": 123456789})
        self.assertEqual(facts[0]["board"]["fields"], {"Notes": "abcde", "Points": 123456789})

    def test_target_epic_items_come_first_in_task_list_order(self):
        self.picking = {"fields": FIELDS, "target_epics": [30]}
        self.q = self.queue("run-1")
        self.put(5)
        self.put(6, fields={"Priority": "4 - Critical"})
        self.put(7)
        self.put(8, status="In progress")
        add_issue(self.fake, 30, labels=[], body="- [ ] #8\n- [ ] #7\n- [ ] #5\n", author="hubot")
        self.assertEqual(self.numbers(), [7, 5, 6])  # #8 is not in To Do: the target epic does not add it
        self.assertEqual(self.q.target_epics_used, [30])

    def test_untrusted_target_epic_is_ignored(self):
        self.picking = {"fields": FIELDS, "target_epics": [30]}
        self.q = self.queue("run-1")
        add_issue(self.fake, 30, labels=[], body="- [ ] #7\n", author="mallory")
        self.put(6, fields={"Priority": "4 - Critical"})
        self.put(7)
        self.assertEqual(self.numbers(), [6, 7])
        self.assertEqual(self.q.target_epics_ignored, [{"number": 30, "reason": "untrusted author"}])


class EpicOrderAndFactsTests(BoardTestCase):
    cfg_overrides = {**BoardTestCase.cfg_overrides, "include_unassigned": True}

    def setUp(self):
        super().setUp()
        add_issue(self.fake, 10, labels=["epic"], body="- [ ] #11\n- [ ] #12\n")
        self.put(12, fields={"Priority": "4 - Critical"})

    def test_a_later_sibling_waits_for_an_earlier_unstarted_one_whatever_its_priority(self):
        self.put(11, status="To Do", labels=["paused"])  # on the board, open, not started, not eligible
        self.assertEqual(self.numbers(), [])
        self.assertEqual(self.q.held_back, [{"number": 12, "behind": 11, "epic": 10}])

    def test_an_earlier_sibling_in_the_blocked_column_releases_the_next(self):
        self.put(11, status="Blocked")
        self.assertEqual(self.numbers(), [12])

    def test_an_earlier_sibling_in_progress_releases_the_next(self):
        self.put(11, status="In progress")
        self.assertEqual(self.numbers(), [12])

    def test_an_earlier_sibling_off_the_board_is_read_from_the_issue(self):
        add_issue(self.fake, 11, labels=["to do"])
        self.assertEqual(self.numbers(), [])
        add_issue(self.fake, 11, state="CLOSED")
        self.assertEqual(self.numbers(), [12])

    def test_an_earlier_sibling_in_an_unmapped_column_counts_as_started(self):
        for column in ("Done", "Backlog"):
            with self.subTest(column):
                self.board["items"] = [i for i in self.board["items"] if i["number"] != 11]
                self.put(11, status=column)
                self.assertEqual(self.numbers(), [12])

    def test_an_earlier_sibling_with_no_status_holds(self):
        self.put(11, status=None)
        self.assertEqual(self.numbers(), [])
        self.assertEqual(self.q.held_back, [{"number": 12, "behind": 11, "epic": 10}])

    def test_an_earlier_sibling_in_to_do_holds(self):
        self.put(11, status="To Do", assignees=["hubot"])  # not eligible (someone else's) and claimed
        self.assertEqual(self.numbers(), [12])
        self.board["items"] = [i for i in self.board["items"] if i["number"] != 11]
        self.put(11, status="To Do", labels=["paused"])
        self.assertEqual(self.numbers(), [])

    def test_an_untrusted_epic_is_ignored(self):
        self.thing(10)["author"] = "mallory"
        self.put(11, status=None)
        self.assertEqual(self.numbers(), [12])
        self.assertEqual(self.q.held_back, [])

    def test_board_facts_name_the_status_and_every_named_field(self):
        self.put(11, status="In progress")
        self.board["items"][0]["fields"] = {"Priority": "4 - Critical", "Size": "1 - Small"}
        self.thing(12)["reactions"] = [{"user": "hubot", "content": "+1"}, {"user": "mallory", "content": "+1"}]
        facts = self.q.facts(self.q.candidates())
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["board"], {"status": "To Do",
                                             "fields": {"Priority": "4 - Critical", "Size": "1 - Small"}})
        self.assertEqual((facts[0]["parent"], facts[0]["position"]), (10, 2))
        self.assertEqual(facts[0]["reactions"], {"+1": 1})

    def test_a_field_with_no_value_is_null(self):
        self.board["items"][0]["fields"] = {}
        facts = self.q.facts(self.q.candidates())
        self.assertEqual(facts[0]["board"]["fields"], {"Priority": None, "Size": None})


class AssignmentTests(BoardTestCase):
    """Who a To Do item is assigned to: mine always, nobody's only with queue.include_unassigned, others' never."""

    def use(self, include_unassigned):
        self.cfg = make_cfg(**{**BoardTestCase.cfg_overrides, "include_unassigned": include_unassigned})
        self.q = self.queue("run-1")

    def seed(self):
        self.put(5, assignees=["octocat"])
        self.put(6)
        self.put(7, assignees=["hubot"])
        self.put(8, assignees=["octocat", "hubot"])

    def test_default_picks_only_items_assigned_to_me(self):
        self.seed()
        self.assertEqual(self.numbers(), [5])
        self.assertEqual(self.q.unassigned_skipped, 1)

    def test_include_unassigned_also_picks_unassigned_items(self):
        self.use(True)
        self.seed()
        self.assertEqual(self.numbers(), [5, 6])
        self.assertEqual(self.q.unassigned_skipped, 0)

    def test_items_assigned_to_others_are_never_picked(self):
        self.put(7, assignees=["hubot"])
        self.put(8, assignees=["octocat", "hubot"])
        for setting in (False, True):
            self.use(setting)
            self.assertEqual(self.numbers(), [], setting)
            self.assertEqual(self.q.unassigned_skipped, 0, setting)

    def test_unassigned_count_covers_only_otherwise_eligible_items(self):
        self.put(6)
        self.put(9, author="mallory")
        self.put(10, labels=["paused"])
        self.put(11, status="In progress")
        self.assertEqual(self.numbers(), [])
        self.assertEqual(self.q.unassigned_skipped, 1)


class ClaimTests(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.put(5)

    def test_claiming_an_item_already_assigned_to_me(self):
        self.put(9, assignees=["octocat"])
        got = self.q.claim(9)
        self.assertEqual((got.previous, got.now), ("todo", "in_progress"))
        self.assertEqual(self.thing(9)["assignees"], ["octocat"])
        self.assertEqual(self.status(9), "In progress")
        self.assertFalse([c for c in self.fake.calls if "--add-assignee" in c])  # no second assignment

    def test_claim_sets_status_assigns_and_leaves_one_marker(self):
        got = self.q.claim(5)
        self.assertEqual((got.number, got.previous, got.now), (5, "todo", "in_progress"))
        self.assertEqual(self.status(5), "In progress")
        self.assertEqual(self.thing(5)["assignees"], ["octocat"])
        self.assertEqual(len(self.marker_comments(5)), 1)
        self.assertEqual(got.marker_comment_id, self.marker_comments(5)[0]["id"])

    def test_ids_are_looked_up_each_run_not_hardcoded(self):
        self.board["id"] = "PVT_other"
        self.board["status_field"] = {"id": "PVTSSF_other", "options": [
            {"id": "x1", "name": "To Do"}, {"id": "x2", "name": "In progress"}, {"id": "x3", "name": "In review"}]}
        self.q.claim(5)
        self.assertEqual(self.status(5), "In progress")
        mutation = [c for c in self.fake.calls if any("updateProjectV2ItemFieldValue" in a for a in c)][0]
        self.assertIn("option=x2", mutation)
        self.assertIn("field=PVTSSF_other", mutation)
        self.assertIn("project=PVT_other", mutation)

    def test_claim_requires_the_item_to_be_in_to_do(self):
        self.put(6, status="In progress")
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(6)
        self.assertEqual(self.fake.writes_made(), [])
        self.assertEqual(self.status(6), "In progress")

    def test_claim_refuses_an_assigned_item(self):
        self.thing(5)["assignees"] = ["hubot"]
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(5)
        self.assertEqual(self.status(5), "To Do")

    def test_status_that_does_not_stick_is_a_lost_claim(self):
        with mock.patch.object(self.gh, "board_set_status", return_value={}):
            with self.assertRaises(claims.ClaimLost):
                self.q.claim(5)
        self.assertEqual(self.thing(5)["assignees"], [])
        self.assertTrue(claims.is_released(claims.parse_marker(self.marker_comments(5)[0]["body"])))
        self.assertFalse((self.claims_dir / "issue-5.lock").exists())

    def test_lost_claim_keeps_an_assignment_made_before_the_claim(self):
        self.put(9, assignees=["octocat"])
        with mock.patch.object(self.gh, "board_set_status", return_value={}):
            with self.assertRaises(claims.ClaimLost):
                self.q.claim(9)
        self.assertEqual(self.thing(9)["assignees"], ["octocat"])
        self.assertFalse([c for c in self.fake.calls if "--remove-assignee" in c])
        self.assertTrue(claims.is_released(claims.parse_marker(self.marker_comments(9)[0]["body"])))

    def test_item_not_on_the_board(self):
        add_issue(self.fake, 9, labels=[])
        with self.assertRaises(LookupError):
            self.q.claim(9)

    def test_local_lock_stops_a_second_session(self):
        self.q.claim(5)
        self.board["items"][0]["status"] = "To Do"
        self.thing(5)["assignees"] = []
        with self.assertRaises(claims.ClaimLost):
            self.queue("run-2").claim(5)


class ReleaseAndMoveTests(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.put(5)

    def test_release_maps_the_state_to_a_column(self):
        self.q.claim(5)
        self.q.release(5)
        self.assertEqual(self.status(5), "To Do")
        self.assertEqual(self.thing(5)["assignees"], [])
        self.assertTrue(claims.is_released(claims.parse_marker(self.marker_comments(5)[0]["body"])))

    def test_release_keeps_an_assignment_made_before_the_claim(self):
        self.put(9, assignees=["octocat"])  # a person assigned it
        self.q.claim(9)
        self.queue("run-2").release(9)  # a later process: it knows only what the marker recorded
        self.assertEqual(self.status(9), "To Do")
        self.assertEqual(self.thing(9)["assignees"], ["octocat"])
        self.assertFalse([c for c in self.fake.calls if "--remove-assignee" in c])
        self.assertTrue(claims.is_released(claims.parse_marker(self.marker_comments(9)[0]["body"])))

    def test_release_removes_an_assignment_the_claim_made(self):
        self.cfg = make_cfg(**{**BoardTestCase.cfg_overrides, "include_unassigned": True})
        self.q = self.queue("run-1")
        self.q.claim(5)
        self.assertEqual(claims.parse_marker(self.marker_comments(5)[0]["body"])["assigned"], "claim")
        self.queue("run-2").release(5)
        self.assertEqual(self.thing(5)["assignees"], [])
        released = claims.parse_marker(self.marker_comments(5)[0]["body"])
        self.assertEqual((released["state"], released["assigned"]), ("released", "claim"))

    def test_release_to_needs_review_uses_the_in_review_column(self):
        self.q.claim(5)
        self.q.release(5, to="needs_review")
        self.assertEqual(self.status(5), "In review")

    def test_move_maps_abstract_states(self):
        self.q.move(5, "blocked")
        self.assertEqual(self.status(5), "Blocked")
        self.assertEqual(self.q.state(5), "blocked")

    def test_move_require_current_mismatch(self):
        with self.assertRaises(claims.ClaimLost):
            self.q.move(5, "needs_review", require_current="in_progress")
        self.assertEqual(self.status(5), "To Do")
        self.q.move(5, "in_progress", require_current="todo")
        self.assertEqual(self.status(5), "In progress")

    def test_dry_run_claim_then_move_records_the_status_write(self):
        ledger = Ledger(self.dir / "dry.jsonl")
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=ledger)
        self.q = self.queue("run-1")
        self.q.claim(5)
        self.q.move(5, "needs_review", require_current="in_progress")
        self.assertEqual(self.status(5), "To Do")
        writes = [e for e in ledger.read() if e["description"].startswith("set board status")]
        self.assertEqual(len(writes), 2)

    def test_states_without_a_column_are_refused(self):
        with self.assertRaises(UnmappedState):
            self.q.move(5, "needs_work")

    def test_a_missing_column_on_the_board_is_reported(self):
        self.board["status_field"]["options"] = [o for o in self.board["status_field"]["options"]
                                                 if o["name"] != "Blocked"]
        with self.assertRaises(LookupError):
            self.q.move(5, "blocked")

    def test_state_of_an_unknown_column_is_none(self):
        self.board["items"][0]["status"] = "Icebox"
        self.assertIsNone(self.q.state(5))


if __name__ == "__main__":
    unittest.main()
