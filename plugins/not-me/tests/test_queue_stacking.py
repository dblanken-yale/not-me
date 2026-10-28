"""`Stack on: owner/repo#N`: readiness, the fact the gatherer returns, and what pick-issue holds back."""
from __future__ import annotations

import json
import unittest

from notme.stacking import parse_refs
from tests.test_queue_labels import REPO, QueueTestCase, add_issue


def add_pr(fake, number, repo=REPO, head="feat/first", state="OPEN", draft=False, author="octocat", fork=False):
    fake.state["repos"][repo]["things"][str(number)] = {
        "number": number, "title": f"PR {number}", "body": "", "state": state, "labels": [], "assignees": [],
        "author": author, "comments": [], "kind": "pr", "headRefName": head, "headRefOid": "c" * 40,
        "baseRefName": "main", "isDraft": draft, "isCrossRepository": fork, "reviewRequests": [],
        "reviewDecision": None, "mergeable": "MERGEABLE", "reviews": [], "threads": []}


class StackTests(QueueTestCase):
    cfg_overrides = {"include_unassigned": True, "stacking": "declared"}

    def setUp(self):
        super().setUp()
        things = self.fake.state["repos"][REPO]["things"]
        for n in ("1", "2", "3"):
            things.pop(n)
        add_pr(self.fake, 20)

    def stack(self, number=5, line=f"Stack on: {REPO}#20", author="octocat", blocked_by=(20,), **issue):
        add_issue(self.fake, number, author=author, body=f"Goal.\n\n{line}\n", **issue)
        self.thing(number)["blocked_by"] = list(blocked_by)
        return self.q.stack_on(number, author)

    def test_trusted_line_with_an_open_pr_is_ready_and_stacks_on_its_head(self):
        fact = self.stack()
        self.assertTrue(fact["ready"] and fact["stacks"])
        self.assertEqual((fact["pr"], fact["head"], fact["base"], fact["state"], fact["draft"]),
                         (f"{REPO}#20", "feat/first", "main", "OPEN", False))
        self.assertTrue(fact["author_trusted"])
        self.assertIsNone(fact["reason"])

    def test_the_fact_is_in_pick_facts_and_never_the_body(self):
        self.stack()
        facts = {f["number"]: f for f in self.q.facts(self.q.candidates())}
        self.assertEqual(facts[5]["stack_on"]["head"], "feat/first")
        self.assertNotIn("Goal.", json.dumps(facts))

    def test_issue_without_a_line_has_no_fact(self):
        add_issue(self.fake, 5, body="Goal.\n")
        self.assertIsNone(self.q.stack_on(5, "octocat"))

    def test_untrusted_author_line_is_ignored(self):
        self.assertIsNone(self.stack(author="mallory"))

    def test_trusted_pr_author_is_required(self):
        add_pr(self.fake, 21, author="mallory")
        fact = self.stack(line=f"Stack on: {REPO}#21", blocked_by=(21,))
        self.assertFalse(fact["ready"])
        self.assertIn("not on the roster", fact["reason"])

    def test_draft_closed_and_fork_predecessors_are_not_ready_with_a_reason(self):
        for pr, kwargs, words in ((21, {"draft": True}, "draft"), (22, {"state": "CLOSED"}, "closed without merging"),
                                  (23, {"fork": True}, "fork")):
            with self.subTest(words):
                add_pr(self.fake, pr, **kwargs)
                fact = self.stack(number=pr + 10, line=f"Stack on: {REPO}#{pr}", blocked_by=(pr,))
                self.assertFalse(fact["ready"])
                self.assertFalse(fact["stacks"])
                self.assertIn(words, fact["reason"])

    def test_another_open_blocker_is_not_ready(self):
        add_issue(self.fake, 7)
        fact = self.stack(blocked_by=(20, 7))
        self.assertFalse(fact["ready"])
        self.assertIn(f"{REPO}#7", fact["reason"])
        add_issue(self.fake, 8, state="CLOSED")  # a closed blocker does not hold it
        self.assertTrue(self.stack(number=9, blocked_by=(20, 8))["ready"])

    def test_unreadable_blockers_are_not_ready(self):
        add_issue(self.fake, 5, body=f"Stack on: {REPO}#20\n")
        self.thing(5)["deps_fail"] = True
        self.assertFalse(self.q.stack_on(5, "octocat")["ready"])

    def test_stacking_off_keeps_it_waiting(self):
        self.q.queue_cfg["stacking"] = "off"
        fact = self.stack()
        self.assertFalse(fact["ready"])
        self.assertIn("queue.stacking is off", fact["reason"])

    def test_merged_predecessor_is_a_normal_issue(self):
        add_pr(self.fake, 24, state="MERGED")
        fact = self.stack(line=f"Stack on: {REPO}#24", blocked_by=(24,))
        self.assertTrue(fact["ready"])
        self.assertFalse(fact["stacks"])
        self.assertEqual(fact["state"], "MERGED")

    def test_a_merged_predecessor_stays_a_normal_issue_even_when_stacking_is_off(self):
        self.q.queue_cfg["stacking"] = "off"
        add_pr(self.fake, 24, state="MERGED")
        self.assertTrue(self.stack(line=f"Stack on: {REPO}#24", blocked_by=())["ready"])

    def test_pr_in_a_repo_that_is_not_allowed_is_refused(self):
        add_pr(self.fake, 3, repo="acme/app-ui")
        fact = self.stack(line="Stack on: acme/app-ui#3", blocked_by=())
        self.assertFalse(fact["ready"])
        self.assertIn("not an allowed repo", fact["reason"])
        self.assertIn("/not-me:allow-repo acme/app-ui", fact["reason"])

    def test_an_issue_ref_uses_its_single_open_pr(self):
        add_issue(self.fake, 30, state="OPEN")
        self.thing(30)["linked_prs"] = [{"number": 20, "repository": {"nameWithOwner": REPO}}]
        fact = self.stack(line=f"Stack on: {REPO}#30", blocked_by=(30,))
        self.assertTrue(fact["ready"])
        self.assertEqual((fact["pr"], fact["head"]), (f"{REPO}#20", "feat/first"))

    def test_an_issue_ref_with_two_open_prs_or_none_is_not_ready(self):
        add_pr(self.fake, 25)
        add_issue(self.fake, 30)
        self.thing(30)["linked_prs"] = [{"number": 20, "repository": {"nameWithOwner": REPO}},
                                        {"number": 25, "repository": {"nameWithOwner": REPO}}]
        self.assertIn("more than one open PR", self.stack(line=f"Stack on: {REPO}#30", blocked_by=(30,))["reason"])
        add_issue(self.fake, 31)
        self.assertIn("no open PR", self.stack(number=6, line=f"Stack on: {REPO}#31", blocked_by=(31,))["reason"])

    def test_two_lines_or_a_malformed_line_is_not_ready(self):
        self.assertFalse(self.stack(line=f"Stack on: {REPO}#20\nStack on: {REPO}#21")["ready"])
        self.assertFalse(self.stack(number=6, line="Stack on: #20")["ready"])

    def test_pick_filter_holds_back_issues_that_cannot_stack(self):
        add_pr(self.fake, 21, draft=True)
        self.stack(number=5)
        self.stack(number=6, line=f"Stack on: {REPO}#21", blocked_by=(21,))
        add_issue(self.fake, 7, body="No line.")
        kept, held = self.q.stack_filter(self.q.candidates(), None)
        self.assertEqual(sorted(c.number for c in kept), [5, 7])
        self.assertEqual([(h["number"], h["ref"]) for h in held], [(6, f"{REPO}#21")])
        self.assertIn("draft", held[0]["reason"])

    def test_parse_refs(self):
        self.assertEqual(parse_refs("x\nStack on: a/b#3\n")["refs"], [("a/b", 3)])
        self.assertEqual(parse_refs("stack on: https://github.com/a/b/pull/4")["refs"], [("a/b", 4)])
        self.assertEqual(parse_refs("see Stack on: a/b#3 inline")["lines"], 0)


if __name__ == "__main__":
    unittest.main()
