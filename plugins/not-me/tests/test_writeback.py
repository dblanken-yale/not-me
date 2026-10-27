from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from notme import gh as ghmod
from notme import writeback as wb
from notme.dryrun import Ledger
from notme.gh import GhClient
from notme.writes import GuardContext
from tests import TempCase
from tests.fakes.fakegh import FakeRunner

REPO = "acme/app"
RUN = "20260101-abc"


def things(fake, repo=REPO):
    return fake.state["repos"][repo]["things"]


class Base(unittest.TestCase):
    def setUp(self):
        self.case = TempCase()
        self.addCleanup(self.case.cleanup)
        self.fake = FakeRunner()
        self.ctx = GuardContext(origin_repo=REPO, allowed_repos=(REPO, "acme/app-ui", "acme/other"), mode="live",
                                project_dir=self.case.project)
        self.gh = GhClient(self.ctx, runner=self.fake, dry_run=False)
        self.cfg = self.case.load()

    def comment_bodies(self, n=1):
        return [c["body"] for c in things(self.fake)[str(n)]["comments"]]

    def tmp_leftovers(self):
        return list(ghmod.notme_tmp_dir().glob("not-me-wb-*"))


class CommentUpsertTests(Base):
    def test_second_call_patches_and_leaves_one_comment(self):
        first = wb.comment_upsert(self.gh, REPO, 1, "claim", "first text", RUN)
        second = wb.comment_upsert(self.gh, REPO, 1, "claim", "second text", "run-2")
        self.assertTrue(first["created"])
        self.assertTrue(second["updated"])
        self.assertEqual(first["id"], second["id"])
        bodies = self.comment_bodies()
        self.assertEqual(len(bodies), 1)
        self.assertIn("second text", bodies[0])
        self.assertIn("<!-- not-me:claim -->", bodies[0])
        self.assertIn("<!-- not-me:run=run-2 -->", bodies[0])
        self.assertNotIn("first text", bodies[0])
        self.assertEqual(self.tmp_leftovers(), [])

    def test_legacy_marker_is_recognised_and_migrated(self):
        self.fake.state["repos"][REPO]["things"]["1"]["comments"].append(
            {"id": 77, "user": "octocat", "assoc": "OWNER", "body": "old <!-- old-bot:status -->",
             "created_at": "2025-01-01T00:00:00Z"})
        out = wb.comment_upsert(self.gh, REPO, 1, "claim", "new", RUN, legacy=["<!-- old-bot:status -->"])
        self.assertEqual(out, {"id": 77, "updated": True})
        self.assertEqual(len(self.comment_bodies()), 1)
        self.assertIn("<!-- not-me:claim -->", self.comment_bodies()[0])

    def test_ignores_other_authors_and_other_kinds(self):
        comments = things(self.fake)["1"]["comments"]
        comments.append({"id": 5, "user": "hubot", "assoc": "MEMBER", "body": "<!-- not-me:claim -->",
                         "created_at": "2025-01-01T00:00:00Z"})
        comments.append({"id": 6, "user": "octocat", "assoc": "OWNER", "body": "<!-- not-me:other -->",
                         "created_at": "2025-01-01T00:00:00Z"})
        out = wb.comment_upsert(self.gh, REPO, 1, "claim", "mine", RUN)
        self.assertTrue(out["created"])
        self.assertEqual(len(comments), 3)

    def test_newest_own_comment_wins(self):
        comments = things(self.fake)["1"]["comments"]
        for cid, ts in ((10, "2025-01-01T00:00:00Z"), (11, "2025-06-01T00:00:00Z")):
            comments.append({"id": cid, "user": "octocat", "assoc": "OWNER", "body": "<!-- not-me:claim -->",
                             "created_at": ts})
        self.assertEqual(wb.find_own_comment(self.gh, REPO, 1, "claim")["id"], 11)
        self.assertIsNone(wb.find_own_comment(self.gh, REPO, 1, "missing"))

    def test_invalid_kind_or_run_id_refused(self):
        with self.assertRaises(ValueError):
            wb.comment_upsert(self.gh, REPO, 1, "a --> b", "x", RUN)
        with self.assertRaises(ValueError):
            wb.comment_upsert(self.gh, REPO, 1, "claim", "x", "bad id -->")


class ReplyTests(Base):
    def test_reply_adds_run_marker_and_never_resolves(self):
        wb.reply_thread(self.gh, REPO, 3, 501, "Renamed it.", RUN)
        thread = things(self.fake)["3"]["threads"][0]
        self.assertEqual(len(thread["comments"]), 2)
        self.assertIn(f"<!-- not-me:run={RUN} -->", thread["comments"][1]["body"])
        self.assertFalse(thread["isResolved"])
        self.assertFalse([e for e in self.fake.state["timeline"] if e["event"] == "thread_resolved"])
        self.assertFalse([c for c in self.fake.calls if any("resolveReviewThread" in a for a in c)])

    def test_resolve_fixed_threads_resolves_listed_only(self):
        self.assertEqual(wb.resolve_fixed_threads(self.gh, REPO, 3, ["PRRT_1"]), ["PRRT_1"])
        self.assertTrue(things(self.fake)["3"]["threads"][0]["isResolved"])

    def test_resolve_refuses_thread_not_on_this_pr(self):
        with self.assertRaises(ValueError):
            wb.resolve_fixed_threads(self.gh, REPO, 3, ["PRRT_1", "PRRT_other"])
        self.assertFalse(things(self.fake)["3"]["threads"][0]["isResolved"])


