"""scripts/run-tests.py on tiny throwaway suites: parallel and serial runs, failures, import errors, crashes."""
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "run-tests.py"

PASSING = '''
import unittest
import helper


class AlphaTests(unittest.TestCase):
    def test_one(self):
        self.assertEqual(helper.VALUE, 42)

    def test_two(self):
        with self.subTest(n=1):
            self.assertTrue(True)


class BetaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready = True

    def test_three(self):
        self.assertTrue(self.ready)

    @unittest.skip("not here")
    def test_skipped(self):
        pass
'''

FAILING = '''
import unittest


class GammaTests(unittest.TestCase):
    def test_bad(self):
        self.assertEqual(1, 2, "gamma went wrong")

    def test_subtest_bad(self):
        for n in range(3):
            with self.subTest(n=n):
                self.assertNotEqual(n, 1, "subtest one went wrong")

    def test_good(self):
        pass
'''

BROKEN_IMPORT = '''
import unittest
import no_such_module_anywhere  # noqa: F401


class NeverTests(unittest.TestCase):
    def test_never(self):
        pass
'''

CLASS_SETUP_ERROR = '''
import unittest


class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise RuntimeError("class setup exploded")

    def test_never_runs(self):
        pass
'''

CRASH = '''
import os
import unittest


class CrashTests(unittest.TestCase):
    def test_crash(self):
        os._exit(0)  # a worker that dies without a word, even with a zero exit code
'''

TEMP_ISOLATION = '''
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PARENT_TMP = {parent!r}
SHARED = Path({shared!r})


def rendezvous(me, other):
    """Write this class's marker in its temp dir, then wait (briefly) for the other class to do the same."""
    mine = Path(tempfile.gettempdir())
    (mine / f"marker-{{me}}").write_text("x")
    (SHARED / f"{{me}}.tmp").write_text(str(mine))
    deadline = time.time() + 10
    while not (SHARED / f"{{other}}.tmp").exists() and time.time() < deadline:
        time.sleep(0.05)
    return mine


class IsoOneTests(unittest.TestCase):
    def test_own_temp_dir(self):
        mine = rendezvous("one", "two")
        self.assertNotEqual(os.path.realpath(str(mine)), os.path.realpath(PARENT_TMP))
        self.assertFalse((mine / "marker-two").exists())
        child = subprocess.run([sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(os.path.realpath(child), os.path.realpath(str(mine)))


class IsoTwoTests(unittest.TestCase):
    def test_own_temp_dir(self):
        mine = rendezvous("two", "one")
        self.assertNotEqual(os.path.realpath(str(mine)), os.path.realpath(PARENT_TMP))
        self.assertFalse((mine / "marker-one").exists())
'''


class RunTestsCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="notme-runtests-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def suite(self, name, **files):
        d = self.root / name
        d.mkdir()
        for fname, text in files.items():
            (d / f"{fname}.py").write_text(textwrap.dedent(text), encoding="utf-8")
        return d

    def run_tests(self, *args):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120,
                              cwd=str(self.root))
        return proc.returncode, proc.stdout + proc.stderr

    def ran(self, out):
        m = re.search(r"^ran (\d+) tests", out, re.M)
        self.assertIsNotNone(m, out)
        return int(m.group(1))


class PassingTests(RunTestsCase):
    def test_parallel_run_passes_and_counts_every_test(self):
        d = self.suite("ok", test_a=PASSING, helper="VALUE = 42\n")
        code, out = self.run_tests("--suite-dir", str(d), "--jobs", "2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ran(out), 4)
        self.assertRegex(out, r"wall")
        self.assertIn("OK", out)

    def test_serial_and_parallel_agree(self):
        d = self.suite("ok", test_a=PASSING, helper="VALUE = 42\n")
        other = self.suite("more", test_b=FAILING.replace("1, 2", "1, 1").replace("n, 1", "n, 7"))
        results = []
        for jobs in ("1", "3"):
            code, out = self.run_tests("--suite-dir", str(d), "--suite-dir", str(other), "--jobs", jobs)
            self.assertEqual(code, 0, out)
            results.append(self.ran(out))
        self.assertEqual(results, [7, 7])

    def test_k_runs_a_subset(self):
        d = self.suite("ok", test_a=PASSING, helper="VALUE = 42\n")
        code, out = self.run_tests("--suite-dir", str(d), "-k", "AlphaTests")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ran(out), 2)

    def test_named_suite_must_exist(self):
        code, out = self.run_tests("--suite", "no-such-suite")
        self.assertEqual(code, 2, out)
        self.assertIn("plugin", out)  # the names it knows


