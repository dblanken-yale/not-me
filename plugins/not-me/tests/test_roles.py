"""role.md: reviewer roles, pass labels and the every-reviewer approval rule (issue 66)."""
from __future__ import annotations

import json
import unittest

from notme import roles, states
from tests import PLUGIN_ROOT, TempCase
from tests import test_cli_contract as base
from tests.test_cli_contract import REPO, things

LABELS = states.Labels({"needs_review": "needs review", "needs_work": "needs work", "ready_to_merge": "ready to merge",
                        "question": "question", "paused": "paused", "needs_human": "needs human"})
ROSTER = """---
schema: 1
mode: live
tracker: acme/app
operator: octocat
---
# Project

## Roster

| GitHub login | Name | Tier | Assignable | Reviewer |
|--------------|------|------|------------|----------|
| octocat | Octo | lead | yes | yes |
| hubot | Hub | lead | yes | yes |
| monalisa | Mona | member | yes | yes |
| dev | Dev | member | yes | no |
"""
ROLES = """---
schema: 1
---
# Roles

## Roles

| GitHub login | Role | Pass label |
|--------------|------|------------|
| octocat | code | pass code review |
| hubot | functional | pass functional review |
"""


def role_doc(*rows: str) -> str:
    return ("---\nschema: 1\n---\n# Roles\n\n## Roles\n\n| GitHub login | Role | Pass label |\n"
            "|---|---|---|\n" + "".join(f"| {r} |\n" for r in rows))


class ParseTests(unittest.TestCase):
    def test_default_role_doc_has_no_rows(self):
        text = (PLUGIN_ROOT / "defaults" / "role.md").read_text(encoding="utf-8")
        self.assertEqual(roles.parse_roles(text), [])
        self.assertFalse(roles.Roles(roles.parse_roles(text)).configured)

    def test_rows_and_empty_pass_label(self):
        rows = roles.parse_roles(role_doc("@octocat | code | pass code review", "hubot | functional | "))
        self.assertEqual(rows, [roles.RoleRow("octocat", "code", "pass code review"),
                                roles.RoleRow("hubot", "functional", "")])
        current = roles.Roles(rows)
        self.assertEqual(current.pass_label_of("OctoCat"), "pass code review")
        self.assertEqual(current.pass_label_of("hubot"), "")
        self.assertEqual(current.pass_label_of("nobody"), "")

    def test_shared_role_takes_the_named_label(self):
        current = roles.Roles(roles.parse_roles(role_doc("octocat | code | ", "hubot | code | pass code review")))
        self.assertEqual(current.pass_label_of("octocat"), "pass code review")

    def test_errors(self):
        state_labels = {"needs_review": "needs review", "ready_to_merge": "ready to merge"}
        rows = roles.parse_roles(role_doc("octocat | code | pass", "hubot | functional | PASS",
                                          "stranger | code | other", "monalisa | qa | Ready To Merge"))
        errs = roles.errors(rows, ["octocat", "hubot", "monalisa"], state_labels)
        self.assertTrue(any("stranger is not on the roster" in e for e in errs), errs)
        self.assertTrue(any("role code has two pass labels" in e for e in errs), errs)
        self.assertTrue(any("roles code, functional share the pass label 'pass'" in e for e in errs), errs)
        self.assertTrue(any("'ready to merge' is the ready_to_merge state label" in e for e in errs), errs)
        self.assertEqual(roles.errors(roles.parse_roles(ROLES), ["octocat", "hubot"], state_labels), [])