class SummaryTests(unittest.TestCase):
    def test_shape(self):
        text = wb.summary("status", "Two things done.", {"Done": ["a", "b"]}, ["alice", "@bob"], RUN)
        lines = text.split("\n")
        self.assertEqual(lines[0], "**TL;DR:** Two things done.")
        self.assertIn("**Waiting on:**", text)
        self.assertIn("- @alice\n- @bob", text)
        self.assertIn("### Done\n\n- a\n- b", text)
        self.assertEqual(text.rstrip().split("\n")[-2:], ["<!-- not-me:status -->", f"<!-- not-me:run={RUN} -->"])
        self.assertLess(text.index("Waiting on"), text.index("### Done"))

    def test_waiting_on_never_collapsed_even_when_long(self):
        text = wb.summary("status", "x", {"Big": ["i"] * 40}, [f"user{i}" for i in range(20)], RUN)
        waiting = text.split("**Waiting on:**")[1].split("<details>")[0]
        self.assertEqual(waiting.count("- @user"), 20)

    def test_nobody_waiting(self):
        self.assertIn("**Waiting on:** nobody", wb.summary("status", "x", {}, None, RUN))

    def test_long_section_collapsed_with_blank_line(self):
        text = wb.summary("status", "x", {"Log": "\n".join(f"line {i}" for i in range(30)), "Short": "one"},
                          None, RUN)
        self.assertRegex(text, r"<details><summary>Log \(30 lines\)</summary>\n\nline 0\n")
        self.assertIn("line 29\n\n</details>", text)
        self.assertIn("### Short", text)
        self.assertEqual(wb.check_text(text, is_summary=True, tracker=REPO, repo=REPO), [])

    def test_exactly_fifteen_lines_not_collapsed(self):
        text = wb.summary("status", "x", {"Log": ["i"] * 15}, None, RUN)
        self.assertNotIn("<details>", text)

    def test_no_assistant_mentions(self):
        text = wb.summary("status", "All green.", {"Done": ["a"]}, ["alice"], RUN)
        self.assertEqual([p for p in wb.check_text(text, is_summary=True, tracker=REPO, repo=REPO)], [])

    def test_empty_sections_skipped_and_status_single_line(self):
        text = wb.summary("status", "a\nb", {"Empty": [], "None": None}, None, RUN)
        self.assertTrue(text.startswith("**TL;DR:** a b\n"))
        self.assertNotIn("Empty", text)


class AttributionTests(Base):
    def set_attr(self, value):
        self.cfg.meta["project"]["attribution"] = value

    def test_modes(self):
        self.set_attr("inherit")
        self.assertEqual(wb.attribution(self.cfg), "")
        self.set_attr("none")
        self.assertEqual(wb.attribution(self.cfg), "")
        self.set_attr("Prepared by the team bot")
        self.assertEqual(wb.attribution(self.cfg), "Prepared by the team bot")

    def test_apply_appends_on_own_line_only_when_set(self):
        self.set_attr("inherit")
        self.assertEqual(wb.apply_attribution("body\n", self.cfg), "body\n")
        self.set_attr("Prepared by the team bot")
        self.assertEqual(wb.apply_attribution("body\n", self.cfg), "body\n\nPrepared by the team bot\n")

    def test_comments_never_carry_attribution(self):
        self.set_attr("Prepared by the team bot")
        wb.comment_upsert(self.gh, REPO, 1, "claim", "hello", RUN)
        self.assertNotIn("Prepared by the team bot", self.comment_bodies()[0])


