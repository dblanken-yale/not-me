"""`Stack on: owner/repo#N`: readiness, the fact the gatherer returns, and what pick-issue holds back."""
from __future__ import annotations

import json
import unittest

from notme.stacking import parse_refs
from notme.writes import GuardContext
from tests.test_queue_either import EitherTestCase
from tests.test_queue_labels import REPO, QueueTestCase, add_issue


def ref(number, repo=REPO):
    """A reference as gh prints it: {number, repository: {name, owner: {login}}}."""
    owner, name = repo.split("/")
    return {"number": number, "repository": {"name": name, "owner": {"login": owner}}}


def add_pr(fake, number, repo=REPO, head="feat/first", state="OPEN", draft=False, author="octocat", fork=False,
           closes=()):
    fake.state["repos"][repo]["things"][str(number)] = {
        "number": number, "title": f"PR {number}", "body": "", "state": state, "labels": [], "assignees": [],
        "author": author, "comments": [], "kind": "pr", "headRefName": head, "headRefOid": "c" * 40,
        "baseRefName": "main", "isDraft": draft, "isCrossRepository": fork, "reviewRequests": [],
        "reviewDecision": None, "mergeable": "MERGEABLE", "reviews": [], "threads": [],
        "closes": [ref(n, repo) for n in closes]}


