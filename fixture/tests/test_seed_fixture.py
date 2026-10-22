import base64
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent
sys.path.insert(0, str(FIXTURE))

import seed_fixture as sf  # noqa: E402

Result = sf.Result
REPO = "acme/x-fixture"
COMP = "acme/x-fixture-companion"
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


def ok(text=""):
    return Result(0, text, "")


class Fake:
    """Injectable `run`: first matching substring rule wins; default is empty success."""

    def __init__(self):
        self.calls = []
        self.rules = []
        self.private = True

    def on(self, needle, response):
        self.rules.append((needle, response))

    def lines(self):
        return [" ".join(a) for a in self.calls]

    def writes(self):
        out = []
        for line in self.lines():
            if re.search(r"gh (issue (create|edit|delete|comment)|pr (create|close|edit|review|comment)|"
                         r"label (create|edit|delete)|workflow run)|-X (POST|PATCH|PUT|DELETE)|"
                         r"^git ", line):
                out.append(line)
        return out

    def __call__(self, argv, input_text=None):
        self.calls.append(list(argv))
        line = " ".join(argv)
        if "repo view" in line:
            return ok(json.dumps({"isPrivate": self.private}))
        for needle, response in self.rules:
            if needle in line:
                return response(argv) if callable(response) else ok(response)
        if "issue create" in line:
            return ok("https://github.com/acme/x-fixture/issues/1\n")
        return ok("")


def run_main(argv, fake, state_dir=None, now=NOW):
    out, err = io.StringIO(), io.StringIO()
    code = sf.main(argv, run=fake, out=out, err=err, now=lambda: now,
                   sleep=lambda s: None, state_dir=state_dir)
    return code, out.getvalue(), err.getvalue()


def lock_issue(rid, ts, number=5):
    return json.dumps([{"number": number, "title": "eval-lock",
                        "body": "<!-- lock:%s ts=%s -->" % (rid, ts.strftime(sf.TS_FORMAT))}])


def make_client(fake, dry=False):
    return sf.Client(run=fake, dry_run=dry, out=io.StringIO(), err=io.StringIO(),
                     sleep=lambda s: None, now=lambda: NOW)


class SafetyTests(unittest.TestCase):
    def test_wrong_suffix_refused(self):
        fake = Fake()
        code, _, err = run_main(["--repo", "acme/x", "--owner", "acme", "--reset"], fake)
        self.assertEqual(code, 2)
        self.assertIn("-fixture", err)
        self.assertEqual(fake.calls, [])

    def test_wrong_owner_refused(self):
        fake = Fake()
        code, _, err = run_main(["--repo", "other/x-fixture", "--owner", "acme", "--reset"], fake)
        self.assertEqual(code, 2)
        self.assertIn("owner", err)
        self.assertEqual(fake.calls, [])

    def test_missing_owner_refused(self):
        fake = Fake()
        with self.assertRaises(sf.SafetyError):
            sf.check_slug(REPO, None)
        code, _, _ = run_main(["--repo", REPO, "--reset"], fake)
        self.assertIn(code, (1, 2))
        self.assertEqual(fake.writes(), [])

    def test_public_repo_refused(self):
        fake = Fake()
        fake.private = False
        code, _, err = run_main(["--repo", REPO, "--owner", "acme", "--reset"], fake)
        self.assertEqual(code, 2)
        self.assertIn("not private", err)
        self.assertEqual(fake.writes(), [])

    def test_companion_checked_too(self):
        fake = Fake()
        code, _, err = run_main(["--repo", REPO, "--companion", "acme/plain", "--owner", "acme",
                                 "--reset"], fake)
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])

    def test_valid_suffixes_accepted(self):
        sf.check_slug("acme/a-fixture", "acme")
        sf.check_slug("Acme/a-fixture-companion", "acme")


