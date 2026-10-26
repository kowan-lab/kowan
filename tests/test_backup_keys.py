"""Backups carry the master keys, so a restore recovers a fully-working server.

The encryption key (secret.key) and JWT key (jwt.key) are archived when held as
files and swapped back into place on restore — encrypted secrets stay
decryptable and sessions stay valid with nothing to carry over by hand.
"""

import gzip
import io
import os
import tarfile
import tempfile
import unittest

from kowan.backup import (
    DB_NAME,
    JWT_KEY_NAME,
    SECRET_KEY_NAME,
    apply_pending_restore,
    create_backup,
    stage_restore,
)


class _FakeSnap:
    """Minimal Snapshotter: writes a stand-in DB file where asked."""

    def snapshot(self, dest_path: str) -> None:
        with open(dest_path, "wb") as f:
            f.write(b"SQLite format 3\x00stand-in")


def _seed_data_dir() -> str:
    d = tempfile.mkdtemp()
    with open(os.path.join(d, SECRET_KEY_NAME), "wb") as f:
        f.write(b"K" * 32)  # 32-byte master key
    with open(os.path.join(d, JWT_KEY_NAME), "w", encoding="utf-8") as f:
        f.write("jwt-signing-secret")
    return d


def _entries(archive: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    tr = tarfile.open(fileobj=gzip.GzipFile(fileobj=io.BytesIO(archive)), mode="r")
    for m in tr:
        if m.isfile():
            f = tr.extractfile(m)
            out[m.name.lstrip("./")] = f.read() if f else b""
    return out


class TestBackupKeys(unittest.TestCase):
    def test_backup_includes_key_files(self):
        data_dir = _seed_data_dir()
        buf = io.BytesIO()
        create_backup(_FakeSnap(), data_dir, buf)

        entries = _entries(buf.getvalue())
        self.assertIn(DB_NAME, entries)
        self.assertEqual(entries.get(SECRET_KEY_NAME), b"K" * 32)
        self.assertEqual(entries.get(JWT_KEY_NAME), b"jwt-signing-secret")

    def test_restore_round_trips_the_keys(self):
        src = _seed_data_dir()
        buf = io.BytesIO()
        create_backup(_FakeSnap(), src, buf)

        # Restore into a DIFFERENT, empty data dir (with different keys) to prove
        # the backup's keys win — otherwise its secrets would be undecryptable.
        dst = tempfile.mkdtemp()
        with open(os.path.join(dst, SECRET_KEY_NAME), "wb") as f:
            f.write(b"X" * 32)  # a different pre-existing key

        stage_restore(dst, io.BytesIO(buf.getvalue()))
        applied = apply_pending_restore(dst)
        self.assertTrue(applied)

        with open(os.path.join(dst, SECRET_KEY_NAME), "rb") as f:
            self.assertEqual(f.read(), b"K" * 32)  # replaced by the backup's key
        with open(os.path.join(dst, JWT_KEY_NAME), encoding="utf-8") as f:
            self.assertEqual(f.read(), "jwt-signing-secret")
        # Key files are re-tightened to 0600.
        self.assertEqual(
            os.stat(os.path.join(dst, SECRET_KEY_NAME)).st_mode & 0o777, 0o600
        )
        self.assertTrue(os.path.exists(os.path.join(dst, DB_NAME)))


if __name__ == "__main__":
    unittest.main()
