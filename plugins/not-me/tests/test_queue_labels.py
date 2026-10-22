from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from notme import claims, queue
from notme.dryrun import Ledger
from notme.gh import GhClient
from notme.queue import Claim
from notme.queue.labels import LabelQueue, is_stale, task_list_numbers
from notme.roster import Member, Roster
from notme.states import IllegalTransition, StateMismatch
from notme.writes import GuardContext
from tests.fakes.fakegh import FakeRunner

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
REPO = "acme/app"

PROJECT_META = {
    "tracker": "auto",
    "operator": "octocat",
    "queue": {"source": "labels", "pick_label": "to do", "opt_in_label": None,
              "board": {"owner": None, "number": None, "status_field": "Status", "priority_fields": []},
              "board_states": {"todo": "To Do", "in_progress": "In progress", "in_review": "In review",
                               "blocked": "Blocked"}},
    "labels": {"todo": "to do", "in_progress": "in progress", "needs_review": "needs review",
               "needs_work": "needs work", "ready_to_merge": "ready to merge", "question": "question",
               "blocked": "blocked", "epic": "epic", "needs_help": "needs help", "needs_human": "needs human",
               "paused": "paused"},
    "limits": {"fix_loop": 3, "ci_wait_minutes": 20, "claim_stale_hours": 8},
}


def make_cfg(**queue_overrides):
    meta = copy.deepcopy(PROJECT_META)
    meta["queue"].update(queue_overrides)
    return SimpleNamespace(meta={"project": meta}, project_dir=None)


def add_issue(fake, number, labels=("to do",), author="octocat", assignees=(), body="", created=None,
              state="OPEN", title=None):
    created = created or f"2026-02-01T00:00:{number:02d}Z"
    fake.state["repos"][REPO]["things"][str(number)] = {
        "number": number, "title": title or f"Issue {number}", "body": body, "state": state,
        "labels": list(labels), "assignees": list(assignees), "author": author, "comments": [], "kind": "issue",
        "created_at": created}


TRUST = lambda login: login in ("octocat", "hubot")  # noqa: E731


class QueueTestCase(unittest.TestCase):
    cfg_overrides: dict = {}

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="notme-queue-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.project = self.dir / "project"
        self.project.mkdir()
        self.fake = FakeRunner()
        self.ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO,), board=("acme", 6), mode="live",
                                project_dir=self.project)
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=False, ledger=Ledger(self.dir / "ledger.jsonl"))
        self.roster = Roster(entries=[Member("octocat", tier="lead"), Member("hubot", tier="authority"),
                                      Member("monalisa", tier="member")], operator="octocat")
        self.claims_dir = self.dir / "claims"
        self.now = NOW
        self.cfg = make_cfg(**self.cfg_overrides)
        self.q = self.queue("run-1")

    def queue(self, run_id, cls=None):
        return (cls or LabelQueue)(self.cfg, self.gh, self.roster, "octocat", run_id, REPO,
                                   claims_dir=self.claims_dir, clock=lambda: self.now)

    def thing(self, n):
        return self.fake.state["repos"][REPO]["things"][str(n)]

    def marker_comments(self, n):
        return [c for c in self.thing(n)["comments"] if claims.parse_marker(c["body"])]


class CandidateTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.fake.state["repos"][REPO]["things"].pop("1")
        self.fake.state["repos"][REPO]["things"].pop("2")

    def numbers(self):
        return [c.number for c in self.q.candidates()]

    def test_only_to_do_issues(self):
        add_issue(self.fake, 5)
        add_issue(self.fake, 6, labels=["in progress"])
        add_issue(self.fake, 7, labels=[])
        self.assertEqual(self.numbers(), [5])

    def test_paused_blocked_and_epic_are_excluded(self):
        add_issue(self.fake, 5)
        add_issue(self.fake, 6, labels=["to do", "paused"])
        add_issue(self.fake, 7, labels=["to do", "blocked"])
        add_issue(self.fake, 8, labels=["to do", "epic"])
        self.assertEqual(self.numbers(), [5])

    def test_issues_assigned_to_someone_else_are_excluded(self):
        add_issue(self.fake, 5)
        add_issue(self.fake, 6, assignees=["hubot"])
        add_issue(self.fake, 7, assignees=["octocat", "hubot"])
        self.assertEqual(self.numbers(), [5])

    def test_issues_assigned_to_me_are_pickable(self):
        # A lead assigns work to the operator before labeling it; that is still the operator's queue.
        add_issue(self.fake, 5, assignees=["octocat"])
        add_issue(self.fake, 6, assignees=["OctoCat"])
        self.assertEqual(self.numbers(), [5, 6])

    def test_untrusted_authors_are_excluded(self):
        add_issue(self.fake, 5, author="hubot")
        add_issue(self.fake, 6, author="mallory")
        add_issue(self.fake, 7, author="monalisa")  # a member: assignable, but not trusted to direct work
        self.assertEqual(self.numbers(), [5])

    def test_closed_issues_are_excluded(self):
        add_issue(self.fake, 5)
        add_issue(self.fake, 6, state="CLOSED")
        self.assertEqual(self.numbers(), [5])

    def test_opt_in_label_is_required_when_configured(self):
        self.cfg = make_cfg(opt_in_label="claude")
        self.q = self.queue("run-1")
        add_issue(self.fake, 5, labels=["to do", "claude"])
        add_issue(self.fake, 6, labels=["to do"])
        self.assertEqual(self.numbers(), [5])

    def test_oldest_first(self):
        add_issue(self.fake, 5, created="2026-02-03T00:00:00Z")
        add_issue(self.fake, 6, created="2026-02-01T00:00:00Z")
        add_issue(self.fake, 7, created="2026-02-02T00:00:00Z")
        self.assertEqual(self.numbers(), [6, 7, 5])

    def test_epic_children_come_first_in_epic_order(self):
        add_issue(self.fake, 10, labels=["epic"], body="Plan\n- [ ] #13\n- [x] #12\n- [ ] #11\n")
        add_issue(self.fake, 11)
        add_issue(self.fake, 12)
        add_issue(self.fake, 13, created="2026-02-20T00:00:00Z")
        add_issue(self.fake, 20, created="2026-01-01T00:00:00Z")  # older, but loose
        got = self.q.candidates()
        self.assertEqual([c.number for c in got], [13, 11, 20, 12])
        self.assertEqual([c.parent for c in got], [10, 10, None, None])

    def test_untrusted_epic_does_not_order_its_children(self):
        add_issue(self.fake, 10, labels=["epic"], author="mallory", body="- [ ] #13\n- [ ] #11\n")
        add_issue(self.fake, 11, created="2026-02-01T00:00:00Z")
        add_issue(self.fake, 13, created="2026-02-02T00:00:00Z")
        got = self.q.candidates()
        self.assertEqual([c.number for c in got], [11, 13])
        self.assertEqual([c.parent for c in got], [None, None])

    def test_epic_child_that_is_not_ready_is_not_a_candidate(self):
        add_issue(self.fake, 10, labels=["epic"], body="- [ ] #11\n- [ ] #12\n")
        add_issue(self.fake, 11, labels=["in progress"])
        add_issue(self.fake, 12)
        self.assertEqual(self.numbers(), [12])

    def test_task_list_ignores_other_repos_and_duplicates(self):
        body = "- [ ] #3\n* [ ] acme/other#9\n- [ ] acme/app#4\n- [ ] #3\n- [x] #5\n"
        self.assertEqual(task_list_numbers(body, REPO), [3, 4])

    def test_candidate_fields(self):
        add_issue(self.fake, 5, title="Fix it", created="2026-02-03T00:00:00Z")
        c = self.q.candidates()[0]
        self.assertEqual((c.number, c.title, c.author, c.labels, c.is_epic, c.created),
                         (5, "Fix it", "octocat", ["to do"], False, "2026-02-03T00:00:00Z"))
        self.assertTrue(c.url.endswith("/issues/5"))


