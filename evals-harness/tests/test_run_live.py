import contextlib
import io
import json
import os
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path, PurePath
from unittest import mock

import _support as s  # noqa: F401
import run_live
import transcript

SECRET = "github_pat_FIXTURETOKEN123"


def rids(scenario, n):
    return f"{scenario.lower()}-{n}-test"


def write_case(root, verb="PR"):
    d = root / "new-N1"
    (d / "graders").mkdir(parents=True)
    (d / "prompt.md").write_text(
        "---\nname: new-N1\nmax_turns: 5\ntimeout_seconds: 77\nallowed_tools: [Bash, Read, Edit]\n---\n\n/not-me:new --bg\n",
        encoding="utf-8")
    (d / "graders" / "verb.md").write_text(f"---\ntype: regex\ntarget: last_message\nmatch: contains\n---\n\n^{verb}\\b\n",
                                          encoding="utf-8")


class Recorder:
    """A runner that records argv, env and options per step and fakes what each step leaves behind."""

    def __init__(self, home, fail=(), grade_ok=True, last_message="PR acme/x#1 opened", session_id="sess-1",
                 raise_on=None):
        self.home = Path(home)
        self.calls = []
        self.fail = set(fail)
        self.grade_ok = grade_ok
        self.last_message = last_message
        self.session_id = session_id
        self.raise_on = raise_on
        self.global_at_launch = None

    def step(self, argv):
        joined = " ".join(argv)
        if argv[0] == "claude":
            return "launch"
        for name, marker in (("grade", "grade_state.py"), ("prepare", "scaffold.py prepare"),
                             ("stage", "scaffold.py stage"), ("clean", "scaffold.py clean"), ("reset", "--reset"),
                             ("teardown", "--teardown"), ("seed", "--scenario")):
            if marker in joined:
                return name
        return "?"

    def transcript(self, workspace, name):
        pdir = transcript.project_dir_for(Path(workspace).resolve(), home=self.home)
        pdir.mkdir(parents=True, exist_ok=True)
        line = {"type": "assistant", "sessionId": name, "cwd": str(workspace),
                "message": {"content": [{"type": "text", "text": self.last_message}]}}
        (pdir / f"{name}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

    def __call__(self, argv, env, **opts):
        name = self.step(argv)
        self.calls.append((name, list(argv), dict(env), dict(opts)))
        if name == self.raise_on:
            raise OSError("boom")
        if name == "prepare":
            stage = Path(argv[argv.index("--stage-dir") + 1])
            stage.mkdir(parents=True, exist_ok=True)
            (stage / "gh-token").write_text(SECRET + "\n", encoding="utf-8")
        if name == "stage":  # the scenario's global layer lands in the real (fake) home
            g = self.home / ".claude" / "not-me"
            g.mkdir(parents=True, exist_ok=True)
            (g / "project.md").write_text("scenario layer\n", encoding="utf-8")
            Path(argv[argv.index("--workspace") + 1]).mkdir(parents=True, exist_ok=True)
        if name == "launch":
            ws = opts["cwd"]
            self.global_at_launch = (self.home / ".claude" / "not-me" / "project.md").read_text(encoding="utf-8")
            if opts.get("stdout_path"):
                Path(opts["stdout_path"]).write_text(json.dumps(
                    {"type": "result", "session_id": self.session_id, "result": self.last_message,
                     "total_cost_usd": 1.5, "num_turns": 7, "is_error": False}), encoding="utf-8")
                self.transcript(ws, self.session_id)
            else:
                self.transcript(ws, "older")
                self.transcript(ws, "newest")
                os.utime(str(transcript.project_dir_for(Path(ws).resolve(), home=self.home) / "older.jsonl"),
                         (1, 1))
        if name == "grade":
            Path(argv[argv.index("--json") + 1]).write_text(json.dumps(
                {"scenario": "N1", "backend": "github", "ok": self.grade_ok, "results": [
                    {"name": "x", "class": "outcome", "ok": self.grade_ok, "detail": ""}]}), encoding="utf-8")
        return 1 if name in self.fail else 0

    def names(self):
        return [c[0] for c in self.calls]

    def call(self, name):
        return next(c for c in self.calls if c[0] == name)


class RunLiveTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp), True)
        self.tmp, self.out, self.home = tmp / "tmp", str(tmp / "out"), tmp / "home"
        self.state = tmp / "state"
        self.cases = tmp / "cases"
        self.home.mkdir(parents=True)
        self.tmp.mkdir()
        write_case(self.cases)
        for patcher in (
            mock.patch.dict(os.environ, {"NOT_ME_FIXTURE_TOKEN": SECRET, "GH_TOKEN": "operator-token",
                                         "GITHUB_TOKEN": "g", "ANTHROPIC_API_KEY": "k",
                                         "CLAUDE_CODE_OAUTH_TOKEN": "o"}),
            mock.patch.object(run_live, "STATE_DIR", self.state),
            mock.patch.object(run_live, "CASES_DIR", self.cases)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.glob = self.home / ".claude" / "not-me"

    def rec(self, **kw):
        return Recorder(self.home, **kw)

    def args(self, *extra):
        return ["--skill", "new", "--scenario", "N1", "--repo", "acme/x-fixture", "--companion",
                "acme/x-fixture-companion", "--owner", "acme", "--out", self.out, *extra]

    def run_main(self, rec, *extra, buf=None):
        return run_live.main(self.args(*extra), runner=rec, rid_factory=rids, out=buf or io.StringIO(),
                             home=self.home)

    def result(self):
        return json.loads((Path(self.out) / "N1-1.json").read_text(encoding="utf-8"))

    def operator_config(self):
        self.glob.mkdir(parents=True)
        (self.glob / "project.md").write_text("operator's own\n", encoding="utf-8")
        (self.glob / "allowed-repos.json").write_text("{}\n", encoding="utf-8")

    def assert_operator_config(self):
        self.assertEqual((self.glob / "project.md").read_text(encoding="utf-8"), "operator's own\n")
        self.assertTrue((self.glob / "allowed-repos.json").is_file())
        self.assertFalse(list(self.state.glob("*/global-backup.json")))

    # dry run

    def test_dry_run_prints_the_command_sequence_and_runs_nothing(self):
        rec, buf = self.rec(), io.StringIO()
        self.assertEqual(self.run_main(rec, "--dry-run", buf=buf), 0)
        self.assertEqual(rec.calls, [])
        kinds = []
        for line in buf.getvalue().splitlines():
            if not line.startswith("+ ") or line.startswith("+ ("):
                continue
            tokens = shlex.split(line[2:])
            if tokens[0] == "claude":
                kinds.append("launch")
                continue
            script = PurePath(tokens[1]).name
            if script == "seed_fixture.py":
                kinds.append("reset" if "--reset" in tokens else "teardown" if "--teardown" in tokens else "seed")
            elif script == "scaffold.py":
                kinds.append(tokens[2])
            elif script == "grade_state.py":
                kinds.append("grade")
        self.assertEqual(kinds, ["reset", "seed", "prepare", "stage", "launch", "grade", "clean", "teardown"])
        self.assertFalse(Path(self.out).exists() and list(Path(self.out).iterdir()))
        self.assertFalse(self.glob.exists())

    def test_dry_run_prints_the_claude_command_and_no_token(self):
        buf = io.StringIO()
        self.run_main(self.rec(), "--dry-run", "--repo", f"acme/{SECRET}-fixture", buf=buf)
        text = buf.getvalue()
        self.assertNotIn("FIXTURETOKEN123", text)
        self.assertIn("<redacted-token>", text)
        line = next(l for l in text.splitlines() if l.startswith("+ claude "))
        for piece in ("claude -p '/not-me:new --bg'", "--plugin-dir", "--output-format json",
                      "--allowedTools Bash,Read,Edit"):
            self.assertIn(piece, line)
        self.assertNotIn("--max-turns", line)

    def test_dry_run_interactive_command(self):
        buf = io.StringIO()
        self.run_main(self.rec(), "--dry-run", "--launch", "interactive", buf=buf)
        line = next(l for l in buf.getvalue().splitlines() if l.startswith("+ claude "))
        self.assertNotIn(" -p ", line)
        self.assertIn("--plugin-dir", line)

    # real runs

    def test_real_run_runs_in_order_and_merges(self):
        rec = self.rec()
        self.assertEqual(self.run_main(rec), 0)
        self.assertEqual(rec.names(), ["reset", "seed", "prepare", "stage", "launch", "grade", "clean", "teardown"])
        merged = self.result()
        self.assertTrue(merged["ok"])
        for key in ("scenario", "skill", "run", "run_id", "backend", "repo", "date", "grade", "errors", "ok",
                    "cost_usd", "session", "graders"):
            self.assertIn(key, merged)
        self.assertNotIn("eval", merged)
        self.assertEqual(merged["cost_usd"], 1.5)
        self.assertEqual(merged["session"]["mode"], "print")
        self.assertEqual(merged["session"]["session_id"], "sess-1")
        self.assertEqual(merged["session"]["turns"], 7)
        self.assertTrue(merged["session"]["transcript"].endswith("sess-1.jsonl"))
        self.assertTrue(merged["graders"]["passed"])
        self.assertTrue((Path(self.out) / "N1-1.graders.json").is_file())
        self.assertNotIn("FIXTURETOKEN", json.dumps(merged))

    def test_print_launch_options_and_workspace_cleanup(self):
        rec = self.rec()
        self.run_main(rec)
        _, argv, _, opts = rec.call("launch")
        self.assertEqual(PurePath(opts["cwd"]).name, "N1.workspace")
        self.assertEqual(opts["timeout"], 77)
        self.assertTrue(opts["stdin_devnull"])
        self.assertEqual(Path(opts["stdout_path"]).name, "N1-1.session.json")
        self.assertFalse(Path(opts["cwd"]).exists(), "the workspace is deleted afterwards")

    def test_keep_temp_keeps_the_workspace(self):
        rec = self.rec()
        self.run_main(rec, "--keep-temp")
        self.assertTrue(Path(rec.call("launch")[3]["cwd"]).is_dir())

    def test_token_only_as_gh_token_in_the_launch_env(self):
        rec = self.rec()
        self.run_main(rec)
        _, argv, env, _ = rec.call("launch")
        self.assertEqual(env["GH_TOKEN"], SECRET)
        for name, a, _, _ in rec.calls:
            self.assertNotIn("FIXTURETOKEN", " ".join(a), name)
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "NOT_ME_FIXTURE_TOKEN", "GITHUB_TOKEN"):
            self.assertNotIn(key, env)
        self.assertEqual([k for k, v in env.items() if v == SECRET], ["GH_TOKEN"])
        self.assertEqual(env["EVAL_NOT_ME_RUN_ID"], "n1-1-test")
        self.assertEqual(env["EVAL_NOT_ME_SCENARIO"], "N1")
        self.assertEqual(env["EVAL_NOT_ME_BACKEND"], "github")

    def test_gh_config_dir_is_an_empty_dir_under_the_stage(self):
        rec = self.rec()
        captured = {}
        orig = rec.__call__

        def spy(argv, env, **opts):
            if argv[0] == "claude":
                d = Path(env["GH_CONFIG_DIR"])
                captured.update(is_dir=d.is_dir(), empty=not list(d.iterdir()),
                                parts=PurePath(d).parts[-3:])
            return orig(argv, env, **opts)
        self.run_main(spy)
        self.assertEqual(captured["parts"], ("n1-1-test", "N1.stage", "gh-config"))
        self.assertTrue(captured["is_dir"] and captured["empty"])

    def test_only_prepare_sees_the_fixture_token(self):
        rec = self.rec()
        self.run_main(rec)
        for name, _, env, _ in rec.calls:
            if name == "prepare":
                self.assertEqual(env["NOT_ME_FIXTURE_TOKEN"], SECRET)
                self.assertEqual(env["NOT_ME_FIXTURE_OWNER"], "acme")
            else:
                self.assertNotIn("NOT_ME_FIXTURE_TOKEN", env, name)

    # the global layer

    def test_global_layer_is_backed_up_and_restored_on_success(self):
        self.operator_config()
        rec = self.rec()
        self.run_main(rec)
        self.assertEqual(rec.global_at_launch, "scenario layer\n")
        self.assert_operator_config()

    def test_global_layer_is_restored_when_launch_raises(self):
        self.operator_config()
        rec = self.rec(raise_on="launch")
        self.assertEqual(self.run_main(rec), 1)
        self.assert_operator_config()
        self.assertEqual(rec.names()[-2:], ["clean", "teardown"])

    def test_global_layer_is_restored_when_grade_raises(self):
        self.operator_config()
        self.assertEqual(self.run_main(self.rec(raise_on="grade")), 1)
        self.assert_operator_config()

    def test_no_prior_layer_means_the_run_layer_is_removed(self):
        self.run_main(self.rec())
        self.assertFalse(self.glob.exists())
        self.assertFalse(list(self.state.glob("*/global-backup.json")))

    def test_stale_backup_record_is_refused_and_restore_global_fixes_it(self):
        self.operator_config()
        rec = self.rec()
        # a run that died after staging: backup taken, run layer in place, record left behind
        run_live.backup_global(self.home, self.state / "dead-1" / run_live.BACKUP_RECORD)
        self.glob.mkdir(parents=True)
        (self.glob / "project.md").write_text("scenario layer\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(rec), 2)
        self.assertIn("--restore-global dead-1", err.getvalue())
        self.assertEqual(rec.calls, [])
        buf = io.StringIO()
        self.assertEqual(run_live.main(["--restore-global", "dead-1"], out=buf, home=self.home), 0)
        self.assert_operator_config()
        self.assertEqual(self.run_main(self.rec()), 0)

    def test_restore_global_unknown_run_is_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_live.main(["--restore-global", "nope"], home=self.home), 1)

    def test_restore_after_crash_before_the_move_keeps_the_operators_config(self):
        self.operator_config()
        record = self.state / "dead-2" / run_live.BACKUP_RECORD
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"original": str(self.glob), "backup": str(record.parent / "global-backup"),
                                      "had_original": True}), encoding="utf-8")
        run_live.restore_global(record)
        self.assert_operator_config()

    # launch modes and grading

    def test_interactive_mode_has_no_capture_and_takes_the_newest_transcript(self):
        rec = self.rec()
        self.assertEqual(self.run_main(rec, "--launch", "interactive"), 0)
        _, argv, _, opts = rec.call("launch")
        self.assertNotIn("-p", argv)
        self.assertIsNone(opts.get("stdout_path"))
        self.assertFalse(opts.get("stdin_devnull"))
        self.assertNotIn("timeout", opts)
        merged = self.result()
        self.assertEqual(merged["session"]["mode"], "interactive")
        self.assertTrue(merged["session"]["transcript"].endswith("newest.jsonl"))
        self.assertIsNone(merged["cost_usd"])

    def test_failed_transcript_graders_fail_the_run_even_when_state_passes(self):
        rec = self.rec(last_message="NONE - idle", grade_ok=True)
        self.assertEqual(self.run_main(rec), 1)
        merged = self.result()
        self.assertTrue(merged["grade"]["ok"])
        self.assertFalse(merged["graders"]["passed"])
        self.assertFalse(merged["ok"])

    def test_failed_state_grade_fails_the_run(self):
        rec = self.rec(grade_ok=False, fail={"grade"})
        self.assertEqual(self.run_main(rec), 1)
        self.assertFalse(self.result()["ok"])

    def test_missing_transcript_is_an_error(self):
        class NoTranscript(Recorder):
            def transcript(self, workspace, name):
                pass
        self.assertEqual(self.run_main(NoTranscript(self.home)), 1)
        merged = self.result()
        self.assertFalse(merged["ok"])
        self.assertTrue(any("no transcript" in e for e in merged["errors"]))

    def test_session_id_missing_falls_back_to_the_newest_transcript(self):
        rec = self.rec(session_id=None)
        self.assertEqual(self.run_main(rec), 0)

    def test_timeout_is_an_error(self):
        class Slow(Recorder):
            def __call__(self, argv, env, **opts):
                code = super().__call__(argv, env, **opts)
                return 124 if argv[0] == "claude" else code
        self.assertEqual(self.run_main(Slow(self.home)), 1)
        self.assertTrue(any("timed out" in e for e in self.result()["errors"]))

    def test_ledger_backend_defaults_to_the_workspace_ledger(self):
        rec = self.rec()
        self.run_main(rec, "--backend", "ledger")
        grade = rec.call("grade")[1]
        ledger = Path(grade[grade.index("--ledger") + 1])
        self.assertEqual(ledger.parts[-4:], (".git", "not-me", "logs", "intended-writes.jsonl"))
        self.assertEqual(ledger.parts[-5], "N1.workspace")
        self.assertEqual(grade[grade.index("--backend") + 1], "ledger")

    def test_ledger_override(self):
        rec = self.rec()
        self.run_main(rec, "--backend", "ledger", "--ledger", "/kept/intended-writes.jsonl")
        grade = rec.call("grade")[1]
        self.assertEqual(Path(grade[grade.index("--ledger") + 1]), Path("/kept/intended-writes.jsonl"))

    # failures and usage

    def test_prepare_failure_still_cleans_and_tears_down(self):
        rec = self.rec(fail={"prepare"})
        self.assertEqual(self.run_main(rec), 1)
        self.assertEqual(rec.names(), ["reset", "seed", "prepare", "clean", "teardown"])

    def test_teardown_runs_even_when_the_seed_fails(self):
        rec = self.rec(fail={"seed"})
        self.assertEqual(self.run_main(rec), 1)
        self.assertEqual(rec.names(), ["reset", "seed", "clean", "teardown"])
        self.assertIn("seed failed", self.result()["errors"])

    def test_missing_fixture_token_is_refused_before_anything_runs(self):
        rec = self.rec()
        os.environ.pop("NOT_ME_FIXTURE_TOKEN")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_main(rec), 2)
        self.assertEqual(rec.calls, [])

    def test_unknown_case_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = run_live.main(self.args("--skill", "nope"), runner=self.rec(), out=io.StringIO(), home=self.home)
        self.assertEqual(code, 2)

    def test_second_invocation_adds_runs_instead_of_overwriting(self):
        self.run_main(self.rec())
        run_live.main(self.args("--runs", "2"), runner=self.rec(), rid_factory=rids, out=io.StringIO(),
                      home=self.home)
        names = sorted(p.name for p in Path(self.out).glob("N1-?.json"))
        self.assertEqual(names, ["N1-1.json", "N1-2.json", "N1-3.json"])

    def test_missing_repo_is_an_error(self):
        env = {k: v for k, v in os.environ.items() if k not in ("NOT_ME_FIXTURE_REPO", "NOT_ME_FIXTURE_OWNER")}
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(io.StringIO()):
            code = run_live.main(["--skill", "new", "--scenario", "N1", "--owner", "acme", "--out", self.out],
                                 runner=self.rec(), out=io.StringIO(), home=self.home)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