class PrBodyTests(Base):
    def test_default_template_sections(self):
        title, body = wb.pr_body(self.cfg, 12, "Add widget", "Adds a widget.", ["Open the page", "Click it"],
                                 [{"repo": "acme/app-ui", "number": 4, "url": "https://example.test/4"}],
                                 ["acme/app-ui#9"], repo=REPO)
        self.assertEqual(title, "12: Add widget")
        for heading in ("## Summary", "## Testing steps", "## Companion PRs", "## References"):
            self.assertIn(heading, body)
        self.assertIn("- [ ] Open the page\n- [ ] Click it", body)
        self.assertIn("- [acme/app-ui#4](https://example.test/4)", body)
        self.assertIn(f"Closes {REPO}#12", body)
        self.assertIn("**References:** acme/app-ui#9", body)
        self.assertIn("<!-- not-me:new -->", body)
        self.assertNotIn("<details>", body)

    def test_closing_keyword_auto_when_tracker_elsewhere(self):
        self.cfg.meta["project"]["tracker"] = "acme/board"
        _, body = wb.pr_body(self.cfg, 12, "T", "d", [], repo=REPO, refs=["#30"])
        self.assertIn("Refs acme/board#12", body)
        self.assertNotIn("Closes", body)
        self.assertIn("acme/board#30", body)
        self.assertEqual(wb.check_text(body, tracker="acme/board", repo=REPO), [])

    def test_closing_keyword_auto_when_tracker_is_this_repo(self):
        self.cfg.meta["project"]["tracker"] = REPO
        _, body = wb.pr_body(self.cfg, 12, "T", "d", [], repo=REPO)
        self.assertIn(f"Closes {REPO}#12", body)
        self.assertNotRegex(body, r"Closes #\d")  # never a bare number, even in the same repo

    def test_explicit_keyword_and_title_template(self):
        self.cfg.meta["standards"]["closing_keyword"] = "Fixes"
        self.cfg.meta["standards"]["pr_title"] = "[{issue}] {issue_title}"
        title, body = wb.pr_body(self.cfg, 5, "Thing", "d", [], repo=REPO)
        self.assertEqual(title, "[5] Thing")
        self.assertIn(f"Fixes {REPO}#5", body)

    def test_attribution_is_last(self):
        self.cfg.meta["project"]["attribution"] = "Prepared by the team bot"
        _, body = wb.pr_body(self.cfg, 1, "T", "d", [], repo=REPO)
        self.assertTrue(body.rstrip().endswith("<!-- not-me:new -->\n\nPrepared by the team bot"))

    def write_template(self, text):
        path = self.case.project / ".github" / "pull_request_template.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")

    def test_repo_template_headings_filled(self):
        self.write_template("## Description\n\n<!-- what -->\n\n## Test plan\n\n- [ ] \n\n## Screenshots\n\nn/a\n")
        _, body = wb.pr_body(self.cfg, 2, "T", "My change.", ["Step one"], repo=REPO)
        self.assertIn("## Description\n\nMy change.\n\n## Test plan\n\n- [ ] Step one\n\n## Screenshots\n\nn/a", body)
        self.assertNotIn("<!-- what -->", body)
        self.assertNotIn("## Summary", body)
        self.assertNotIn("## Companion PRs", body)
        self.assertIn(f"Closes {REPO}#2", body)

    def test_repo_template_without_matching_headings_is_appended(self):
        self.write_template("## Checklist\n\n- [ ] done\n")
        _, body = wb.pr_body(self.cfg, 2, "T", "My change.", ["Step one"],
                             [{"repo": "acme/app-ui", "number": 4}], repo=REPO)
        self.assertTrue(body.startswith("## [2: T](https://github.com/acme/app/issues/2)\n\n"
                                        "## Checklist\n\n- [ ] done\n\n## Summary\n\nMy change."))
        self.assertIn("## Testing steps\n\n- [ ] Step one", body)
        self.assertIn("## Companion PRs\n\n- acme/app-ui#4", body)

    def test_explicit_template_path_and_missing_template(self):
        (self.case.project / "tpl.md").write_text("## Summary\n\nx\n", encoding="utf-8")
        self.cfg.meta["standards"]["pr_template"] = "tpl.md"
        _, body = wb.pr_body(self.cfg, 2, "T", "Mine.", [], repo=REPO)
        self.assertTrue(body.startswith("## [2: T](https://github.com/acme/app/issues/2)\n\n## Summary\n\nMine."))
        self.cfg.meta["standards"]["pr_template"] = "nope.md"
        _, body = wb.pr_body(self.cfg, 2, "T", "Mine.", [], repo=REPO)
        self.assertIn("## Testing steps", body)

    def test_template_outside_project_refused(self):
        outside = Path(tempfile.mkdtemp()) / "t.md"
        outside.write_text("## Summary\n", encoding="utf-8")
        self.cfg.meta["standards"]["pr_template"] = str(outside)
        with self.assertRaises(ghmod.UnsafePath):
            wb.pr_body(self.cfg, 2, "T", "Mine.", [], repo=REPO)


