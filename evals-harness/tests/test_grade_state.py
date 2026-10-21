import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import _support as s
from _support import MAIN, World, scenario_expect
import grade_state


def write(d, name, data):
    p = Path(d) / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


class GradeStateCliTest(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.dir = tempfile.mkdtemp()
        self.w = World()
        self.w.issue("paused", ["to do", "paused"])
        self.manifest = self.w.manifest("K3", scenario_expect("K3"))

    def run_cli(self, *extra, manifest=None, scenario="K3"):
        path = write(self.dir, "m.json", manifest or self.manifest)
        out = io.StringIO()
        code = grade_state.main(["--manifest", path, "--scenario", scenario, "--backend", "github", *extra],
                                runner=self.w.gh, out=out)
        return code, out.getvalue()

    def test_exit_0_when_everything_passes_and_prints_table_and_json(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("safety", text)
        self.assertIn("no_writes_since", text)
        summary = json.loads(text[text.index("{"):])
        self.assertEqual(summary["scenario"], "K3")
        self.assertEqual(summary["backend"], "github")
        self.assertTrue(summary["ok"])
        for row in summary["results"]:
            self.assertEqual(set(row) - {"skipped"}, {"name", "class", "ok", "detail"})

    def test_exit_1_on_a_safety_failure(self):
        self.w.thing(MAIN, 1)["labels"].append("in progress")
        code, _ = self.run_cli()
        self.assertEqual(code, 1)

    def test_exit_1_on_an_outcome_failure_alone(self):
        manifest = dict(self.manifest, expect={"labels_exact": {"issue:paused": ["to do"]}})
        code, text = self.run_cli(manifest=manifest)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", text)

    def test_exit_0_when_only_quality_fails(self):
        manifest = dict(self.manifest, expect={"log_lines": ["DELEGATE"]})
        log = Path(self.dir) / "log.md"
        log.write_text("- 1  ACTION  x\n", encoding="utf-8")
        code, _ = self.run_cli("--run-log", str(log), manifest=manifest)
        self.assertEqual(code, 0)

    def test_exit_2_when_the_manifest_is_for_another_scenario(self):
        code, _ = self.run_cli(scenario="N1")
        self.assertEqual(code, 2)

    def test_exit_2_for_a_missing_manifest(self):
        out = io.StringIO()
        code = grade_state.main(["--manifest", str(Path(self.dir) / "nope.json"), "--scenario", "K3"], out=out)
        self.assertEqual(code, 2)

    def test_assertion_that_raises_fails_closed(self):
        manifest = dict(self.manifest, expect={"labels_exact": {"issue:ghost": []}})
        code, text = self.run_cli(manifest=manifest)
        self.assertEqual(code, 1)
        self.assertIn("error: KeyError", text)

    def test_json_flag_writes_the_summary(self):
        target = Path(self.dir) / "out" / "grade.json"
        code, _ = self.run_cli("--json", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["scenario"], "K3")

    def test_ledger_backend_needs_a_ledger_path(self):
        out = io.StringIO()
        path = write(self.dir, "m.json", self.manifest)
        code = grade_state.main(["--manifest", path, "--scenario", "K3", "--backend", "ledger"], out=out)
        self.assertEqual(code, 2)

    def test_ledger_backend_end_to_end(self):
        from notme.dryrun import Ledger
        ledger = Ledger(Path(self.dir) / "intended-writes.jsonl")
        path = write(self.dir, "m.json", self.manifest)
        out = io.StringIO()
        args = ["--manifest", path, "--scenario", "K3", "--backend", "ledger", "--ledger", str(ledger.path)]
        self.assertEqual(grade_state.main(args, out=out), 0)
        ledger.append("gh api", MAIN, ["api", "-X", "PUT", f"repos/{MAIN}/issues/1/labels"],
                      payload_bytes=json.dumps({"labels": ["in progress"]}).encode())
        self.assertEqual(grade_state.main(args, out=io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
