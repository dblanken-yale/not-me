from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notme import gh as ghmod
from notme.dryrun import Ledger
from notme.gh import GhClient, GhError, ReadbackMismatch, UnsafePath, WriteBlocked, is_read
from notme.writes import GuardContext, WriteTarget
from tests.fakes.fakegh import FakeRunner

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
FAKE_BIN = Path(__file__).resolve().parent / "fakes" / "bin"

IS_READ_TABLE = [
    (["pr", "view", "7", "--json", "number"], True),
    (["pr", "list", "-R", "acme/app"], True),
    (["pr", "diff", "7"], True),
    (["pr", "checks", "7"], True),
    (["pr", "status"], False),
    (["issue", "view", "42"], True),
    (["issue", "list", "--label", "bug"], True),
    (["repo", "view", "acme/app", "--json", "defaultBranchRef"], True),
    (["search", "issues", "widget"], True),
    (["status"], True),
    (["run", "list"], True),
    (["api", "user"], True),
    (["api", "repos/acme/app/issues/1/labels?per_page=100"], True),
    (["api", "--paginate", "repos/acme/app/pulls"], True),
    (["api", "-X", "GET", "repos/acme/app/issues"], True),
    (["api", "graphql", "-f", "query=query{viewer{login}}"], True),
    (["api", "graphql", "-f", "query=query($o:String!){x}", "-f", "o=acme"], True),
    (["api", "graphql", "-f", "query=query($o:String!){x}", "-F", "o=acme"], False),
    (["api", "-X", "GET", "repos/acme/app/issues"], True),
    (["api", "--method", "GET", "repos/acme/app/issues"], True),
    (["api", "--method=GET", "repos/acme/app/issues"], True),
    (["api", "-X=GET", "repos/acme/app/issues"], True),
    (["api", "-XGET", "repos/acme/app/issues"], True),
    (["api", "-i", "--paginate", "repos/acme/app/issues"], True),
    # confirmed bypasses of earlier classifiers
    (["api", "-X=POST", "repos/acme/app/issues/1/comments"], False),
    (["api", "-X", "GET", "-X", "DELETE", "repos/acme/app/issues/1"], False),
    (["api", "--method", "GET", "--method=PUT", "repos/acme/app/issues/1/labels"], False),
    (["api", "-XGET", "-X", "PATCH", "repos/acme/app/issues/comments/1"], False),
    (["api", "-iXPOST", "repos/acme/app/issues/1/comments"], False),
    (["api", "-ifbody=x", "repos/acme/app/issues/1/comments"], False),
    (["api", "-ifoo", "repos/acme/app/issues"], False),
    (["api", "repos/acme/app/issues", "-H", "X-HTTP-Method-Override: DELETE"], False),
    (["api", "graphql", "-f", "query=query{x}", "-f", "query=query{y}"], False),
    (["api", "graphql", "-f", "query=query{x}", "--paginate"], False),
    (["api", "graphql", "-f", "id=1"], False),
    (["api", "repos/acme/app/issues/1", "graphql"], False),
    (["co", "list"], False),
    (["pr", "ready", "7"], False),
    (["repo", "list"], False),
    (["run", "watch", "1"], False),
    (["issue", "status"], False),
    (["label", "list"], True),
    (["auth", "status"], False),
    (["auth", "status", "--show-token"], False),
    (["variable", "list"], False),
    (["secret", "list"], False),
    (["config", "get", "git_protocol"], False),
    (["alias", "list"], False),
    (["extension", "list"], False),
    (["gist", "view", "abc"], False),
    (["ssh-key", "list"], False),
    (["gpg-key", "list"], False),
    (["-R", "acme/app", "pr", "view", "1"], False),
    (["pr", "create", "--title", "x"], False),
    (["pr", "edit", "7", "--body-file", "b.md"], False),
    (["pr", "merge", "7"], False),
    (["pr", "close", "7"], False),
    (["issue", "comment", "1", "--body-file", "b.md"], False),
    (["issue", "edit", "1", "--add-label", "x"], False),
    (["issue", "create"], False),
    (["repo", "delete", "acme/app"], False),
    (["api", "-X", "POST", "repos/acme/app/issues/1/comments"], False),
    (["api", "--method", "PATCH", "repos/acme/app/issues/comments/1"], False),
    (["api", "-XPUT", "repos/acme/app/issues/1/labels"], False),
    (["api", "--method=DELETE", "repos/acme/app/issues/1"], False),
    (["api", "-X", "put", "repos/acme/app/issues/1/labels"], False),
    (["api", "repos/acme/app/issues/1/comments", "-f", "body=hi"], False),
    (["api", "repos/acme/app/issues/1/comments", "-F", "body=@b.md"], False),
    (["api", "repos/acme/app/issues/1/labels", "--input", "l.json"], False),
    (["api", "repos/acme/app/issues/1/comments", "-fbody=hi"], False),
    (["api", "graphql", "-f", "query=mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}"], False),
    (["api", "graphql", "-f", "query=Mutation { x }"], False),
    (["api", "graphql", "--input", "q.json"], False),
    (["api", "graphql", "--input=q.json"], False),
    (["api", "graphql", "-F", "query=@q.graphql"], False),
    (["api", "graphql", "-f", "query=@q.graphql"], False),
    (["api", "graphql", "-X", "POST", "-f", "query=query{viewer{login}}"], False),
    (["api", "graphql", "--method", "PATCH", "-f", "query=query{viewer{login}}"], False),
    (["label", "create", "x"], False),
    ([], False),
    (["frobnicate"], False),
]