class PrBodyHeadingTests(Base):
    def write_template(self, text):
        path = self.case.project / ".github" / "pull_request_template.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")

    def test_same_repo_tracker_heading_is_first_line(self):
        _, body = wb.pr_body(self.cfg, 12, "Add widget", "Adds a widget.", ["Open it"], repo=REPO)
        self.assertTrue(body.startswith(
            "## [12: Add widget](https://github.com/acme/app/issues/12)\n\n## Summary\n\nAdds a widget."))
        self.assertIn(f"## References\n\nCloses {REPO}#12", body)

    def test_other_repo_tracker_links_to_tracker(self):
        self.cfg.meta["project"]["tracker"] = "acme/tracker"
        _, body = wb.pr_body(self.cfg, 7, "Fix it", "d", [], repo=REPO)
        self.assertEqual(body.split("\n")[0], "## [7: Fix it](https://github.com/acme/tracker/issues/7)")
        self.assertIn("## References\n\nRefs acme/tracker#7", body)

    def test_heading_text_ignores_title_template(self):
        self.cfg.meta["standards"]["pr_title"] = "[{issue}] {issue_title}"
        title, body = wb.pr_body(self.cfg, 5, "Thing", "d", [], repo=REPO)
        self.assertEqual(title, "[5] Thing")
        self.assertEqual(body.split("\n")[0], "## [5: Thing](https://github.com/acme/app/issues/5)")

    def test_heading_above_repo_template(self):
        self.write_template("## Checklist\n\n- [ ] done\n")
        _, body = wb.pr_body(self.cfg, 2, "T", "My change.", ["Step one"], repo=REPO)
        self.assertTrue(body.startswith(
            "## [2: T](https://github.com/acme/app/issues/2)\n\n## Checklist\n\n- [ ] done\n\n## Summary"))

    def test_heading_above_template_with_matching_headings(self):
        self.write_template("## Description\n\n<!-- what -->\n\n## Test plan\n\n- [ ] \n")
        _, body = wb.pr_body(self.cfg, 2, "T", "My change.", ["Step one"], repo=REPO)
        self.assertTrue(body.startswith(
            "## [2: T](https://github.com/acme/app/issues/2)\n\n## Description\n\nMy change.\n\n## Test plan"))

    def test_no_issue_no_heading_and_no_closing_line(self):
        title, body = wb.pr_body(self.cfg, None, "Tidy docs", "d", ["One"], refs=["acme/app-ui#9"], repo=REPO)
        self.assertTrue(body.startswith("## Summary\n\nd"))
        self.assertNotIn("https://github.com/", body)
        self.assertNotRegex(body, r"(?m)^(Closes|Refs) ")
        self.assertIn("## References\n\n**References:** acme/app-ui#9", body)
        self.assertEqual(title, "Tidy docs")
        self.assertEqual(wb.check_text(body, tracker=REPO, repo="acme/app-ui"), [])

    def test_no_issue_and_no_refs_drops_references_section(self):
        _, body = wb.pr_body(self.cfg, None, "Tidy docs", "d", [], repo=REPO)
        self.assertNotIn("## References", body)
        self.assertIn("<!-- not-me:new -->", body)

    def test_no_repo_or_tracker_no_heading(self):
        _, body = wb.pr_body(self.cfg, 12, "T", "d", [])
        self.assertTrue(body.startswith("## Summary"))
        self.assertIn("Closes #12", body)

    def test_unsafe_tracker_no_heading(self):
        self.cfg.meta["project"]["tracker"] = "acme/tracker)evil"
        _, body = wb.pr_body(self.cfg, 12, "T", "d", [], repo=REPO)
        self.assertTrue(body.startswith("## Summary"))

    def test_title_is_escaped_and_newlines_collapsed(self):
        raw = "Fix [a] \\ `b` <c> *d* _e_ @octocat #5 & ok\nsecond line"
        _, body = wb.pr_body(self.cfg, 3, raw, "d", [], repo=REPO)
        first = body.split("\n")[0]
        self.assertEqual(
            first,
            "## [3: Fix \\[a\\] \\\\ \\`b\\` \\<c\\> \\*d\\* \\_e\\_ \\@octocat \\#5 \\& ok second line]"
            "(https://github.com/acme/app/issues/3)")
        # exactly one unescaped ] closes the link text, right before the URL
        unescaped = [m.start() for m in re.finditer(r"(?<!\\)(?:\\\\)*\]", first)]
        self.assertEqual(len(unescaped), 1)
        self.assertTrue(first[unescaped[0]:].startswith("](https://github.com/acme/app/issues/3)"))

    def test_body_with_heading_passes_check_text(self):
        _, body = wb.pr_body(self.cfg, 12, "Add widget", "Adds a widget.", ["Open it"], repo=REPO)
        self.assertEqual(wb.check_text(body, tracker=REPO, repo=REPO), [])
        self.cfg.meta["project"]["tracker"] = "acme/tracker"
        _, body = wb.pr_body(self.cfg, 12, "Add widget", "Adds a widget.", ["Open it"], repo=REPO)
        self.assertEqual(wb.check_text(body, tracker="acme/tracker", repo=REPO), [])
        _, body = wb.pr_body(self.cfg, 12, "Follow up on #5", "d", [], repo=REPO)
        self.assertEqual(wb.check_text(body, tracker="acme/tracker", repo=REPO), [])

    def test_escaped_hash_in_body_text_is_still_a_bare_ref(self):
        self.assertEqual(wb.check_text("fixes \\#5", tracker="acme/tracker", repo=REPO),
                         ["line 1: bare #5; write owner/repo#5"])
        self.assertEqual(wb.check_text("a\\\\#6", tracker="acme/tracker", repo=REPO),
                         ["line 1: bare #6; write owner/repo#6"])

    def test_hash_in_issue_heading_link_text_is_not_a_bare_ref(self):
        heading = "## [3: Fix #5 and \\#6](https://github.com/acme/app/issues/3)"
        self.assertEqual(wb.check_text(heading, tracker="acme/tracker", repo=REPO), [])

    def test_hash_outside_issue_heading_link_text_is_still_a_bare_ref(self):
        heading = "## [3: Fix \\#5](https://github.com/acme/app/issues/3)"
        self.assertEqual(wb.check_text(heading + " see #7\nand #8", tracker="acme/tracker", repo=REPO),
                         ["line 1: bare #7; write owner/repo#7", "line 2: bare #8; write owner/repo#8"])
        self.assertEqual(wb.check_text("\n" + heading, tracker="acme/tracker", repo=REPO),
                         ["line 2: bare #5; write owner/repo#5"])

    def test_ai_words_in_issue_heading_link_text_pass(self):
        heading = "## [3: Add data model for AI widgets](https://github.com/acme/app/issues/3)\n"
        self.assertEqual(wb.check_text(heading, tracker=REPO, repo=REPO), [])
        _, body = wb.pr_body(self.cfg, 3, "Add data model for AI widgets", "Adds it.", ["Open it"], repo=REPO)
        self.assertEqual(wb.check_text(body, tracker=REPO, repo=REPO), [])

    def test_ai_words_outside_heading_link_text_still_flagged(self):
        heading = "## [3: Add data model for AI widgets](https://github.com/acme/app/issues/3)"
        self.assertEqual(wb.check_text(heading + "\nUses an AI model.", tracker=REPO, repo=REPO),
                         ["line 2: mentions 'AI'", "line 2: mentions 'model'"])
        self.assertEqual(wb.check_text(heading + " AI", tracker=REPO, repo=REPO), ["line 1: mentions 'AI'"])
        self.assertEqual(wb.check_text("\n" + heading, tracker=REPO, repo=REPO),
                         ["line 2: mentions 'AI'", "line 2: mentions 'model'"])

    def test_ai_words_on_plain_first_line_still_flagged(self):
        self.assertEqual(wb.check_text("## Add AI widgets\n", tracker=REPO, repo=REPO), ["line 1: mentions 'AI'"])
        self.assertEqual(wb.check_text("## [3: AI](https://example.test/3)", tracker=REPO, repo=REPO),
                         ["line 1: mentions 'AI'"])

    def test_issue_heading_keeps_path_rule(self):
        line = "## [3: Fix #5 in /" + "Users/x/y](https://github.com/acme/app/issues/3)"
        problems = wb.check_text(line, tracker="acme/tracker", repo=REPO)
        self.assertEqual(problems, ["line 1: local file path"])

    def make_pr(self, **kw):
        _, body = wb.pr_body(self.cfg, 12, "Add widget", "Desc.", ["One"], repo=REPO, **kw)
        things(self.fake)["3"]["body"] = body
        return body

    HEADING = "## [12: Add widget](https://github.com/acme/app/issues/12)\n\n"

    def test_companion_upsert_keeps_heading(self):
        self.make_pr()
        wb.companion_prs_upsert(self.gh, REPO, 3, [{"repo": "acme/app-ui", "number": 4}])
        after = things(self.fake)["3"]["body"]
        self.assertTrue(after.startswith(self.HEADING + "## Summary"))
        self.assertIn("## Companion PRs\n\n- acme/app-ui#4", after)

    def test_testing_steps_append_keeps_heading(self):
        self.make_pr()
        wb.testing_steps_append(self.gh, REPO, 3, ["Two"])
        after = things(self.fake)["3"]["body"]
        self.assertTrue(after.startswith(self.HEADING + "## Summary"))
        self.assertIn("- [ ] One\n- [ ] Two", after)

    def test_set_section_keeps_heading_when_replacing_and_adding(self):
        body = self.make_pr()
        replaced = wb._set_section(body, wb._SUMMARY_HEADING, "## Summary", "New text.")
        self.assertTrue(replaced.startswith(self.HEADING + "## Summary\n\nNew text."))
        added = wb._set_section(body, r"(?i:notes)\b", "## Notes", "A note.")
        self.assertTrue(added.startswith(self.HEADING + "## Summary"))
        self.assertIn("## Notes\n\nA note.", added)

    def test_set_section_keeps_heading_when_section_missing_in_template_body(self):
        self.write_template("Free text only.\n")
        body = self.make_pr()
        self.assertTrue(body.startswith(self.HEADING + "Free text only.\n\n## Summary"))
        wb.companion_prs_upsert(self.gh, REPO, 3, [{"repo": "acme/app-ui", "number": 4}])
        self.assertTrue(things(self.fake)["3"]["body"].startswith(self.HEADING + "Free text only."))


