"""The live-mode marker: `mode: live` counts only when signed with the operator token by `notme mode live`."""
from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path

from notme import humans
from tests import TempCase, sign_live

LIVE = "---\nschema: 1\nmode: live\n---\n# Project\n"
DRY = "---\nschema: 1\nmode: dry-run\n---\n# Project\n"



class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.tc = TempCase()
        self.addCleanup(self.tc.cleanup)
        self.home = self.tc.home
        self.home.mkdir(parents=True, exist_ok=True)

    def test_a_written_marker_verifies(self):
        token = humans.create_token(self.home)
        path = humans.write_live_marker(self.home)
        self.assertEqual(path, self.home / humans.LIVE_MARKER)
        self.assertTrue(humans.live_confirmed(self.home))
        text = path.read_text(encoding="utf-8")
        self.assertNotIn(token, text)
        self.assertRegex(text.strip(), r"^[0-9a-f]{64}$")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.endswith(".tmp")], [])

    def test_no_marker_wrong_marker_or_no_token_is_not_confirmed(self):
        self.assertFalse(humans.live_confirmed(self.home))
        self.assertFalse(humans.live_confirmed(None))
        humans.create_token(self.home)
        self.assertFalse(humans.live_confirmed(self.home))
        (self.home / humans.LIVE_MARKER).write_text("0" * 64 + "\n", encoding="utf-8")
        self.assertFalse(humans.live_confirmed(self.home))
        (self.home / humans.LIVE_MARKER).write_text("x" * 5000, encoding="utf-8")
        self.assertFalse(humans.live_confirmed(self.home))
        humans.write_live_marker(self.home)
        humans.token_path(self.home).unlink()
        self.assertFalse(humans.live_confirmed(self.home))

    def test_rotating_the_token_invalidates_the_marker(self):
        sign_live(self.home)
        humans.token_path(self.home).unlink()
        humans.create_token(self.home)
        self.assertFalse(humans.live_confirmed(self.home))

    def test_a_marker_is_bound_to_its_home(self):
        sign_live(self.home)
        other = self.tc.root / "other-home"
        other.mkdir()
        os.replace(str(humans.token_path(self.home)), str(humans.token_path(other)))  # same token, new home
        (other / humans.LIVE_MARKER).write_bytes((self.home / humans.LIVE_MARKER).read_bytes())
        self.assertFalse(humans.live_confirmed(other))

    def test_writing_without_a_token_fails_and_clear_removes(self):
        with self.assertRaises(ValueError):
            humans.write_live_marker(self.home)
        self.assertFalse(humans.clear_live_marker(self.home))
        sign_live(self.home)
        self.assertTrue(humans.clear_live_marker(self.home))
        self.assertFalse((self.home / humans.LIVE_MARKER).exists())


class EpochTests(unittest.TestCase):
    """The epoch keys the marker too, and going back to dry-run replaces it: no saved marker can be replayed."""

    def setUp(self):
        self.tc = TempCase()
        self.addCleanup(self.tc.cleanup)
        self.home = self.tc.home
        self.home.mkdir(parents=True, exist_ok=True)
        self.epoch = self.home / humans.LIVE_EPOCH

    def test_signing_creates_a_private_epoch(self):
        self.assertFalse(self.epoch.exists())
        sign_live(self.home)
        text = self.epoch.read_text(encoding="utf-8").strip()
        self.assertRegex(text, r"^[0-9a-f]{64}$")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.epoch.stat().st_mode), 0o600)
        sign_live(self.home)  # signing again keeps the epoch
        self.assertEqual(self.epoch.read_text(encoding="utf-8").strip(), text)

    def test_a_saved_marker_does_not_verify_after_dry_run(self):
        marker = sign_live(self.home)
        saved = marker.read_bytes()
        old_epoch = self.epoch.read_bytes()
        self.assertTrue(humans.clear_live_marker(self.home))
        self.assertNotEqual(self.epoch.read_bytes(), old_epoch)
        marker.write_bytes(saved)  # the agent restores its copy
        self.assertFalse(humans.live_confirmed(self.home))

    def test_clear_replaces_the_epoch_even_without_a_marker(self):
        sign_live(self.home)
        humans.live_marker_path(self.home).unlink()
        before = self.epoch.read_bytes()
        self.assertFalse(humans.clear_live_marker(self.home))
        self.assertNotEqual(self.epoch.read_bytes(), before)

    def test_missing_or_garbled_epoch_is_unconfirmed(self):
        sign_live(self.home)
        good = self.epoch.read_bytes()
        for bad in (b"", b"zz" * 32, b"\xff\xfe" * 40, b"a" * 5000):
            self.epoch.write_bytes(bad)
            if os.name == "posix":
                os.chmod(str(self.epoch), 0o600)
            self.assertFalse(humans.live_confirmed(self.home), bad[:8])
        self.epoch.unlink()
        self.assertFalse(humans.live_confirmed(self.home))
        self.epoch.write_bytes(good)
        if os.name == "posix":
            os.chmod(str(self.epoch), 0o600)
            self.assertTrue(humans.live_confirmed(self.home))
            os.chmod(str(self.epoch), 0o644)  # readable by others: not trusted
            self.assertFalse(humans.live_confirmed(self.home))

    def test_a_v1_marker_does_not_verify(self):
        import hashlib
        import hmac as hmac_mod

        token = humans.create_token(self.home)
        humans.new_live_epoch(self.home)
        bound = os.path.normcase(os.path.realpath(str(self.home)))
        v1 = hmac_mod.new(token.encode("utf-8"), b"not-me mode live v1\0" + bound.encode("utf-8"),
                          hashlib.sha256).hexdigest()
        humans.live_marker_path(self.home).write_text(v1 + "\n", encoding="utf-8")
        self.assertFalse(humans.live_confirmed(self.home))

    def test_signing_repairs_a_garbled_epoch(self):
        humans.create_token(self.home)
        self.epoch.write_text("garbled\n", encoding="utf-8")
        humans.write_live_marker(self.home)
        self.assertTrue(humans.live_confirmed(self.home))


