import contextlib
import io
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path, PurePath
from unittest import mock

import _support as s
import run_live


def rids(scenario, n):
    return f"{scenario.lower()}-{n}-test"


class Recorder:
    """A runner that records each command and can fail or write files for chosen steps."""

    def __init__(self, fail=(), grade_ok=True, write_outputs=True):
        self.calls = []
        self.fail = set(fail)
        self.grade_ok = grade_ok
        self.write_outputs = write_outputs

    def step(self, argv):
        joined = " ".join(argv)
        for name, marker in (("grade", "grade_state.py"), ("eval", "plugin eval"), ("prepare", "scaffold.py prepare"),
                             ("clean", "scaffold.py clean"), ("reset", "--reset"),
                             ("teardown", "--teardown"), ("seed", "--scenario")):
            if marker in joined:
                return name
        return "?"

    def __call__(self, argv, env):
        name = self.step(argv)
        self.calls.append((name, list(argv), dict(env)))
        if self.write_outputs and name == "eval":
            d = Path(argv[argv.index("--json") + 1])
            d.mkdir(parents=True, exist_ok=True)
            (d / "aggregate-result.json").write_text(json.dumps({"summary": {"total_cost_usd": 1.5}}), encoding="utf-8")
        if self.write_outputs and name == "grade":
            Path(argv[argv.index("--json") + 1]).write_text(json.dumps(
                {"scenario": "N1", "backend": "github", "ok": self.grade_ok, "results": [
                    {"name": "x", "class": "outcome", "ok": self.grade_ok, "detail": ""}]}), encoding="utf-8")
        return 1 if name in self.fail else 0

    def names(self):
        return [n for n, _, _ in self.calls]