class PrSectionTests(Base):
    def make_pr(self, **kw):
        _, body = wb.pr_body(self.cfg, 12, "T", "Desc.", ["One"], **kw, repo=REPO)
        things(self.fake)["3"]["body"] = body
        return body

    def body(self):
        return things(self.fake)["3"]["body"]

    def test_companion_upsert_replaces_only_that_section_and_is_idempotent(self):
        before = self.make_pr()
        comp = [{"repo": "acme/app-ui", "number": 4, "url": "https://example.test/4"}]
        self.assertEqual(wb.companion_prs_upsert(self.gh, REPO, 3, comp), {"updated": True})
        after = self.body()
        self.assertIn("## Companion PRs\n\n- [acme/app-ui#4](https://example.test/4)\n\n## References", after)
        self.assertEqual(after.replace("- [acme/app-ui#4](https://example.test/4)", "_No companion PRs._"), before)
        calls = len(self.fake.writes_made())
        self.assertEqual(wb.companion_prs_upsert(self.gh, REPO, 3, comp), {"unchanged": True})
        self.assertEqual(len(self.fake.writes_made()), calls)
        self.assertEqual(self.body(), after)

    def test_template_pr_keeps_closing_keyword_through_upserts(self):
        path = self.case.project / ".github" / "pull_request_template.md"
        path.parent.mkdir(parents=True)
        path.write_text("## Summary\n\nx\n\n## Testing steps\n\n- [ ] \n", encoding="utf-8")
        _, body = wb.pr_body(self.cfg, 12, "T", "Desc.", ["One"], repo=REPO, refs=["acme/app-ui#9"])
        self.assertIn(f"## References\n\nCloses {REPO}#12", body)
        things(self.fake)["3"]["body"] = body
        wb.companion_prs_upsert(self.gh, REPO, 3, [{"repo": "acme/app-ui", "number": 4}])
        self.assertIn(f"Closes {REPO}#12", self.body())
        self.assertIn("**References:** acme/app-ui#9", self.body())
        wb.testing_steps_append(self.gh, REPO, 3, ["Two"])
        after = self.body()
        self.assertLess(after.index("- [ ] Two"), after.index("## References"))
        self.assertLess(after.index("- [ ] Two"), after.index(f"Closes {REPO}#12"))
        self.assertIn("## Companion PRs\n\n- acme/app-ui#4", after)

    def test_section_ends_at_closing_keyword_line(self):
        things(self.fake)["3"]["body"] = "## Companion PRs\n\n- old\n\nCloses #12\n\n<!-- not-me:new -->"
        wb.companion_prs_upsert(self.gh, REPO, 3, [{"repo": "acme/app-ui", "number": 4}])
        self.assertEqual(self.body(), "## Companion PRs\n\n- acme/app-ui#4\n\nCloses #12\n\n<!-- not-me:new -->")

    def test_companion_section_created_before_marker_when_missing(self):
        things(self.fake)["3"]["body"] = "Plain body.\n\n<!-- not-me:new -->\n\nSigned"
        wb.companion_prs_upsert(self.gh, REPO, 3, [{"repo": "acme/app-ui", "number": 4}])
        self.assertEqual(self.body(),
                         "Plain body.\n\n## Companion PRs\n\n- acme/app-ui#4\n\n<!-- not-me:new -->\n\nSigned")

    def test_companion_section_created_at_end_without_marker(self):
        things(self.fake)["3"]["body"] = "Plain body."
        wb.companion_prs_upsert(self.gh, REPO, 3, [{"repo": "acme/app-ui", "number": 4}])
        self.assertEqual(self.body(), "Plain body.\n\n## Companion PRs\n\n- acme/app-ui#4")

    def test_testing_steps_append_adds_only_new_items(self):
        self.make_pr()
        self.assertEqual(wb.testing_steps_append(self.gh, REPO, 3, ["One", "Two", "Three"]), {"updated": True})
        self.assertIn("## Testing steps\n\n- [ ] One\n- [ ] Two\n- [ ] Three\n\n## Companion PRs", self.body())
        self.assertEqual(wb.testing_steps_append(self.gh, REPO, 3, ["Two"]), {"unchanged": True})

    def test_testing_steps_append_creates_section_and_uses_configured_heading(self):
        things(self.fake)["3"]["body"] = "Body."
        wb.testing_steps_append(self.gh, REPO, 3, ["A"])
        self.assertEqual(self.body(), "Body.\n\n## Testing steps\n\n- [ ] A")
        things(self.fake)["3"]["body"] = "Body.\n\n## QA checks\n\n- [x] old\n\n## Other\n\nx"
        self.cfg.meta["prove"]["steps_heading"] = "QA checks"
        wb.testing_steps_append(self.gh, REPO, 3, ["B"], cfg=self.cfg)
        self.assertEqual(self.body(), "Body.\n\n## QA checks\n\n- [x] old\n- [ ] B\n\n## Other\n\nx")

    def test_testing_steps_append_finds_template_testing_heading(self):
        things(self.fake)["3"]["body"] = "## Testing\n\n- [ ] first"
        wb.testing_steps_append(self.gh, REPO, 3, ["second"])
        self.assertEqual(self.body(), "## Testing\n\n- [ ] first\n- [ ] second")

    def test_headings_inside_code_fences_ignored(self):
        things(self.fake)["3"]["body"] = "```\n## Companion PRs\n```\n\n## Companion PRs\n\n- old"
        wb.companion_prs_upsert(self.gh, REPO, 3, [{"repo": "acme/app-ui", "number": 4}])
        self.assertEqual(self.body(), "```\n## Companion PRs\n```\n\n## Companion PRs\n\n- acme/app-ui#4")