class OutcomeTests(unittest.TestCase):
    """The approval rule on its own: required = roster reviewers minus the author."""

    reviewers = ["octocat", "hubot", "monalisa"]

    def setUp(self):
        self.roles = roles.Roles(roles.parse_roles(ROLES))

    def run_rule(self, reviews, author="dev", usable=None):
        return roles.outcome(reviews, author, self.reviewers, self.roles, usable)

    def test_zero_approvals(self):
        out = self.run_rule([("octocat", "COMMENTED")])
        self.assertFalse(out.all_approved)
        self.assertEqual((out.approved, out.earned), ([], []))
        self.assertEqual(out.waiting, self.reviewers)

    def test_partial_approval_earns_the_role_label(self):
        out = self.run_rule([("octocat", "APPROVED"), ("hubot", "COMMENTED")])
        self.assertFalse(out.all_approved)
        self.assertEqual(out.earned, ["pass code review"])
        self.assertEqual(out.waiting, ["hubot", "monalisa"])

    def test_full_approval(self):
        out = self.run_rule([("octocat", "APPROVED"), ("hubot", "APPROVED"), ("monalisa", "APPROVED")])
        self.assertTrue(out.all_approved)
        self.assertEqual(sorted(out.earned), ["pass code review", "pass functional review"])

    def test_reviewer_without_a_row_still_counts(self):
        out = self.run_rule([("octocat", "APPROVED"), ("hubot", "APPROVED")])
        self.assertFalse(out.all_approved)
        self.assertEqual(out.waiting, ["monalisa"])

    def test_changed_review_loses_the_approval(self):
        out = self.run_rule([("octocat", "APPROVED"), ("octocat", "CHANGES_REQUESTED"), ("octocat", "COMMENTED")])
        self.assertEqual((out.earned, out.approved), ([], []))
        out = self.run_rule([("octocat", "APPROVED"), ("octocat", "DISMISSED")])
        self.assertEqual(out.earned, [])

    def test_shared_role_keeps_its_label_while_one_still_approves(self):
        shared = roles.Roles(roles.parse_roles(role_doc("octocat | code | pass code review",
                                                        "hubot | code | pass code review")))
        out = roles.outcome([("octocat", "APPROVED"), ("hubot", "APPROVED"), ("hubot", "CHANGES_REQUESTED")],
                            "dev", self.reviewers, shared)
        self.assertEqual(out.earned, ["pass code review"])

    def test_author_is_not_required(self):
        out = self.run_rule([("octocat", "APPROVED"), ("hubot", "APPROVED")], author="monalisa")
        self.assertTrue(out.all_approved)

    def test_nobody_required_means_one_approval(self):
        out = roles.outcome([("octocat", "APPROVED")], "hubot", ["hubot"], self.roles)
        self.assertTrue(out.all_approved)
        self.assertFalse(roles.outcome([], "hubot", ["hubot"], self.roles).all_approved)

    def test_missing_labels_are_not_managed(self):
        out = self.run_rule([("octocat", "APPROVED"), ("hubot", "APPROVED")], usable=["pass functional review"])
        self.assertEqual(out.earned, ["pass functional review"])
        self.assertEqual(out.managed, ["pass functional review"])

    def test_managed_labels_keep_the_repo_spelling(self):
        out = self.run_rule([("octocat", "APPROVED")], usable=["Pass Code Review"])
        self.assertEqual(out.earned, ["Pass Code Review"])