class RunLiveTest(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"NOT_ME_FIXTURE_TOKEN": "fixture-token", "GH_TOKEN": "operator-token"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, *extra):
        return ["--skill", "new", "--scenario", "N1", "--repo", "acme/x-fixture", "--companion",
                "acme/x-fixture-companion", "--owner", "acme", "--out", self.out, *extra]

    def test_dry_run_prints_the_command_sequence_and_runs_nothing(self):
        rec, buf = Recorder(), io.StringIO()
        code = run_live.main(self.args("--dry-run"), runner=rec, rid_factory=rids, out=buf)
        self.assertEqual(code, 0)
        self.assertEqual(rec.calls, [])
        lines = [l for l in buf.getvalue().splitlines() if l.startswith("+ ") and not l.startswith("+ (")]
        kinds = []
        for line in lines:
            tokens = shlex.split(line[2:])
            script = PurePath(tokens[0] if tokens[0] == "claude" else tokens[1]).name
            if script == "seed_fixture.py":
                kinds.append("reset" if "--reset" in tokens else "teardown" if "--teardown" in tokens else "seed")
            elif script == "scaffold.py":
                kinds.append(tokens[2])
            elif script == "grade_state.py":
                kinds.append("grade")
            elif tokens[:3] == ["claude", "plugin", "eval"]:
                kinds.append("eval")
        self.assertEqual(kinds, ["reset", "seed", "prepare", "eval", "grade", "clean", "teardown"])
        self.assertEqual(list(Path(self.out).iterdir()), [])

    def test_dry_run_eval_command_has_the_required_flags(self):
        buf = io.StringIO()
        run_live.main(self.args("--dry-run", "--cap", "4"), runner=Recorder(), rid_factory=rids, out=buf)
        line = next(l for l in buf.getvalue().splitlines() if "claude plugin eval" in l)
        for piece in ("--case new-N1", "--runs 1", "--scaffold", "--trust-plugin", "--ablation none",
                      "--allow-tools Bash,Write,Edit", "--max-cost-usd 4", "--no-publish", "--json"):
            self.assertIn(piece, line)

    def test_real_run_runs_in_order_merges_and_tears_down(self):
        rec = Recorder()
        code = run_live.main(self.args(), runner=rec, rid_factory=rids, out=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(rec.names(), ["reset", "seed", "prepare", "eval", "grade", "clean", "teardown"])
        merged = json.loads((Path(self.out) / "N1-1.json").read_text(encoding="utf-8"))
        self.assertTrue(merged["ok"])
        self.assertEqual(merged["cost_usd"], 1.5)
        self.assertEqual(merged["grade"]["scenario"], "N1")
        self.assertEqual(merged["run_id"], "n1-1-test")

    def test_eval_gets_only_eval_keys_and_no_tokens(self):
        rec = Recorder()
        run_live.main(self.args(), runner=rec, rid_factory=rids, out=io.StringIO())
        env = next(e for n, _, e in rec.calls if n == "eval")
        self.assertEqual(env["EVAL_NOT_ME_RUN_ID"], "n1-1-test")
        self.assertEqual(env["EVAL_NOT_ME_SCENARIO"], "N1")
        self.assertEqual(PurePath(env["EVAL_NOT_ME_STAGE_DIR"]).parts[-2:], ("n1-1-test", "N1.stage"))
        self.assertNotIn("NOT_ME_FIXTURE_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)

    def test_only_prepare_sees_the_fixture_token(self):
        rec = Recorder()
        run_live.main(self.args(), runner=rec, rid_factory=rids, out=io.StringIO())
        prep = next(e for n, _, e in rec.calls if n == "prepare")
        self.assertEqual(prep["NOT_ME_FIXTURE_TOKEN"], "fixture-token")
        self.assertEqual(prep["NOT_ME_FIXTURE_OWNER"], "acme")
        argv = next(a for n, a, _ in rec.calls if n == "prepare")
        self.assertIn("--stage-dir", argv)
        self.assertIn("--manifest", argv)

    def test_prepare_failure_still_cleans_and_tears_down(self):
        rec = Recorder(fail={"prepare"})
        code = run_live.main(self.args(), runner=rec, rid_factory=rids, out=io.StringIO())
        self.assertEqual(code, 1)
        self.assertEqual(rec.names(), ["reset", "seed", "prepare", "clean", "teardown"])

    def test_missing_fixture_token_is_refused_before_anything_runs(self):
        rec = Recorder()
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("NOT_ME_FIXTURE_TOKEN")
            with contextlib.redirect_stderr(io.StringIO()):
                code = run_live.main(self.args(), runner=rec, rid_factory=rids, out=io.StringIO())
        self.assertEqual((code, rec.calls), (2, []))

    def test_keep_temp_is_forwarded_to_the_eval(self):
        rec = Recorder()
        run_live.main(self.args("--keep-temp"), runner=rec, rid_factory=rids, out=io.StringIO())
        self.assertIn("--keep-temp", next(a for n, a, _ in rec.calls if n == "eval"))

    def test_teardown_runs_even_when_the_seed_fails(self):
        rec = Recorder(fail={"seed"})
        code = run_live.main(self.args(), runner=rec, rid_factory=rids, out=io.StringIO())
        self.assertEqual(code, 1)
        self.assertEqual(rec.names(), ["reset", "seed", "clean", "teardown"])
        merged = json.loads((Path(self.out) / "N1-1.json").read_text(encoding="utf-8"))
        self.assertFalse(merged["ok"])
        self.assertIn("seed failed", merged["errors"])

    def test_teardown_runs_when_the_runner_raises(self):
        class Boom(Recorder):
            def __call__(self, argv, env):
                if self.step(argv) == "eval":
                    raise OSError("claude not found")
                return super().__call__(argv, env)
        rec = Boom()
        code = run_live.main(self.args(), runner=rec, rid_factory=rids, out=io.StringIO())
        self.assertEqual(code, 1)
        self.assertEqual(rec.names()[-2:], ["clean", "teardown"])

    def test_failed_grade_gives_exit_1_but_still_records_the_result(self):
        rec = Recorder(grade_ok=False, fail={"grade"})
        code = run_live.main(self.args(), runner=rec, rid_factory=rids, out=io.StringIO())
        self.assertEqual(code, 1)
        merged = json.loads((Path(self.out) / "N1-1.json").read_text(encoding="utf-8"))
        self.assertFalse(merged["grade"]["ok"])

    def test_second_invocation_adds_runs_instead_of_overwriting(self):
        run_live.main(self.args(), runner=Recorder(), rid_factory=rids, out=io.StringIO())
        run_live.main(self.args("--runs", "2"), runner=Recorder(), rid_factory=rids, out=io.StringIO())
        names = sorted(p.name for p in Path(self.out).glob("N1-?.json"))
        self.assertEqual(names, ["N1-1.json", "N1-2.json", "N1-3.json"])

    def test_ledger_backend_reads_the_kept_ledger(self):
        rec = Recorder()
        run_live.main(self.args("--backend", "ledger", "--keep-temp", "--ledger", "/kept/intended-writes.jsonl"),
                      runner=rec, rid_factory=rids, out=io.StringIO())
        grade = next(a for n, a, _ in rec.calls if n == "grade")
        self.assertEqual(Path(grade[grade.index("--ledger") + 1]), Path("/kept/intended-writes.jsonl"))
        self.assertEqual(grade[grade.index("--backend") + 1], "ledger")

    def test_ledger_backend_without_keep_temp_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = run_live.main(self.args("--backend", "ledger"), runner=Recorder(), out=io.StringIO())
        self.assertEqual(code, 2)

    def test_missing_repo_is_an_error(self):
        env = {k: v for k, v in os.environ.items() if k not in ("NOT_ME_FIXTURE_REPO", "NOT_ME_FIXTURE_OWNER")}
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(io.StringIO()):
            code = run_live.main(["--skill", "new", "--scenario", "N1", "--owner", "acme", "--out", self.out],
                                 runner=Recorder(), out=io.StringIO())
        self.assertEqual(code, 2)

    def test_jobs_above_one_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run_live.main(self.args("-j", "2"), runner=Recorder(), out=io.StringIO())


if __name__ == "__main__":
    unittest.main()