class LockTests(unittest.TestCase):
    def fx(self, fake):
        return sf.Fixture(make_client(fake), REPO)

    def test_acquire_creates_lock_issue(self):
        fake = Fake()
        fake.on("issue create", "https://github.com/acme/x-fixture/issues/9\n")
        number = self.fx(fake).acquire("r1")
        self.assertEqual(number, 9)
        create = [l for l in fake.lines() if "issue create" in l][0]
        self.assertIn("--title eval-lock", create)
        self.assertIn("lock:r1 ts=2026-10-21T12:00:00Z", create)

    def test_fresh_lock_held_by_other_refuses(self):
        fake = Fake()
        fake.on("issue list", lock_issue("other", NOW - timedelta(minutes=30)))
        with self.assertRaises(sf.LockError):
            self.fx(fake).acquire("r1")
        self.assertEqual(fake.writes(), [])

    def test_stale_lock_taken_over_with_edit_and_reread(self):
        fake = Fake()
        fake.on("issue list", lock_issue("other", NOW - timedelta(hours=3)))
        fake.on("issue view", lambda a: ok(json.dumps(
            {"body": "<!-- lock:r1 ts=2026-10-21T12:00:00Z -->"})))
        self.assertEqual(self.fx(fake).acquire("r1"), 5)
        lines = fake.lines()
        edit = [i for i, l in enumerate(lines) if "issue edit 5" in l]
        view = [i for i, l in enumerate(lines) if "issue view 5" in l]
        self.assertTrue(edit and view and edit[0] < view[0])

    def test_lost_compare_and_set_refuses(self):
        fake = Fake()
        fake.on("issue list", lock_issue("other", NOW - timedelta(hours=3)))
        fake.on("issue view", json.dumps(
            {"body": "<!-- lock:thief ts=2026-10-21T12:00:00Z -->"}))
        with self.assertRaises(sf.LockError):
            self.fx(fake).acquire("r1")

    def test_release_deletes_own_lock_and_refuses_fresh_foreign_lock(self):
        fake = Fake()
        fake.on("issue list", lock_issue("r1", NOW))
        self.fx(fake).release("r1")
        self.assertIn("gh issue delete 5 --repo %s --yes" % REPO, fake.lines())
        other = Fake()
        other.on("issue list", lock_issue("someone", NOW))
        with self.assertRaises(sf.LockError):
            self.fx(other).release("r1")
        self.assertEqual(other.writes(), [])