class StatesTests(unittest.TestCase):
    def test_pass_labels_are_additive_and_never_the_state(self):
        current = ["needs review", "pass code review", "bug"]
        self.assertEqual(LABELS.current(current, "pr"), ["needs_review"])
        self.assertEqual(states.transition(current, "needs_review", "ready_to_merge", LABELS, "pr"),
                         ["pass code review", "bug", "ready to merge"])
        with self.assertRaises(states.IllegalTransition):
            states.transition(current, "needs_work", "needs_review", LABELS, "pr")

    def test_set_pass_labels(self):
        managed = ["pass code review", "pass functional review"]
        self.assertEqual(states.set_pass_labels(["needs review", "pass code review", "bug"],
                                                ["pass functional review"], managed),
                         ["needs review", "bug", "pass functional review"])

    def test_swap_state_keeps_pass_and_additive_labels(self):
        self.assertEqual(states.swap_state(["needs review", "paused", "pass x"], "ready_to_merge", LABELS, "pr"),
                         ["paused", "pass x", "ready to merge"])
        self.assertEqual(states.swap_state(["needs review"], "needs_review", LABELS, "pr"), ["needs review"])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tc = TempCase()
        self.addCleanup(self.tc.cleanup)
        self.tc.write("global", "project", ROSTER)

    def test_no_roles_by_default(self):
        cfg = self.tc.load()
        self.assertFalse(cfg.roles().configured)
        self.assertEqual(cfg.explain()["roles"], {"configured": False, "layer": "default", "reviewers": [],
                                                  "ignored": [], "errors": []})

    def test_repo_layer_may_add_role_rows_with_extends(self):
        self.tc.write("global", "role", role_doc("octocat | code | pass code review"))
        self.tc.write("repo", "role", "---\nextends: global\n---\n## Roles\n\n| GitHub login | Role | Pass label |\n"
                                      "|---|---|---|\n| hubot | functional | pass functional review |\n")
        cfg = self.tc.load()
        self.assertEqual(cfg.roles().describe(),
                         [{"login": "octocat", "role": "code", "pass_label": "pass code review"},
                          {"login": "hubot", "role": "functional", "pass_label": "pass functional review"}])
        self.assertEqual(cfg.check(), [])

    def test_repo_layer_may_set_roles_on_its_own(self):
        self.tc.write("repo", "role", ROLES)
        self.assertTrue(self.tc.load().roles().configured)

    def test_repo_role_doc_with_an_empty_table_keeps_the_global_rows(self):
        self.tc.write("global", "role", ROLES)
        self.tc.write("repo", "role", role_doc())
        cfg = self.tc.load()
        self.assertTrue(cfg.roles().configured)
        self.assertEqual([r["login"] for r in cfg.roles().describe()], ["octocat", "hubot"])

    def test_repo_row_cannot_change_or_blank_a_trusted_row(self):
        self.tc.write("global", "role", ROLES)
        self.tc.write("repo", "role", role_doc("octocat | code | ", "hubot | functional | pass functional review",
                                               "monalisa | qa | pass qa"))
        cfg = self.tc.load()
        self.assertEqual(cfg.roles().pass_label_of("octocat"), "pass code review")
        self.assertEqual(cfg.roles().pass_label_of("monalisa"), "pass qa")
        ignored = cfg.explain()["roles"]["ignored"]
        self.assertEqual([i["login"] for i in ignored], ["octocat"])
        self.assertTrue(any("row for octocat is ignored" in w for w in cfg.warnings), cfg.warnings)

    def test_repo_roster_cannot_drop_a_trusted_reviewer_but_may_add_one(self):
        self.tc.write("repo", "project", "---\nschema: 1\n---\n# Project\n\n## Roster\n\n"
                                         "| GitHub login | Name | Tier | Assignable | Reviewer |\n|---|---|---|---|---|\n"
                                         "| hubot | Hub | lead | yes | no |\n| newbie | New | member | yes | yes |\n")
        reviewers = self.tc.load().gate_reviewers()
        self.assertEqual(sorted(reviewers), ["hubot", "monalisa", "newbie", "octocat"])

    def test_changes_label_may_not_be_ready_to_merge(self):
        self.tc.write("repo", "review", "---\nschema: 1\nchanges_label: ready_to_merge\n---\n# Review\n")
        errs = self.tc.load().check()
        self.assertTrue(any("review.changes_label must not be ready_to_merge" in e and e.startswith("repo ")
                            for e in errs), errs)

    def test_errors_reach_check_and_explain_with_the_file(self):
        self.tc.write("repo", "role", role_doc("stranger | code | needs review"))
        cfg = self.tc.load()
        errs = cfg.check()
        self.assertTrue(any(e.startswith("repo .claude/not-me/role.md: stranger is not on the roster") for e in errs),
                        errs)
        self.assertTrue(any("is the needs_review state label" in e for e in errs), errs)
        self.assertEqual(cfg.explain()["roles"]["errors"], errs)

    def test_row_for_a_non_reviewer_warns(self):
        self.tc.write("repo", "role", role_doc("dev | code | pass code review"))
        self.assertTrue(any("dev is on the roster but not a reviewer" in w for w in self.tc.load().warnings))


