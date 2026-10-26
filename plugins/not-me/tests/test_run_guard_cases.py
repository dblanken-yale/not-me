from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from unittest import mock

from tests import run_guard_cases as rgc

CASE = ("c1", "block", None, "Bash", {"command": "git push"}, "app")


def _proc(rc: int, err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=b"", stderr=err.encode("utf-8"))


class RetryOnSignalTest(unittest.TestCase):
    def _main(self, results):
        fake = mock.Mock(side_effect=results)
        out = io.StringIO()
        with mock.patch.object(rgc.gc, "CASES", [CASE]), \
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


if __name__ == "__main__":
    unittest.main()
