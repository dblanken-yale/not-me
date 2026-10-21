from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from notme.dryrun import Ledger, LedgerError
from tests.fakes import POSIX_MODES, symlink_or_skip


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="notme-ledger-")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.ledger = Ledger(self.dir / "logs" / "intended-writes.jsonl")

    def test_read_missing_is_empty(self):
        self.assertEqual(self.ledger.read(), [])

    def test_append_with_payload(self):
        body = self.dir / "body.md"
        body.write_text("hello **world**\n", encoding="utf-8")
        entry = self.ledger.append("gh issue comment", "acme/app", ["issue", "comment", "1"], body, "say hi")
        sha = hashlib.sha256(body.read_bytes()).hexdigest()
        self.assertEqual(entry["payload_sha256"], sha)
        self.assertEqual(entry["payload_path"], f"payloads/{sha}.md")
        copied = self.ledger.path.parent / "payloads" / f"{sha}.md"
        self.assertEqual(copied.read_text(), "hello **world**\n")
        [read] = self.ledger.read()
        self.assertEqual(set(read), {"ts", "op", "target", "argv", "payload_sha256", "payload_path", "description"})
        self.assertEqual(read["argv"], ["issue", "comment", "1"])
        self.assertEqual(read["description"], "say hi")

    def test_payload_copy_is_private(self):
        import os
        entry = self.ledger.append("a", "t", ["x"], payload_bytes=b"hello")
        copied = self.ledger.path.parent / entry["payload_path"]
        if POSIX_MODES:
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
            self.assertEqual(copied.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(copied.read_bytes(), b"hello")

    def test_payload_copy_never_follows_a_planted_symlink(self):
        import hashlib
        target = self.dir / "victim.txt"
        target.write_text("keep me", encoding="utf-8")
        sha = hashlib.sha256(b"payload").hexdigest()
        planted = self.ledger.path.parent / "payloads" / f"{sha}.md"
        planted.parent.mkdir(parents=True)
        symlink_or_skip(self, planted, target)
        with self.assertRaises(LedgerError):
            self.ledger.append("a", "t", ["x"], payload_bytes=b"payload")
        self.assertEqual(target.read_text(), "keep me")

    def test_symlinked_ledger_file_is_refused(self):
        victim = self.dir / "victim.log"
        victim.write_text("keep", encoding="utf-8")
        self.ledger.path.parent.mkdir(parents=True)
        symlink_or_skip(self, self.ledger.path, victim)
        with self.assertRaises(LedgerError):
            self.ledger.append("a", "t", ["x"])
        self.assertEqual(victim.read_text(), "keep")

    def test_symlinked_ledger_dir_is_refused(self):
        real = self.dir / "elsewhere"
        real.mkdir()
        link = self.dir / "linked"
        symlink_or_skip(self, link, real)
        with self.assertRaises(LedgerError):
            Ledger(link / "intended-writes.jsonl").append("a", "t", ["x"], payload_bytes=b"p")
        self.assertEqual(list(real.iterdir()), [])

    def test_symlinked_intermediate_dir_is_refused(self):
        base = self.dir / "base"
        base.mkdir()
        real = self.dir / "elsewhere"
        real.mkdir()
        symlink_or_skip(self, (base / "not-me"), real)
        ledger = Ledger(base / "not-me" / "logs" / "intended-writes.jsonl", base=base)
        with self.assertRaises(LedgerError):
            ledger.append("a", "t", ["x"], payload_bytes=b"p")
        self.assertEqual(list(real.iterdir()), [])

    def test_symlinked_not_me_fallback_base_refused(self):
        project = self.dir / "plain2"
        project.mkdir()
        real = self.dir / "elsewhere2"
        real.mkdir()
        symlink_or_skip(self, (project / ".not-me"), real)
        with self.assertRaises(LedgerError):
            Ledger.default_for(project).append("a", "t", ["x"])
        self.assertEqual(list(real.iterdir()), [])

    def test_missing_tail_is_created_private_one_by_one(self):
        base = self.dir / "base2"
        base.mkdir()
        ledger = Ledger(base / "not-me" / "logs" / "intended-writes.jsonl", base=base)
        ledger.append("a", "t", ["x"], payload_bytes=b"p")
        for d in (base / "not-me", base / "not-me" / "logs", base / "not-me" / "logs" / "payloads"):
            if POSIX_MODES:
                self.assertEqual(d.stat().st_mode & 0o777, 0o700)

    def test_path_outside_base_refused(self):
        with self.assertRaises(LedgerError):
            Ledger(self.dir / "x" / "l.jsonl", base=self.dir / "other").append("a", "t", ["x"])

    def test_ledger_file_is_private_and_appends(self):
        self.ledger.append("a", "t", ["x"])
        self.ledger.append("b", "t", ["y"])
        if POSIX_MODES:
            self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.ledger.read()), 2)

    def test_append_without_payload(self):
        entry = self.ledger.append("gh api", "acme/app", ["api", "x"])
        self.assertIsNone(entry["payload_sha256"])
        self.assertIsNone(entry["payload_path"])

    def test_entries_accumulate_and_bad_lines_skipped(self):
        self.ledger.append("a", "t", ["x"])
        with open(self.ledger.path, "a", encoding="utf-8") as fh:
            fh.write("garbage\n")
        self.ledger.append("b", "t", ["y"])
        self.assertEqual([e["op"] for e in self.ledger.read()], ["a", "b"])

    def test_default_for_uses_git_common_dir(self):
        subprocess.run(["git", "init", "-q", str(self.dir / "repo")], check=True)
        ledger = Ledger.default_for(self.dir / "repo")
        self.assertEqual(ledger.path, (self.dir / "repo" / ".git").resolve() / "not-me" / "logs" / "intended-writes.jsonl")

    def test_default_for_outside_git_stays_in_project(self):
        plain = self.dir / "plain"
        plain.mkdir()
        self.assertTrue(str(Ledger.default_for(plain).path).startswith(str(plain)))


if __name__ == "__main__":
    unittest.main()