class OpenPrTests(Base):
    def test_sequence_create_then_full_labels_then_review(self):
        out = wb.open_pr(self.gh, self.cfg, REPO, "feat/x", "main", "T", "Body\n", ["needs review", "bug"],
                         ["hubot"], "octocat")
        self.assertEqual(out["number"], 4)
        self.assertTrue(out["url"].endswith("/pull/4"))
        events = [e["event"] for e in self.fake.state["timeline"] if e["number"] == 4]
        self.assertEqual(events, ["pr_created", "labels_set", "review_requested"])
        pr = things(self.fake)["4"]
        self.assertEqual(sorted(pr["labels"]), ["bug", "needs review"])
        self.assertEqual(pr["reviewRequests"], ["hubot"])
        self.assertEqual(pr["assignees"], ["octocat"])
        self.assertEqual(self.tmp_leftovers(), [])

    def test_label_readback_mismatch_becomes_writeback_mismatch(self):
        self.fake.drop_adds = True
        with self.assertRaises(wb.WritebackMismatch):
            wb.open_pr(self.gh, self.cfg, REPO, "feat/x", "main", "T", "Body\n", ["new label"], [], None)

    def test_no_labels_or_reviewers_means_no_extra_calls(self):
        wb.open_pr(self.gh, self.cfg, REPO, "feat/x", "main", "T", "Body\n", [], [], None, draft=True)
        events = [e["event"] for e in self.fake.state["timeline"] if e["number"] == 4]
        self.assertEqual(events, ["pr_created"])
        self.assertTrue(things(self.fake)["4"]["isDraft"])

    def test_dry_run_records_ledger_and_writes_nothing(self):
        ledger = Ledger(self.case.root / "ledger" / "intended.jsonl")
        dry = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=ledger)
        out = wb.open_pr(dry, self.cfg, REPO, "feat/x", "main", "T", "Body\n", ["needs review"], ["hubot"], "octocat")
        self.assertTrue(out["dry_run"])
        self.assertIsNone(out["number"])
        self.assertEqual(self.fake.writes_made(), [])
        self.assertNotIn("4", things(self.fake))
        entries = ledger.read()
        self.assertEqual([e["description"] for e in entries],
                         [f"open PR feat/x -> main", f"set labels on {REPO}#<new PR>",
                          f"request review on {REPO}#<new PR>"])
        self.assertIn("reviewers[]=hubot", entries[2]["argv"])
        self.assertEqual(len(out["would"]), 3)


