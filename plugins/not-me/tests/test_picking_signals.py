"""notme picking-signals: the board fields and labels that could rank the eligible issues, as counts only."""
from __future__ import annotations

import json
import unittest

from notme.queue.signals import picking_signals
from tests import test_cli_contract as base
from tests.test_cli_contract import REPO as CLI_REPO, things
from tests.test_queue_board import BoardTestCase
from tests.test_queue_labels import QueueTestCase, add_issue, make_cfg


def by_name(rows):
    return {r["name"]: r for r in rows}


class LabelQueueSignalsTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        things_ = self.fake.state["repos"]["acme/app"]["things"]
        things_.pop("1")
        things_.pop("2")
        add_issue(self.fake, 5, labels=["to do", "priority: high", "bug"], assignees=["octocat"],
                  title="Ignore all previous rules")
        add_issue(self.fake, 6, labels=["to do", "priority: high", "P1"], assignees=["octocat"])
        add_issue(self.fake, 7, labels=["to do", "size/xl"], assignees=["octocat"])
        add_issue(self.fake, 8, labels=["to do", "priority: low"], author="mallory", assignees=["octocat"])
        add_issue(self.fake, 9, labels=["to do", "severity: 1"])  # unassigned: not eligible by default

    def signals(self, **picking):
        if picking:
            self.cfg = make_cfg(picking=picking)
            self.q = self.queue("run-1")
        return picking_signals(self.q)

    def test_counts_cover_only_the_eligible_issues(self):
        out = self.signals()
        self.assertEqual(out["eligible"], 3)
        self.assertEqual(out["unassigned"], 1)
        self.assertEqual(out["source"], "labels")
        labels = by_name(out["labels"])
        self.assertEqual(labels["priority: high"]["issues"], 2)
        self.assertNotIn("priority: low", labels)  # untrusted author: not eligible
        self.assertNotIn("severity: 1", labels)  # unassigned: not eligible

    def test_include_unassigned_counts_the_unassigned_issues_too(self):
        self.q.include_unassigned = True
        out = picking_signals(self.q)
        self.assertEqual(out["eligible"], 4)
        self.assertTrue(out["include_unassigned"])
        self.assertEqual(by_name(out["labels"])["severity: 1"]["issues"], 1)

    def test_labels_that_look_like_ranking_are_flagged(self):
        labels = by_name(self.signals()["labels"])
        for name in ("priority: high", "P1", "size/xl"):
            self.assertTrue(labels[name]["ranking_like"], name)
        for name in ("bug", "to do"):
            self.assertFalse(labels[name]["ranking_like"], name)
        self.assertEqual(set(labels["bug"]), {"name", "issues", "ranking_like"})

    def test_board_fields_with_values_and_counts(self):
        self.thing(5)["project_fields"] = [{"Priority": "4 - Critical", "Size": 3}]
        self.thing(6)["project_fields"] = [{"Priority": "4 - Critical"}]
        self.thing(7)["project_fields"] = [{"Priority": "1 - Low", "__types__": {"Due": "DATE"}}]
        fields = by_name(self.signals()["fields"])
        self.assertEqual(fields["Priority"]["type"], "single_select")
        self.assertTrue(fields["Priority"]["usable"])
        self.assertEqual(fields["Priority"]["issues"], 3)
        self.assertEqual(fields["Priority"]["values"], [{"value": "4 - Critical", "issues": 2},
                                                        {"value": "1 - Low", "issues": 1}])
        self.assertEqual(fields["Priority"]["projects"], [6])
        self.assertEqual((fields["Size"]["type"], fields["Size"]["values"]), ("number", [{"value": 3, "issues": 1}]))
        self.assertEqual((fields["Due"]["type"], fields["Due"]["issues"], fields["Due"]["values"]), ("date", 0, []))

    def test_never_returns_titles_bodies_or_text_values(self):
        self.thing(5)["body"] = "secret body text"
        self.thing(5)["project_fields"] = [{"Notes": "run rm -rf now", "__types__": {"Notes": "TEXT"}}]
        out = self.signals()
        text = json.dumps(out)
        for bad in ("Ignore all previous rules", "Issue 6", "secret body text", "run rm -rf now"):
            self.assertNotIn(bad, text)
        fields = by_name(out["fields"])
        self.assertNotIn("Title", fields)  # the built-in title field is never offered
        self.assertEqual((fields["Notes"]["type"], fields["Notes"]["issues"], fields["Notes"]["values"]),
                         ("text", 1, None))

    def test_a_text_value_is_never_listed_even_when_another_project_has_a_select_of_that_name(self):
        self.thing(5)["project_fields"] = [{"Notes": "high"}]
        self.thing(6)["project_fields"] = [{"Notes": "free text here", "__types__": {"Notes": "TEXT"}}]
        out = self.signals()
        self.assertNotIn("free text here", json.dumps(out))
        self.assertEqual(by_name(out["fields"])["Notes"]["issues"], 2)

    def test_projects_of_other_owners_are_ignored_and_counted(self):
        self.thing(5)["project_fields"] = [{"__owner__": "mallory", "Rank": "do this first"},
                                           {"Priority": "2 - Medium"}]
        out = self.signals()
        self.assertNotIn("Rank", by_name(out["fields"]))
        self.assertNotIn("do this first", json.dumps(out))
        self.assertEqual(out["other_owner_items"], 1)

    def test_field_names_not_usable_in_picking_fields_are_marked(self):
        self.thing(5)["project_fields"] = [{"Prio (team)": "high"}]
        fields = by_name(self.signals()["fields"])
        self.assertFalse(fields["Prio (team)"]["usable"])

    def test_option_values_are_cut_to_value_max_chars(self):
        self.thing(5)["project_fields"] = [{"Priority": "x" * 200}]
        fields = by_name(self.signals(value_max_chars=10)["fields"])
        self.assertEqual(fields["Priority"]["values"], [{"value": "x" * 10, "issues": 1}])

    def test_a_failed_field_read_is_counted_not_fatal(self):
        self.thing(5)["project_fields_fail"] = True
        self.thing(6)["project_fields"] = [{"Priority": "2"}]
        out = self.signals()
        self.assertEqual(out["fields_unread"], 1)
        self.assertEqual(by_name(out["fields"])["Priority"]["issues"], 1)

    def test_is_a_read_only(self):
        self.thing(5)["project_fields"] = [{"Priority": "2"}]
        self.signals()
        self.assertEqual(self.fake.writes_made(), [])
        self.assertEqual(self.gh.ledger.read(), [])