class StackTests(QueueTestCase):
    cfg_overrides = {"include_unassigned": True, "stacking": "declared"}

    def setUp(self):
        super().setUp()
        things = self.fake.state["repos"][REPO]["things"]
        for n in ("1", "2", "3"):
            things.pop(n)
        add_issue(self.fake, 40, labels=[], title="The earlier work")  # the issue PR 20 closes
        add_pr(self.fake, 20, closes=(40,))

    def stack(self, number=5, line=f"Stack on: {REPO}#20", author="octocat", blocked_by=(40,), **issue):
        """An issue with the line in its body. GitHub dependencies link issues, so the blocker is issue 40."""
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

    def test_a_pr_with_no_blocker_at_all_is_ready(self):
        self.assertTrue(self.stack(blocked_by=())["ready"])

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

    def test_a_body_last_edited_by_someone_off_the_roster_is_held(self):
        add_issue(self.fake, 5, body=f"Stack on: {REPO}#20\n")
        self.thing(5)["blocked_by"] = [40]
        self.thing(5)["editor"] = "mallory"
        fact = self.q.stack_on(5, "octocat")
        self.assertFalse(fact["ready"])
        self.assertIn("last edited by mallory", fact["reason"])

    def test_a_body_edited_by_a_trusted_login_is_fine_and_an_unreadable_editor_is_held(self):
        add_issue(self.fake, 5, body=f"Stack on: {REPO}#20\n")
        self.thing(5)["blocked_by"] = [40]
        self.thing(5)["editor"] = "hubot"
        self.assertTrue(self.q.stack_on(5, "octocat")["ready"])
        add_issue(self.fake, 6, body=f"Stack on: {REPO}#20\n")
        self.thing(6)["editor_fail"] = True
        self.assertIn("could not be read", self.q.stack_on(6, "octocat")["reason"])

    def test_trusted_pr_author_is_required(self):
        add_pr(self.fake, 21, author="mallory", closes=(40,))
        fact = self.stack(line=f"Stack on: {REPO}#21")
        self.assertFalse(fact["ready"])
        self.assertIn("not on the roster", fact["reason"])

    def test_draft_closed_and_fork_predecessors_are_not_ready_with_a_reason(self):
        for pr, kwargs, words in ((21, {"draft": True}, "draft"), (22, {"state": "CLOSED"}, "closed without merging"),
                                  (23, {"fork": True}, "fork")):
            with self.subTest(words):
                add_pr(self.fake, pr, closes=(40,), **kwargs)
                fact = self.stack(number=pr + 10, line=f"Stack on: {REPO}#{pr}")
                self.assertFalse(fact["ready"])
                self.assertFalse(fact["stacks"])
                self.assertIn(words, fact["reason"])

    def test_another_open_blocker_is_not_ready(self):
        add_issue(self.fake, 7)
        fact = self.stack(blocked_by=(40, 7))
        self.assertFalse(fact["ready"])
        self.assertIn(f"{REPO}#7", fact["reason"])
        add_issue(self.fake, 8, state="CLOSED")  # a closed blocker does not hold it
        self.assertTrue(self.stack(number=9, blocked_by=(40, 8))["ready"])

    def test_a_blocker_the_pr_does_not_close_holds_even_when_it_is_a_pr_number(self):
        fact = self.stack(blocked_by=(20,))  # GitHub links issues to issues, so this is some other work
        self.assertTrue(fact["ready"])  # the named PR itself is never another blocker
        add_pr(self.fake, 26)
        self.assertFalse(self.stack(number=6, blocked_by=(26,))["ready"])

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
        fact = self.stack(line=f"Stack on: {REPO}#24", blocked_by=())
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

    def test_an_allowed_companion_pr_is_held_because_the_branch_is_made_from_origin(self):
        self.gh.ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO, "acme/app-ui"), mode="live",
                                   project_dir=self.project)
        add_pr(self.fake, 3, repo="acme/app-ui")
        fact = self.stack(line="Stack on: acme/app-ui#3", blocked_by=())
        self.assertFalse(fact["ready"] or fact["stacks"])
        self.assertIn("not in this checkout's repo", fact["reason"])

    def test_an_issue_ref_uses_its_single_open_pr_and_is_itself_the_named_blocker(self):
        add_issue(self.fake, 30, state="OPEN")
        self.thing(30)["linked_prs"] = [ref(20)]
        fact = self.stack(line=f"Stack on: {REPO}#30", blocked_by=(30,))
        self.assertTrue(fact["ready"])
        self.assertEqual((fact["pr"], fact["head"]), (f"{REPO}#20", "feat/first"))

    def test_an_issue_ref_with_two_open_prs_or_none_is_not_ready(self):
        add_pr(self.fake, 25)
        add_issue(self.fake, 30)
        self.thing(30)["linked_prs"] = [ref(20), ref(25)]
        self.assertIn("more than one open PR", self.stack(line=f"Stack on: {REPO}#30", blocked_by=(30,))["reason"])
        add_issue(self.fake, 31)
        self.assertIn("no open PR", self.stack(number=6, line=f"Stack on: {REPO}#31", blocked_by=(31,))["reason"])

    def test_a_closed_issue_ref_whose_prs_gh_does_not_list_is_resolved(self):
        add_issue(self.fake, 32, state="CLOSED")  # the fake lists no PR, as when a merged PR is not returned
        fact = self.stack(line=f"Stack on: {REPO}#32", blocked_by=())
        self.assertTrue(fact["ready"])
        self.assertFalse(fact["stacks"])

    def test_an_issue_ref_whose_pr_merged_is_a_normal_issue(self):
        add_pr(self.fake, 27, state="MERGED")
        add_issue(self.fake, 33, state="CLOSED")
        self.thing(33)["linked_prs"] = [ref(27)]
        fact = self.stack(line=f"Stack on: {REPO}#33", blocked_by=())
        self.assertTrue(fact["ready"])
        self.assertFalse(fact["stacks"])

    def test_two_lines_of_any_mix_or_a_malformed_stack_on_line_is_not_ready(self):
        self.assertFalse(self.stack(line=f"Stack on: {REPO}#20\nStack on: {REPO}#21")["ready"])
        self.assertFalse(self.stack(number=6, line=f"Stack on: {REPO}#20\nBased on: {REPO}#20")["ready"])
        self.assertFalse(self.stack(number=7, line="Stack on: #20")["ready"])

    def test_the_other_two_phrasings_work_and_prose_does_not_match(self):
        for phrase in ("Based on", "On top of", "based on", "ON TOP OF"):
            with self.subTest(phrase):
                self.assertTrue(self.stack(number=8, line=f"{phrase}: {REPO}#20")["stacks"])
        add_issue(self.fake, 9, body="Based on feedback from the team, add a pager.\nOn top of that, fix it.")
        self.assertIsNone(self.q.stack_on(9, "octocat"))

    def test_pick_filter_holds_back_issues_that_cannot_stack(self):
        add_pr(self.fake, 21, draft=True, closes=(40,))
        self.stack(number=5)
        self.stack(number=6, line=f"Stack on: {REPO}#21")
        add_issue(self.fake, 7, body="No line.")
        kept, held = self.q.stack_filter(self.q.candidates(), None)
        self.assertEqual(sorted(c.number for c in kept), [5, 7])
        self.assertEqual([(h["number"], h["ref"]) for h in held], [(6, f"{REPO}#21")])
        self.assertIn("draft", held[0]["reason"])

    def test_parse_refs(self):
        self.assertEqual(parse_refs("x\nStack on: a/b#3\n")["refs"], [("a/b", 3)])
        self.assertEqual(parse_refs("stack on: https://github.com/a/b/pull/4")["refs"], [("a/b", 4)])
        self.assertEqual(parse_refs("On top of: a/b#5")["refs"], [("a/b", 5)])
        self.assertEqual(parse_refs("see Stack on: a/b#3 inline")["lines"], 0)
        self.assertEqual(parse_refs("Based on feedback: a/b#3")["lines"], 0)

    def test_lines_in_code_fences_and_html_comments_are_ignored(self):
        fenced = "Example:\n```\nStack on: a/b#3\n```\n~~~\nBased on: a/b#4\n~~~\n"
        self.assertEqual(parse_refs(fenced), {"refs": [], "lines": 0})
        self.assertEqual(parse_refs("<!--\nStack on: a/b#3\n-->\nOn top of: a/b#9"),
                         {"refs": [("a/b", 9)], "lines": 1})
        self.assertEqual(parse_refs("```\nunclosed\nStack on: a/b#3"), {"refs": [], "lines": 0})