class EffectiveModeTests(unittest.TestCase):
    def setUp(self):
        self.tc = TempCase()
        self.addCleanup(self.tc.cleanup)

    def mode(self):
        cfg = self.tc.load()
        return cfg, cfg.meta["project"]["mode"]

    def assert_downgraded(self, cfg):
        self.assertEqual(cfg.meta["project"]["mode"], "dry-run")
        msgs = [w for w in cfg.warnings if "not confirmed" in w]
        self.assertEqual(len(msgs), 1, cfg.warnings)
        self.assertIn("project.md:3", msgs[0])
        self.assertIn("/not-me:mode live", msgs[0])
        self.assertEqual(cfg.live_unconfirmed, msgs[0])
        entry = cfg.explain()["keys"]["project.mode"]
        self.assertEqual(entry["value"], "dry-run")
        self.assertEqual(entry["source"]["layer"], "global")
        self.assertEqual(entry["downgraded"]["configured"], "live")
        self.assertIn("not confirmed", entry["downgraded"]["reason"])

    def test_global_live_with_a_valid_marker_is_live(self):
        self.tc.write("global", "project", LIVE)
        sign_live(self.tc.home)
        cfg, mode = self.mode()
        self.assertEqual(mode, "live")
        self.assertIsNone(cfg.live_unconfirmed)
        self.assertFalse([w for w in cfg.warnings if "not confirmed" in w])
        self.assertNotIn("downgraded", cfg.explain()["keys"]["project.mode"])

    def test_global_live_without_a_marker_is_dry_run_with_a_warning(self):
        self.tc.write("global", "project", LIVE)
        humans.create_token(self.tc.home)
        self.assert_downgraded(self.tc.load())

    def test_global_live_with_a_wrong_or_stale_marker_is_dry_run(self):
        self.tc.write("global", "project", LIVE)
        sign_live(self.tc.home)
        humans.token_path(self.tc.home).unlink()
        humans.create_token(self.tc.home)  # rotated
        self.assert_downgraded(self.tc.load())
        (self.tc.home / humans.LIVE_MARKER).write_text("f" * 64, encoding="utf-8")
        self.assert_downgraded(self.tc.load())

    def test_global_live_without_a_token_file_is_dry_run(self):
        self.tc.write("global", "project", LIVE)
        sign_live(self.tc.home)
        humans.token_path(self.tc.home).unlink()
        self.assert_downgraded(self.tc.load())

    def test_repo_layer_live_is_never_honoured_even_with_a_marker(self):
        self.tc.write("global", "project", DRY)
        self.tc.write("repo", "project", LIVE)
        sign_live(self.tc.home)
        cfg, mode = self.mode()
        self.assertEqual(mode, "dry-run")
        self.assertIsNone(cfg.live_unconfirmed)

    def test_global_dry_run_with_a_valid_marker_stays_dry_run(self):
        self.tc.write("global", "project", DRY)
        sign_live(self.tc.home)
        cfg, mode = self.mode()
        self.assertEqual(mode, "dry-run")
        self.assertIsNone(cfg.live_unconfirmed)
        self.assertNotIn("downgraded", cfg.explain()["keys"]["project.mode"])


if __name__ == "__main__":
    unittest.main()