class ClaimTests(QueueTestCase):
    def test_happy_path(self):
        got = self.q.claim(1)
        self.assertEqual(got, Claim(number=1, previous="todo", now="in_progress",
                                    marker_comment_id=self.thing(1)["comments"][0]["id"], run_id="run-1"))
        self.assertEqual(self.thing(1)["labels"], ["in progress"])
        self.assertEqual(self.thing(1)["assignees"], ["octocat"])
        markers = self.marker_comments(1)
        self.assertEqual(len(markers), 1)
        self.assertEqual(claims.parse_marker(markers[0]["body"]),
                         {"login": "octocat", "run": "run-1", "ts": "2026-03-01T12:00:00Z"})
        self.assertIn("not-me claimed this for octocat", markers[0]["body"])

    def test_labels_are_replaced_in_one_put_with_unrelated_labels_kept(self):
        self.thing(1)["labels"] = ["to do", "bug"]
        self.q.claim(1)
        puts = [c for c in self.fake.calls if "PUT" in c]
        self.assertEqual(len(puts), 1)
        self.assertEqual(sorted(self.thing(1)["labels"]), ["bug", "in progress"])

    def test_second_claim_updates_the_marker_instead_of_adding_one(self):
        self.q.claim(1)
        self.q.release(1)
        self.now = NOW + timedelta(minutes=5)
        second = self.queue("run-2").claim(1)
        markers = self.marker_comments(1)
        self.assertEqual(len(markers), 1)
        self.assertEqual(claims.parse_marker(markers[0]["body"])["run"], "run-2")
        self.assertEqual(claims.parse_marker(markers[0]["body"])["ts"], "2026-03-01T12:05:00Z")
        self.assertEqual(second.marker_comment_id, markers[0]["id"])

    def test_refuses_when_assigned_to_someone_else(self):
        self.thing(1)["assignees"] = ["hubot"]
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(1)
        self.assertEqual(self.fake.writes_made(), [])
        self.assertEqual(self.thing(1)["labels"], ["to do"])

    def test_refuses_when_already_in_progress(self):
        self.thing(1)["labels"] = ["in progress"]
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(1)
        self.assertEqual(self.fake.writes_made(), [])

    def test_refuses_a_closed_issue(self):
        self.thing(1)["state"] = "CLOSED"
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(1)

    def test_an_issue_that_is_not_to_do_is_an_illegal_transition_not_a_write(self):
        self.thing(2)["labels"] = ["needs work"]
        with self.assertRaises(IllegalTransition):
            self.q.claim(2)
        self.assertEqual(self.fake.writes_made(), [])

    def test_lost_race_on_assignee_releases_ours(self):
        self.fake.race_assignee = "hubot"
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(1)
        self.assertEqual(self.thing(1)["assignees"], ["hubot"])  # the winner keeps it
        self.assertEqual(self.thing(1)["labels"], ["in progress"])  # and keeps the winner's state
        released = claims.parse_marker(self.marker_comments(1)[0]["body"])
        self.assertTrue(claims.is_released(released))
        self.assertFalse((self.claims_dir / "issue-1.lock").exists())

    def test_older_foreign_marker_wins_and_ours_is_released(self):
        foreign = claims.marker_body("hubot", "run-9", claims.iso(NOW - timedelta(minutes=1)))
        self.fake.post_comment(REPO, 1, "hubot", foreign)
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(1)
        self.assertEqual(self.thing(1)["assignees"], [])
        self.assertEqual(self.thing(1)["labels"], ["to do"])  # nobody else holds it, so it goes back to the queue
        mine = [c for c in self.marker_comments(1) if "octocat" in c["body"]]
        self.assertEqual(len(mine), 1)
        self.assertTrue(claims.is_released(claims.parse_marker(mine[0]["body"])))
        untouched = [c for c in self.marker_comments(1) if "hubot" in c["body"]][0]
        self.assertEqual(untouched["body"], foreign)

    def test_newer_foreign_marker_loses_to_us(self):
        foreign = claims.marker_body("hubot", "run-9", claims.iso(NOW + timedelta(minutes=1)))
        self.fake.post_comment(REPO, 1, "hubot", foreign)
        self.assertEqual(self.q.claim(1).now, "in_progress")
        self.assertEqual(self.thing(1)["assignees"], ["octocat"])

    def test_forged_marker_is_ignored(self):
        # hubot writes a comment that claims to be octocat's, and an older "foreign" one by an untrusted user
        self.fake.post_comment(REPO, 1, "hubot", claims.marker_body("octocat", "run-x", claims.iso(NOW)))
        self.fake.post_comment(REPO, 1, "mallory",
                               claims.marker_body("mallory", "run-m", claims.iso(NOW - timedelta(minutes=1))))
        self.assertEqual(self.q.marker_comments(1), [])
        got = self.q.claim(1)
        self.assertEqual(self.thing(1)["assignees"], ["octocat"])
        mine = [c for c in self.marker_comments(1) if c["user"] == "octocat"]
        self.assertEqual(len(mine), 1)  # a new comment of our own, not an edit of hubot's
        self.assertEqual(got.marker_comment_id, mine[0]["id"])
        forged = [c for c in self.marker_comments(1) if c["user"] == "hubot"][0]
        self.assertFalse(claims.is_released(claims.parse_marker(forged["body"])))

    def test_long_timeline_is_read_across_pages(self):
        for _ in range(150):
            self.fake._event(REPO, 1, "labeled")
        self.assertEqual(self.q.claim(1).now, "in_progress")
        events = [e for e in self.fake.state["timeline"] if e["number"] == 1]
        self.assertGreater(events.index([e for e in events if e["event"] == "assigned"][0]), 100)
        pages = [c for c in self.fake.calls if "timeline" in " ".join(c)]
        self.assertEqual(len(pages), 2)

    def test_foreign_assignment_on_page_two_is_seen(self):
        for _ in range(99):
            self.fake._event(REPO, 1, "labeled")
        self.fake.race_assignee = "hubot"
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(1)

    def test_missing_own_assignment_fails_closed(self):
        self.fake._r_timeline = lambda g, flags: []
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(1)
        self.assertEqual(self.thing(1)["assignees"], [])
        self.assertEqual(self.thing(1)["labels"], ["to do"])
        self.assertTrue(claims.is_released(claims.parse_marker(self.marker_comments(1)[0]["body"])))
        self.assertFalse((self.claims_dir / "issue-1.lock").exists())

    def test_stale_or_released_foreign_markers_do_not_block(self):
        self.fake.post_comment(REPO, 1, "hubot", claims.marker_body("hubot", "r", claims.iso(NOW - timedelta(days=2))))
        self.fake.post_comment(REPO, 1, "monalisa", claims.marker_body("monalisa", "r", claims.iso(NOW - timedelta(minutes=1)),
                                                                       released=True))
        self.assertEqual(self.q.claim(1).now, "in_progress")

    def test_a_past_foreign_assignment_that_was_removed_does_not_count(self):
        self.fake.add_assignee(REPO, 1, "hubot")
        self.thing(1)["assignees"].remove("hubot")
        self.fake._event(REPO, 1, "unassigned", assignee={"login": "hubot"})
        self.assertEqual(self.q.claim(1).now, "in_progress")

    def test_local_lock_stops_a_second_session(self):
        self.q.claim(1)
        other = self.queue("run-2")
        self.thing(1)["labels"] = ["to do"]  # as if the label had been reset
        self.thing(1)["assignees"] = []
        before = len(self.fake.writes_made())
        with self.assertRaises(claims.ClaimLost):
            other.claim(1)
        self.assertEqual(len(self.fake.writes_made()), before)

    def test_stale_local_lock_is_replaced(self):
        claims.acquire_lock(self.claims_dir / "issue-1.lock", "run-dead", 8, NOW - timedelta(hours=9))
        self.assertEqual(self.q.claim(1).run_id, "run-1")
        self.assertEqual(claims.read_lock(self.claims_dir / "issue-1.lock")["run_id"], "run-1")

    def test_lock_is_dropped_when_the_claim_is_refused(self):
        self.thing(1)["assignees"] = ["hubot"]
        with self.assertRaises(claims.ClaimLost):
            self.q.claim(1)
        self.assertFalse((self.claims_dir / "issue-1.lock").exists())

    def test_dry_run_claim_changes_nothing(self):
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=Ledger(self.dir / "dry.jsonl"))
        self.q = self.queue("run-1")
        before = self.fake.snapshot()["repos"]
        got = self.q.claim(1)
        self.assertEqual((got.now, got.marker_comment_id), ("in_progress", None))
        self.assertEqual(self.fake.snapshot()["repos"], before)


