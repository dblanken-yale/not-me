from __future__ import annotations

import unittest

from notme import migrations


class MigrationTests(unittest.TestCase):
    def test_current_schema(self):
        self.assertEqual(migrations.CURRENT_SCHEMA, 1)

    def test_needs_upgrade(self):
        self.assertFalse(migrations.needs_upgrade({"schema": 1}))
        self.assertFalse(migrations.needs_upgrade({}))
        self.assertTrue(migrations.needs_upgrade({"schema": 0}))

    def test_schema_one_is_noop(self):
        meta, body, notes = migrations.upgrade("project", {"schema": 1, "mode": "live"}, "b")
        self.assertEqual((meta, body, notes), ({"schema": 1, "mode": "live"}, "b", []))

    def test_unknown_higher_schema_raises(self):
        with self.assertRaises(migrations.MigrationError):
            migrations.upgrade("project", {"schema": 99}, "")

    def test_registry_steps_run_in_order(self):
        calls = []

        def step(doc, meta, body):
            calls.append(doc)
            return dict(meta, schema=1, upgraded=True), body, ["renamed a key"]

        saved = dict(migrations.REGISTRY)
        migrations.REGISTRY[0] = step
        self.addCleanup(lambda: (migrations.REGISTRY.clear(), migrations.REGISTRY.update(saved)))
        meta, _, notes = migrations.upgrade("verify", {"schema": 0}, "")
        self.assertEqual(meta, {"schema": 1, "upgraded": True})
        self.assertEqual(notes, ["renamed a key"])
        self.assertEqual(calls, ["verify"])

    def test_missing_step_raises(self):
        with self.assertRaises(migrations.MigrationError):
            migrations.upgrade("project", {"schema": -5}, "")


if __name__ == "__main__":
    unittest.main()
