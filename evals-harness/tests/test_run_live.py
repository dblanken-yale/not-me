import contextlib
import io
import json
import os
import shlex
import shutil
import tempfile
import time
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

    def __init__(self, home, fail=(), grade_ok=True, last_message="PR acme/x#1 opened", session_id="0f8b1c2e-1111-4222-8333-444455556666",
                 raise_on=None, transcript_name=None):
        self.transcript_name = transcript_name
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
                self.transcript(ws, self.transcript_name or self.session_id)
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

    def replaced(self, rid):
        return self.home / ".claude" / f"not-me.replaced-{rid}"

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
        self.assertEqual(merged["session"]["session_id"], "0f8b1c2e-1111-4222-8333-444455556666")
        self.assertEqual(merged["session"]["turns"], 7)
        self.assertTrue(merged["session"]["transcript"].endswith("0f8b1c2e-1111-4222-8333-444455556666.jsonl"))
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
        # nothing is deleted: the layer the run wrote waits beside the config for the operator
        self.assertEqual((self.replaced("n1-1-test") / "project.md").read_text(encoding="utf-8"), "scenario layer\n")

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

    def test_no_prior_layer_means_the_run_layer_is_moved_aside(self):
        self.run_main(self.rec())
        self.assertFalse(self.glob.exists())
        self.assertEqual((self.replaced("n1-1-test") / "project.md").read_text(encoding="utf-8"), "scenario layer\n")
        self.assertFalse(list(self.state.glob("*/global-backup.json")))

    def dead_run(self, rid, host=None, pid=999999999, created=None):
        """A run that died after staging: backup taken, run layer in place, record left behind."""
        record = self.state / rid / run_live.BACKUP_RECORD
        run_live.backup_global(self.home, rid, record)
        data = json.loads(record.read_text(encoding="utf-8"))
        data.update(pid=pid, host=host or data["host"], **({"created": created} if created else {}))
        record.write_text(json.dumps(data), encoding="utf-8")
        self.glob.mkdir(parents=True, exist_ok=True)
        (self.glob / "project.md").write_text("scenario layer\n", encoding="utf-8")
        return record

    def test_stale_backup_record_is_refused_and_restore_global_fixes_it(self):
        self.operator_config()
        self.dead_run("dead-1")
        rec = self.rec()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(rec), 2)
        self.assertIn("--restore-global dead-1", err.getvalue())
        self.assertEqual(rec.calls, [])
        self.assertEqual(run_live.main(["--restore-global", "dead-1"], out=io.StringIO(), home=self.home), 0)
        self.assert_operator_config()
        self.assertFalse((self.home / ".claude" / "not-me.backup-dead-1").exists())
        self.assertEqual(self.run_main(self.rec()), 0)

    def test_backup_sits_beside_not_me_under_dot_claude_not_in_the_state_dir(self):
        self.operator_config()
        seen = {}
        rec = self.rec()
        orig = rec.__call__

        def spy(argv, env, **opts):
            if argv[0] == "claude":
                seen["backup"] = (self.home / ".claude" / "not-me.backup-n1-1-test" / "project.md").read_text(
                    encoding="utf-8")
                seen["in_state"] = any(self.state.rglob("project.md"))
            return orig(argv, env, **opts)
        self.run_main(spy)
        self.assertEqual(seen["backup"], "operator's own\n")
        self.assertFalse(seen["in_state"], "the operator's config must not sit under evals/.state")

    def test_a_live_run_is_not_called_dead(self):
        self.operator_config()
        self.dead_run("live-1", pid=os.getppid())  # the parent process stands in for a live run
        data_path = self.state / "live-1" / run_live.BACKUP_RECORD
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(self.rec()), 2)
            self.assertIn("still going", err.getvalue())
            # the only restore it names is the forced one, behind a check that the process is gone
            self.assertIn("--restore-global live-1 --force", err.getvalue())
            self.assertIn(f"ps -p {os.getppid()}", err.getvalue())
            self.assertEqual(run_live.main(["--restore-global", "live-1"], home=self.home), 1)
        self.assertIn("still going", err.getvalue())
        self.assertTrue(data_path.is_file())

    def test_a_dead_record_from_another_host_is_offered_for_restore(self):
        # the host name can change on macOS with the network, so a dead pid decides, whatever host is recorded
        self.operator_config()
        self.dead_run("far-1", host="some-other-host")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(self.rec()), 2)
        self.assertIn("--restore-global far-1`", err.getvalue())
        self.assertNotIn("still going", err.getvalue())

    def test_a_live_record_with_another_host_name_is_still_live(self):
        self.operator_config()
        self.dead_run("far-2", host="old-wifi-name", pid=os.getppid())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(self.rec()), 2)
        self.assertIn("still going", err.getvalue())

    def test_force_restores_despite_a_live_pid(self):
        self.operator_config()
        record = self.dead_run("live-2", pid=os.getppid())
        self.assertTrue(run_live.record_is_live(json.loads(record.read_text(encoding="utf-8"))))
        self.assertEqual(run_live.main(["--restore-global", "live-2", "--force"], out=io.StringIO(),
                                       home=self.home), 0)
        self.assert_operator_config()

    def test_force_without_restore_global_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(self.rec(), "--force"), 2)
        self.assertIn("--restore-global", err.getvalue())

    def test_unreadable_start_time_counts_as_live(self):
        with mock.patch.object(run_live, "process_start_time", return_value=None):
            self.assertTrue(run_live.record_is_live({"pid": os.getppid(), "created": 1.0}))

    def test_a_reused_pid_counts_dead_by_start_time(self):
        # the process behind the pid started after the record was written: the run that wrote it is gone
        with mock.patch.object(run_live, "process_start_time", return_value=5000.0):
            self.assertFalse(run_live.record_is_live({"pid": os.getppid(), "created": 1000.0}))
            self.assertTrue(run_live.record_is_live({"pid": os.getppid(), "created": 6000.0}))

    def test_process_start_time_reads_this_process(self):
        started = run_live.process_start_time(os.getpid())
        self.assertIsNotNone(started, "no way to read a process start time on this platform")
        self.assertLessEqual(started, time.time() + run_live.START_SLACK)
        self.assertGreater(started, time.time() - 6 * 3600)

    def test_liveness_with_the_real_start_time(self):
        # the parent process started before now and long after 1970
        if run_live.process_start_time(os.getppid()) is None:
            self.skipTest("cannot read the parent's start time here")
        self.assertTrue(run_live.record_is_live({"pid": os.getppid(), "created": time.time()}))
        self.assertFalse(run_live.record_is_live({"pid": os.getppid(), "created": 1.0}))

    def test_a_dead_pid_is_not_live(self):
        self.assertFalse(run_live.record_is_live({"pid": 999999999, "created": time.time()}))

    def test_two_records_restore_newest_first_by_timestamp_not_name(self):
        self.operator_config()
        older = self.dead_run("n1-10-x", created=1000.0)
        # the second run backed up the first run's scenario layer as the "original"
        (self.glob / "project.md").write_text("scenario layer\n", encoding="utf-8")
        self.dead_run("n1-9-x", created=2000.0)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(self.rec()), 2)
            self.assertIn("--restore-global n1-9-x", err.getvalue())
            self.assertEqual(run_live.main(["--restore-global", "n1-10-x"], home=self.home), 1)
        self.assertIn("newest first", err.getvalue())
        self.assertTrue(older.is_file())
        self.assertEqual(run_live.main(["--restore-global", "n1-9-x"], out=io.StringIO(), home=self.home), 0)
        self.assertEqual(run_live.main(["--restore-global", "n1-10-x"], out=io.StringIO(), home=self.home), 0)
        self.assert_operator_config()

    def test_restoring_an_older_run_while_the_newest_is_live_says_still_going(self):
        self.operator_config()
        older = self.dead_run("n1-10-x", created=1000.0)
        newest = self.dead_run("n1-11-x", pid=os.getppid())  # created now, by a process that is still running
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run_live.main(["--restore-global", "n1-10-x"], home=self.home), 1)
        self.assertIn("run n1-11-x is still going", err.getvalue())
        self.assertNotIn("newest first", err.getvalue())
        self.assertTrue(older.is_file())
        self.assertTrue(newest.is_file())

    def test_a_future_timestamp_cannot_jump_the_queue_and_is_named(self):
        self.operator_config()
        self.dead_run("real-1")
        forged = self.state / "forged-1" / run_live.BACKUP_RECORD
        forged.parent.mkdir(parents=True)
        forged.write_text(json.dumps({
            "original": str(self.glob.absolute()),
            "backup": str(run_live.backup_path(self.home, "forged-1").absolute()),
            "pid": 999999999, "created": 1e12}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(self.rec()), 2)
        text = err.getvalue()
        self.assertIn("--restore-global real-1`", text)
        self.assertIn("tampered", text)
        self.assertIn(str(forged), text)
        self.assertIn("in the future", text)
        self.assertEqual(run_live.main(["--restore-global", "real-1"], out=io.StringIO(), home=self.home), 0)
        self.assertEqual((self.glob / "project.md").read_text(encoding="utf-8"), "operator's own\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run_live.main(["--restore-global", "forged-1"], home=self.home), 1)
        self.assertIn(f"delete {forged}", err.getvalue())
        self.assertEqual((self.glob / "project.md").read_text(encoding="utf-8"), "operator's own\n")

    def test_a_mismatched_newest_record_does_not_block_restoring_the_real_one(self):
        # A record with foreign paths is tampered: it is named when a new run is refused, but it is never
        # "newest" and never blocks restoring a real record.
        self.operator_config()
        self.dead_run("real-2", created=1000.0)
        (self.glob / "project.md").write_text("scenario layer\n", encoding="utf-8")
        bad = self.dead_run("bad-2")
        data = json.loads(bad.read_text(encoding="utf-8"))
        data["original"] = str(self.tmp / "elsewhere")
        bad.write_text(json.dumps(data), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(self.rec()), 2)
        self.assertIn("--restore-global real-2`", err.getvalue())
        self.assertIn(f"{bad} (it does not point at", err.getvalue())
        self.assertEqual(run_live.main(["--restore-global", "real-2"], out=io.StringIO(), home=self.home), 0)
        self.assertEqual((self.glob / "project.md").read_text(encoding="utf-8"), "operator's own\n")
        self.assertFalse((self.state / "real-2" / run_live.BACKUP_RECORD).exists())
        self.assertTrue(bad.is_file())

    def test_a_forged_live_record_from_the_future_does_not_block_restoring_the_real_one(self):
        self.operator_config()
        self.dead_run("real-3", created=1000.0)
        forged = self.state / "forged-3" / run_live.BACKUP_RECORD
        forged.parent.mkdir(parents=True)
        forged.write_text(json.dumps({
            "original": str(self.glob.absolute()),
            "backup": str(run_live.backup_path(self.home, "forged-3").absolute()),
            "pid": os.getppid(), "created": 1e12}), encoding="utf-8")
        # given the raw `created`, the forged record would look live
        self.assertTrue(run_live.record_is_live(json.loads(forged.read_text(encoding="utf-8"))))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.run_main(self.rec()), 2)
        self.assertNotIn("still going", err.getvalue())
        self.assertIn("in the future", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run_live.main(["--restore-global", "real-3"], out=io.StringIO(), home=self.home), 0)
        self.assertEqual((self.glob / "project.md").read_text(encoding="utf-8"), "operator's own\n")
        self.assertTrue(forged.is_file())

    def test_a_forged_record_and_planted_backup_cannot_lose_the_operators_config(self):
        self.operator_config()
        forged = self.state / "forged-4" / run_live.BACKUP_RECORD
        forged.parent.mkdir(parents=True)
        forged.write_text(json.dumps({
            "original": str(self.glob.absolute()),
            "backup": str(run_live.backup_path(self.home, "forged-4").absolute()),
            "had_original": True, "pid": 999999999, "created": 1.0}), encoding="utf-8")
        planted = run_live.backup_path(self.home, "forged-4")
        planted.mkdir(parents=True)
        (planted / "project.md").write_text("---\nmode: live\n---\n", encoding="utf-8")
        buf = io.StringIO()
        self.assertEqual(run_live.main(["--restore-global", "forged-4"], out=buf, home=self.home), 0)
        aside = self.replaced("forged-4")
        self.assertEqual((aside / "project.md").read_text(encoding="utf-8"), "operator's own\n")
        self.assertTrue((aside / "allowed-repos.json").is_file())
        self.assertIn(str(aside), buf.getvalue())

    def test_replaced_path_takes_a_suffix_when_taken(self):
        self.operator_config()
        self.replaced("x-1").mkdir(parents=True)
        (self.replaced("x-1").parent / "not-me.replaced-x-1-2").mkdir()
        aside = run_live._move_aside(self.glob, self.replaced("x-1"))
        self.assertEqual(aside, self.replaced("x-1").parent / "not-me.replaced-x-1-3")
        self.assertEqual((aside / "project.md").read_text(encoding="utf-8"), "operator's own\n")
        self.assertFalse(self.glob.exists())

    def test_run_loop_stops_at_the_first_restore_failure(self):
        self.operator_config()
        rec = self.rec()

        real_move = run_live._move_aside

        def move_then_recreate(path, replaced):
            moved = real_move(path, replaced)
            if Path(path) == self.glob:
                self.glob.mkdir(parents=True)
                (self.glob / "racer").write_text("x", encoding="utf-8")
            return moved
        with mock.patch.object(run_live, "_move_aside", move_then_recreate), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = run_live.main(self.args("--runs", "3"), runner=rec, rid_factory=rids, out=io.StringIO(),
                                 home=self.home)
        self.assertEqual(code, 1)
        self.assertEqual(rec.names().count("reset"), 1, "no second run after a failed restore")
        self.assertIn("could not be restored", err.getvalue())
        merged = self.result()
        self.assertFalse(merged["ok"])
        self.assertTrue(any("could not restore" in e for e in merged["errors"]))
        # the operator's config is intact in the backup, not nested inside the recreated directory
        self.assertEqual((self.home / ".claude" / "not-me.backup-n1-1-test" / "project.md").read_text(
            encoding="utf-8"), "operator's own\n")
        self.assertFalse((self.glob / "not-me.backup-n1-1-test").exists())
        self.assertTrue((self.state / "n1-1-test" / run_live.BACKUP_RECORD).is_file())

    def test_run_restore_ignores_a_rewritten_record(self):
        self.operator_config()
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "keep").write_text("x", encoding="utf-8")
        rec = self.rec()
        orig = rec.__call__

        def spy(argv, env, **opts):
            if argv[0] == "claude":  # the session rewrites the record
                p = self.state / "n1-1-test" / run_live.BACKUP_RECORD
                p.write_text(json.dumps({"original": str(victim), "backup": str(self.tmp), "had_original": False}),
                             encoding="utf-8")
            return orig(argv, env, **opts)
        self.run_main(spy)
        self.assertTrue((victim / "keep").is_file())
        self.assert_operator_config()

    def test_restore_global_refuses_a_record_that_points_elsewhere(self):
        self.operator_config()
        record = self.dead_run("dead-3")
        data = json.loads(record.read_text(encoding="utf-8"))
        victim = self.tmp / "victim"
        victim.mkdir()
        data["original"] = str(victim)
        record.write_text(json.dumps(data), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run_live.main(["--restore-global", "dead-3"], home=self.home), 1)
        self.assertIn("does not point at", err.getvalue())
        self.assertIn(f"delete {record}", err.getvalue())
        self.assertTrue(victim.is_dir())
        self.assertTrue((self.home / ".claude" / "not-me.backup-dead-3").is_dir())

    def test_an_existing_backup_directory_fails_the_run_and_touches_nothing(self):
        self.operator_config()
        foreign = run_live.backup_path(self.home, "n1-1-test")
        foreign.mkdir(parents=True)
        (foreign / "marker").write_text("someone else's\n", encoding="utf-8")
        rec = self.rec()
        self.assertEqual(self.run_main(rec), 1)
        self.assertNotIn("stage", rec.names())
        self.assert_operator_config()
        self.assertFalse((self.glob / "marker").exists())
        self.assertEqual((foreign / "marker").read_text(encoding="utf-8"), "someone else's\n")
        self.assertTrue(any("already exists" in e for e in self.result()["errors"]))

    def test_a_layer_that_vanishes_before_the_backup_is_not_left_as_the_operators(self):
        self.operator_config()
        rec = self.rec()
        orig = rec.__call__

        def spy(argv, env, **opts):
            code = orig(argv, env, **opts)
            if rec.calls[-1][0] == "prepare":  # after the run started, before backup_global looks
                shutil.rmtree(str(self.glob))
            return code
        self.run_main(spy)
        self.assertEqual(rec.global_at_launch, "scenario layer\n")
        self.assertFalse(self.glob.exists(), "the scenario layer must not stay behind as the operator's config")
        self.assertFalse(list(self.state.glob("*/global-backup.json")))

    def test_backup_refuses_when_the_rename_fails_and_moves_nothing(self):
        self.operator_config()
        with mock.patch.object(run_live.os, "rename", side_effect=OSError(18, "Invalid cross-device link")):
            with self.assertRaises(RuntimeError) as ctx:
                run_live.backup_global(self.home, "x-1", self.state / "x-1" / run_live.BACKUP_RECORD)
        self.assertIn("same filesystem", str(ctx.exception))
        self.assertFalse((self.state / "x-1" / run_live.BACKUP_RECORD).exists())
        self.assert_operator_config()

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_dangling_symlink_is_backed_up_and_restored(self):
        self.glob.parent.mkdir(parents=True)
        os.symlink(str(self.tmp / "nowhere"), str(self.glob))
        self.run_main(self.rec())
        self.assertTrue(os.path.islink(str(self.glob)))
        self.assertEqual(os.readlink(str(self.glob)), str(self.tmp / "nowhere"))

    def test_restore_global_never_deletes_when_the_backup_is_missing(self):
        # A session can rewrite the record; had_original: false must not turn into a delete of the real config.
        # It renames whatever is at ~/.claude/not-me aside and says where it went.
        self.operator_config()
        record = self.dead_run("dead-9")
        shutil.rmtree(str(run_live.backup_path(self.home, "dead-9")))
        data = json.loads(record.read_text(encoding="utf-8"))
        data["had_original"] = False
        record.write_text(json.dumps(data), encoding="utf-8")
        buf = io.StringIO()
        self.assertEqual(run_live.main(["--restore-global", "dead-9"], out=buf, home=self.home), 0)
        aside = self.replaced("dead-9")
        self.assertTrue((aside / "project.md").is_file())  # kept, whatever it holds
        self.assertFalse(self.glob.exists())
        self.assertIn(f"renamed to {aside}", buf.getvalue())
        self.assertNotIn("by hand, then", buf.getvalue())
        self.assertFalse(record.exists())

    def test_restore_global_missing_backup_with_had_original_true_is_left_alone(self):
        self.operator_config()
        record = self.dead_run("dead-8")
        shutil.rmtree(str(run_live.backup_path(self.home, "dead-8")))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run_live.main(["--restore-global", "dead-8"], home=self.home), 1)
        self.assertIn("no backup", err.getvalue())
        self.assertTrue((self.glob / "project.md").is_file())
        self.assertTrue(record.is_file())

    def test_a_backup_missing_at_restore_time_fails_the_run_and_keeps_the_record(self):
        self.operator_config()
        rec = self.rec()
        orig = rec.__call__

        def spy(argv, env, **opts):
            if argv[0] == "claude":
                shutil.rmtree(str(run_live.backup_path(self.home, "n1-1-test")))
            return orig(argv, env, **opts)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_main(spy), 1)
        errors = " ".join(self.result()["errors"])
        self.assertIn(str(run_live.backup_path(self.home, "n1-1-test")), errors)
        self.assertTrue((self.state / "n1-1-test" / run_live.BACKUP_RECORD).is_file())
        # the scenario layer (mode: live and all) is not left in place as the operator's config
        self.assertFalse(self.glob.exists())
        self.assertEqual((self.replaced("n1-1-test") / "project.md").read_text(encoding="utf-8"),
                         "scenario layer\n")

    def test_a_record_that_cannot_be_deleted_after_restore_global_is_reported_as_leftover(self):
        self.operator_config()
        record = self.dead_run("dead-7")
        real_unlink = Path.unlink

        def unlink(path, *a, **kw):
            if path.name == run_live.BACKUP_RECORD:
                raise PermissionError("denied")
            return real_unlink(path, *a, **kw)
        buf = io.StringIO()
        with mock.patch.object(Path, "unlink", unlink):
            self.assertEqual(run_live.main(["--restore-global", "dead-7"], out=buf, home=self.home), 0)
        self.assertIn("restored", buf.getvalue())
        self.assertIn(f"Leftover record at {record}, delete it", buf.getvalue())
        self.assertNotIn("could not restore", buf.getvalue())
        self.assertEqual((self.glob / "project.md").read_text(encoding="utf-8"), "operator's own\n")

    def test_a_record_that_cannot_be_deleted_after_a_run_is_not_a_failed_restore(self):
        self.operator_config()
        real_unlink = Path.unlink

        def unlink(path, *a, **kw):
            if path.name == run_live.BACKUP_RECORD:
                raise PermissionError("denied")
            return real_unlink(path, *a, **kw)
        with mock.patch.object(Path, "unlink", unlink), contextlib.redirect_stderr(io.StringIO()) as err:
            code = run_live.main(self.args("--runs", "2"), runner=self.rec(), rid_factory=rids, out=io.StringIO(),
                                 home=self.home)
        self.assertEqual(code, 1)
        self.assertNotIn("could not be restored", err.getvalue())
        errors = self.result()["errors"]
        self.assertFalse(any("could not restore" in e for e in errors))
        record = self.state / "n1-1-test" / run_live.BACKUP_RECORD
        self.assertTrue(any(f"leftover record at {record}, delete it" in e for e in errors), errors)
        self.assertEqual((self.glob / "project.md").read_text(encoding="utf-8"), "operator's own\n")

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_force_remove_never_chmods_through_a_symlink(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        os.chmod(str(outside), 0o644)
        before = os.stat(str(outside)).st_mode
        ws = self.tmp / "ws"
        locked = ws / "locked"
        locked.mkdir(parents=True)
        os.symlink(str(outside), str(locked / "link"))
        os.chmod(str(locked), 0o500)
        self.addCleanup(lambda: locked.exists() and os.chmod(str(locked), 0o700))
        try:
            run_live._force_remove(ws)
        except OSError:
            pass  # the unwritable directory may stop the delete; the point is the outside file
        self.assertEqual(os.stat(str(outside)).st_mode, before)

    def test_restore_global_unknown_run_is_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_live.main(["--restore-global", "nope"], home=self.home), 1)

    def test_restore_global_rejects_a_path_like_run_id(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_live.main(["--restore-global", "../x"], home=self.home), 2)

    def test_scenario_must_look_like_an_id(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = run_live.main(self.args("--scenario", "../../x"), runner=self.rec(), out=io.StringIO(),
                                 home=self.home)
        self.assertEqual(code, 2)

    def test_restore_after_crash_before_the_move_keeps_the_operators_config(self):
        self.operator_config()
        record = self.state / "dead-2" / run_live.BACKUP_RECORD
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"original": str(self.glob.absolute()),
                                      "backup": str((self.home / ".claude" / "not-me.backup-dead-2").absolute()),
                                      "had_original": True, "pid": 999999999, "host": "h", "created": 1.0}),
                          encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "no backup"):
            run_live.restore_global(self.state, "dead-2", self.home)
        self.assertEqual((self.glob / "project.md").read_text(encoding="utf-8"), "operator's own\n")  # untouched
        self.assertTrue(record.is_file())  # the person checks the config and clears the record by hand

    # credentials

    def test_launch_env_blocks_global_git_config_and_ssh(self):
        rec = self.rec()
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/agent", "GIT_CONFIG_PARAMETERS": "'x=y'"}):
            self.run_main(rec)
        env = rec.call("launch")[2]
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("GIT_CONFIG_PARAMETERS", env)
        self.assertEqual(env["GIT_SSH_COMMAND"], "false")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(PurePath(env["GIT_CONFIG_GLOBAL"]).parts[-3:], ("n1-1-test", "N1.stage", "gitconfig-empty"))

    def test_staged_token_file_is_gone_before_the_session_starts(self):
        rec = self.rec()
        seen = {}
        orig = rec.__call__

        def spy(argv, env, **opts):
            if argv[0] == "claude":
                seen["exists"] = (self.state / "n1-1-test" / "N1.stage" / "gh-token").exists()
            return orig(argv, env, **opts)
        self.assertEqual(self.run_main(spy), 0)
        self.assertFalse(seen["exists"])

    def test_session_id_that_is_not_a_uuid_is_ignored(self):
        rec = self.rec(session_id="../../etc/passwd", transcript_name="abc")
        self.assertEqual(self.run_main(rec), 0)
        merged = self.result()
        self.assertIsNone(merged["session"]["session_id"])
        self.assertTrue(merged["session"]["transcript"].endswith(".jsonl"))

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