class ReviewPostCliTests(base.CliCase):
    """notme review-post --labels-auto over the fake GitHub. PR 3 is by `dev`; octocat (the operator) posts."""

    global_project = ROSTER

    def setUp(self):
        super().setUp()
        self.tc.write("global", "review", "---\nschema: 1\nauto_approve: true\n---\n# Review\n")
        things(self.fake)["3"]["author"] = "dev"
        self.defs = self.fake.state["repos"][REPO].setdefault("label_defs", {})
        for name in ("needs review", "ready to merge", "needs work", "pass code review", "pass functional review"):
            self.defs[name] = {"color": "000000", "description": ""}
        self.body = self.write_file("body.md", "Reviewed.\n")

    def roles_on(self, text=ROLES):
        self.tc.write("repo", "role", text)

    def others(self, *reviews):
        things(self.fake)["3"]["reviews"].extend({"id": 900 + i, "user": u, "state": s, "body": ""}
                                                  for i, (u, s) in enumerate(reviews))

    def post(self, event="APPROVE", *extra):
        return self.ok("review-post", "3", "--event", event, "--body-file", self.body, "--labels-auto", *extra)

    def labels(self):
        return things(self.fake)["3"]["labels"]

    def test_no_roles_one_approval_is_ready_to_merge(self):
        out = self.post()
        self.assertEqual(self.labels(), ["ready to merge"])
        self.assertIsNone(out["approval"])
        self.assertEqual(out["labels"], ["ready to merge"])

    def test_no_roles_request_changes(self):
        self.post("REQUEST_CHANGES")
        self.assertEqual(self.labels(), ["needs work"])

    def test_partial_approval_keeps_needs_review_and_adds_the_pass_label(self):
        self.roles_on()
        out = self.post()
        self.assertEqual(self.labels(), ["needs review", "pass code review"])
        self.assertFalse(out["approval"]["all_approved"])
        self.assertEqual(out["approval"]["waiting"], ["hubot", "monalisa"])

    def test_full_approval_moves_to_ready_to_merge(self):
        self.roles_on()
        self.others(("hubot", "APPROVED"), ("monalisa", "APPROVED"))
        out = self.post()
        self.assertEqual(sorted(self.labels()), ["pass code review", "pass functional review", "ready to merge"])
        self.assertTrue(out["approval"]["all_approved"])

    def test_zero_approvals_comment_changes_nothing_but_pass_labels(self):
        self.roles_on()
        things(self.fake)["3"]["labels"] = ["needs review", "pass functional review"]
        self.post("COMMENT")
        self.assertEqual(self.labels(), ["needs review"])  # hubot never approved

    def test_changed_review_removes_the_pass_label(self):
        self.roles_on()
        self.post()
        self.assertIn("pass code review", self.labels())
        self.post("REQUEST_CHANGES")
        self.assertEqual(self.labels(), ["needs work"])

    def test_missing_pass_label_warns_once_and_is_skipped(self):
        self.roles_on()
        del self.defs["pass code review"]
        out = self.post()
        self.assertEqual(self.labels(), ["needs review"])
        self.assertEqual(len([w for w in out["warnings"] if "pass code review" in w]), 1)

    def test_role_without_pass_label_still_counts(self):
        self.roles_on(role_doc("octocat | code | ", "hubot | functional | "))
        self.others(("hubot", "APPROVED"), ("monalisa", "APPROVED"))
        self.post()
        self.assertEqual(self.labels(), ["ready to merge"])

    def test_dry_run_counts_the_review_it_would_post(self):
        self.roles_on()
        self.others(("hubot", "APPROVED"), ("monalisa", "APPROVED"))
        out = self.post("APPROVE", "--dry-run")
        self.assertTrue(out["dry_run"])
        self.assertTrue(out["approval"]["all_approved"])
        self.assertIn("ready to merge", out["labels"])
        self.assertEqual(self.labels(), ["needs review"])

    def test_labels_set_and_labels_auto_are_exclusive(self):
        code, payload, _ = self.cli("review-post", "3", "--event", "COMMENT", "--body-file", self.body,
                                    "--labels-auto", "--labels-set", "x")
        self.assertEqual((code, payload["kind"]), (2, "usage"))

    def test_approve_still_needs_auto_approve(self):
        self.tc.write("global", "review", "---\nschema: 1\n---\n# Review\n")
        code, payload, _ = self.cli("review-post", "3", "--event", "APPROVE", "--body-file", self.body,
                                    "--labels-auto")
        self.assertEqual((code, payload["kind"]), (1, "approve_refused"))

    def test_labels_ensure_and_doctor_cover_pass_labels(self):
        self.roles_on()
        del self.defs["pass functional review"]
        doctor = self.ok("doctor")
        check = next(c for c in doctor["checks"] if c["name"] == "pass labels")
        self.assertFalse(check["ok"] or check["required"])
        self.assertIn("pass functional review", check["detail"])
        self.assertIn("pass functional review", self.ok("labels-ensure", "--check")["missing"])
        self.ok("labels-ensure")
        self.assertIn("pass functional review", self.defs)

    def test_non_final_approve_leaves_needs_work_alone(self):
        self.roles_on()
        things(self.fake)["3"]["labels"] = ["needs work"]
        self.post()
        self.assertEqual(self.labels(), ["needs work", "pass code review"])

    def test_non_final_approve_leaves_ready_to_merge_alone(self):
        self.roles_on()
        things(self.fake)["3"]["labels"] = ["ready to merge"]
        self.post()
        self.assertEqual(self.labels(), ["ready to merge", "pass code review"])

    def test_final_approve_from_a_state_without_the_edge_warns_and_keeps_the_state(self):
        self.roles_on()
        things(self.fake)["3"]["labels"] = ["needs work"]
        self.others(("hubot", "APPROVED"), ("monalisa", "APPROVED"))
        out = self.post()
        self.assertEqual(sorted(self.labels()), ["needs work", "pass code review", "pass functional review"])
        self.assertEqual(len([w for w in out["warnings"] if "cannot move to ready_to_merge" in w]), 1)

    def test_pass_labels_use_the_repo_spelling(self):
        self.roles_on()
        del self.defs["pass code review"]
        self.defs["Pass Code Review"] = {"color": "000000", "description": ""}
        out = self.post()
        self.assertEqual(self.labels(), ["needs review", "Pass Code Review"])
        self.assertEqual(out["approval"]["pass_labels"], ["Pass Code Review"])
        self.assertNotIn("warnings", out)

    def test_changes_label_ready_to_merge_is_refused_before_posting(self):
        self.tc.write("repo", "review", "---\nschema: 1\nchanges_label: ready_to_merge\n---\n# Review\n")
        code, payload, _ = self.cli("review-post", "3", "--event", "REQUEST_CHANGES", "--body-file", self.body,
                                    "--labels-auto")
        self.assertEqual((code, payload["kind"]), (4, "config_invalid"))
        self.assertEqual(things(self.fake)["3"]["reviews"], [])

    def test_pass_labels_go_on_the_code_repo_when_the_tracker_differs(self):
        self.tc.write("global", "project", ROSTER.replace("tracker: acme/app",
                                                          "tracker: acme/other\nallow_repos: [acme/app]"))
        self.roles_on()
        del self.defs["pass functional review"]
        check = self.ok("labels-ensure", "--check")
        self.assertEqual(check["repo"], "acme/other")
        self.assertNotIn("pass functional review", check["missing"])
        self.assertEqual(check["pass_labels"]["repo"], REPO)
        self.assertEqual(check["pass_labels"]["missing"], ["pass functional review"])
        self.assertFalse(check["ok"])
        doctor = self.ok("doctor")
        self.assertIn(REPO, next(c for c in doctor["checks"] if c["name"] == "pass labels")["detail"])
        self.ok("labels-ensure")
        self.assertIn("pass functional review", self.defs)

    def test_config_explain_and_check_report_role_errors(self):
        self.roles_on(role_doc("stranger | code | pass"))
        explain = self.ok("config", "--explain")
        self.assertTrue(explain["roles"]["errors"])
        code, payload, _ = self.cli("config", "--check")
        self.assertEqual(code, 4)
        self.assertTrue(any("stranger" in e for e in payload["errors"]), json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
