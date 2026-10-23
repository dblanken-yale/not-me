from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notme import claims

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


class MarkerTests(unittest.TestCase):
    def test_round_trip(self):
        body = claims.marker_body("octocat", "run-1", "2026-03-01T12:00:00Z")
        self.assertIn("not-me claimed this for octocat", body)
        self.assertEqual(claims.parse_marker(body), {"login": "octocat", "run": "run-1", "ts": "2026-03-01T12:00:00Z"})

    def test_released_marker(self):
        body = claims.marker_body("octocat", "run-1", "2026-03-01T12:00:00Z", released=True)
        self.assertIn("released", body.splitlines()[0])
        self.assertTrue(claims.is_released(claims.parse_marker(body)))

    def test_marker_records_that_the_claim_assigned(self):
        body = claims.marker_body("octocat", "run-1", "2026-03-01T12:00:00Z", released=True, assigned="claim")
        self.assertEqual(claims.parse_marker(body), {"login": "octocat", "run": "run-1", "ts": "2026-03-01T12:00:00Z",
                                                     "state": "released", "assigned": "claim"})
        self.assertNotIn("assigned", claims.parse_marker(claims.marker_body("octocat", "r", "t")))
        with self.assertRaises(ValueError):
            claims.marker_body("octocat", "r", "t", assigned="a --> b")

    def test_parse_finds_the_marker_inside_other_text(self):
        body = "hello\n<!-- not-me:claim login=hubot run=r2 ts=2026-03-01T11:00:00Z -->\nbye"
        self.assertEqual(claims.parse_marker(body)["login"], "hubot")

    def test_parse_returns_none_without_a_marker_or_fields(self):
        self.assertIsNone(claims.parse_marker("just a comment"))
        self.assertIsNone(claims.parse_marker(None))
        self.assertIsNone(claims.parse_marker("<!-- not-me:claim run=r -->"))
        self.assertIsNone(claims.parse_marker("<!-- other:claim login=a ts=b -->"))

    def test_unsafe_values_cannot_be_written_into_a_marker(self):
        with self.assertRaises(ValueError):
            claims.marker_body("octo cat", "r", "t")
        with self.assertRaises(ValueError):
            claims.marker_body("octocat", "r --> <!--", "t")


class StaleTests(unittest.TestCase):
    def test_stale_boundaries(self):
        fresh = claims.iso(NOW - timedelta(hours=7, minutes=59))
        old = claims.iso(NOW - timedelta(hours=8, minutes=1))
        self.assertFalse(claims.stale(fresh, 8, NOW))
        self.assertTrue(claims.stale(old, 8, NOW))

    def test_unreadable_timestamp_is_stale(self):
        self.assertTrue(claims.stale("yesterday-ish", 8, NOW))
        self.assertTrue(claims.stale(None, 8, NOW))

    def test_parse_ts_accepts_z_and_naive(self):
        self.assertEqual(claims.parse_ts("2026-03-01T12:00:00Z"), NOW)
        self.assertEqual(claims.parse_ts(datetime(2026, 3, 1, 12, 0, 0)), NOW)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="notme-claims-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = claims.lock_path(self.dir / "not-me" / "claims", 7)

    def test_lock_path_name(self):
        self.assertEqual(self.path.name, "issue-7.lock")

    def test_acquire_writes_json_with_run_and_ts(self):
        claims.acquire_lock(self.path, "run-1", 8, NOW)
        self.assertEqual(json.loads(self.path.read_text()), {"run_id": "run-1", "ts": "2026-03-01T12:00:00Z"})

    def test_a_live_lock_from_another_run_blocks(self):
        claims.acquire_lock(self.path, "run-1", 8, NOW)
        with self.assertRaises(claims.ClaimLost):
            claims.acquire_lock(self.path, "run-2", 8, NOW + timedelta(hours=1))

    def test_the_same_run_may_retake_its_lock(self):
        claims.acquire_lock(self.path, "run-1", 8, NOW)
        claims.acquire_lock(self.path, "run-1", 8, NOW + timedelta(hours=1))
        self.assertEqual(claims.read_lock(self.path)["ts"], "2026-03-01T13:00:00Z")

    def test_a_stale_lock_is_taken_over(self):
        claims.acquire_lock(self.path, "run-1", 8, NOW)
        claims.acquire_lock(self.path, "run-2", 8, NOW + timedelta(hours=9))
        self.assertEqual(claims.read_lock(self.path)["run_id"], "run-2")

    def test_a_corrupt_lock_is_taken_over(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json")
        claims.acquire_lock(self.path, "run-2", 8, NOW)
        self.assertEqual(claims.read_lock(self.path)["run_id"], "run-2")

    def test_release_only_removes_our_own_lock(self):
        claims.acquire_lock(self.path, "run-1", 8, NOW)
        self.assertFalse(claims.release_lock(self.path, "run-2"))
        self.assertTrue(self.path.exists())
        self.assertTrue(claims.release_lock(self.path, "run-1"))
        self.assertFalse(self.path.exists())
        self.assertFalse(claims.release_lock(self.path, "run-1"))


if __name__ == "__main__":
    unittest.main()
