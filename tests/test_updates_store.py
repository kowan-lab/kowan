"""DB snapshot/restore/retention and the `updates` table (lock, history, pruning)."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kowan.store import Store
from kowan.updater import dbsnap


def _rec(i, status="updated", comp="server", host=None, started=None):
    return {"id": f"u{i}", "component": comp, "hostId": host, "mode": "update", "status": status,
            "startedAt": started or f"2026-10-25T10:{i:02d}:00Z", "phases": [{"phase": "lock", "ok": True}],
            "logs": ["a"]}


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "kowan.db")
        st = Store(self.db)  # WAL mode, real schema
        st.set_setting("k", "before")
        self.store = st
        Path(self.dir, "secret.key").write_bytes(b"K" * 32)

    def tearDown(self):
        self.store.close()

    def _setting(self, path=None):
        c = sqlite3.connect(path or self.db)
        try:
            return c.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0]
        finally:
            c.close()

    def test_snapshot_is_consistent_while_open_and_has_keys(self):
        snap = dbsnap.take_snapshot(self.db, self.dir, "u1")
        self.assertTrue(snap.endswith("backups/pre-update-u1.db"))
        self.assertEqual(self._setting(snap), "before")
        self.assertTrue(Path(self.dir, "backups", "pre-update-u1.secret.key").is_file())
        dbsnap.quick_check(snap)
        leftovers = sorted(p.name for p in Path(self.dir, "backups").iterdir())
        self.assertEqual(leftovers, ["pre-update-u1.db", "pre-update-u1.secret.key"])

    def test_restore_moves_current_aside_and_restores(self):
        snap = dbsnap.take_snapshot(self.db, self.dir, "u1")
        self.store.set_setting("k", "after-migration")
        self.store.close()
        Path(self.db + "-wal").write_bytes(b"stale wal")
        Path(self.dir, "secret.key").write_bytes(b"X" * 32)
        aside = os.path.join(self.dir, "backups", "rolled-back-u1")
        info = dbsnap.restore_snapshot(self.db, snap, aside)
        self.assertEqual(self._setting(), "before")
        self.assertFalse(Path(self.db + "-wal").exists())
        self.assertTrue(Path(aside, "kowan.db").is_file(), "old db kept, never deleted")
        self.assertEqual(Path(self.dir, "secret.key").read_bytes(), b"K" * 32)
        self.assertEqual(info["keys"], ["secret.key"])
        self.store = Store(self.db)  # reopenable

    def test_restore_keeps_update_history(self):
        self.store.save_update(_rec(1, "updated"))
        snap = dbsnap.take_snapshot(self.db, self.dir, "u2")
        # after the snapshot: the record of this very update, and a status change
        self.store.save_update(_rec(2, "running"))
        self.store.save_update(_rec(1, "rolled_back"))
        self.store.close()
        info = dbsnap.restore_snapshot(self.db, snap, os.path.join(self.dir, "backups", "rb"))
        self.assertEqual(info["carried"], ["updates"])
        self.store = Store(self.db)
        self.assertEqual(self.store.get_update("u2")["status"], "running")
        self.assertEqual(self.store.get_update("u1")["status"], "rolled_back")

    def test_corrupt_snapshot_leaves_live_db_untouched(self):
        bad = os.path.join(self.dir, "bad.db")
        Path(bad).write_bytes(b"not a database at all" * 100)
        with self.assertRaises(dbsnap.SnapshotError):
            dbsnap.restore_snapshot(self.db, bad, os.path.join(self.dir, "aside"))
        self.assertEqual(self._setting(), "before")
        with self.assertRaises(dbsnap.SnapshotError):
            dbsnap.restore_snapshot(self.db, os.path.join(self.dir, "missing.db"), os.path.join(self.dir, "aside"))

    def test_prune_keeps_newest_and_protected(self):
        for i in range(6):
            p = dbsnap.take_snapshot(self.db, self.dir, f"s{i}")
            os.utime(p, (1000 + i, 1000 + i))
        old_pf = Path(self.dir, "backups", "preflight-x.db")
        old_pf.write_bytes(b"")
        os.utime(old_pf, (1, 1))
        removed = dbsnap.prune_snapshots(self.dir, keep=3, protect={"s0"})
        left = sorted(p.name for p in Path(self.dir, "backups").glob("pre-update-*.db"))
        self.assertEqual(left, ["pre-update-s0.db", "pre-update-s3.db", "pre-update-s4.db", "pre-update-s5.db"])
        self.assertFalse(Path(self.dir, "backups", "pre-update-s1.secret.key").exists())
        self.assertFalse(old_pf.exists())
        self.assertTrue(removed)


class TestUpdatesTable(unittest.TestCase):
    def setUp(self):
        self.st = Store(":memory:")

    def test_roundtrip_shape(self):
        self.st.save_update(_rec(1, host="h1", comp="agent"))
        r = self.st.get_update("u1")
        self.assertEqual(r["hostId"], "h1")
        self.assertEqual(r["phases"], [{"phase": "lock", "ok": True}])
        self.assertEqual(set(r) >= {"fromImage", "toVersion", "finishedAt", "startedBy", "logs"}, True)
        self.st.save_update(_rec(2))
        self.assertIsNone(self.st.get_update("u2")["hostId"])

    def test_lock_conflict_and_stale(self):
        self.assertIsNone(self.st.acquire_update(_rec(1, "running"), "2026-10-25T09:00:00Z"))
        blocking = self.st.acquire_update(_rec(2, "running"), "2026-10-25T09:00:00Z")
        self.assertEqual(blocking["id"], "u1")
        # other component / other host are independent
        self.assertIsNone(self.st.acquire_update(_rec(3, "running", comp="agent", host="h1"), "x"))
        self.assertIsNone(self.st.acquire_update(_rec(4, "running", comp="agent", host="h2"), "x"))
        # stale: lock older than the cutoff is marked unknown, new one wins
        self.assertIsNone(self.st.acquire_update(_rec(5, "running"), "2026-10-25T11:00:00Z"))
        self.assertEqual(self.st.get_update("u1")["status"], "unknown")
        self.assertIn("stale", self.st.get_update("u1")["error"])

    def test_list_last_and_prune(self):
        for i in range(60):
            self.st.save_update(_rec(i, "updated" if i % 2 else "rolled_back"))
        self.st.save_update(_rec(0, "running", started="2026-10-25T09:00:00Z"))
        self.assertEqual(len(self.st.list_updates("server", None, 5)), 5)
        self.assertEqual(self.st.list_updates("server", None, 5)[0]["id"], "u59")
        self.assertEqual(self.st.last_update("server", None, ["rolled_back"])["id"], "u58")
        self.st.prune_updates("server", None, 50)
        ids = {r["id"] for r in self.st.list_updates("server", None, 200)}
        self.assertIn("u0", ids, "running records are never pruned")
        self.assertEqual(len(ids), 51)
        self.assertEqual([r["id"] for r in self.st.list_updates_by_status(["running"])], ["u0"])


if __name__ == "__main__":
    unittest.main()


class TestSnapshotEdges(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "kowan.db")
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT INTO settings VALUES ('k', 'v')")
        c.commit()
        c.close()

    def test_snapshot_of_a_broken_database_fails_cleanly(self):
        Path(self.db).write_bytes(b"garbage" * 200)
        with self.assertRaises(dbsnap.SnapshotError):
            dbsnap.take_snapshot(self.db, self.dir, "bad")
        self.assertFalse([p for p in Path(self.dir, "backups").iterdir() if p.name.endswith(".db")])

    def test_preflight_copy_and_rolled_back_pruning(self):
        snap = dbsnap.take_snapshot(self.db, self.dir, "u1")
        copy = dbsnap.copy_for_preflight(snap, self.dir, "u1")
        self.assertTrue(Path(copy).is_file())
        for i in range(5):
            d = Path(self.dir, "backups", f"rolled-back-{i}")
            d.mkdir()
            os.utime(d, (1000 + i, 1000 + i))
        os.utime(copy, (1, 1))
        removed = dbsnap.prune_snapshots(self.dir, keep=2)
        self.assertIn(copy, removed)
        self.assertEqual(len([p for p in Path(self.dir, "backups").glob("rolled-back-*")]), 2)
        self.assertEqual(dbsnap.prune_snapshots(os.path.join(self.dir, "nowhere")), [])

    def test_carry_tables_is_best_effort(self):
        self.assertEqual(dbsnap.carry_tables(self.db, os.path.join(self.dir, "missing.db"), ["updates"]), [])
        other = os.path.join(self.dir, "old.db")
        c = sqlite3.connect(other)
        c.execute("CREATE TABLE updates (id TEXT PRIMARY KEY, status TEXT)")
        c.execute("INSERT INTO updates VALUES ('u1', 'ok')")
        c.execute("CREATE TABLE nocols (zzz TEXT)")
        c.commit()
        c.close()
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE nocols (aaa TEXT)")
        c.commit()
        c.close()
        self.assertEqual(dbsnap.carry_tables(self.db, other, ["updates", "nocols", "absent"]), ["updates"])
        c = sqlite3.connect(self.db)
        self.assertEqual(c.execute("SELECT status FROM updates").fetchone()[0], "ok")
        c.close()

    def test_restore_rolls_files_back_on_error(self):
        snap = dbsnap.take_snapshot(self.db, self.dir, "u1")
        aside = os.path.join(self.dir, "aside")
        with mock.patch("kowan.updater.dbsnap.shutil.copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(dbsnap.SnapshotError):
                dbsnap.restore_snapshot(self.db, snap, aside)
        self.assertTrue(Path(self.db).is_file())  # the live database was put back