class ReleaseAndMoveTests(QueueTestCase):
    def test_release_restores_todo_unassigns_and_marks_the_comment(self):
        self.q.claim(1)
        self.q.release(1)
        self.assertEqual(self.thing(1)["labels"], ["to do"])
        self.assertEqual(self.thing(1)["assignees"], [])
        self.assertEqual(len(self.marker_comments(1)), 1)
        self.assertTrue(claims.is_released(claims.parse_marker(self.marker_comments(1)[0]["body"])))
        self.assertFalse((self.claims_dir / "issue-1.lock").exists())

    def test_release_to_another_state(self):
        self.q.claim(1)
        self.q.release(1, to="needs_human")
        self.assertEqual(self.thing(1)["labels"], ["needs human"])

    def test_release_of_an_unclaimed_issue_is_harmless(self):
        self.q.release(1)
        self.assertEqual(self.thing(1)["labels"], ["to do"])
        self.assertEqual(self.thing(1)["comments"], [])

    def test_move_follows_the_graph(self):
        self.q.claim(1)
        self.q.move(1, "needs_review")
        self.assertEqual(self.thing(1)["labels"], ["needs review"])
        self.assertEqual(self.q.state(1), "needs_review")

    def test_move_rejects_illegal_edges(self):
        with self.assertRaises(IllegalTransition):
            self.q.move(1, "needs_review")

    def test_move_require_current(self):
        with self.assertRaises(StateMismatch):
            self.q.move(1, "needs_review", require_current="in_progress")
        self.q.move(1, "in_progress", require_current="todo")
        self.assertEqual(self.thing(1)["labels"], ["in progress"])

    def test_state_of_an_issue_with_no_state_label(self):
        self.assertEqual(self.q.state(2), None)
        self.assertEqual(self.q.state(1), "todo")


