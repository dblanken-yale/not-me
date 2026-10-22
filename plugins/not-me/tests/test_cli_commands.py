"""Behaviour of individual notme subcommands, over a fake GitHub and (for git) a real scratch repository."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from notme import claims
from notme import feedback as feedback_mod
from notme import gh as gh_mod
from notme import git as git_mod
from tests import PLUGIN_ROOT, TempCase
from tests import test_cli_contract as base
from tests.test_cli_contract import REPO, add_pr_state, things

SHA = "a" * 40


class ConfigTests(base.CliCase):
    def test_explain_with_no_project_has_defaults_and_no_absolute_paths(self):
        code, payload, _ = self.cli("config", "--explain", "--project-dir", "/nonexistent", project=False, repo=False)
        self.assertEqual(code, 0)
        self.assertIn("project.labels.todo", payload["keys"])
        self.assertIsNone(payload["layers"]["repo"])
        self.assertNotRegex(json.dumps(payload), r'"/[A-Za-z]|[A-Za-z]:\\\\')

    def test_explain_shows_no_absolute_paths_with_layers_present(self):
        self.tc.write("repo", "project", "---\nschema: 1\nlimits: {fix_loop: 5}\n---\n# Project\n")
        payload = self.ok("config", "--explain")
        text = json.dumps(payload)
        self.assertNotIn(str(self.tc.root), text)
        self.assertNotRegex(text, r'"/[A-Za-z]|[A-Za-z]:\\\\')
        self.assertEqual(payload["keys"]["project.limits.fix_loop"]["source"]["layer"], "repo")

    def test_layer_option_limits_the_output(self):
        payload = self.ok("config", "--explain", "--layer", "global")
        self.assertTrue(payload["keys"])
        self.assertTrue(all(v["source"]["layer"] == "global" for v in payload["keys"].values()))
        raw = self.ok("config", "--layer", "global")
        self.assertEqual(raw["config"]["project"]["tracker"], REPO)

    def test_plain_config_has_roster(self):
        payload = self.ok("config")
        self.assertEqual(payload["roster"]["leads"], ["octocat", "hubot"])
        self.assertEqual(payload["config"]["project"]["mode"], "live")

    def test_me_reads_config_without_calling_gh_and_gh_when_auto(self):
        self.assertEqual(self.ok("me"), {"operator": "octocat", "source": "config"})
        self.assertEqual(self.fake.calls, [])
        self.tc.write("global", "project", base.GLOBAL_PROJECT.replace("operator: octocat", "operator: auto"))
        self.assertEqual(self.ok("me"), {"operator": "octocat", "source": "gh"})

    def test_config_commands_never_call_gh(self):
        self.ok("config")
        self.ok("config", "--explain")
        self.ok("pause", "on")
        self.assertEqual(self.fake.calls, [])


class IssueTests(base.CliCase):
    def test_pick_then_claim_then_release(self):
        picked = self.ok("pick-issue")
        self.assertEqual(picked["number"], 1)
        self.assertTrue(self.ok("pick-issue", "--exclude", "1").get("none"))
        claim = self.ok("claim-issue", "1")
        self.assertEqual((claim["previous"], claim["now"]), ("todo", "in_progress"))
        issue = things(self.fake)["1"]
        self.assertEqual(issue["labels"], ["in progress"])
        self.assertEqual(issue["assignees"], ["octocat"])
        self.assertEqual(self.ok("release-issue", "1", "--to", "blocked")["to"], "blocked")
        self.assertEqual(things(self.fake)["1"]["labels"], ["blocked"])
        self.assertEqual(things(self.fake)["1"]["assignees"], [])

    def test_move_issue_checks_the_required_state(self):
        code, payload, _ = self.cli("move-issue", "1", "--to", "in_progress", "--require-current", "blocked")
        self.assertEqual((code, payload["kind"]), (1, "state_mismatch"))
        self.ok("move-issue", "1", "--to", "in_progress", "--require-current", "todo")
        self.assertEqual(things(self.fake)["1"]["labels"], ["in progress"])

    def test_transition_follows_edges(self):
        code, payload, _ = self.cli("transition", "1", "--from", "todo", "--to", "ready_to_merge")
        self.assertEqual((code, payload["kind"]), (1, "illegal_transition"))
        payload = self.ok("transition", "1", "--from", "todo", "--to", "in_progress")
        self.assertEqual(payload["labels"], ["in progress"])
        pr = self.ok("transition", "3", "--kind", "pr", "--from", "needs_review", "--to", "needs_work")
        self.assertEqual(pr["labels"], ["needs work"])

    def test_issue_withholds_text_from_untrusted_authors(self):
        self.assertEqual(self.ok("issue", "1")["body"], "Body of First issue")
        things(self.fake)["1"]["author"] = "mallory"
        payload = self.ok("issue", "1")
        self.assertFalse(payload["author_trusted"])
        self.assertIsNone(payload["body"])
        self.assertIsNone(payload["title"])

    def test_labels_sync_add_remove_and_check(self):
        payload = self.ok("labels-sync", "1", "--add", "extra", "--remove", "to do", "--check")
        self.assertFalse(payload["in_sync"])
        self.assertEqual(things(self.fake)["1"]["labels"], ["to do"])
        payload = self.ok("labels-sync", "1", "--add", "extra", "--remove", "to do")
        self.assertEqual(things(self.fake)["1"]["labels"], ["extra"])
        self.assertTrue(payload["changed"])
        self.assertTrue(self.ok("labels-sync", "1", "--set", "extra")["in_sync"])


class LabelsEnsureTests(base.CliCase):
    def test_creates_only_the_missing_labels(self):
        defs = self.fake.state["repos"][REPO].setdefault("label_defs", {})
        for name in ("to do", "epic", "paused"):
            defs[name] = {"color": "000000", "description": "mine"}
        payload = self.ok("labels-ensure")
        self.assertEqual(len(payload["created"]), 8)
        self.assertEqual(sorted(payload["present"]), ["epic", "paused", "to do"])
        self.assertEqual(len(defs), 11)
        self.assertEqual(defs["to do"]["color"], "000000")  # untouched
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{6}", d["color"]) and d["description"] for d in defs.values()))
        again = self.ok("labels-ensure")
        self.assertEqual(again["created"], [])
        self.assertTrue(again["ok"])

    def test_check_reports_without_creating(self):
        payload = self.ok("labels-ensure", "--check")
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["missing"]), 11)
        self.assertEqual(self.fake.writes_made(), [])


class PickReviewsTests(base.CliCase):
    """The filtering table: one PR per case, the verdict is the pick or the skip reason."""

    def review(self, pr, commit=SHA, at="2026-01-01T00:00:00Z"):
        pr["reviews"].append({"id": 1, "event": "COMMENT", "body": "x", "user": "octocat", "commit_id": commit,
                              "submitted_at": at})

    def test_table(self):
        def comment_after(fake, pr):
            fake.post_comment(REPO, 3, "hubot", "pushed a fix", created_at="2026-01-02T00:00:00Z")

        def comment_before(fake, pr):
            fake.post_comment(REPO, 3, "hubot", "earlier", created_at="2025-12-31T00:00:00Z")

        cases = [
            ("never reviewed", {}, None, ("pick", "never reviewed")),
            ("not requested", {"reviewRequests": []}, None, ("skip", "operator is not a requested reviewer")),
            ("untrusted author", {"author": "mallory"}, None, ("skip", "author not trusted")),
            ("own PR", {"author": "octocat"}, None, ("skip", "operator is the author")),
            ("draft", {"isDraft": True}, None, ("skip", "draft")),
            ("unchanged", {}, lambda f, p: self.review(p), ("skip", "unchanged since the last review")),
            ("new commits", {"headRefOid": "c" * 40}, lambda f, p: self.review(p),
             ("pick", "new commits since the last review")),
            ("author commented", {}, lambda f, p: (self.review(p), comment_after(f, p)),
             ("pick", "author commented since the last review")),
            ("comment before the review", {}, lambda f, p: (self.review(p), comment_before(f, p)),
             ("skip", "unchanged since the last review")),
            ("not labeled", {"labels": ["needs work"]}, None, ("absent", None)),
        ]
        for name, overrides, setup, expected in cases:
            with self.subTest(name):
                self.fake.state = type(self.fake)().state  # fresh GitHub for each case
                pr = add_pr_state(self.fake, **overrides)
                if setup:
                    setup(self.fake, pr)
                payload = self.ok("pick-reviews")
                kind, why = expected
                if kind == "pick":
                    self.assertEqual((payload["number"], payload["why"]), (3, why), payload)
                elif kind == "skip":
                    self.assertTrue(payload["none"])
                    self.assertEqual(payload["skipped"], [{"number": 3, "reason": why}])
                else:
                    self.assertEqual(payload, {"none": True, "skipped": []})

    def test_lowest_number_first_with_others_listed(self):
        add_pr_state(self.fake)
        second = dict(things(self.fake)["3"], number=7, title="Second", reviews=[], comments=[], threads=[])
        things(self.fake)["7"] = second
        payload = self.ok("pick-reviews")
        self.assertEqual((payload["number"], payload["others"]), (3, [7]))


class ProveTargetTests(base.CliCase):
    def setUp(self):
        super().setUp()
        add_pr_state(self.fake)

    def prove(self, strategy, template=None):
        t = f"\"{template}\"" if template else "null"
        self.tc.write("global", "prove", "---\nschema: 1\ntarget: {strategy: [%s], url_template: %s, "
                      "bot_comment_regex: null}\n---\n# Prove\n" % (", ".join(strategy), t))

    def deployment(self, url="https://deploy-3--app.netlify.app/", state="success"):
        self.fake.state.setdefault("deployments", []).append(
            {"repo": REPO, "sha": SHA, "id": 11, "statuses": [{"state": state, "environment_url": url}]})

    def test_deployment_wins_by_default(self):
        self.deployment()
        self.fake.post_comment(REPO, 3, "netlify[bot]", "Preview: https://deploy-preview-3--app.netlify.app/")
        payload = self.ok("prove-target", "3")
        self.assertEqual((payload["strategy"], payload["url"]), ("deployments", "https://deploy-3--app.netlify.app/"))

    def test_bot_comment_when_no_deployment(self):
        self.tc.write("global", "prove", "---\nschema: 1\ntarget: {bots: [\"netlify[bot]\"]}\n---\n# Prove\n")
        self.fake.post_comment(REPO, 3, "netlify[bot]", "Preview: https://deploy-preview-3--app.netlify.app/ ok")
        payload = self.ok("prove-target", "3")
        self.assertEqual(payload["strategy"], "bot-comment")
        self.assertEqual(payload["url"], "https://deploy-preview-3--app.netlify.app/")
        self.assertEqual(payload["tried"], ["deployments", "bot-comment"])

    def test_comment_from_a_stranger_is_ignored(self):
        self.fake.post_comment(REPO, 3, "mallory", "Preview: https://evil.vercel.app/")
        self.prove(["deployments", "bot-comment", "url-template"], "https://pr-{pr}.example.test/{owner}/{name}")
        payload = self.ok("prove-target", "3")
        self.assertEqual((payload["strategy"], payload["url"]), ("url-template", "https://pr-3.example.test/acme/app"))

    def test_failed_deployment_status_is_skipped(self):
        self.deployment(state="failure")
        self.assertTrue(self.ok("prove-target", "3")["none"])

    def test_strategy_order_is_the_configured_order(self):
        self.deployment()
        self.prove(["url-template", "deployments"], "https://pr-{pr}.example.test/")
        payload = self.ok("prove-target", "3")
        self.assertEqual((payload["strategy"], payload["url"]), ("url-template", "https://pr-3.example.test/"))
        self.prove(["deployments", "url-template"], "https://pr-{pr}.example.test/")
        self.assertEqual(self.ok("prove-target", "3")["strategy"], "deployments")

    def test_none_when_nothing_matches(self):
        self.assertEqual(self.ok("prove-target", "3"), {"none": True, "tried": ["deployments", "bot-comment",
                                                                              "url-template", "local"]})


class StatusAndPauseTests(base.CliCase):
    def test_status_shape(self):
        things(self.fake)["2"]["labels"] = ["needs human"]
        things(self.fake)["3"]["labels"] = ["question", "paused"]
        payload = self.ok("status")
        self.assertEqual(set(payload), {"enabled", "paused", "mode", "repo", "tracker", "operator", "active_run",
                                        "items", "stale_claims", "last_runs", "warnings"})
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["paused"], {"global": False, "project": False})
        self.assertEqual(payload["operator"], "octocat")
        got = {(i["kind"], i["number"]): i["states"] for i in payload["items"]}
        self.assertEqual(got, {("issue", 2): ["needs_human"], ("pr", 3): ["question", "paused"]})
        self.assertEqual(self.fake.writes_made(), [])

    def test_status_reports_stale_claims_and_last_runs(self):
        issue = things(self.fake)["1"]
        issue.update({"labels": ["in progress"], "assignees": ["octocat"]})
        self.fake.post_comment(REPO, 1, "octocat", claims.marker_body("octocat", "r", "2020-01-01T00:00:00Z"))
        started = self.ok("log", "start", "--skill", "new", "--ref", "1")
        self.ok("log", "finish", started["path"], "--status", "done")
        payload = self.ok("status")
        self.assertEqual([c["number"] for c in payload["stale_claims"]], [1])
        self.assertEqual(payload["last_runs"]["new"]["status"], "done")
        self.assertIsNone(payload["active_run"])

    def test_status_survives_unreadable_github(self):
        def broken(argv):
            raise FileNotFoundError(2, "no", "gh")

        code, payload, _ = self.cli("status", runner=broken)
        self.assertEqual(code, 0)
        self.assertEqual(payload["items"], [])
        self.assertTrue(any("not read" in w for w in payload["warnings"]))

    def test_pause_on_and_off(self):
        self.assertEqual(self.ok("pause", "on"), {"paused": True, "scope": "project"})
        self.assertEqual(self.ok("status")["paused"]["project"], True)
        self.assertEqual(self.ok("pick-issue"), {"none": True, "reason": "paused"})
        code, payload, _ = self.cli("claim-issue", "1")
        self.assertEqual((code, payload["kind"]), (1, "paused"))
        self.assertEqual(self.ok("pause", "off"), {"paused": False, "scope": "project"})
        self.assertEqual(self.ok("pick-issue")["number"], 1)

    def test_global_pause_writes_the_home_file(self):
        self.ok("pause", "on", "--global")
        self.assertTrue((self.tc.home / "PAUSED").is_file())
        self.assertEqual(self.ok("status")["paused"]["global"], True)
        self.ok("pause", "off", "--global")
        self.assertFalse((self.tc.home / "PAUSED").exists())

    def test_disabled_config_stops_picking(self):
        self.tc.write("global", "project", base.GLOBAL_PROJECT.replace("mode: live", "mode: live\nenabled: false"))
        self.assertEqual(self.ok("pick-issue"), {"none": True, "reason": "disabled"})
        self.assertFalse(self.ok("status")["enabled"])


class RunLogTests(base.CliCase):
    def test_start_action_question_finish_last(self):
        started = self.ok("log", "start", "--skill", "update", "--ref", "PR 3")
        path = started["path"]
        again, payload, _ = self.cli("log", "start", "--skill", "update", "--ref", "PR 4")
        self.assertEqual((again, payload["kind"]), (1, "active_run"))
        self.assertEqual(self.ok("status")["active_run"]["run_id"], started["run_id"])
        msg = self.write_file("msg.txt", "pushed the branch\n")
        self.ok("log", "action", path, "--message-file", msg, "--kind", "ACTION")
        q = self.write_file("q.json", json.dumps({"question": "Which?", "decision": "A", "undo": "use B"}))
        self.ok("log", "question", path, "--file", q)
        fails = self.write_file("f.txt", "tests red\n")
        finished = self.ok("log", "finish", path, "--status", "needs work", "--failures-file", fails)
        self.assertIn("tests red", finished["summary"])
        last = self.ok("log", "last", "--skill", "update")
        self.assertEqual((last["status"], last["entries"]), ("needs work", 2))
        self.assertIsNone(self.ok("status")["active_run"])

    def test_log_path_outside_the_log_root_is_refused(self):
        stray = self.write_file("stray.md", "# not a log\n")
        msg = self.write_file("msg.txt", "x\n")
        code, payload, _ = self.cli("log", "action", stray, "--message-file", msg)
        self.assertEqual((code, payload["kind"]), (2, "usage"))
        self.assertEqual(Path(stray).read_text(), "# not a log\n")


class AllowRepoTests(base.CliCase):
    def test_add_list_remove(self):
        self.assertTrue(self.ok("allow-repo", "add", "acme/extra")["added"])
        self.assertFalse(self.ok("allow-repo", "add", "acme/extra")["added"])
        self.assertTrue(self.ok("allow-repo", "add", "acme/wide", "--everywhere")["added"])
        entries = self.ok("allow-repo", "list")["entries"]
        self.assertEqual({(e["scope"], e["repo"]) for e in entries}, {(REPO, "acme/extra"), ("*", "acme/wide")})
        self.assertTrue(self.ok("allow-repo", "remove", "acme/extra")["removed"])
        self.assertEqual([e["repo"] for e in self.ok("allow-repo", "list")["entries"]], ["acme/wide"])

    def test_board_entries_and_bad_input(self):
        self.assertTrue(self.ok("allow-repo", "add", "--board", "acme/6")["added"])
        self.assertEqual(self.ok("allow-repo", "list")["entries"][0]["board"], "acme/6")
        code, payload, _ = self.cli("allow-repo", "add", "not a slug")
        self.assertEqual(code, 2)
        code, _, _ = self.cli("allow-repo", "add")
        self.assertEqual(code, 2)

    def test_allowing_a_repo_unblocks_the_write(self):
        self.tc.write("global", "project", base.NO_OTHERS_PROJECT)
        self.assertEqual(self.cli("request-review", "acme/other#1", "--reviewers", "hubot")[0], 6)
        self.ok("allow-repo", "add", "acme/other", "--everywhere")
        things(self.fake, "acme/other")["1"] = dict(things(self.fake)["3"], number=1)
        self.ok("request-review", "acme/other#1", "--reviewers", "hubot")


class GuardCheckTests(base.CliCase):
    def event(self, command):
        return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(self.project),
                           "tool_input": {"command": command}})

    def test_allow_and_block_decisions(self):
        (self.project / ".claude" / "not-me").mkdir(parents=True)
        allow = self.ok("guard", "check", stdin=self.event("ls -la"))
        self.assertEqual(allow, {"decision": "allow", "rule": None, "message": ""})
        block = self.ok("guard", "check", stdin=self.event("gh pr comment 3 -R evil/repo --body hi"))
        self.assertEqual(block["decision"], "block")
        self.assertEqual(block["rule"], "other_repo")
        self.assertIn("evil/repo", block["message"])

    def test_bad_stdin_is_usage(self):
        code, payload, _ = self.cli("guard", "check", stdin="not json")
        self.assertEqual((code, payload["kind"]), (2, "usage"))


class PullRequestTests(base.CliCase):
    def setUp(self):
        super().setUp()
        add_pr_state(self.fake)

    def test_pick_needs_work(self):
        pr = things(self.fake)["3"]
        pr.update({"labels": ["needs work"], "assignees": ["octocat"], "author": "octocat"})
        payload = self.ok("pick-needs-work")
        self.assertEqual((payload["number"], payload["head_ref"]), (3, "feat/widget"))
        pr["labels"] = ["needs work", "needs human"]
        self.assertEqual(self.ok("pick-needs-work"), {"none": True})

    def add_pr(self, number, **fields):
        pr = dict(things(self.fake)["3"], number=number, title=f"PR {number}", comments=[], threads=[], reviews=[],
                  labels=[], assignees=[], reviewRequests=[], headRefName=f"feat/p{number}", baseRefName="main")
        pr.update(fields)
        things(self.fake)[str(number)] = pr
        return pr

    def test_pick_needs_work_exclude_skips_numbers(self):
        things(self.fake)["3"].update({"labels": ["needs work"], "assignees": ["octocat"], "author": "octocat"})
        self.add_pr(5, labels=["needs work"], assignees=["octocat"], author="octocat")
        self.assertEqual(self.ok("pick-needs-work")["others"], [5])
        payload = self.ok("pick-needs-work", "--exclude", "3")
        self.assertEqual((payload["number"], payload["others"]), (5, []))
        self.assertEqual(self.ok("pick-needs-work", "--exclude", "3", "--exclude", "5"), {"none": True})

    def test_pick_needs_work_include_answered(self):
        self.add_pr(5, labels=["question"], assignees=["octocat"], author="octocat")
        self.assertEqual(self.ok("pick-needs-work", "--include-answered"), {"none": True})  # nobody answered
        self.fake.post_comment(REPO, 5, "octocat", "Question: A or B?", created_at="2026-01-01T00:00:00Z")
        self.assertEqual(self.ok("pick-needs-work", "--include-answered"), {"none": True})  # the operator's own
        self.fake.post_comment(REPO, 5, "octocat", feedback_mod.MARKER_UPDATE + "\nsummary",
                               created_at="2026-01-01T00:00:00Z")
        self.fake.post_comment(REPO, 5, "mallory", "use the other one", created_at="2026-01-03T00:00:00Z")
        self.assertEqual(self.ok("pick-needs-work", "--include-answered"), {"none": True})  # untrusted answer
        self.fake.post_comment(REPO, 5, "hubot", "Go with option B.", created_at="2026-01-02T00:00:00Z")
        payload = self.ok("pick-needs-work", "--include-answered")
        self.assertEqual((payload["number"], payload["answered"]), (5, True))
        self.assertEqual(self.ok("pick-needs-work"), {"none": True})  # off by default
        self.assertEqual(self.ok("pick-needs-work", "--include-answered", "--exclude", "5"), {"none": True})

    def test_pick_needs_work_answered_must_be_newer_than_the_marker(self):
        self.add_pr(5, labels=["question"], assignees=["octocat"], author="octocat")
        self.fake.post_comment(REPO, 5, "hubot", "Option B.", created_at="2026-01-01T00:00:00Z")
        self.fake.post_comment(REPO, 5, "octocat", feedback_mod.MARKER_UPDATE + "\nsummary",
                               created_at="2026-01-02T00:00:00Z")
        self.assertEqual(self.ok("pick-needs-work", "--include-answered"), {"none": True})

    def test_pick_needs_work_include_answered_orders_by_number_and_flags_only_answered(self):
        things(self.fake)["3"].update({"labels": ["needs work"], "assignees": ["octocat"], "author": "octocat"})
        self.add_pr(2, labels=["question"], assignees=["octocat"], author="octocat")
        self.fake.post_comment(REPO, 2, "hubot", "Do it.", created_at="2026-01-02T00:00:00Z")
        payload = self.ok("pick-needs-work", "--include-answered")
        self.assertEqual((payload["number"], payload["answered"], payload["others"]), (2, True, [3]))
        payload = self.ok("pick-needs-work", "--include-answered", "--exclude", "2")
        self.assertEqual(payload["number"], 3)
        self.assertNotIn("answered", payload)

    def test_pr_children_lists_stacked_prs_and_skips_untrusted_and_forks(self):
        self.add_pr(5, baseRefName="feat/widget", author="hubot")
        self.add_pr(6, baseRefName="feat/widget", author="mallory")
        self.add_pr(7, baseRefName="feat/widget", author="hubot", isCrossRepository=True)
        self.add_pr(8, baseRefName="main", author="hubot")
        payload = self.ok("pr-children", "3")
        self.assertEqual((payload["head"], [c["number"] for c in payload["children"]]), ("feat/widget", [5]))
        self.assertEqual(payload["children"][0]["base"], "feat/widget")
        self.assertEqual(payload["children"][0]["head"], "feat/p5")
        self.assertEqual(payload["children"][0]["author"], "hubot")
        self.assertIn("url", payload["children"][0])
        self.assertEqual({s["number"]: s["reason"] for s in payload["skipped"]},
                         {6: "author not trusted", 7: "cross-repository (fork) PR"})
        self.assertEqual(self.ok("pr-children", "8")["children"], [])
        self.assertEqual(self.fake.writes_made(), [])

    def test_pr_children_accepts_a_full_ref(self):
        self.add_pr(5, baseRefName="feat/widget", author="hubot")
        self.assertEqual([c["number"] for c in self.ok("pr-children", f"{REPO}#3")["children"]], [5])

    def test_pr_by_head_finds_open_same_repo_prs_from_trusted_authors(self):
        self.add_pr(5, headRefName="feat/dup", author="hubot")
        self.add_pr(6, headRefName="feat/dup", author="mallory")
        self.add_pr(7, headRefName="feat/dup", author="hubot", state="CLOSED")
        self.add_pr(8, headRefName="feat/dup", author="hubot", isCrossRepository=True)
        payload = self.ok("pr-by-head", "feat/dup")
        self.assertEqual([p["number"] for p in payload["prs"]], [5])
        self.assertEqual({s["number"] for s in payload["skipped"]}, {6, 8})
        self.assertEqual(self.ok("pr-by-head", "feat/missing")["prs"], [])
        argv = [c for c in self.fake.calls if c[:2] == ["pr", "list"]][0]
        self.assertEqual(argv[argv.index("--head") + 1], "feat/dup")
        self.assertEqual(argv[argv.index("--limit") + 1], "200")

    def test_pr_by_head_honours_repo_option(self):
        things(self.fake, "acme/other")["1"] = dict(things(self.fake)["3"], number=1, headRefName="feat/o",
                                                    author="hubot")
        payload = self.ok("pr-by-head", "feat/o", "--repo", "acme/other", repo=False)
        self.assertEqual((payload["repo"], [p["number"] for p in payload["prs"]]), ("acme/other", [1]))

    def test_followup_honours_repo_and_defaults_to_the_tracker(self):
        title, body = self.write_file("t.txt", "Follow up\n"), self.write_file("b.md", "Do more\n")
        made = self.ok("followup", "--title-file", title, "--body-file", body)
        self.assertEqual(made["number"], 4)  # acme/app is the tracker
        other = self.ok("followup", "--title-file", title, "--body-file", body, "--repo", "acme/other", repo=False)
        self.assertEqual(other["number"], 1)
        self.assertEqual(things(self.fake, "acme/other")["1"]["title"], "Follow up")
        self.assertEqual(len(things(self.fake)), 4)

    def test_followup_in_a_repo_off_the_allow_list_is_blocked(self):
        self.tc.write("global", "project", base.NO_OTHERS_PROJECT)
        title, body = self.write_file("t.txt", "Follow up\n"), self.write_file("b.md", "Do more\n")
        code, payload, _ = self.cli("followup", "--title-file", title, "--body-file", body, "--repo", "acme/other",
                                    repo=False)
        self.assertEqual((code, payload["blocked"]), (6, True))
        self.assertEqual(self.fake.writes_made(), [])

    def test_issue_edit_body(self):
        body = self.write_file("b.md", "New body\n")
        payload = self.ok("issue-edit-body", "1", "--body-file", body)
        self.assertEqual(payload, {"repo": REPO, "number": 1, "updated": True})
        self.assertEqual(things(self.fake)["1"]["body"], "New body\n")

    def test_issue_edit_body_full_ref_blocked_and_unsafe_file(self):
        body = self.write_file("b.md", "New body\n")
        things(self.fake, "acme/other")["1"] = dict(things(self.fake)["1"], number=1)
        self.ok("issue-edit-body", "acme/other#1", "--body-file", body)
        self.assertEqual(things(self.fake, "acme/other")["1"]["body"], "New body\n")
        self.tc.write("global", "project", base.NO_OTHERS_PROJECT)
        code, payload, _ = self.cli("issue-edit-body", "acme/app-ui#1", "--body-file", body)
        self.assertEqual((code, payload["blocked"]), (6, True))
        outside = TempCase()
        self.addCleanup(outside.cleanup)
        path = outside.root / "b.md"
        path.write_text("x", encoding="utf-8")
        code, payload, _ = self.cli("issue-edit-body", "1", "--body-file", str(path))
        self.assertEqual((code, payload["kind"]), (1, "unsafe_path"))
        self.assertEqual(things(self.fake)["1"]["body"], "Body of First issue")

    def test_labels_sync_takes_a_full_ref_for_a_companion_pr(self):
        things(self.fake, "acme/app-ui")["1"] = dict(things(self.fake)["3"], number=1, labels=["needs review"])
        payload = self.ok("labels-sync", "acme/app-ui#1", "--add", "extra")
        self.assertEqual((payload["repo"], payload["number"]), ("acme/app-ui", 1))
        self.assertEqual(things(self.fake, "acme/app-ui")["1"]["labels"], ["needs review", "extra"])
        self.ok("labels-sync", "1", "--add", "tracker-side")  # a bare number is still the tracker's item
        self.assertEqual(things(self.fake)["1"]["labels"], ["to do", "tracker-side"])

    def test_claim_pr_and_release(self):
        claim = self.ok("claim-pr", "3")
        self.assertEqual(claim["number"], 3)
        marker = [c for c in things(self.fake)["3"]["comments"] if "not-me:claim" in c["body"]]
        self.assertEqual(len(marker), 1)
        self.ok("release-pr", "3")
        marker = [c for c in things(self.fake)["3"]["comments"] if "not-me:claim" in c["body"]]
        self.assertIn("state=released", marker[0]["body"])

    def test_claim_pr_lost_to_a_live_claim(self):
        self.fake.post_comment(REPO, 3, "hubot", claims.marker_body("hubot", "other", claims.iso(claims.utcnow())))
        code, payload, _ = self.cli("claim-pr", "3")
        self.assertEqual((code, payload["claim_lost"]), (3, True))

    def test_claim_pr_refuses_paused(self):
        things(self.fake)["3"]["labels"] = ["needs work", "paused"]
        self.assertEqual(self.cli("claim-pr", "3")[0], 3)

    def test_bundle_and_feedback(self):
        bundle = self.ok("bundle", "3")
        self.assertEqual((bundle["number"], bundle["author"], bundle["author_trusted"]), (3, "hubot", True))
        feedback = self.ok("feedback", "3")
        self.assertEqual(feedback["number"], 3)
        self.assertIn("findings", feedback)

    def test_ref_forms(self):
        for ref in ("3", "#3", "acme/app#3", "https://github.com/acme/app/pull/3"):
            with self.subTest(ref):
                self.assertEqual(self.ok("bundle", ref)["number"], 3)

    def test_pr_open_creates_pr_labels_and_reviewers(self):
        title = self.write_file("title.txt", "Fix the widget\n")
        body = self.write_file("body.md", "## Summary\n\nDone.\n")
        payload = self.ok("pr-open", "--head", "feat/x", "--base", "main", "--title-file", title, "--body-file", body,
                          "--labels", "needs review", "--reviewers", "hubot", "--assignee", "octocat")
        new = things(self.fake)[str(payload["number"])]
        self.assertEqual((new["title"], new["labels"], new["reviewRequests"]), ("Fix the widget", ["needs review"],
                                                                                ["hubot"]))

    def test_pr_open_refuses_a_protected_head(self):
        title = self.write_file("title.txt", "T\n")
        body = self.write_file("body.md", "B\n")
        code, payload, _ = self.cli("pr-open", "--head", "main", "--base", "develop", "--title-file", title,
                                    "--body-file", body)
        self.assertEqual((code, payload["kind"]), (1, "protected_branch"))

    def test_review_post_approve_needs_global_auto_approve(self):
        body = self.write_file("body.md", "Looks good\n")
        code, payload, _ = self.cli("review-post", "3", "--event", "APPROVE", "--body-file", body)
        self.assertEqual((code, payload["kind"]), (1, "approve_refused"))
        self.assertEqual(self.fake.writes_made(), [])
        self.tc.write("global", "review", "---\nschema: 1\nauto_approve: true\n---\n# Review\n")
        payload = self.ok("review-post", "3", "--event", "APPROVE", "--body-file", body,
                          "--labels-set", "ready to merge")
        self.assertEqual(payload["state"], "APPROVED")
        self.assertEqual(things(self.fake)["3"]["labels"], ["ready to merge"])

    def test_review_post_with_inline_comments(self):
        body = self.write_file("body.md", "Please change\n")
        inline = self.write_file("inline.json", json.dumps([{"path": "a.py", "line": 3, "body": "rename"}]))
        payload = self.ok("review-post", "3", "--event", "REQUEST_CHANGES", "--body-file", body,
                          "--comments-file", inline)
        self.assertEqual(payload["state"], "CHANGES_REQUESTED")

    def test_reply_resolve_and_comment_upsert(self):
        body = self.write_file("body.md", "Fixed in the last commit\n")
        self.ok("reply-thread", "3", "--to", "501", "--body-file", body)
        self.assertEqual(len(things(self.fake)["3"]["threads"][0]["comments"]), 2)
        ids = self.write_file("ids.json", json.dumps(["PRRT_1"]))
        self.assertEqual(self.ok("resolve-threads", "3", "--ids-file", ids)["resolved"], ["PRRT_1"])
        self.assertTrue(things(self.fake)["3"]["threads"][0]["isResolved"])
        first = self.ok("comment-upsert", "3", "--marker", "status", "--body-file", body)
        second = self.ok("comment-upsert", "3", "--marker", "status", "--body-file", body)
        self.assertTrue(first["created"] and second["updated"])

    def test_pr_body_testing_steps_and_companions(self):
        body = self.write_file("body.md", "## Summary\n\nNew body.\n")
        self.ok("pr-body", "3", "--body-file", body)
        self.assertIn("New body.", things(self.fake)["3"]["body"])
        steps = self.write_file("steps.txt", "- [ ] Open the page\n- Click save\n")
        self.assertTrue(self.ok("testing-steps", "3", "--steps-file", steps)["updated"])
        self.assertIn("- [ ] Click save", things(self.fake)["3"]["body"])
        self.assertTrue(self.ok("testing-steps", "3", "--steps-file", steps)["unchanged"])
        comp = self.write_file("c.json", json.dumps([{"repo": "acme/app-ui", "number": 9, "url": "https://x.test/9"}]))
        self.assertTrue(self.ok("companion-prs", "3", "--companions-file", comp)["updated"])
        self.assertIn("acme/app-ui#9", things(self.fake)["3"]["body"])
        bad = self.write_file("bad.json", "{}")
        self.assertEqual(self.cli("companion-prs", "3", "--companions-file", bad)[0], 2)

    def test_request_review(self):
        self.assertEqual(self.ok("request-review", "3", "--reviewers", "hubot,mona")["requested"], ["hubot", "mona"])
        self.assertEqual(things(self.fake)["3"]["reviewRequests"], ["octocat", "hubot", "mona"])

    def test_followup_creates_and_links_into_the_parent(self):
        title = self.write_file("title.txt", "Later\n")
        body = self.write_file("body.md", "Do it later\n")
        payload = self.ok("followup", "--title-file", title, "--body-file", body, "--source", "3", "--parent", "2")
        self.assertEqual(payload["parent"], 2)
        self.assertIn(f"- [ ] #{payload['number']}", things(self.fake)["2"]["body"])

    def test_check_text(self):
        good = self.write_file("good.md", "**TL;DR:** done\n")
        self.assertEqual(self.ok("check-text", "--file", good, "--summary"), {"ok": True, "problems": []})
        bad = self.write_file("bad.md", "see acme/other#4 and /Users/someone/file\n")
        code, payload, _ = self.cli("check-text", "--file", bad, "--summary")
        self.assertEqual((code, payload["kind"], payload["ok"]), (1, "text_lint", False))
        self.assertTrue(any("TL;DR" in p for p in payload["problems"]))


class DoctorAndPreflightTests(base.CliCase):
    def test_doctor_lists_checks(self):
        payload = self.ok("doctor")
        names = [c["name"] for c in payload["checks"]]
        self.assertEqual(names, ["python", "git", "gh", "config", "gh auth", "gh scopes", "guard", "spawn depth"])
        self.assertTrue(payload["ok"])
        self.assertTrue(all({"name", "ok", "detail", "required"} <= set(c) for c in payload["checks"]))

    def test_doctor_fails_when_gh_is_missing(self):
        def no_gh(argv):
            raise FileNotFoundError(2, "no", "gh")

        code, payload, _ = self.cli("doctor", runner=no_gh)
        self.assertEqual((code, payload["kind"]), (7, "doctor"))
        self.assertFalse(next(c for c in payload["checks"] if c["name"] == "gh")["ok"])

    def test_preflight_in_a_scratch_repo(self):
        init_repo(self.project)
        payload = self.ok("preflight")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual((payload["branch"], payload["clean"], payload["operator"]), ("main", True, "octocat"))
        self.assertEqual(payload["guard"], {"active": False, "selftest": None})
        (self.project / ".claude" / "not-me").mkdir(parents=True)
        hook = [sys.executable, str(PLUGIN_ROOT / "hooks" / "repo_guard.py")]
        payload = self.ok("preflight", hook_cmd=hook)
        self.assertEqual(payload["guard"], {"active": True, "selftest": True})
        self.ok("pause", "on")
        payload = self.ok("preflight", hook_cmd=hook)
        self.assertFalse(payload["ok"])
        self.assertIn("paused", payload["blockers"])

    def test_preflight_push_check_without_a_remote_is_a_blocker(self):
        init_repo(self.project)
        payload = self.ok("preflight", "--push-check")
        self.assertFalse(payload["push_check"]["ok"])
        self.assertIn("push check failed", payload["blockers"])


class WaitChecksTests(base.CliCase):
    """wait-checks over a fake clock: sleep advances the clock and may change the checks."""

    def setUp(self):
        super().setUp()
        add_pr_state(self.fake)
        self.now = 1000.0
        self.sleeps = []
        self.on_sleep = lambda: None

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        self.on_sleep()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = base.cli.main(["--project-dir", str(self.project), "--repo", REPO, "wait-checks", "3", *argv],
                             runner=self.fake, env=self.env, stdin=base.TtyIO(""), stdout=out, stderr=err,
                             sleep=self.sleep, clock=self.clock, process_check=lambda: False)
        self.assertEqual(out.getvalue().count("\n"), 1)
        return code, json.loads(out.getvalue())

    @staticmethod
    def checks(*buckets):
        return [{"name": f"c{i}", "state": b.upper(), "bucket": b, "workflow": "ci"} for i, b in enumerate(buckets)]

    def test_no_checks_is_none_without_waiting(self):
        code, payload = self.run_cli()
        self.assertEqual((code, payload["state"], payload["checks"], payload["waited_s"]), (0, "none", [], 0))
        self.assertEqual(self.sleeps, [])

    def test_all_passing_returns_at_once(self):
        things(self.fake)["3"]["checks"] = self.checks("pass", "skipping")
        code, payload = self.run_cli()
        self.assertEqual((code, payload["state"], payload["waited_s"]), (0, "success", 0))
        self.assertEqual(len(payload["checks"]), 2)
        self.assertEqual(self.sleeps, [])

    def test_polls_every_30_seconds_until_checks_finish(self):
        things(self.fake)["3"]["checks"] = self.checks("pass", "pending")
        polls = []

        def finish():
            polls.append(self.now)
            if len(polls) == 3:
                things(self.fake)["3"]["checks"] = self.checks("pass", "pass")

        self.on_sleep = finish
        code, payload = self.run_cli("--minutes", "10")
        self.assertEqual((code, payload["state"], payload["waited_s"]), (0, "success", 90))
        self.assertEqual(self.sleeps, [30, 30, 30])

    def test_failure_exits_zero_and_waits_for_the_rest(self):
        things(self.fake)["3"]["checks"] = self.checks("fail", "pending")

        def finish():
            things(self.fake)["3"]["checks"] = self.checks("fail", "pass")

        self.on_sleep = finish
        code, payload = self.run_cli()
        self.assertEqual((code, payload["state"], payload["waited_s"]), (0, "failure", 30))

    def test_deadline_returns_pending(self):
        things(self.fake)["3"]["checks"] = self.checks("pass", "pending")
        code, payload = self.run_cli("--minutes", "1.5")
        self.assertEqual((code, payload["state"], payload["waited_s"]), (0, "pending", 90))
        self.assertEqual(self.sleeps, [30, 30, 30])

    def test_deadline_with_a_failure_is_failure(self):
        things(self.fake)["3"]["checks"] = self.checks("fail", "pending")
        code, payload = self.run_cli("--minutes", "0")
        self.assertEqual((code, payload["state"], payload["waited_s"]), (0, "failure", 0))

    def test_default_minutes_come_from_limits_ci_wait_minutes(self):
        self.tc.write("global", "project", base.GLOBAL_PROJECT.replace("operator: octocat", "operator: octocat\n"
                                                                         "limits: {ci_wait_minutes: 2}"))
        things(self.fake)["3"]["checks"] = self.checks("pending")
        code, payload = self.run_cli()
        self.assertEqual((code, payload["state"], payload["waited_s"]), (0, "pending", 120))

    def test_rollup_conclusions_are_understood(self):
        things(self.fake)["3"]["statusCheckRollup"] = [
            {"name": "a", "conclusion": "SUCCESS", "status": "COMPLETED"},
            {"name": "b", "conclusion": "", "status": "IN_PROGRESS"}]
        code, payload = self.run_cli("--minutes", "0")
        self.assertEqual((code, payload["state"]), (0, "pending"))
        things(self.fake)["3"]["statusCheckRollup"][1].update(conclusion="FAILURE", status="COMPLETED")
        self.assertEqual(self.run_cli("--minutes", "0")[1]["state"], "failure")

    def test_makes_no_writes(self):
        things(self.fake)["3"]["checks"] = self.checks("pass")
        self.run_cli()
        self.assertEqual(self.fake.writes_made(), [])


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.test", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env, check=True).stdout


def init_repo(path):
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@example.test")
    (Path(path) / "a.txt").write_text("one\n", encoding="utf-8")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "init")


class TmpDirTests(base.CliCase):
    def test_tmp_dir_is_fresh_private_and_accepted_as_input(self):
        first = self.ok("tmp-dir")["path"]
        second = self.ok("tmp-dir")["path"]
        self.addCleanup(shutil.rmtree, first, True)
        self.addCleanup(shutil.rmtree, second, True)
        self.assertNotEqual(first, second)
        root = os.path.realpath(str(gh_mod.notme_tmp_dir()))
        for path in (first, second):
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(os.path.dirname(os.path.realpath(path)), root)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
            self.assertEqual(os.listdir(path), [])
        body = Path(first) / "body.md"
        body.write_text("hello\n", encoding="utf-8")
        gh_mod.check_input_path(str(body), self.project)


class GitTests(base.CliCase):
    def setUp(self):
        super().setUp()
        init_repo(self.project)

    def test_retro_save_reviewed_and_is_reviewed(self):
        audit = self.write_file("audit.md", "# Audit\nall fine\n")
        common = self.project / ".git" / "not-me" / "retro"
        self.assertEqual(self.ok("retro", "reviewed"), {"reviewed": []})
        self.assertEqual(self.ok("retro", "is-reviewed", "run-1"), {"run_id": "run-1", "reviewed": False})
        saved = self.ok("retro", "save", "--run-id", "run-1", "--audit-file", audit)
        self.assertEqual((saved["run_id"], saved["reviewed"]), ("run-1", False))
        self.assertEqual((common / "run-1.md").read_text(encoding="utf-8"), "# Audit\nall fine\n")
        self.assertFalse((common / "reviewed.txt").exists())
        self.assertTrue(self.ok("retro", "save", "--run-id", "run-1", "--audit-file", audit, "--reviewed")["reviewed"])
        self.ok("retro", "save", "--run-id", "run-1", "--audit-file", audit, "--reviewed")  # once only
        self.ok("retro", "save", "--run-id", "run-2", "--audit-file", audit, "--reviewed")
        self.assertEqual(self.ok("retro", "reviewed"), {"reviewed": ["run-1", "run-2"]})
        self.assertTrue(self.ok("retro", "is-reviewed", "run-2")["reviewed"])
        self.assertFalse(self.ok("retro", "is-reviewed", "run-3")["reviewed"])
        self.assertEqual(self.fake.calls, [])

    def test_retro_save_refuses_bad_run_ids_and_unsafe_files(self):
        audit = self.write_file("audit.md", "x\n")
        for rid in ("../evil", "a/b", "", "-x", "a b"):
            with self.subTest(rid=rid):
                code, payload, _ = self.cli("retro", "save", "--run-id", rid, "--audit-file", audit)
                self.assertEqual((code, payload["kind"]), (2, "usage"))
        code, payload, _ = self.cli("retro", "is-reviewed", "../x")
        self.assertEqual(code, 2)
        outside = TempCase()
        self.addCleanup(outside.cleanup)
        path = outside.root / "audit.md"
        path.write_text("x", encoding="utf-8")
        code, payload, _ = self.cli("retro", "save", "--run-id", "r", "--audit-file", str(path))
        self.assertEqual((code, payload["kind"]), (1, "unsafe_path"))
        self.assertFalse((self.project / ".git" / "not-me" / "retro" / "r.md").exists())

    def test_log_list_newest_first_with_filters(self):
        self.assertEqual(self.ok("log", "list"), {"logs": []})
        first = self.ok("log", "start", "--skill", "new", "--ref", "1")
        listed = self.ok("log", "list")["logs"]
        self.assertEqual(len(listed), 1)
        self.assertEqual((listed[0]["skill"], listed[0]["ref"], listed[0]["path"]), ("new", "1", first["path"]))
        self.assertEqual(set(listed[0]), {"path", "run_id", "skill", "ref", "status"})

    def test_log_list_orders_by_timestamp_and_limits(self):
        root = Path(self.ok("log", "start", "--skill", "new", "--ref", "1")["path"]).parent.parent
        for skill, stamp, ref in (("new", "20260102-000000", "2"), ("update", "20260103-000000", "3"),
                                  ("new", "20260101-000000", "4")):
            folder = root / skill
            folder.mkdir(exist_ok=True)
            (folder / f"{stamp}-x.md").write_text(f"# {skill} run: {ref} ({stamp}-x)\n", encoding="utf-8")
        every = self.ok("log", "list")["logs"]
        names = [Path(e["path"]).name for e in every]
        self.assertEqual(names, sorted(names, reverse=True))
        self.assertEqual(len(every), 4)
        self.assertEqual(len(self.ok("log", "list", "--limit", "2")["logs"]), 2)
        self.assertEqual({e["skill"] for e in self.ok("log", "list", "--skill", "update")["logs"]}, {"update"})
        code, payload, _ = self.cli("log", "list", "--limit", "0")
        self.assertEqual(code, 2)

    def test_remote_branch_exists(self):
        origin = self.tc.root / "origin.git"
        git(self.tc.root, "init", "-q", "--bare", "-b", "main", str(origin))
        git(self.project, "remote", "add", "origin", str(origin))
        git(self.project, "push", "-q", "origin", "main")
        git(self.project, "branch", "feat/x/y")
        git(self.project, "push", "-q", "origin", "feat/x/y")
        sha = git(self.project, "rev-parse", "main").strip()
        self.assertEqual(self.ok("git", "remote-branch-exists", "main"), {"exists": True, "sha": sha})
        self.assertEqual(self.ok("git", "remote-branch-exists", "feat/x/y")["exists"], True)
        self.assertEqual(self.ok("git", "remote-branch-exists", "feat/x"), {"exists": False, "sha": None})
        self.assertEqual(self.ok("git", "remote-branch-exists", "y"), {"exists": False, "sha": None})  # no suffix match
        self.assertEqual(self.ok("git", "remote-branch-exists", "nope"), {"exists": False, "sha": None})

    def test_remote_branch_exists_without_origin_is_a_git_error(self):
        code, payload, _ = self.cli("git", "remote-branch-exists", "main")
        self.assertEqual((code, payload["kind"]), (1, "git_error"))

    def test_remote_branch_exists_rejects_bad_names(self):
        for name in ("a..b", "--upload-pack=x", "a:b", "a b"):
            with self.subTest(name=name):
                code, payload, _ = self.cli("git", "remote-branch-exists", name)
                self.assertEqual((code, payload["kind"]), (2, "usage"))

    def test_status_and_branch_create(self):
        status = self.ok("git", "status")
        self.assertEqual((status["branch"], status["clean"], status["origin"]), ("main", True, None))
        self.assertEqual(self.ok("git", "branch-create", "feat/x", "--base", "main"), {"branch": "feat/x",
                                                                                         "base": "main"})
        self.assertEqual(self.ok("git", "status")["branch"], "feat/x")
        code, payload, _ = self.cli("git", "branch-create", "develop", "--base", "main")
        self.assertEqual((code, payload["kind"]), (1, "protected_branch"))

    def test_stash_and_unstash_by_sha(self):
        self.assertIsNone(self.ok("git", "stash")["stash"])
        (self.project / "a.txt").write_text("changed\n", encoding="utf-8")
        sha = self.ok("git", "stash")["stash"]
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertFalse((self.project / "a.txt").read_text() == "changed\n")
        self.ok("git", "unstash", sha)
        self.assertEqual((self.project / "a.txt").read_text(), "changed\n")

    def test_checkpoint_merges_the_base(self):
        git(self.project, "checkout", "-q", "-b", "feat/y")
        (self.project / "b.txt").write_text("b\n", encoding="utf-8")
        git(self.project, "add", "b.txt")
        git(self.project, "commit", "-q", "-m", "b")
        git(self.project, "checkout", "-q", "main")
        (self.project / "c.txt").write_text("c\n", encoding="utf-8")
        git(self.project, "add", "c.txt")
        git(self.project, "commit", "-q", "-m", "c")
        git(self.project, "checkout", "-q", "feat/y")
        payload = self.ok("git", "checkpoint", "--base", "main")
        self.assertEqual((payload["branch"], payload["conflicts"]), ("feat/y", False))
        self.assertTrue((self.project / "c.txt").exists())

    def test_worktree_add_and_remove(self):
        sha = git(self.project, "rev-parse", "HEAD").strip()
        wt = str(self.project / ".git" / "not-me" / "worktrees" / "wt")
        self.ok("git", "worktree-add", wt, sha)
        self.assertTrue(Path(wt, "a.txt").exists())
        self.ok("git", "worktree-remove", wt)
        self.assertFalse(Path(wt).exists())

    def test_push_refuses_protected_branches(self):
        code, payload, _ = self.cli("git", "push", "main")
        self.assertEqual((code, payload["kind"]), (1, "protected_branch"))
        code, payload, _ = self.cli("git", "push", "main", "--dry-run")
        self.assertEqual((code, payload["kind"]), (1, "protected_branch"))

    def test_dry_run_push_rehearses_and_records(self):
        git(self.project, "remote", "add", "origin", "https://github.com/acme/app.git")
        git(self.project, "checkout", "-q", "-b", "feat/z")
        with mock.patch("notme.git.push_dry_run") as rehearse, mock.patch("notme.git._run",
                                                                         wraps=git_mod._run) as run:
            payload = self.ok("git", "push", "feat/z", "--dry-run")
            self.assertTrue(self.ok("git", "push-check")["ok"])
        self.assertEqual((payload["pushed"], payload["dry_run"]), (False, True))
        self.assertEqual(rehearse.call_args_list[0].args[0], "feat/z")
        self.assertFalse([c for c in run.call_args_list if c.args[0][:1] == ["push"]])  # nothing really pushed
        self.assertEqual([e["op"] for e in self.ledger()], ["git push"])

    def test_dry_run_push_still_checks_the_push_urls(self):
        bare = self.tc.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        git(self.project, "remote", "add", "origin", "https://github.com/acme/app.git")
        git(self.project, "config", f"url.{bare}.insteadOf", "https://github.com/acme/app.git")
        git(self.project, "checkout", "-q", "-b", "feat/z")
        code, payload, _ = self.cli("git", "push", "feat/z", "--dry-run")
        self.assertEqual((code, payload["kind"]), (1, "git_error"))
        self.assertEqual(git(bare, "branch", "--list").strip(), "")

    def test_push_check_fails_without_origin(self):
        code, payload, _ = self.cli("git", "push-check")
        self.assertEqual((code, payload["kind"]), (1, "push_check"))


if __name__ == "__main__":
    unittest.main()