class ResetTests(unittest.TestCase):
    def fake(self, readback_main="basesha"):
        fake = Fake()
        fake.on("number,title,state,headRefName", "[]")
        fake.on("number,title,state,labels", "[]")
        fake.on("pr list", json.dumps([{"number": 3, "headRefName": "feat/a"}]))
        fake.on("--paginate", "main %s\ndevelop basesha\nfeat/a sha1\nold sha2\n" % readback_main)
        fake.on("issue list --repo %s --state all --limit 1000 --json number,title" % REPO,
                json.dumps([{"number": 5, "title": "eval-lock"},
                            {"number": 6, "title": "stray"}]))
        fake.on("issue list --repo %s --state open" % REPO, lock_issue("reset", NOW))
        fake.on("git/ref/tags/fixture-baseline", "commit basesha")
        fake.on("label list", json.dumps([
            {"name": "bug", "color": "d73a4a", "description": "x"},
            {"name": "to do", "color": "0e8a16", "description": "Ready to be picked up"}]))
        return fake

    def test_reset_command_order(self):
        fake = self.fake()
        run_main(["--repo", REPO, "--owner", "acme", "--reset"], fake)
        lines = fake.lines()

        def first(text):
            return next(i for i, l in enumerate(lines) if text in l)

        self.assertTrue(first("pr close 3") < first("-X DELETE repos/%s/git/refs/heads/feat/a" % REPO)
                        < first("issue delete 6") < first("-X PATCH repos/%s/git/refs/heads/main" % REPO)
                        < first("-X PATCH repos/%s/git/refs/heads/develop" % REPO)
                        < first("label create"))
        self.assertIn("-X DELETE repos/%s/git/refs/heads/old" % REPO, " | ".join(lines))
        self.assertNotIn("heads/main -", " | ".join(l for l in lines if "DELETE" in l))
        self.assertFalse(any("issue delete 5 " in l for l in lines), "lock must survive the clean")
        self.assertIn("gh label delete bug --repo %s --yes" % REPO, lines)
        self.assertTrue(any("force=true" in l for l in lines))

    def test_reset_with_clean_readback_exits_zero(self):
        fake = Fake()
        fake.on("git/ref/tags/fixture-baseline", "commit basesha")
        fake.on("--paginate", "main basesha\ndevelop basesha\n")
        fake.on("label list", json.dumps(
            [{"name": l["name"], "color": l["color"], "description": l["description"]}
             for l in sf.load_labels()]))
        code, _, err = run_main(["--repo", REPO, "--owner", "acme", "--reset"], fake)
        self.assertEqual(code, 0, err)

    def test_reset_readback_mismatch_exits_one(self):
        fake = self.fake(readback_main="drifted")
        code, _, err = run_main(["--repo", REPO, "--owner", "acme", "--reset"], fake)
        self.assertEqual(code, 1)
        self.assertIn("read-back mismatch", err)

    def test_dry_run_runs_nothing(self):
        fake = Fake()
        code, out, _ = run_main(["--repo", REPO, "--owner", "acme", "--reset", "--dry-run"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("+ gh api -X PATCH", out)
        self.assertIn("+ gh label create", out)

    def test_dry_run_scenario_writes_no_manifest(self):
        fake = Fake()
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run_main(["--repo", REPO, "--owner", "acme", "--scenario", "N1",
                                     "--run-id", "r1", "--dry-run"], fake, state_dir=tmp)
            self.assertEqual(code, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(fake.calls, [])
        self.assertIn("[eval:r1:N1]", out)


class ScenarioTests(unittest.TestCase):
    def n1_fake(self):
        core = (FIXTURE / "repo" / "calc" / "core.py").read_text(encoding="utf-8")
        fake = Fake()
        counter = {"n": 0}

        def create(argv):
            counter["n"] += 1
            return ok("https://github.com/%s/issues/%d\n" % (REPO, counter["n"]))

        fake.on("issue create", create)
        fake.on("Accept: application/vnd.github.raw", core)
        fake.on("--jq .sha", "filesha\n")
        fake.on("--jq .commit.sha", "commitsha\n")
        fake.on("git/ref/tags/fixture-baseline", "commit basesha")
        fake.on("--jq .login", "octo\n")
        fake.on("--paginate", "main mainsha\ndevelop basesha\n")
        fake.on("issue list --repo %s --state open --json number" % REPO,
                json.dumps([{"number": n} for n in range(1, 20)]))
        return fake

    def test_n1_gh_calls_and_manifest(self):
        fake = self.n1_fake()
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run_main(["--repo", REPO, "--owner", "acme", "--scenario", "N1",
                                       "--run-id", "r1"], fake, state_dir=tmp)
            self.assertEqual(code, 0, err)
            manifest = json.loads((Path(tmp) / "r1" / "N1.json").read_text(encoding="utf-8"))
        lines = fake.lines()
        put = [l for l in lines if "-X PUT" in l]
        self.assertEqual(len(put), 1)
        self.assertIn("branch=main", put[0])
        content = re.search(r"content=(\S+)", put[0]).group(1)
        self.assertIn("return a - b", base64.b64decode(content).decode())
        issue = [l for l in lines if "issue create" in l and "eval-lock" not in l][0]
        self.assertIn("[eval:r1:N1] add() returns the wrong result", issue)
        self.assertIn("--label to do", issue)
        self.assertIn("--assignee octo", issue)
        self.assertFalse(any("workflow run" in l for l in lines))
        for key in ("run_id", "scenario", "repo", "started_at", "baseline_sha", "shas",
                    "created", "snapshot", "expect", "writes"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["run_id"], "r1")
        self.assertEqual(manifest["baseline_sha"], "basesha")
        self.assertEqual(manifest["created"]["issues"][0]["key"], "bug")
        self.assertEqual(manifest["created"]["issues"][0]["number"], 2)
        self.assertEqual(manifest["shas"]["main"], "mainsha")
        self.assertEqual(manifest["created"]["commits"][0]["sha"], "commitsha")
        self.assertEqual(manifest["expect"]["skill"], "new")
        for key in ("branches", "labels", "issues", "prs"):
            self.assertIn(key, manifest["snapshot"])

    def test_bot_issue_goes_through_workflow(self):
        fake = self.n1_fake()
        runs = {"n": 0}

        def run_list(argv):
            runs["n"] += 1
            return ok(json.dumps([{"databaseId": 100 if runs["n"] == 1 else 101}]))

        fake.rules.insert(0, ("run list", run_list))
        fake.rules.insert(0, ("--json number --limit 200 --label to do",
                                json.dumps([{"number": n} for n in range(1, 20)])))
        fake.rules.insert(0, ("issue list --repo %s --state open --limit 200" % REPO, json.dumps(
            [{"number": 8, "title": "[eval:r1:N8] Urgent maintenance"}])))
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run_main(["--repo", REPO, "--owner", "acme", "--scenario", "N8",
                                     "--run-id", "r1"], fake, state_dir=tmp)
        self.assertEqual(code, 0, err)
        lines = fake.lines()
        dispatch = [l for l in lines if "workflow run post-as-bot.yml" in l]
        self.assertEqual(len(dispatch), 1)
        self.assertIn("kind=issue", dispatch[0])
        self.assertIn("title=[eval:r1:N8] Urgent maintenance", dispatch[0])
        self.assertTrue(any("run watch 101" in l and "--exit-status" in l for l in lines))
        self.assertTrue(any("issue edit 8" in l and "--add-label to do" in l for l in lines))

    def test_every_scenario_parses_and_has_expect(self):
        files = sorted((FIXTURE / "scenarios").glob("*.json"))
        ids = {f.stem for f in files}
        for needed in ("N1", "N2", "N3", "N4", "N7", "N8", "U1", "U3", "U4", "U5",
                       "R1", "R2", "P1", "K3", "M1"):
            self.assertIn(needed, ids)
        labels = {l["name"] for l in sf.load_labels()}
        for f in files:
            spec = json.loads(f.read_text(encoding="utf-8"))
            self.assertIsInstance(spec.get("expect"), dict, f.name)
            self.assertTrue(spec["expect"], f.name)
            items = list(spec.get("issues", [])) + list(spec.get("prs", []))
            for item in items:
                self.assertIn(item.get("as", "operator"), ("operator", "bot"), f.name)
                for label in item.get("labels", []):
                    self.assertIn(label, labels, f.name)

    def test_scenario_patches_apply_to_the_baseline(self):
        for f in sorted((FIXTURE / "scenarios").glob("*.json")):
            spec = json.loads(f.read_text(encoding="utf-8"))
            main_files, branch_files = {}, {}
            for patch in spec.get("patches", []):
                path = patch["path"]
                if patch.get("on_main"):
                    files = main_files
                else:
                    files = branch_files.setdefault(patch["branch"], dict(main_files))
                if path not in files:
                    files[path] = (FIXTURE / "repo" / path).read_text(encoding="utf-8")
                self.assertIn(patch["find"], files[path], "%s %s" % (f.name, path))
                files[path] = files[path].replace(patch["find"], patch["replace"], 1)

    def test_scenario_prs_use_existing_branches(self):
        for f in sorted((FIXTURE / "scenarios").glob("*.json")):
            spec = json.loads(f.read_text(encoding="utf-8"))
            branches = {p["branch"] for p in spec.get("patches", []) if "branch" in p}
            for pr in spec.get("prs", []):
                self.assertIn(pr["head"], branches, f.name)


class VisibilityTests(unittest.TestCase):
    SPEC = {"prs": [{"head": "h", "title": "t", "labels": ["needs work"],
                     "assignee": "octo"}],
            "issues": [{"title": "i", "labels": ["to do"]}, {"title": "plain"}]}
    CREATED = {"issues": [{"number": 4}, {"number": 5}], "prs": [{"number": 7}]}

    def run_wait(self, fake, dry=False, polls=30):
        sleeps = []
        client = make_client(fake, dry=dry)
        client.sleep = sleeps.append
        sf.Fixture(client, REPO).wait_visible(self.SPEC, self.CREATED, polls=polls)
        return sleeps

    def test_visible_at_once(self):
        fake = Fake()
        fake.on("pr list", json.dumps([{"number": 7}]))
        fake.on("issue list", json.dumps([{"number": 4}]))
        self.assertEqual(self.run_wait(fake), [])
        lines = fake.lines()
        pr = [l for l in lines if "pr list" in l][0]
        self.assertIn("--label needs work", pr)
        self.assertIn("--assignee octo", pr)
        self.assertEqual(len([l for l in lines if "issue list" in l]), 1)

    def test_visible_after_polls(self):
        fake = Fake()
        count = {"n": 0}

        def pr_list(argv):
            count["n"] += 1
            return ok(json.dumps([{"number": 7}] if count["n"] > 2 else []))

        fake.on("pr list", pr_list)
        fake.on("issue list", json.dumps([{"number": 4}]))
        self.assertEqual(self.run_wait(fake), [3, 3])

    def test_timeout_raises(self):
        fake = Fake()
        fake.on("issue list", json.dumps([{"number": 4}]))
        with self.assertRaises(sf.SeedError) as ctx:
            self.run_wait(fake, polls=4)
        self.assertIn("pr #7", str(ctx.exception))

    def test_dry_run_does_not_poll(self):
        fake = Fake()
        self.assertEqual(self.run_wait(fake, dry=True), [])
        self.assertEqual(fake.calls, [])


class BootstrapTests(unittest.TestCase):
    def test_staged_tree_has_dot_github_not_underscore(self):
        staged = {}

        def run(argv, input_text=None):
            line = " ".join(argv)
            if argv[:3] == ["gh", "repo", "clone"]:
                Path(argv[-1]).mkdir(parents=True)
            if line.endswith(" add -A"):
                root = Path(argv[2])
                staged["files"] = sorted(
                    p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
            if "repo view" in line:
                return ok(json.dumps({"isPrivate": True}))
            if "git/ref/tags/fixture-baseline" in line:
                return Result(1, "", "not found")
            return ok("")

        out, err = io.StringIO(), io.StringIO()
        code = sf.main(["--repo", REPO, "--owner", "acme", "--bootstrap"], run=run,
                       out=out, err=err, sleep=lambda s: None)
        self.assertEqual(code, 0, err.getvalue())
        files = staged["files"]
        self.assertIn(".github/workflows/post-as-bot.yml", files)
        self.assertIn(".github/workflows/ci.yml", files)
        self.assertFalse([f for f in files if f.startswith("_github")])


class VerifyTests(unittest.TestCase):
    def labels_json(self):
        return json.dumps([{"name": l["name"], "color": l["color"], "description": l["description"]}
                           for l in sf.load_labels()])

    def test_verify_clean_exits_zero(self):
        fake = Fake()
        fake.on("git/ref/tags/fixture-baseline", "commit basesha")
        fake.on("--paginate", "main basesha\ndevelop basesha\n")
        fake.on("label list", self.labels_json())
        code, out, _ = run_main(["--repo", REPO, "--owner", "acme", "--verify"], fake)
        self.assertEqual(code, 0)
        self.assertIn("no differences", out)
        self.assertEqual(fake.writes(), [])

    def test_verify_prints_diff_and_exits_one(self):
        fake = Fake()
        fake.on("git/ref/tags/fixture-baseline", "commit basesha")
        fake.on("--paginate", "main moved\ndevelop basesha\nextra s\n")
        fake.on("label list", self.labels_json())
        code, out, _ = run_main(["--repo", REPO, "--owner", "acme", "--verify"], fake)
        self.assertEqual(code, 1)
        self.assertIn("/branches/main", out)
        self.assertIn("/branches/extra", out)

    def test_diff_snapshots(self):
        self.assertEqual(sf.diff_snapshots({"a": {"b": 1}}, {"a": {"b": 1}}), [])
        self.assertEqual(len(sf.diff_snapshots({"a": {"b": 1}}, {"a": {"b": 2, "c": 3}})), 2)


class ContentTests(unittest.TestCase):
    def test_labels_match_plugin_roles(self):
        names = [l["name"] for l in sf.load_labels()]
        self.assertEqual(sorted(names), sorted([
            "to do", "in progress", "needs review", "needs work", "ready to merge", "question",
            "blocked", "epic", "needs help", "needs human", "paused"]))
        for l in sf.load_labels():
            self.assertRegex(l["color"], r"^[0-9a-f]{6}$")

    def test_no_forbidden_strings_in_fixture(self):
        pattern = re.compile("|".join(["yale" + "sites", "mike" + "tullo", "lan" + "do",
                                       "pan" + "theon", "/Us" + "ers/"]), re.I)
        for path in FIXTURE.rglob("*"):
            if path.is_file() and path != Path(__file__).resolve() and "__pycache__" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="replace")
                self.assertIsNone(pattern.search(text), str(path))

    def test_fixture_workflow_inputs(self):
        text = (FIXTURE / "repo/_github/workflows/post-as-bot.yml").read_text(encoding="utf-8")
        for name in ("kind", "number", "title", "body", "path", "line", "branch"):
            self.assertRegex(text, r"\n      %s:\n" % name)
        self.assertIn("workflow_dispatch", text)


if __name__ == "__main__":
    unittest.main()