class StaleTests(QueueTestCase):
    def issue_with_marker(self, age_hours, labels=("in progress",), released=False):
        body = claims.marker_body("octocat", "run-1", claims.iso(NOW - timedelta(hours=age_hours)), released=released)
        return {"number": 12, "labels": [{"name": n} for n in labels], "comments": [{"author": {"login": "octocat"}, "body": body}]}

    def test_old_marker_without_a_branch_is_stale(self):
        self.assertTrue(is_stale(self.issue_with_marker(9), 8, [], "in progress", NOW, is_trusted=TRUST))

    def test_fresh_marker_is_not_stale(self):
        self.assertFalse(is_stale(self.issue_with_marker(1), 8, [], "in progress", NOW, is_trusted=TRUST))

    def test_a_branch_for_the_issue_keeps_it_alive(self):
        issue = self.issue_with_marker(30)
        self.assertFalse(is_stale(issue, 8, ["feat/12-widget"], "in progress", NOW, is_trusted=TRUST))
        self.assertFalse(is_stale(issue, 8, ["12-widget"], "in progress", NOW, is_trusted=TRUST))
        self.assertTrue(is_stale(issue, 8, ["feat/123-other", "112-x"], "in progress", NOW, is_trusted=TRUST))

    def test_only_in_progress_issues_can_be_stale(self):
        self.assertFalse(is_stale(self.issue_with_marker(30, labels=("to do",)), 8, [], "in progress", NOW, is_trusted=TRUST))

    def test_no_marker_or_released_marker_is_not_reported(self):
        self.assertFalse(is_stale({"number": 12, "labels": [{"name": "in progress"}], "comments": []}, 8, [],
                                  "in progress", NOW, is_trusted=TRUST))
        self.assertFalse(is_stale(self.issue_with_marker(30, released=True), 8, [], "in progress", NOW, is_trusted=TRUST))

    def test_marker_written_by_someone_else_or_untrusted_is_ignored(self):
        issue = self.issue_with_marker(30)
        issue["comments"][0]["author"] = {"login": "hubot"}  # hubot forging octocat's marker
        self.assertFalse(is_stale(issue, 8, [], "in progress", NOW, is_trusted=TRUST))
        issue["comments"][0]["author"] = {"login": "octocat"}
        self.assertTrue(is_stale(issue, 8, [], "in progress", NOW, is_trusted=TRUST))
        self.assertFalse(is_stale(issue, 8, [], "in progress", NOW, is_trusted=lambda login: False))

    def test_queue_stale_reads_the_issue_and_never_releases_it(self):
        self.q.claim(1)
        self.now = NOW + timedelta(hours=9)
        self.assertTrue(self.q.stale(1, branches=[]))
        self.assertFalse(self.q.stale(1, branches=["fix/1-first-issue"]))
        self.assertEqual(self.thing(1)["labels"], ["in progress"])
        self.assertEqual(self.thing(1)["assignees"], ["octocat"])


