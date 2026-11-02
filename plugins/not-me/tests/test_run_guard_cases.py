from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from unittest import mock

from tests import run_guard_cases as rgc

CASE = ("c1", "block", None, "Bash", {"command": "git push"}, "app")
CASE2 = ("c2", "block", None, "Bash", {"command": "git push"}, "app")


def _proc(rc: int, err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=b"", stderr=err.encode("utf-8"))


class RetryOnSignalTest(unittest.TestCase):
    def _main(self, results, cases=(CASE,)):
        fake = mock.Mock(side_effect=results)
        out = io.StringIO()
        with mock.patch.object(rgc.gc, "CASES", list(cases)), \
                mock.patch.object(rgc.gc, "MESSAGE_CHECKS", {}), \
                mock.patch.object(rgc.gc, "build_fixtures", return_value={"PATH": "x"}), \
                mock.patch.object(rgc.subprocess, "run", fake), \
                contextlib.redirect_stdout(out):
            rc = rgc.main([])
        return rc, out.getvalue(), fake

    def test_crash_then_pass_counts_as_passed(self):
        rc, out, fake = self._main([_proc(-11, "Fatal Python error: Segmentation fault"), _proc(2)])
        self.assertEqual(rc, 0)
        self.assertEqual(fake.call_count, 2)
        self.assertIn("RETRY c1: hook killed by signal 11, passed on retry", out)
        self.assertIn("Fatal Python error: Segmentation fault", out)
        self.assertIn("guard cases: 1 run, 1 passed, 0 failed", out)

    def test_crash_twice_fails(self):
        rc, out, fake = self._main([_proc(-11, "first trace"), _proc(-11, "second trace")])
        self.assertEqual(rc, 1)
        self.assertEqual(fake.call_count, 2)
        self.assertIn("FAIL c1", out)
        self.assertIn("crashed twice (signal 11)", out)
        self.assertIn("first trace", out)
        self.assertIn("second trace", out)
        self.assertIn("guard cases: 1 run, 0 passed, 1 failed", out)

    def test_crash_then_wrong_answer_fails_normally(self):
        rc, out, fake = self._main([_proc(-11, "first trace"), _proc(0)])
        self.assertEqual(rc, 1)
        self.assertEqual(fake.call_count, 2)
        self.assertIn("want block, got allow", out)
        self.assertIn("first trace", out)

    def test_wrong_answer_is_not_retried(self):
        rc, out, fake = self._main([_proc(0), _proc(2)])
        self.assertEqual(rc, 1)
        self.assertEqual(fake.call_count, 1)
        self.assertIn("want block, got allow", out)
        self.assertNotIn("RETRY", out)

    def test_hook_env_enables_faulthandler(self):
        _, _, fake = self._main([_proc(2)])
        env = fake.call_args.kwargs["env"]
        self.assertEqual(env["PYTHONFAULTHANDLER"], "1")
        self.assertEqual(env["PATH"], "x")

    def test_timeout_fails_and_run_continues(self):
        timeout = subprocess.TimeoutExpired(cmd=["hook"], timeout=60, stderr=b"stuck in decide")
        rc, out, fake = self._main([timeout, _proc(2)], cases=(CASE, CASE2))
        self.assertEqual(rc, 1)
        self.assertEqual(fake.call_count, 2)
        line = next(x for x in out.splitlines() if x.startswith("FAIL c1"))
        self.assertIn("timed out after 60s", line)
        self.assertIn("stuck in decide", out)
        self.assertNotIn("FAIL c2", out)
        self.assertNotIn("RETRY", out)
        self.assertIn("guard cases: 2 run, 1 passed, 1 failed (c1)", out)

    def test_timeout_without_stderr(self):
        rc, out, fake = self._main([subprocess.TimeoutExpired(cmd=["hook"], timeout=60)])
        self.assertEqual(rc, 1)
        self.assertEqual(fake.call_count, 1)
        self.assertIn("timed out after 60s", out)

    def test_summary_names_failed_and_retried(self):
        rc, out, _ = self._main([_proc(-11, "trace"), _proc(2), _proc(0)], cases=(CASE, CASE2))
        self.assertEqual(rc, 1)
        self.assertIn("guard cases: 2 run, 1 passed, 1 failed (c2); retried: c1", out)

    def test_summary_clean_run_has_no_suffix(self):
        _, out, _ = self._main([_proc(2)])
        self.assertEqual(out.strip().splitlines()[-1], "guard cases: 1 run, 1 passed, 0 failed")


if __name__ == "__main__":
    unittest.main()