class AutoStackTests(QueueTestCase):
    """queue.stacking: auto. With no declared line, an issue whose open blockers all resolve to one ready PR
    stacks on it; any other open blocker holds it."""

    cfg_overrides = {"include_unassigned": True, "stacking": "auto"}

    def setUp(self):
        super().setUp()
        things = self.fake.state["repos"][REPO]["things"]
        for n in ("1", "2", "3"):
            things.pop(n)
        self.blocker(40, 20)

    def blocker(self, number, pr=None, **pr_kwargs):
        """Issue `number`, and when pr is given an open PR that closes it."""
        add_issue(self.fake, number, labels=[], title=f"Earlier work {number}")
        if pr is not None:
            add_pr(self.fake, pr, closes=(number,), **pr_kwargs)
            self.thing(number)["linked_prs"] = [ref(pr)]

    def dependent(self, number=5, blocked_by=(40,), body="Goal.\n", author="octocat"):
        add_issue(self.fake, number, author=author, body=body)
        self.thing(number)["blocked_by"] = list(blocked_by)
        return self.q.stack_on(number, author)

    def test_the_only_blocker_with_an_open_pr_stacks_without_a_line(self):
        fact = self.dependent()
        self.assertTrue(fact["ready"] and fact["stacks"])
        self.assertEqual((fact["ref"], fact["pr"], fact["head"], fact["via"]),
                         (f"{REPO}#40", f"{REPO}#20", "feat/first", "blockers"))
        self.assertIsNone(fact["reason"])

    def test_a_declared_line_still_wins_and_says_so(self):
        fact = self.dependent(body=f"Stack on: {REPO}#20\n")
        self.assertTrue(fact["stacks"])
        self.assertEqual(fact["via"], "declared")

    def test_no_open_blocker_is_a_normal_issue(self):
        self.assertIsNone(self.dependent(blocked_by=()))
        add_issue(self.fake, 41, state="CLOSED")
        self.assertIsNone(self.dependent(number=6, blocked_by=(41,)))

    def test_a_blocker_with_no_open_pr_still_waits(self):
        self.blocker(41)
        fact = self.dependent(blocked_by=(41,))
        self.assertFalse(fact["ready"] or fact["stacks"])
        self.assertIn(f"{REPO}#41 has no open PR", fact["reason"])

    def test_a_draft_fork_or_untrusted_pr_is_refused(self):
        for number, pr, kwargs, words in ((41, 21, {"draft": True}, "draft"), (42, 22, {"fork": True}, "fork"),
                                          (43, 23, {"author": "mallory"}, "not on the roster")):
            with self.subTest(words):
                self.blocker(number, pr, **kwargs)
                fact = self.dependent(number=number + 10, blocked_by=(number,))
                self.assertFalse(fact["ready"] or fact["stacks"])
                self.assertIn(words, fact["reason"])

    def test_two_open_blockers_wait_unless_one_pr_closes_both(self):
        self.blocker(41, 21)
        fact = self.dependent(blocked_by=(40, 41))
        self.assertFalse(fact["ready"])
        self.assertIn("more than one open PR", fact["reason"])
        add_issue(self.fake, 42)
        self.thing(20)["closes"] = [ref(40), ref(42)]
        self.thing(42)["linked_prs"] = [ref(20)]
        fact = self.dependent(number=6, blocked_by=(40, 42))
        self.assertTrue(fact["ready"] and fact["stacks"])
        self.assertEqual(fact["pr"], f"{REPO}#20")

    def test_one_blocker_with_a_pr_and_one_without_waits(self):
        self.blocker(41)
        fact = self.dependent(blocked_by=(40, 41))
        self.assertFalse(fact["ready"])
        self.assertIn(f"{REPO}#41 has no open PR", fact["reason"])

    def test_a_trusted_body_line_names_the_blocker(self):
        for n, line in ((61, f"Blocked by: {REPO}#40"), (62, f"Depends on: https://github.com/{REPO}/issues/40"),
                        (63, f"depends on: {REPO}#20")):
            with self.subTest(line):
                fact = self.dependent(number=n, blocked_by=(), body=f"Goal.\n\n{line}\n")
                self.assertTrue(fact["ready"] and fact["stacks"])
                self.assertEqual(fact["pr"], f"{REPO}#20")

    def test_body_lines_count_only_from_a_trusted_author_and_editor(self):
        self.assertIsNone(self.dependent(blocked_by=(), body=f"Blocked by: {REPO}#40\n", author="mallory"))
        add_issue(self.fake, 6, body=f"Blocked by: {REPO}#40\n")
        self.thing(6)["blocked_by"] = []
        self.thing(6)["editor"] = "mallory"
        fact = self.q.stack_on(6, "octocat")
        self.assertFalse(fact["ready"])
        self.assertIn("last edited by mallory", fact["reason"])

    def test_a_blocker_in_another_repo_waits(self):
        fact = self.dependent(blocked_by=(), body="Blocked by: acme/app-ui#3\n")
        self.assertFalse(fact["ready"])
        self.assertIn("acme/app-ui", fact["reason"])

    def test_unreadable_blockers_give_no_fact(self):
        add_issue(self.fake, 5, body="Goal.\n")
        self.thing(5)["deps_fail"] = True
        self.assertIsNone(self.q.stack_on(5, "octocat"))

    def test_a_declared_line_still_waits_on_a_body_blocker_without_a_pr(self):
        self.blocker(41)
        fact = self.dependent(body=f"Stack on: {REPO}#20\nBlocked by: {REPO}#41\n")
        self.assertFalse(fact["ready"])
        self.assertIn(f"{REPO}#41", fact["reason"])
        fact = self.dependent(number=6, body=f"Stack on: {REPO}#20\nBlocked by: {REPO}#40\n")
        self.assertTrue(fact["ready"])  # the issue the named PR closes is not another blocker

    def test_declared_mode_ignores_blockers_without_a_line(self):
        self.q.queue_cfg["stacking"] = "declared"
        self.assertIsNone(self.dependent())

    def test_pick_filter_holds_back_a_blocker_without_a_pr(self):
        self.blocker(41)
        self.dependent(number=5)
        self.dependent(number=6, blocked_by=(41,))
        kept, held = self.q.stack_filter(self.q.candidates(), None)
        self.assertNotIn(6, [c.number for c in kept])
        self.assertIn(5, [c.number for c in kept])
        self.assertEqual([(h["number"], h["ref"]) for h in held], [(6, f"{REPO}#41")])


class EitherStackTests(EitherTestCase):
    """The either queue shares the stack_on, stack_filter and blocked_by logic of the base queue."""

    cfg_overrides = {**EitherTestCase.cfg_overrides, "stacking": "declared"}

    def test_stack_applies_to_the_either_queue(self):
        add_issue(self.fake, 40, labels=[], title="The earlier work")
        add_pr(self.fake, 20, closes=(40,))
        add_pr(self.fake, 21, draft=True, closes=(40,))
        self.put(5, status=None, labels=["to do"], body=f"Stack on: {REPO}#20\n")
        self.put(6, status="To Do", labels=[], body=f"Based on: {REPO}#21\n")
        for n in (5, 6):
            self.thing(n)["blocked_by"] = [40]
        kept, held = self.q.stack_filter(self.q.candidates(), None)
        self.assertEqual([c.number for c in kept], [5])
        self.assertEqual([(h["number"], h["ref"]) for h in held], [(6, f"{REPO}#21")])
        facts = self.q.facts(kept)
        self.assertEqual((facts[0]["stack_on"]["head"], facts[0]["blocked_by"][0]["number"]), ("feat/first", 40))


if __name__ == "__main__":
    unittest.main()