class FailureTests(RunTestsCase):
    def test_failure_exits_1_with_its_traceback(self):
        d = self.suite("bad", test_a=PASSING, helper="VALUE = 42\n", test_g=FAILING)
        for jobs in ("1", "2"):
            with self.subTest(jobs=jobs):
                code, out = self.run_tests("--suite-dir", str(d), "--jobs", jobs)
                self.assertEqual(code, 1, out)
                self.assertEqual(self.ran(out), 7)
                self.assertIn("gamma went wrong", out)
                self.assertIn("subtest one went wrong", out)
                self.assertIn("test_g.GammaTests", out)
                self.assertIn("Traceback", out)
                self.assertRegex(out, r"FAILED \(failures=2")

    def test_import_error_exits_1_and_names_the_module(self):
        d = self.suite("imp", test_a=PASSING, helper="VALUE = 42\n", test_broken=BROKEN_IMPORT)
        for jobs in ("1", "2"):
            with self.subTest(jobs=jobs):
                code, out = self.run_tests("--suite-dir", str(d), "--jobs", jobs)
                self.assertEqual(code, 1, out)
                self.assertIn("test_broken", out)
                self.assertIn("no_such_module_anywhere", out)

    def test_class_setup_error_exits_1(self):
        d = self.suite("cls", test_a=PASSING, helper="VALUE = 42\n", test_s=CLASS_SETUP_ERROR)
        code, out = self.run_tests("--suite-dir", str(d), "--jobs", "2")
        self.assertEqual(code, 1, out)
        self.assertIn("class setup exploded", out)

    def test_zero_tests_exits_1(self):
        d = self.suite("empty", helper="VALUE = 42\n")
        for jobs in ("1", "2"):
            with self.subTest(jobs=jobs):
                code, out = self.run_tests("--suite-dir", str(d), "--jobs", jobs)
                self.assertEqual(code, 1, out)
                self.assertIn("zero tests ran", out)

    def test_k_matching_nothing_exits_1(self):
        d = self.suite("ok", test_a=PASSING, helper="VALUE = 42\n")
        code, out = self.run_tests("--suite-dir", str(d), "-k", "NoSuchTest")
        self.assertEqual(code, 1, out)
        self.assertIn("zero tests ran", out)

    def test_worker_crash_exits_1_and_says_so(self):
        d = self.suite("crash", test_a=PASSING, helper="VALUE = 42\n", test_c=CRASH)
        for jobs in ("1", "2"):
            with self.subTest(jobs=jobs):
                code, out = self.run_tests("--suite-dir", str(d), "--jobs", jobs)
                self.assertEqual(code, 1, out)
                self.assertIn("crashed units=1", out)
                self.assertRegex(out, r"CRASH: .*\n  test_c\.CrashTests \(1 tests\)\n-")  # pinned on that unit only
                self.assertEqual(self.ran(out), 4)  # every other unit still ran and reported


class TempIsolationTests(RunTestsCase):
    def test_each_worker_gets_its_own_temp_dir_and_removes_it(self):
        shared = self.root / "shared"
        shared.mkdir()
        text = TEMP_ISOLATION.format(parent=tempfile.gettempdir(), shared=str(shared))
        d = self.suite("iso", test_iso=text)
        code, out = self.run_tests("--suite-dir", str(d), "--jobs", "2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ran(out), 2)
        seen = [Path((shared / f"{n}.tmp").read_text()) for n in ("one", "two")]
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(seen[0].parent, seen[1].parent)  # both under the run's root
        self.assertFalse(seen[0].parent.exists(), f"{seen[0].parent} left behind")


if __name__ == "__main__":
    unittest.main()