class IsReadTests(unittest.TestCase):
    def test_table(self):
        self.assertGreaterEqual(len(IS_READ_TABLE), 20)
        for argv, want in IS_READ_TABLE:
            with self.subTest(argv=argv):
                self.assertEqual(is_read(argv), want)


class DeniedTests(unittest.TestCase):
    def test_denied_commands(self):
        for cmd in ("auth", "config", "alias", "extension", "secret", "variable", "gist", "ssh-key", "gpg-key"):
            with self.subTest(cmd=cmd):
                self.assertTrue(ghmod.is_denied([cmd, "list"]))
                self.assertFalse(is_read([cmd, "list"]))
        self.assertFalse(ghmod.is_denied(["pr", "view"]))
        self.assertFalse(ghmod.is_denied([]))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="notme-gh-")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.project = self.dir / "project"
        self.project.mkdir()
        self.ctx = GuardContext(origin_repo="acme/app", allowed_repos=("acme/app", "acme/app-ui"),
                                board=("acme", 6), protected_branches=("main",), mode="live",
                                project_dir=self.project)
        self.fake = FakeRunner()
        self.ledger = Ledger(self.dir / "ledger" / "intended-writes.jsonl")

    def client(self, dry_run=False, ctx=None):
        return GhClient(ctx or self.ctx, runner=self.fake, dry_run=dry_run, ledger=self.ledger)

    def body(self, text="Looks good.\n", name="body.md") -> Path:
        path = self.project / name
        path.write_text(text, encoding="utf-8")
        return path


