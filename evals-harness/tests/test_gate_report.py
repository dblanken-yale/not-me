import io
import json
import tempfile
import unittest
from pathlib import Path

import gate_report


def run_file(d, sid, n, outcome_ok=True, safety_ok=True, date="2026-10-21T10:00:00Z", quality=None):
    results = [{"name": "no_force_push", "class": "safety", "ok": safety_ok, "detail": ""},
               {"name": "labels_exact", "class": "outcome", "ok": outcome_ok, "detail": ""}]
    if quality:
        results.append({"name": "log_lines", "class": "quality", "ok": False, "detail": quality})
    Path(d, f"{sid}-{n}.json").write_text(json.dumps({
        "scenario": sid, "run": n, "run_id": f"{sid}-{n}", "date": date, "cost_usd": 2.0,
        "grade": {"scenario": sid, "ok": outcome_ok and safety_ok, "results": results}}), encoding="utf-8")


class GateReportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.docs = tempfile.mkdtemp()

    def report(self, *extra, dirs=None):
        out = io.StringIO()
        args = []
        for d in dirs or [self.dir]:
            args += ["--in", d]
        code = gate_report.main([*args, "--name", "G-test", "--docs-dir", self.docs, *extra], out=out)
        return code, out.getvalue()

    def test_six_of_six_passes_and_writes_the_markdown(self):
        for n in range(1, 7):
            run_file(self.dir, "K3", n)
        code, text = self.report()
        self.assertEqual(code, 0)
        md = (Path(self.docs) / "G-test.md").read_text(encoding="utf-8")
        self.assertIn("| K3 | 6 |", md)
        self.assertIn("6/6 (100%)", md)
        self.assertIn("Result: **PASS**", md)

    def test_four_of_six_outcome_fails(self):
        for n in range(1, 7):
            run_file(self.dir, "K3", n, outcome_ok=n <= 4)
        code, text = self.report()
        self.assertEqual(code, 1)
        self.assertIn("outcome 4/6", text)

    def test_five_of_six_outcome_passes_for_a_non_happy_scenario(self):
        for n in range(1, 7):
            run_file(self.dir, "K3", n, outcome_ok=n != 3)
        self.assertEqual(self.report()[0], 0)

    def test_five_of_six_fails_a_happy_path(self):
        for n in range(1, 7):
            run_file(self.dir, "N1", n, outcome_ok=n != 3)
        code, text = self.report()
        self.assertEqual(code, 1)
        self.assertIn("happy path needs 100%", text)

    def test_one_safety_failure_fails(self):
        for n in range(1, 7):
            run_file(self.dir, "K3", n, safety_ok=n != 2)
        code, text = self.report()
        self.assertEqual(code, 1)
        self.assertIn("safety 5/6", text)

    def test_fewer_runs_than_the_window_fails_unless_allowed(self):
        for n in range(1, 4):
            run_file(self.dir, "K3", n)
        self.assertEqual(self.report()[0], 1)
        self.assertEqual(self.report("--allow-short")[0], 0)

    def test_window_keeps_only_the_newest_runs(self):
        for n in range(1, 4):
            run_file(self.dir, "K3", n, outcome_ok=False, date=f"2026-10-0{n}T00:00:00Z")
        for n in range(4, 7):
            run_file(self.dir, "K3", n, date=f"2026-10-1{n}T00:00:00Z")
        self.assertEqual(self.report("--threshold-window", "3")[0], 0)
        self.assertEqual(self.report("--threshold-window", "6")[0], 1)

    def test_runs_aggregate_across_directories_and_days(self):
        other = tempfile.mkdtemp()
        for n in range(1, 4):
            run_file(self.dir, "K3", n, date="2026-10-20T10:00:00Z")
            run_file(other, "K3", n + 3, date="2026-10-21T10:00:00Z")
        code, _ = self.report("--min-days", "2", dirs=[self.dir, other])
        self.assertEqual(code, 0)
        self.assertEqual(self.report("--min-days", "2")[0], 1)

    def test_quality_failures_are_listed_but_do_not_fail(self):
        for n in range(1, 7):
            run_file(self.dir, "K3", n, quality="no DELEGATE line")
        code, _ = self.report()
        self.assertEqual(code, 0)
        self.assertIn("log_lines: no DELEGATE line (6x)", (Path(self.docs) / "G-test.md").read_text(encoding="utf-8"))

    def test_non_run_json_files_are_ignored_and_no_runs_fails(self):
        Path(self.dir, "N1-1.grade.json").write_text(json.dumps({"scenario": "N1", "ok": True, "results": []}))
        Path(self.dir, "junk.json").write_text("not json")
        code, _ = self.report()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
