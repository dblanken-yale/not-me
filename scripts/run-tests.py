#!/usr/bin/env python3
"""Run the not-me unit tests in parallel worker processes. Exits 1 on a failure, an error, a crashed worker, or when
zero tests ran; 2 on a usage error.

Work is split by test class: every class (with its setUpClass) is one unit, handed to the next free worker, the
classes that took longest last time first. Each worker sets sys.path and discovers the suites exactly as a serial
run does, then runs the units it is given. --jobs 1 runs every unit in one worker, one after another.
"""
from __future__ import annotations

import argparse
import atexit
import concurrent.futures as futures
import fnmatch
import hashlib
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import OrderedDict
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "not-me"
TESTS = PLUGIN / "tests"
FIXTURE = ROOT / "fixture"
SCRIPT_TESTS = ROOT / "scripts" / "tests"
DOC_TESTS = ROOT / "docs" / "tests"
FIXTURE_TESTS = FIXTURE / "tests"
HARNESS = ROOT / "evals-harness"
HARNESS_TESTS = HARNESS / "tests"

# (start_dir, top_level_dir, extra sys.path entry)
SUITE_NAMES = {
    "plugin": (TESTS, PLUGIN, PLUGIN),
    "scripts": (SCRIPT_TESTS, SCRIPT_TESTS, None),
    "docs": (DOC_TESTS, DOC_TESTS, None),
    "fixture": (FIXTURE_TESTS, FIXTURE_TESTS, FIXTURE),
    "harness": (HARNESS_TESTS, HARNESS_TESTS, HARNESS),
}
SUITES = list(SUITE_NAMES.values())

MAX_DEFAULT_JOBS = 8  # past this the git-heavy tests mostly wait on each other

Suite = Tuple[str, str, Optional[str]]  # as SUITES, with str paths (sent to workers)


# --- discovery (the same in the parent and in every worker) ----------------

def discover(suites: Sequence[Suite]) -> unittest.TestSuite:
    """sys.path and discovery exactly as a serial run in one process does them, in the same order."""
    sys.path.insert(0, str(PLUGIN))
    suite = unittest.TestSuite()
    for start_dir, top, extra in suites:
        if extra is not None and str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
        suite.addTests(unittest.defaultTestLoader.discover(start_dir=str(start_dir), top_level_dir=str(top)))
    return suite