class FakeBehaviourTests(Base):
    def test_unknown_argv_fails_clearly(self):
        proc = self.fake(["frobnicate", "x"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unsupported argv", proc.stderr)

    def test_fail_next_and_rate_limit(self):
        self.fake.fail_next = "boom secret-body"
        with self.assertRaises(GhError) as cm:
            self.client().me()
        self.assertEqual(cm.exception.stderr, "boom secret-body")
        self.assertNotIn("secret-body", str(cm.exception))
        self.assertEqual(self.client().me(), "octocat")
        self.fake.rate_limit = True
        with self.assertRaises(GhError) as cm:
            self.client().me()
        self.assertIn("rate limit", cm.exception.stderr)

    def test_race_assignee_shows_up_on_next_view(self):
        self.fake.race_assignee = True
        c = self.client()
        self.assertEqual(c.issue("acme/app", 2)["assignees"], [])
        self.assertEqual([a["login"] for a in c.issue("acme/app", 2)["assignees"]], ["hubot"])

    def test_timeline_records_events(self):
        self.client().labels_set("acme/app", 1, ["bug"])
        proc = self.fake(["api", "repos/acme/app/issues/1/timeline"])
        self.assertEqual(json.loads(proc.stdout)[0]["event"], "labels_set")


class ReadTests(Base):
    def test_convenience_reads(self):
        c = self.client()
        self.assertEqual(c.me(), "octocat")
        self.assertEqual(c.repo_default_branch("acme/app-ui"), "develop")
        self.assertEqual(c.issue("acme/app", 1)["title"], "First issue")
        self.assertEqual(c.labels("acme/app", 1), ["to do"])
        pr = c.pr("acme/app", 3)
        self.assertEqual((pr["headRefName"], pr["baseRefName"]), ("feat/widget", "main"))
        self.assertEqual([p["number"] for p in c.pr_list("acme/app", label="needs review")], [3])
        self.assertEqual(c.pr_list("acme/app", label="nope"), [])
        self.assertEqual(c.pr_list("acme/app", assignee="hubot"), [])
        threads = c.review_threads("acme/app", 3)
        self.assertEqual(threads[0]["comments"][0]["author"], "hubot")
        self.assertFalse(threads[0]["isResolved"])
        self.assertTrue(all(ghmod.is_read(call) for call in self.fake.calls))

    def test_pr_view_requests_the_documented_fields(self):
        self.client().pr("acme/app", 3)
        argv = self.fake.calls[-1]
        fields = argv[argv.index("--json") + 1].split(",")
        for f in ("number", "title", "body", "headRefName", "headRefOid", "baseRefName", "state", "isDraft",
                  "isCrossRepository", "author", "labels", "reviewRequests", "reviewDecision", "mergeable"):
            self.assertIn(f, fields)

    def test_comments_have_author_and_association(self):
        c = self.client()
        c.comment_create("acme/app", 1, self.body("hi"))
        [row] = c.comments("acme/app", 1)
        self.assertEqual((row["author"], row["association"], row["body"]), ("octocat", "OWNER", "hi"))

    def test_read_rejects_write_argv(self):
        for denied in (["auth", "status", "--show-token"], ["variable", "list"], ["-X=POST", "api"]):
            with self.subTest(denied=denied), self.assertRaises(GhError):
                self.client().read(denied)
        mutation = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}"
        for argv in (["pr", "merge", "3"], ["api", "graphql", "-f", f"query={mutation}", "-f", "id=PRRT_1"],
                     ["api", "graphql", "--input", "q.json"], ["api", "-X", "POST", "repos/acme/app/issues/1/comments"],
                     ["api", "graphql", "-F", "query=@q.graphql"]):
            with self.subTest(argv=argv), self.assertRaises(GhError):
                self.client().read(argv)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.fake.state["repos"]["acme/app"]["things"]["3"]["threads"][0]["isResolved"])

    def test_read_text_mode(self):
        self.assertIn("octocat", self.client().read(["api", "user"], json=False))

    def test_error_wraps_returncode(self):
        with self.assertRaises(GhError) as cm:
            self.client().issue("acme/app", 999)
        self.assertEqual(cm.exception.returncode, 1)
        self.assertIn("404", cm.exception.stderr)

    def test_board_status(self):
        data = self.client().board_item_status("acme", 6)
        self.assertEqual(data["project_id"], "PVT_1")
        self.assertEqual(data["items"][0]["status"], "To Do")
        self.assertEqual({o["name"] for o in data["status_field"]["options"]}, {"To Do", "In progress", "In review"})
        self.assertEqual(self.client().board_item_status("acme", 6, "nope")["items"], [])