class BotMarkerTests(QueueTestCase):
    """A bot's claim marker reads the same in the stale checks as on the REST path: `<slug>[bot]`."""

    def setUp(self):
        super().setUp()
        self.roster = Roster(entries=[Member("octocat", tier="lead"), Member("renovate[bot]", tier="lead")],
                             operator="octocat")
        self.q = self.queue("run-1")
        self.thing(1)["labels"] = ["in progress"]
        old = claims.iso(NOW - timedelta(hours=30))
        self.fake.post_comment(REPO, 1, "renovate[bot]", claims.marker_body("renovate[bot]", "r", old))

    def test_queue_stale_sees_the_bot_marker(self):
        self.assertTrue(self.q.stale(1, branches=[]))

    def test_stale_claims_sees_the_bot_marker(self):
        found = queue.stale_claims(self.gh, REPO, self.q.labels, 8, [], is_trusted=self.q.marker_trusted)
        self.assertEqual([c["number"] for c in found], [1])

    def test_unlisted_bot_marker_still_ignored(self):
        self.roster = Roster(entries=[Member("octocat", tier="lead")], operator="octocat")
        q = self.queue("run-2")
        self.assertFalse(q.stale(1, branches=[]))


class FactoryTests(QueueTestCase):
    def test_labels_source_gives_a_label_queue(self):
        q = queue.make_queue(self.cfg, self.gh, self.roster, "octocat", "run-1", project_dir=self.project)
        self.assertIsInstance(q, LabelQueue)
        self.assertEqual(q.tracker, REPO)  # tracker "auto" is the origin

    def test_board_source_gives_a_board_queue(self):
        from notme.queue.board import BoardQueue
        cfg = make_cfg(source="board", board={"owner": "acme", "number": 6, "status_field": "Status",
                                              "priority_fields": []})
        self.assertIsInstance(queue.make_queue(cfg, self.gh, self.roster, "octocat", "r"), BoardQueue)

    def test_unknown_source_and_missing_tracker(self):
        with self.assertRaises(ValueError):
            queue.make_queue(make_cfg(source="jira"), self.gh, self.roster, "octocat", "r")
        bare = GhClient(GuardContext(mode="live"), runner=self.fake, dry_run=False)
        with self.assertRaises(ValueError):
            queue.make_queue(self.cfg, bare, self.roster, "octocat", "r")

    def test_bad_operator_or_tracker_is_refused(self):
        with self.assertRaises(ValueError):
            LabelQueue(self.cfg, self.gh, self.roster, "--bad", "r", REPO)
        with self.assertRaises(ValueError):
            LabelQueue(self.cfg, self.gh, self.roster, "octocat", "r", "not a slug")


if __name__ == "__main__":
    unittest.main()