class ReviewPostTests(Base):
    def reviews(self):
        return things(self.fake)["3"]["reviews"]

    def test_event_matches_verdict(self):
        for verdict, event in (("comment", "COMMENT"), ("request_changes", "REQUEST_CHANGES"),
                               ("approve", "APPROVE")):
            with self.subTest(verdict=verdict):
                out = wb.review_post(self.gh, REPO, 3, verdict, "Notes.", run_id=RUN)
                self.assertEqual(out["event"], event)
                self.assertEqual(self.reviews()[-1]["event"], event)
                self.assertIn("<!-- not-me:review -->", self.reviews()[-1]["body"])
                self.assertIn(f"<!-- not-me:run={RUN} -->", self.reviews()[-1]["body"])

    def test_approve_needs_the_flag_at_the_client(self):
        path = ghmod.notme_tmp_dir() / "approve-body.md"
        path.write_text("ok", encoding="utf-8")
        self.addCleanup(path.unlink)
        with self.assertRaises(ValueError):
            self.gh.review_post(REPO, 3, "APPROVE", path)
        self.assertEqual(self.reviews(), [])
        wb.review_post(self.gh, REPO, 3, "approve", "ok", run_id=RUN)
        self.assertEqual(self.reviews()[0]["event"], "APPROVE")

    def test_unknown_verdict_refused(self):
        with self.assertRaises(ValueError):
            wb.review_post(self.gh, REPO, 3, "merge", "x", run_id=RUN)

    def test_inline_comments_and_labels(self):
        wb.review_post(self.gh, REPO, 3, "request_changes", "See inline.",
                       inline=[{"path": "src/widget.py", "line": 12, "body": "Rename."}],
                       labels_set=["needs work"], run_id=RUN)
        self.assertEqual(self.reviews()[0]["comments"], [{"path": "src/widget.py", "line": 12, "body": "Rename."}])
        self.assertEqual(things(self.fake)["3"]["labels"], ["needs work"])
        self.assertEqual(self.tmp_leftovers(), [])

    def test_event_mismatch_raises(self):
        self.fake.review_state_override = "COMMENTED"
        with self.assertRaises(wb.WritebackMismatch):
            wb.review_post(self.gh, REPO, 3, "request_changes", "x", run_id=RUN)

    def test_label_mismatch_raises(self):
        self.fake.drop_adds = True
        with self.assertRaises(wb.WritebackMismatch):
            wb.review_post(self.gh, REPO, 3, "comment", "x", labels_set=["brand new"], run_id=RUN)

    def test_dry_run_skips_readback(self):
        dry = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=Ledger(self.case.root / "l.jsonl"))
        self.assertTrue(wb.review_post(dry, REPO, 3, "comment", "x", run_id=RUN)["dry_run"])
        self.assertEqual(self.reviews(), [])


class LabelSwapTests(Base):
    def test_one_labels_set_call_keeps_others(self):
        before = len(self.fake.writes_made())
        out = wb.label_swap(self.gh, REPO, 3, ["needs review"], ["needs work"], ["needs review", "bug"])
        self.assertEqual(out, ["bug", "needs work"])
        self.assertEqual(len(self.fake.writes_made()) - before, 1)
        self.assertEqual(sorted(things(self.fake)["3"]["labels"]), ["bug", "needs work"])

    def test_add_already_present_not_duplicated(self):
        out = wb.label_swap(self.gh, REPO, 3, [], ["needs review"], ["needs review"])
        self.assertEqual(out, ["needs review"])


