from __future__ import annotations

import unittest

from notme import roster
from notme.roster import Member
from tests import TempCase

TABLE = """intro

## Roster

| GitHub login | Name | Tier | Assignable | Reviewer |
|---|---|---|---|---|
| @octocat |  Mona Octo | Lead | yes | ✓ |
| hubot | Hu Bot | authority | no | x |
|  monalisa | Mona | member | TRUE | false |
| | blank login | member | yes | yes |

## Authority notes
"""


class ParseTests(unittest.TestCase):
    def test_parse_full_table(self):
        members = roster.parse_roster(TABLE)
        self.assertEqual(members, [
            Member("octocat", "Mona Octo", "lead", True, True),
            Member("hubot", "Hu Bot", "authority", False, True),
            Member("monalisa", "Mona", "member", True, False),
        ])

    def test_missing_columns_and_order(self):
        members = roster.parse_roster("| Tier | Login |\n|--|--|\n| lead | octocat |\n")
        self.assertEqual(members, [Member("octocat", "", "lead", False, False)])

    def test_empty_table_and_no_table(self):
        self.assertEqual(roster.parse_roster("| GitHub login | Name |\n|---|---|\n"), [])
        self.assertEqual(roster.parse_roster("no table here"), [])

    def test_unknown_tier_is_member(self):
        self.assertEqual(roster.parse_roster("| login | tier |\n|-|-|\n| a | boss |\n")[0].tier, "member")


class EffectiveTests(unittest.TestCase):
    def test_empty_roster_operator_is_only_lead(self):
        r = roster.effective([], "octocat")
        self.assertEqual(r.leads, ["octocat"])
        self.assertTrue(r.is_trusted("OctoCat"))
        self.assertFalse(r.is_trusted("hubot"))
        self.assertEqual(r.assignable, ["octocat"])

    def test_tiers(self):
        r = roster.effective(roster.parse_roster(TABLE), "someone")
        self.assertEqual(r.leads, ["octocat"])
        self.assertEqual(r.authorities, ["hubot"])
        self.assertEqual(r.members, ["monalisa"])
        self.assertEqual(r.reviewers, ["octocat", "hubot"])
        self.assertEqual(r.assignable, ["octocat", "monalisa"])
        self.assertTrue(r.is_trusted("hubot"))
        self.assertFalse(r.is_trusted("monalisa"))
        self.assertFalse(r.is_trusted("someone"))

    def test_merge_by_login(self):
        low = [Member("octocat", "A", "member", False, False), Member("hubot", "H", "member", False, False)]
        high = [Member("OctoCat", "B", "lead", True, True), Member("monalisa", "M", "member", True, False)]
        merged = roster.merge_members(low, high)
        self.assertEqual([m.login for m in merged], ["OctoCat", "hubot", "monalisa"])
        self.assertEqual(merged[0].tier, "lead")


class ConfigRosterTests(unittest.TestCase):
    def test_extends_unions_rows_by_login(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        tc.write("global", "project", "---\noperator: octocat\n---\n## Roster\n\n| GitHub login | Tier |\n|-|-|\n"
                                      "| octocat | lead |\n| hubot | member |\n")
        tc.write("repo", "project", "---\nextends: global\n---\n## Roster\n\n| GitHub login | Tier |\n|-|-|\n"
                                    "| hubot | authority |\n| monalisa | member |\n")
        cfg = tc.load()
        r = cfg.effective_roster()
        self.assertEqual(r.leads, ["octocat"])
        self.assertEqual(r.authorities, ["hubot"])
        self.assertEqual(r.members, ["monalisa"])
        self.assertIn("monalisa", cfg.bodies["project"])
        self.assertIn("octocat", cfg.bodies["project"])

    def test_without_extends_repo_roster_alone(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        tc.write("global", "project", "---\n---\n| login | tier |\n|-|-|\n| octocat | lead |\n")
        tc.write("repo", "project", "---\n---\n| login | tier |\n|-|-|\n| hubot | lead |\n")
        self.assertEqual(tc.load().effective_roster().leads, ["hubot"])

    def test_default_roster_uses_operator(self):
        tc = TempCase()
        self.addCleanup(tc.cleanup)
        self.assertEqual(tc.load().effective_roster(operator="octocat").leads, ["octocat"])


if __name__ == "__main__":
    unittest.main()