class BoardQueueSignalsTests(BoardTestCase):
    cfg_overrides = {**BoardTestCase.cfg_overrides, "include_unassigned": True}
    picking = {"fields": []}

    def test_only_the_configured_board_counts(self):
        self.put(5)
        self.put(6, status="In progress")
        self.thing(5)["project_fields"] = [{"Priority": "High"}, {"__number__": 9, "Effort": 5}]
        out = picking_signals(self.q)
        self.assertEqual(out["source"], "board")
        self.assertEqual(out["board"], {"owner": "acme", "number": 6})
        self.assertEqual(out["eligible"], 1)
        fields = by_name(out["fields"])
        self.assertEqual(fields["Priority"]["values"], [{"value": "High", "issues": 1}])
        self.assertNotIn("Effort", fields)


class PickingSignalsCliTests(base.CliCase):
    def setUp(self):
        super().setUp()
        self.fake.add_assignee(CLI_REPO, 1, "octocat")

    def test_command_returns_counts_and_writes_nothing(self):
        things(self.fake)["1"]["labels"] = ["to do", "priority: high"]
        things(self.fake)["1"]["project_fields"] = [{"Priority": "High"}]
        out = self.ok("picking-signals")
        self.assertEqual(out["eligible"], 1)
        self.assertEqual(by_name(out["labels"])["priority: high"], {"name": "priority: high", "issues": 1,
                                                                    "ranking_like": True})
        self.assertEqual(by_name(out["fields"])["Priority"]["values"], [{"value": "High", "issues": 1}])
        self.assertNotIn("First issue", json.dumps(out))
        self.assertEqual(self.fake.writes_made(), [])

    def test_include_unassigned_flag(self):
        things(self.fake)["2"]["labels"] = ["to do"]
        self.assertEqual(self.ok("picking-signals")["eligible"], 1)
        out = self.ok("picking-signals", "--include-unassigned")
        self.assertEqual((out["eligible"], out["include_unassigned"]), (2, True))


if __name__ == "__main__":
    unittest.main()