class FollowupTests(Base):
    def test_creates_in_tracker_with_marker_and_source_line(self):
        self.cfg.meta["project"]["tracker"] = "acme/other"
        out = wb.followup(self.gh, self.cfg, "Tidy later", "Details.", source_number=3, labels=["to do"])
        issue = things(self.fake, "acme/other")[str(out["number"])]
        self.assertEqual(issue["title"], "Tidy later")
        self.assertIn("Follow-up from acme/app#3", issue["body"])
        self.assertIn("<!-- not-me:followup -->", issue["body"])
        self.assertEqual(issue["labels"], ["to do"])
        self.assertEqual(wb.check_text(issue["body"], tracker="acme/other", repo="acme/other"), [])

    def test_auto_tracker_is_origin_and_parent_task_list_appended(self):
        out = wb.followup(self.gh, self.cfg, "Tidy", "Details.", source_number=3, parent=2)
        self.assertEqual(out["parent"], 2)
        self.assertEqual(things(self.fake)["2"]["body"], f"Body of Second issue\n- [ ] #{out['number']}\n")
        self.assertIn(str(out["number"]), things(self.fake))

    def test_no_source_line_without_number(self):
        out = wb.followup(self.gh, self.cfg, "Tidy", "Details.")
        self.assertNotIn("Follow-up from", things(self.fake)[str(out["number"])]["body"])

    def test_real_run_without_number_raises_before_parent(self):
        real = self.gh
        orig = real.write
        real.write = lambda *a, **k: {"output": "created"} if "issue" in a[1][:1] else orig(*a, **k)
        with self.assertRaises(wb.WritebackMismatch):
            wb.followup(real, self.cfg, "Tidy", "Details.", parent=2)
        self.assertEqual(things(self.fake)["2"]["body"], "Body of Second issue")

    def test_dry_run_creates_nothing(self):
        ledger = Ledger(self.case.root / "l.jsonl")
        dry = GhClient(self.ctx, runner=self.fake, dry_run=True, ledger=ledger)
        out = wb.followup(dry, self.cfg, "Tidy", "Details.", source_number=3, parent=2)
        self.assertTrue(out["dry_run"])
        self.assertIsNone(out["number"])
        self.assertEqual(self.fake.writes_made(), [])
        self.assertEqual(len(ledger.read()), 2)
        self.assertEqual(things(self.fake)["2"]["body"], "Body of Second issue")


class CheckTextTests(unittest.TestCase):
    def problems(self, text, **kw):
        kw.setdefault("tracker", "acme/board")
        kw.setdefault("repo", REPO)
        return wb.check_text(text, **kw)

    def test_bare_reference_flagged_only_when_tracker_differs(self):
        self.assertTrue(self.problems("See #12"))
        self.assertEqual(self.problems("See #12", tracker=REPO), [])
        self.assertEqual(self.problems("See acme/board#12 and https://x.test/a#issuecomment-5"), [])
        self.assertTrue(wb.check_text("See #12"))  # unknown tracker: stay strict

    def test_bare_reference_in_code_or_comment_ignored(self):
        self.assertEqual(self.problems("Use `#12` or\n```\n#13\n```\n<!-- #14 -->"), [])

    def test_local_paths_flagged(self):
        self.assertTrue(self.problems("see " + "/" + "Users/someone/code/x"))
        self.assertTrue(self.problems("see /home/someone/x"))
        self.assertEqual(self.problems("see src/widget.py"), [])

    def test_paths_and_assistant_names_flagged_inside_code(self):
        self.assertTrue(self.problems("see `" + "/" + "Users/x`"))
        self.assertTrue(self.problems("```\nrun in /home/x/y\n```"))
        self.assertTrue(self.problems("`Cl" + "aude`"))
        self.assertTrue(self.problems("```\nthe model\n```"))

    def test_assistant_mentions_flagged_outside_attribution(self):
        for text in ("Written by AI", "an assistant named Cl" + "aude", "the model decided"):
            with self.subTest(text=text):
                self.assertTrue(self.problems(text))
        self.assertEqual(self.problems("Fine.\n\nCreated with AI", attribution_text="Created with AI"), [])
        self.assertTrue(self.problems("Created with AI"))
        self.assertEqual(self.problems("Said, wait; maid"), [])

    def test_details_needs_blank_line(self):
        self.assertTrue(self.problems("<details><summary>x</summary>\ntext\n</details>"))
        self.assertTrue(self.problems("<details>\ntext\n</details>"))
        self.assertEqual(self.problems("<details><summary>x</summary>\n\ntext\n\n</details>"), [])
        self.assertEqual(self.problems("<details>\n\ntext\n\n</details>"), [])

    def test_tldr_required_for_summaries(self):
        self.assertTrue(self.problems("Some text", is_summary=True))
        self.assertEqual(self.problems("\n**TL;DR:** done", is_summary=True), [])


if __name__ == "__main__":
    unittest.main()