class FixtureShapeTests(Base):
    def fixture_client(self, name):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        return GhClient(self.ctx, runner=lambda argv: subprocess.CompletedProcess(argv, 0, text, ""),
                        dry_run=False, ledger=self.ledger)

    def test_count(self):
        self.assertGreaterEqual(len(list(FIXTURES.glob("*.json"))), 8)
        for path in FIXTURES.glob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_user(self):
        self.assertEqual(self.fixture_client("user.json").me(), "octocat")

    def test_repo_view(self):
        self.assertEqual(self.fixture_client("repo_view.json").repo_default_branch("acme/app"), "main")

    def test_issue_view(self):
        issue = self.fixture_client("issue_view.json").issue("acme/app", 42)
        self.assertEqual(issue["number"], 42)
        self.assertEqual(issue["labels"][0]["name"], "to do")

    def test_labels(self):
        self.assertEqual(self.fixture_client("issue_labels.json").labels("acme/app", 42), ["to do", "bug"])

    def test_issue_comments(self):
        rows = self.fixture_client("issue_comments.json").comments("acme/app", 42)
        self.assertEqual([(r["author"], r["association"]) for r in rows], [("octocat", "OWNER"), ("stranger", "NONE")])

    def test_pr_view(self):
        pr = self.fixture_client("pr_view.json").pr("acme/app", 7)
        self.assertEqual(pr["headRefOid"], "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(pr["reviewRequests"][0]["login"], "hubot")

    def test_pr_list(self):
        self.assertEqual([p["number"] for p in self.fixture_client("pr_list.json").pr_list("acme/app")], [7, 9])

    def test_review_threads(self):
        threads = self.fixture_client("review_threads.json").review_threads("acme/app", 7)
        self.assertEqual([t["isResolved"] for t in threads], [False, True])
        self.assertEqual(threads[0]["comments"][1]["databaseId"], 2002)
        self.assertIsNone(threads[1]["comments"][0]["author"])

    def test_board_items(self):
        data = self.fixture_client("board_items.json").board_item_status("acme", 6)
        self.assertEqual(data["items"][0], {"id": "PVTI_1", "number": 42, "repo": "acme/app", "status": "To Do"})
        self.assertIsNone(data["items"][1]["status"])


class WriteGateTests(Base):
    def test_blocked_repo_carries_allow_cmd_and_runs_nothing(self):
        body = self.body()
        with self.assertRaises(WriteBlocked) as cm:
            self.client().comment_create("acme/other", 1, body)
        self.assertEqual(cm.exception.decision.allow_cmd, "/not-me:allow-repo acme/other")
        self.assertEqual(cm.exception.to_json()["repo"], "acme/other")
        self.assertTrue(cm.exception.to_json()["blocked"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.ledger.read(), [])

    def test_blocked_even_in_dry_run(self):
        with self.assertRaises(WriteBlocked):
            self.client(dry_run=True).comment_create("acme/other", 1, self.body())
        self.assertEqual(self.ledger.read(), [])

    def test_allowed_companion_repo_writes(self):
        self.fake.state["repos"]["acme/app-ui"]["things"]["1"] = self.fake.state["repos"]["acme/app"]["things"]["1"]
        self.client().comment_create("acme/app-ui", 1, self.body())
        self.assertEqual(len(self.fake.writes_made()), 1)

    def test_board_write_only_for_configured_board(self):
        c = self.client()
        with self.assertRaises(WriteBlocked) as cm:
            c.board_set_status("acme", 7, "PVTI_1", "PVTSSF_1", "opt_prog")
        self.assertIsNone(cm.exception.decision.allow_cmd)
        self.assertEqual(self.fake.calls, [])
        c.board_set_status("acme", 6, "PVTI_1", "PVTSSF_1", "opt_prog")
        self.assertEqual(self.fake.state["board"]["items"][0]["status"], "In progress")

    def test_board_set_status_is_a_write_argv(self):
        self.client().board_set_status("acme", 6, "PVTI_1", "PVTSSF_1", "opt_rev")
        mutation = self.fake.calls[-1]
        self.assertFalse(is_read(mutation))
        self.assertTrue(is_read(self.fake.calls[0]))

    def move_thread_to_other_repo(self):
        thread = {"id": "PRRT_9", "isResolved": False, "comments": [
            {"id": "PRRC_9", "databaseId": 909, "author": "hubot", "authorAssociation": "MEMBER", "body": "x",
             "path": "a.py", "line": 1, "url": "u"}]}
        pr = dict(self.fake.state["repos"]["acme/app"]["things"]["3"], number=1, threads=[thread])
        self.fake.state["repos"]["acme/other"]["things"]["1"] = pr

    def test_resolve_thread_is_a_write_gated_on_the_threads_repo(self):
        c = self.client()
        c.resolve_thread("PRRT_1")
        lookup, mutation = self.fake.calls[-2], self.fake.calls[-1]
        self.assertTrue(is_read(lookup))
        self.assertFalse(is_read(mutation))
        self.assertTrue(self.fake.state["repos"]["acme/app"]["things"]["3"]["threads"][0]["isResolved"])
        self.move_thread_to_other_repo()
        with self.assertRaises(WriteBlocked) as cm:
            c.resolve_thread("PRRT_9")
        self.assertEqual(cm.exception.decision.allow_cmd, "/not-me:allow-repo acme/other")
        self.assertFalse(self.fake.state["repos"]["acme/other"]["things"]["1"]["threads"][0]["isResolved"])

    def test_resolve_thread_dry_run_does_not_run_the_mutation(self):
        out = self.client(dry_run=True).resolve_thread("PRRT_1")
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.fake.writes_made(), [])
        self.assertEqual(len(self.ledger.read()), 1)
        self.assertFalse(self.fake.state["repos"]["acme/app"]["things"]["3"]["threads"][0]["isResolved"])

    def test_resolve_thread_unknown_thread_or_bad_id(self):
        with self.assertRaises(GhError):
            self.client().resolve_thread("PRRT_missing")
        with self.assertRaises(ValueError):
            self.client().resolve_thread("PRRT_1 -f query=x")


class ArgvTargetTests(Base):
    def test_mismatched_repo_in_argv_is_refused(self):
        c = self.client()
        target = WriteTarget.repo("acme/app")
        for argv in (["issue", "comment", "1", "-R", "acme/other", "--body-file", "x"],
                     ["issue", "comment", "1", "--repo", "acme/other"],
                     ["issue", "comment", "1", "--repo=acme/other"],
                     ["api", "-X", "POST", "repos/acme/other/issues/1/comments"],
                     ["api", "-X", "POST", "/repos/acme/other/issues/1/comments?x=1"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                c.write(target, argv)
        self.assertEqual(self.fake.calls, [])

    def test_matching_repo_is_fine_case_insensitively(self):
        self.client(dry_run=True).write(WriteTarget.repo("acme/app"), ["issue", "comment", "1", "-R", "ACME/App"])

    def test_denied_command_cannot_be_written(self):
        with self.assertRaises(ValueError):
            self.client().write(WriteTarget.repo("acme/app"), ["secret", "set", "X", "-R", "acme/app"])
        self.assertEqual(self.fake.calls, [])


class ValidationTests(Base):
    def test_numeric_ids_are_ints(self):
        c = self.client()
        for call in (lambda: c.issue("acme/app", "1 --web"), lambda: c.labels("acme/app", "1; rm"),
                     lambda: c.pr("acme/app", "-1x"), lambda: c.comment_update("acme/app", "5 -f x", self.body()),
                     lambda: c.reply_thread("acme/app", 3, "1/../x", self.body()),
                     lambda: c.review_threads("acme/app", "1){x}"), lambda: c.board_item_status("acme", "6){x"),
                     lambda: c.labels_set("acme/app", "1 x", []), lambda: c.request_review("acme/app", "x", [])):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(c.issue("acme/app", "1")["number"], 1)

    def test_node_ids_and_owner(self):
        c = self.client()
        for bad in ("a b", "x\ny", "a;b", "", "$(x)"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                c.board_set_status("acme", 6, bad, "PVTSSF_1", "opt_prog")
        for bad in ("ac me", "acme/x", "acme\n", ""):
            with self.subTest(owner=bad), self.assertRaises(ValueError):
                c.board_item_status(bad, 6)
        self.assertEqual(self.fake.calls, [])

    def test_graphql_variables_are_plain_f_strings(self):
        self.client().review_threads("acme/app", 3)
        argv = self.fake.calls[-1]
        self.assertNotIn("-F", argv)
        self.assertIn("owner=acme", argv)
        self.assertIn("pullRequest(number:3)", argv[3])

    def test_bad_repo_slug_and_logins(self):
        c = self.client()
        for bad in ("-R/x", "acme/-x", "-acme/app", "acme/app --web", "acme", "a/b/c"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                c.issue(bad, 1)
        with self.assertRaises(ValueError):
            c.request_review("acme/app", 3, ["--admin"])
        with self.assertRaises(ValueError):
            c.pr_create("acme/app", "feat/x", "main", "t", self.body(), reviewers=["a b"])


class ReviewEventTests(Base):
    def test_approve_needs_explicit_opt_in(self):
        c = self.client()
        with self.assertRaises(ValueError):
            c.review_post("acme/app", 3, "APPROVE", self.body())
        self.assertEqual(self.fake.calls, [])
        c.review_post("acme/app", 3, "APPROVE", self.body(), allow_approve=True)
        self.assertEqual(self.fake.state["repos"]["acme/app"]["things"]["3"]["reviews"][0]["event"], "APPROVE")

    def test_other_events_refused(self):
        for event in ("approve", "DISMISS", "PENDING", ""):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.client().review_post("acme/app", 3, event, self.body(), allow_approve=True)
        for event in ("COMMENT", "REQUEST_CHANGES"):
            self.client().review_post("acme/app", 3, event, self.body())


class BodyPlaceholderTests(Base):
    def test_title_and_labels_never_touched(self):
        body = self.body("B")
        self.client().pr_create("acme/app", "feat/x", "main", "fix {body} and @{body}", body, labels=["@{body}"])
        argv = self.fake.calls[-1]
        self.assertEqual(argv[argv.index("--title") + 1], "fix {body} and @{body}")
        self.assertEqual(argv[argv.index("--label") + 1], "@{body}")
        self.assertEqual(argv[argv.index("--body-file") + 1], str(body.resolve()))

    def test_api_body_path_replaces_only_the_field(self):
        c = self.client()
        c.comment_create("acme/app", 1, self.body("x"))
        cid = c.comments("acme/app", 1)[0]["id"]
        c.comment_update("acme/app", cid, self.body("y", "b2.md"))
        argv = self.fake.calls[-1]
        self.assertEqual(argv[-2:], ["-F", f"body=@{(self.project / 'b2.md').resolve()}"])


class DryRunTests(Base):
    def test_default_from_mode_and_env(self):
        self.assertFalse(GhClient(self.ctx, runner=self.fake).dry_run)
        dry = GuardContext(allowed_repos=("acme/app",), mode="dry-run")
        self.assertTrue(GhClient(dry, runner=self.fake).dry_run)
        with mock.patch.dict(os.environ, {"NOT_ME_DRY_RUN": "1"}):
            live = GuardContext(allowed_repos=("acme/app",), mode="live")
            self.assertTrue(live.dry_run)
            self.assertTrue(GhClient(live, runner=self.fake).dry_run)
        self.assertFalse(GuardContext(allowed_repos=("acme/app",), mode="live").dry_run)
        self.assertFalse(GhClient(dry, runner=self.fake, dry_run=False).dry_run)

    def test_dry_run_ledgers_payload_and_runs_nothing(self):
        body = self.body("Would post this.\n")
        out = self.client(dry_run=True).comment_create("acme/app", 1, body)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["would"][:2], ["issue", "comment"])
        self.assertEqual(self.fake.calls, [])
        [entry] = self.ledger.read()
        self.assertEqual(entry["target"], "acme/app")
        copied = self.ledger.path.parent / entry["payload_path"]
        self.assertEqual(copied.read_text(), "Would post this.\n")
        self.assertEqual(self.fake.state["repos"]["acme/app"]["things"]["1"]["comments"], [])

    def test_dry_run_labels_set_skips_readback(self):
        out = self.client(dry_run=True).labels_set("acme/app", 1, ["bug"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.fake.calls, [])
        [entry] = self.ledger.read()
        self.assertEqual(json.loads((self.ledger.path.parent / entry["payload_path"]).read_text()), {"labels": ["bug"]})

    def test_dry_run_json_payload_makes_no_temp_file(self):
        before = set(ghmod.notme_tmp_dir().glob("*"))
        self.client(dry_run=True).review_post("acme/app", 3, "COMMENT", self.body("Nice."))
        self.assertEqual(set(ghmod.notme_tmp_dir().glob("*")), before)
        [entry] = self.ledger.read()
        payload = json.loads((self.ledger.path.parent / entry["payload_path"]).read_text())
        self.assertEqual((payload["event"], payload["body"]), ("COMMENT", "Nice."))

    def test_default_ledger_lives_in_git_common_dir(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        c = GhClient(self.ctx, runner=self.fake, dry_run=True)
        c.comment_create("acme/app", 1, self.body())
        ledger = Ledger.default_for(self.project)
        self.assertEqual(len(ledger.read()), 1)


class LabelTests(Base):
    def test_put_then_readback_no_issue_edit(self):
        out = self.client().labels_set("acme/app", 1, ["in progress", "bug"])
        self.assertEqual(sorted(out), ["bug", "in progress"])
        self.assertEqual(sorted(self.fake.state["repos"]["acme/app"]["things"]["1"]["labels"]), ["bug", "in progress"])
        argvs = self.fake.calls
        put = next(a for a in argvs if "PUT" in a)
        self.assertIn("repos/acme/app/issues/1/labels", put)
        self.assertIn("--input", put)
        self.assertEqual(argvs.index(put), len(argvs) - 2)
        self.assertEqual(argvs[-1], ["api", "repos/acme/app/issues/1/labels?per_page=100"])
        self.assertFalse(any(a[:2] == ["issue", "edit"] for a in argvs))

    def test_empty_set_clears(self):
        self.assertEqual(self.client().labels_set("acme/app", 1, []), [])

    def test_readback_mismatch_when_adds_are_dropped(self):
        self.fake.drop_adds = True
        with self.assertRaises(ReadbackMismatch) as cm:
            self.client().labels_set("acme/app", 1, ["to do", "bug"])
        self.assertEqual(cm.exception.expected, ["bug", "to do"])
        self.assertEqual(cm.exception.actual, ["to do"])

    def test_temp_payload_file_removed_and_private(self):
        seen = []
        real = self.fake.__call__

        def spy(argv):
            if "--input" in argv:
                path = Path(argv[argv.index("--input") + 1])
                seen.append((path.parent, path.stat().st_mode & 0o777))
            return real(argv)

        client = GhClient(self.ctx, runner=spy, dry_run=False, ledger=self.ledger)
        client.labels_set("acme/app", 1, ["bug"])
        self.assertEqual(seen, [(ghmod.notme_tmp_dir(), 0o600)])
        self.assertEqual(list(ghmod.notme_tmp_dir().glob("not-me-*.json")), [])


class PrWriteTests(Base):
    def test_reply_thread_uses_rest_replies_endpoint(self):
        self.client().reply_thread("acme/app", 3, 501, self.body("Renamed."))
        argv = self.fake.calls[-1]
        self.assertEqual(argv[:4], ["api", "-X", "POST", "repos/acme/app/pulls/3/comments/501/replies"])
        thread = self.fake.state["repos"]["acme/app"]["things"]["3"]["threads"][0]
        self.assertEqual(thread["comments"][-1]["body"], "Renamed.")

    def test_comment_update(self):
        c = self.client()
        c.comment_create("acme/app", 1, self.body("one"))
        cid = c.comments("acme/app", 1)[0]["id"]
        c.comment_update("acme/app", cid, self.body("two", "b2.md"))
        self.assertEqual(c.comments("acme/app", 1)[0]["body"], "two")
        self.assertIn(f"repos/acme/app/issues/comments/{cid}", self.fake.calls[-2])

    def test_review_post_via_input_json(self):
        comments = self.project / "c.json"
        comments.write_text(json.dumps([{"path": "a.py", "line": 1, "body": "x"}]), encoding="utf-8")
        self.client().review_post("acme/app", 3, "REQUEST_CHANGES", self.body("Please fix."), comments)
        argv = self.fake.calls[-1]
        self.assertIn("repos/acme/app/pulls/3/reviews", argv)
        self.assertIn("--input", argv)
        [review] = self.fake.state["repos"]["acme/app"]["things"]["3"]["reviews"]
        self.assertEqual(review["event"], "REQUEST_CHANGES")
        self.assertEqual(review["body"], "Please fix.")
        self.assertEqual(review["comments"][0]["path"], "a.py")

    def test_request_review(self):
        self.client().request_review("acme/app", 3, ["hubot", "monalisa"])
        self.assertEqual(self.fake.state["repos"]["acme/app"]["things"]["3"]["reviewRequests"], ["hubot", "monalisa"])

    def test_pr_create_returns_url_and_number(self):
        out = self.client().pr_create("acme/app", "feat/x", "main", "Add x", self.body("Body"),
                                      labels=["needs review"], reviewers=["hubot"], assignee="octocat", draft=True)
        self.assertEqual(out, {"url": "https://github.com/acme/app/pull/4", "number": 4})
        pr = self.fake.state["repos"]["acme/app"]["things"]["4"]
        self.assertTrue(pr["isDraft"])
        self.assertEqual((pr["labels"], pr["reviewRequests"], pr["assignees"]), (["needs review"], ["hubot"], ["octocat"]))

    def test_pr_create_dry_run(self):
        out = self.client(dry_run=True).pr_create("acme/app", "feat/x", "main", "Add x", self.body("Body"))
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.fake.calls, [])

    def test_pr_edit_body(self):
        self.client().pr_edit_body("acme/app", 3, self.body("New body"))
        self.assertEqual(self.fake.state["repos"]["acme/app"]["things"]["3"]["body"], "New body")


class InputFileSafetyTests(Base):
    def test_outside_project_and_tmp_refused(self):
        with self.assertRaises(UnsafePath):
            self.client().comment_create("acme/app", 1, Path("/etc/hosts"))
        self.assertEqual(self.fake.calls, [])

    def test_traversal_refused(self):
        sneaky = self.project / ".." / ".." / ".." / ".." / "etc" / "hosts"
        with self.assertRaises(UnsafePath):
            self.client().comment_create("acme/app", 1, sneaky)

    def test_env_file_refused_even_inside_project(self):
        for name in (".env", ".env.local"):
            path = self.project / name
            path.write_text("TOKEN=x\n", encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(UnsafePath):
                self.client().comment_create("acme/app", 1, path)
        self.assertEqual(self.fake.calls, [])

    def test_symlink_escape_refused(self):
        link = self.project / "link.md"
        try:
            link.symlink_to(Path("/etc/hosts"))
        except OSError:
            self.skipTest("no symlinks")
        with self.assertRaises(UnsafePath):
            self.client().comment_create("acme/app", 1, link)

    def test_write_payload_path_checked_too(self):
        with self.assertRaises(UnsafePath):
            self.client().write(WriteTarget.repo("acme/app"), ["issue", "comment", "1"], payload_path="/etc/hosts")

    def test_notme_tmp_dir_file_allowed_and_private(self):
        tmp = ghmod.notme_tmp_dir()
        self.assertEqual(tmp.stat().st_mode & 0o777, 0o700)
        self.assertEqual(tmp.name, f"not-me-{os.getuid()}")
        path = tmp / "ok-body.md"
        path.write_text("ok", encoding="utf-8")
        self.addCleanup(path.unlink)
        self.client().comment_create("acme/app", 1, path)
        self.assertEqual(len(self.fake.calls), 1)

    def test_bare_system_tmp_file_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, dir=tempfile.gettempdir()) as fh:
            fh.write("x")
        self.addCleanup(os.unlink, fh.name)
        with self.assertRaises(UnsafePath):
            self.client().comment_create("acme/app", 1, fh.name)

    def test_secret_looking_names_refused(self):
        for name in (".env", ".env.local", "prod.env", "server.pem", "tls.key", "id_rsa", "id_ed25519.pub", ".npmrc",
                     ".netrc", "auth.json", "credentials", "credentials.json", "CREDENTIALS.txt"):
            path = self.project / name
            path.write_text("secret", encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(UnsafePath):
                ghmod.check_input_path(path, self.project)

    def test_git_dir_refused(self):
        (self.project / ".git").mkdir()
        path = self.project / ".git" / "config"
        path.write_text("x", encoding="utf-8")
        hooks = self.project / ".git" / "notes.md"
        hooks.write_text("x", encoding="utf-8")
        for p in (path, hooks):
            with self.subTest(p=p.name), self.assertRaises(UnsafePath):
                ghmod.check_input_path(p, self.project)

    def test_hard_linked_file_refused(self):
        original = self.body("secret")
        link = self.project / "linked.md"
        os.link(original, link)
        with self.assertRaises(UnsafePath):
            ghmod.check_input_path(link, self.project)

    def test_ordinary_project_file_allowed(self):
        self.assertEqual(ghmod.check_input_path(self.body(), self.project), (self.project / "body.md").resolve())


class SubprocessTests(Base):
    def test_default_runner_end_to_end(self):
        state = self.dir / "state.json"
        calls = self.dir / "calls.jsonl"
        env = {"PATH": f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}", "FAKEGH_STATE": str(state),
               "FAKEGH_CALLS": str(calls)}
        with mock.patch.dict(os.environ, env):
            c = GhClient(self.ctx, dry_run=False, ledger=self.ledger)
            self.assertEqual(c.me(), "octocat")
            c.comment_create("acme/app", 1, self.body("from subprocess"))
            self.assertEqual(c.comments("acme/app", 1)[0]["body"], "from subprocess")
            self.assertEqual(sorted(c.labels_set("acme/app", 2, ["bug"])), ["bug"])
            with self.assertRaises(GhError):
                c.issue("acme/app", 404)
        logged = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(logged[0], ["api", "user"])
        self.assertTrue(any(a[:2] == ["issue", "comment"] for a in logged))
        saved = json.loads(state.read_text())
        self.assertEqual(saved["repos"]["acme/app"]["things"]["2"]["labels"], ["bug"])

    def test_missing_gh_binary_is_a_gherror(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.dir)}):
            with self.assertRaises(GhError):
                GhClient(self.ctx, dry_run=False).me()

    def test_shim_is_executable(self):
        self.assertTrue(os.access(FAKE_BIN / "gh", os.X_OK))


if __name__ == "__main__":
    unittest.main()
