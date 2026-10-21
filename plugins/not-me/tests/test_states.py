from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from notme import states
from notme.gh import GhClient, ReadbackMismatch
from notme.dryrun import Ledger
from notme.states import IllegalTransition, Labels
from notme.writes import GuardContext
from tests.fakes.fakegh import FakeRunner

MAPPING = {"todo": "to do", "in_progress": "in progress", "needs_review": "needs review",
           "needs_work": "needs work", "ready_to_merge": "ready to merge", "question": "question",
           "blocked": "blocked", "epic": "epic", "needs_help": "needs help", "needs_human": "needs human",
           "paused": "paused"}
LABELS = Labels(MAPPING)

ISSUE_LEGAL = [("todo", "in_progress"), ("in_progress", "needs_review"), ("in_progress", "blocked"),
               ("in_progress", "todo"), ("in_progress", "needs_human"), ("needs_review", "needs_work"),
               ("needs_review", "ready_to_merge"), ("needs_review", "question"), ("needs_work", "in_progress"),
               ("question", "needs_review"), ("question", "in_progress"), ("blocked", "todo"),
               ("needs_human", "in_progress"), ("needs_human", "todo")]
ISSUE_ILLEGAL = [("todo", "needs_review"), ("todo", "blocked"), ("needs_review", "in_progress"),
                 ("blocked", "in_progress"), ("ready_to_merge", "todo"), ("needs_work", "needs_review"),
                 ("todo", "todo")]


class LabelsMapTests(unittest.TestCase):
    def test_name_and_abstract_round_trip(self):
        self.assertEqual(LABELS.name("needs_review"), "needs review")
        self.assertEqual(LABELS.abstract("needs review"), "needs_review")
        self.assertEqual(LABELS.abstract("Needs Review"), "needs_review")

    def test_unknown_names_raise_helpful_key_errors(self):
        with self.assertRaises(KeyError) as ctx:
            LABELS.name("shipped")
        self.assertIn("known states", str(ctx.exception))
        with self.assertRaises(KeyError) as ctx:
            LABELS.abstract("bug")
        self.assertIn("not one of the configured state labels", str(ctx.exception))

    def test_current_ignores_unrelated_and_additive_labels(self):
        names = ["bug", "in progress", "paused"]
        self.assertEqual(LABELS.current(names), ["in_progress"])
        self.assertEqual(LABELS.additive(names), ["paused"])


class TransitionTests(unittest.TestCase):
    def test_every_legal_issue_edge(self):
        for frm, to in ISSUE_LEGAL:
            with self.subTest(frm=frm, to=to):
                out = states.transition([MAPPING[frm]], frm, to, LABELS)
                self.assertEqual(out, [MAPPING[to]])

    def test_illegal_issue_edges(self):
        for frm, to in ISSUE_ILLEGAL:
            with self.subTest(frm=frm, to=to):
                with self.assertRaises(IllegalTransition):
                    states.transition([MAPPING[frm]], frm, to, LABELS)

    def test_pr_edges(self):
        for frm, to in [("needs_review", "needs_work"), ("needs_work", "needs_review"),
                        ("needs_review", "ready_to_merge"), ("needs_review", "question"),
                        ("question", "needs_review")]:
            with self.subTest(frm=frm, to=to):
                self.assertEqual(states.transition([MAPPING[frm]], frm, to, LABELS, kind="pr"), [MAPPING[to]])
        with self.assertRaises(IllegalTransition):
            states.transition(["needs work"], "needs_work", "in_progress", LABELS, kind="pr")

    def test_needs_human_is_additive_on_a_pr(self):
        out = states.transition(["needs review", "bug"], "needs_review", "needs_human", LABELS, kind="pr")
        self.assertEqual(sorted(out), ["bug", "needs human", "needs review"])

    def test_needs_human_replaces_in_progress_on_an_issue(self):
        out = states.transition(["in progress"], "in_progress", "needs_human", LABELS)
        self.assertEqual(out, ["needs human"])

    def test_paused_is_additive_from_any_state_and_kind(self):
        for kind, frm in (("issue", "in_progress"), ("pr", "needs_work"), ("issue", "blocked")):
            with self.subTest(kind=kind, frm=frm):
                out = states.transition([MAPPING[frm]], frm, "paused", LABELS, kind=kind)
                self.assertEqual(sorted(out), sorted([MAPPING[frm], "paused"]))

    def test_unrelated_and_additive_labels_survive_a_move(self):
        out = states.transition(["bug", "to do", "paused", "claude"], "todo", "in_progress", LABELS)
        self.assertEqual(sorted(out), ["bug", "claude", "in progress", "paused"])

    def test_frm_must_be_carried(self):
        with self.assertRaises(IllegalTransition):
            states.transition(["bug"], "todo", "in_progress", LABELS)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            states.transition(["to do"], "todo", "in_progress", LABELS, kind="epic")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="notme-states-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = FakeRunner()
        ctx = GuardContext(origin_repo="acme/app", allowed_repos=("acme/app",), mode="live", project_dir=self.dir)
        self.gh = GhClient(ctx, runner=self.fake, dry_run=False, ledger=Ledger(self.dir / "ledger.jsonl"))

    def test_apply_puts_the_full_set_and_reads_it_back(self):
        new = states.apply(self.gh, "acme/app", 1, "todo", "in_progress", LABELS)
        self.assertEqual(new, ["in progress"])
        self.assertEqual(self.fake.state["repos"]["acme/app"]["things"]["1"]["labels"], ["in progress"])
        puts = [c for c in self.fake.calls if "PUT" in c]
        self.assertEqual(len(puts), 1)
        self.assertIn("repos/acme/app/issues/1/labels", puts[0])
        read_after = self.fake.calls[self.fake.calls.index(puts[0]) + 1]
        self.assertEqual(read_after, ["api", "repos/acme/app/issues/1/labels?per_page=100"])

    def test_apply_refuses_an_illegal_edge_before_writing(self):
        with self.assertRaises(IllegalTransition):
            states.apply(self.gh, "acme/app", 1, "todo", "needs_review", LABELS)
        self.assertEqual(self.fake.writes_made(), [])

    def test_apply_surfaces_a_failed_read_back(self):
        self.fake.drop_adds = True
        with self.assertRaises(ReadbackMismatch):
            states.apply(self.gh, "acme/app", 1, "todo", "in_progress", LABELS)


if __name__ == "__main__":
    unittest.main()