def flatten(suite: unittest.TestSuite) -> List[unittest.TestCase]:
    out: List[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            out.extend(flatten(item))
        else:
            out.append(item)
    return out


def _load_failure(test: unittest.TestCase) -> bool:
    """A stand-in unittest makes for a module that failed to import or load."""
    return type(test).__module__ == "unittest.loader"


def unit_key(test: unittest.TestCase) -> str:
    if _load_failure(test):  # one unit per module that failed
        return test.id()
    cls = type(test)
    return f"{cls.__module__}.{cls.__qualname__}"


def matches(test_id: str, patterns: Sequence[str]) -> bool:
    """unittest -k semantics: a pattern without a wildcard is a substring."""
    return not patterns or any(fnmatch.fnmatchcase(test_id, p if "*" in p else f"*{p}*") for p in patterns)


def plan(suite: unittest.TestSuite, patterns: Sequence[str]) -> "OrderedDict[str, List[str]]":
    units: "OrderedDict[str, List[str]]" = OrderedDict()
    for test in flatten(suite):
        if _load_failure(test) or matches(test.id(), patterns):  # a module that fails to import always counts
            units.setdefault(unit_key(test), []).append(test.id())
    return units


# --- the worker ------------------------------------------------------------

_TESTS: Dict[str, List[unittest.TestCase]] = {}


def _private_temp_dir(run_root: str) -> None:
    """Give this worker its own temp dir under the run's root (removed when it exits; the parent removes the root
    after the run, which also catches a worker that crashed), so tests that check what lands in the temp dir, or in
    the not-me dir under it, never see another worker's files. Subprocesses inherit it through os.environ."""
    path = tempfile.mkdtemp(prefix="worker-", dir=run_root)
    for name in ("TMPDIR", "TEMP", "TMP", "XDG_RUNTIME_DIR"):  # notme prefers XDG_RUNTIME_DIR when it is set
        os.environ[name] = path
    tempfile.tempdir = None  # recompute gettempdir() from the environment
    atexit.register(shutil.rmtree, path, ignore_errors=True)


def _worker_init(base_path: List[str], suites: Sequence[Suite], run_root: str) -> None:
    _private_temp_dir(run_root)
    sys.path[:] = base_path  # the parent's path before its own discovery, so the order is the serial order
    for test in flatten(discover(suites)):
        _TESTS.setdefault(test.id(), []).append(test)


class _Collect(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.marks: List[str] = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.marks.append(".")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.marks.append("F")

    def addError(self, test, err):
        super().addError(test, err)
        self.marks.append("E")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.marks.append("s")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.marks.append("x")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.marks.append("u")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.marks.append("F" if issubclass(err[0], test.failureException) else "E")


def _describe(test: Any) -> str:
    doc = test.shortDescription()
    return f"{test}\n{doc}" if doc else str(test)


def run_unit(key: str, ids: List[str]) -> Dict[str, Any]:
    started = time.perf_counter()
    suite = unittest.TestSuite()
    missing = []
    for test_id in ids:
        found = _TESTS.pop(test_id, None)
        if found:
            suite.addTests(found)
        else:
            missing.append(test_id)
    result = _Collect()
    suite.run(result)
    errors = [(_describe(t), tb) for t, tb in result.errors]
    errors += [(m, "the worker did not discover this test") for m in missing]
    return {
        "key": key, "pid": os.getpid(), "seconds": time.perf_counter() - started, "run": result.testsRun,
        "marks": "".join(result.marks),
        "failures": [(_describe(t), tb) for t, tb in result.failures], "errors": errors,
        "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
        "unexpected_successes": [str(t) for t in result.unexpectedSuccesses],
    }


# --- the parent ------------------------------------------------------------

def times_file(suites: Sequence[Suite]) -> Path:
    """Where the last run's unit times are kept for these suites (an ordering hint only)."""
    digest = hashlib.sha256(json.dumps([list(s) for s in suites]).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"notme-run-tests-{digest}.json"


def _load_times(path: Path) -> Dict[str, float]:
    if path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_times(path: Path, times: Dict[str, float]) -> None:
    """Write a fresh file and rename it over the old one, so a symlink planted at the name is replaced, not
    followed. Skipped when the name is a symlink."""
    if path.is_symlink():
        return
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(times, sort_keys=True))
        os.replace(tmp, str(path))
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass  # only an ordering hint


def default_jobs() -> int:
    return max(1, min(os.cpu_count() or 1, MAX_DEFAULT_JOBS))


def _failed_unit(key: str, count: int, message: str) -> Dict[str, Any]:
    return {"key": key, "pid": None, "seconds": 0.0, "run": count, "marks": "E", "failures": [],
            "errors": [(key, message)], "skipped": 0, "expected_failures": 0, "unexpected_successes": []}


def _run_pool(order: List[str], units: Dict[str, List[str]], workers: int, initargs: tuple, collect) -> List[str]:
    """Run the units on a pool of spawned workers, passing each result to collect. Returns the units lost to a
    worker that died (in order); empty when every unit reported."""
    ctx = multiprocessing.get_context("spawn")  # the same on every OS; no state leaks from the parent
    lost: List[str] = []
    with futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_worker_init,
                                     initargs=initargs) as pool:
        pending = {pool.submit(run_unit, k, units[k]): k for k in order}
        for fut in futures.as_completed(pending):
            key = pending[fut]
            try:
                res = fut.result()
            except BrokenProcessPool:
                lost.append(key)
                continue
            except Exception as exc:  # a result that could not come back (it would not pickle, say)
                res = _failed_unit(key, len(units[key]), f"{type(exc).__name__}: {exc}")
            collect(res)
    return [k for k in order if k in lost]


def run(suites: Sequence[Suite], jobs: int, patterns: Sequence[str], out=sys.stdout, err=sys.stderr) -> int:
    started = time.perf_counter()
    base_path = list(sys.path)
    units = plan(discover(suites), patterns)
    sys.path[:] = base_path
    times_path = times_file(suites)
    times = _load_times(times_path)
    # longest first (by last run's time, else by test count) so no long class starts last
    order = sorted(units, key=lambda k: (-times.get(k, 0.0), -len(units[k])))
    workers = max(1, min(jobs, len(order)))
    results: List[Dict[str, Any]] = []

    def collect(res: Dict[str, Any]) -> None:
        results.append(res)
        err.write(res["marks"])
        err.flush()

    run_root = tempfile.mkdtemp(prefix="notme-run-tests-tmp-")  # holds each worker's private temp dir
    initargs = (base_path, list(suites), run_root)
    crashed: List[str] = []
    try:
        unfinished = _run_pool(order, units, workers, initargs, collect) if order else []
        if unfinished:
            # a worker died and took every unit not yet reported with it: run each of those alone in a fresh
            # worker, so the rest still report and the crash is pinned on the unit that caused it
            err.write(f"\n[a worker crashed; running {len(unfinished)} unfinished units one per worker]\n")
            with futures.ThreadPoolExecutor(max_workers=workers) as threads:
                for key, lost in zip(unfinished, threads.map(
                        lambda k: _run_pool([k], units, 1, initargs, collect), unfinished)):
                    if lost:
                        crashed.append(key)
    finally:
        shutil.rmtree(run_root, ignore_errors=True)
    err.write("\n")
    wall = time.perf_counter() - started
    return report(results, crashed, units, workers, wall, out, times, times_path)


def report(results, crashed, units, workers, wall, out, times, times_path) -> int:
    total = sum(r["run"] for r in results)
    failures = sum(len(r["failures"]) for r in results)
    errors = sum(len(r["errors"]) for r in results)
    unexpected = sum(len(r["unexpected_successes"]) for r in results)
    for r in sorted(results, key=lambda r: r["key"]):
        if not (r["failures"] or r["errors"] or r["unexpected_successes"]):
            continue
        print("=" * 70, file=out)
        print(f"{r['key']}  (worker {r['pid']}, {r['seconds']:.1f}s)", file=out)
        for kind, items in (("FAIL", r["failures"]), ("ERROR", r["errors"])):
            for desc, tb in items:
                print("-" * 70, file=out)
                print(f"{kind}: {desc}", file=out)
                print(tb.rstrip(), file=out)
        for desc in r["unexpected_successes"]:
            print("-" * 70, file=out)
            print(f"UNEXPECTED SUCCESS: {desc}", file=out)
    if crashed:
        print("=" * 70, file=out)
        print("CRASH: the worker process died while running each of these units, alone (their tests are not "
              "counted; run one with -k and --jobs 1 to look closer):", file=out)
        for key in sorted(crashed):
            print(f"  {key} ({len(units[key])} tests)", file=out)
    print("-" * 70, file=out)
    slow = sorted(results, key=lambda r: -r["seconds"])[:5]
    if slow:
        print("slowest units: " + ", ".join(f"{r['key']} {r['seconds']:.1f}s" for r in slow), file=out)
    print(f"ran {total} tests in {wall:.1f}s wall ({workers} worker{'s' if workers != 1 else ''}, "
          f"{len(units)} units)", file=out)
    if not crashed:
        times.update({r["key"]: round(r["seconds"], 2) for r in results})
        _save_times(times_path, times)
    if total == 0 and not crashed:
        print("error: zero tests ran", file=out)
        return 1
    if failures or errors or unexpected or crashed:
        parts = [f"failures={failures}", f"errors={errors}"]
        if unexpected:
            parts.append(f"unexpected successes={unexpected}")
        if crashed:
            parts.append(f"crashed units={len(crashed)}")
        print(f"FAILED ({', '.join(parts)})", file=out)
        return 1
    skipped = sum(r["skipped"] for r in results)
    print("OK" + (f" (skipped={skipped})" if skipped else ""), file=out)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-j", "--jobs", type=int, default=0,
                        help=f"worker processes (default: CPU count, at most {MAX_DEFAULT_JOBS}; 1 runs serially)")
    parser.add_argument("--suite", action="append", default=[], metavar="NAME",
                        help=f"run only this suite ({', '.join(SUITE_NAMES)}); repeat for more")
    parser.add_argument("--suite-dir", action="append", default=[], metavar="DIR",
                        help="run the tests found under DIR (its own top-level dir) instead of the built-in suites")
    parser.add_argument("-k", action="append", default=[], dest="patterns", metavar="PATTERN",
                        help="run only tests whose id matches (substring, or a * pattern); repeat for more")
    args = parser.parse_args(argv)
    if args.jobs < 0:
        parser.error("--jobs must be 0 (auto) or more")
    unknown = [n for n in args.suite if n not in SUITE_NAMES]
    if unknown:
        parser.error(f"unknown suite {', '.join(unknown)}; known: {', '.join(SUITE_NAMES)}")
    if args.suite_dir:
        suites: List[Suite] = [(str(Path(d).resolve()), str(Path(d).resolve()), None) for d in args.suite_dir]
    else:
        picked = [SUITE_NAMES[n] for n in args.suite] if args.suite else SUITES
        suites = [(str(d), str(top), None if extra is None else str(extra)) for d, top, extra in picked]
    present = [s for s in suites if Path(s[0]).is_dir()]
    if not present:
        print("no tests yet")
        return 0
    return run(present, args.jobs or default_jobs(), args.patterns)


if __name__ == "__main__":
    sys.exit(main())
