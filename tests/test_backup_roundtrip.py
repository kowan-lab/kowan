"""Config backup and restore on disk: the archive carries the database, the
master keys and the stacks tree (symlinks left out); a restore stages, then
applies at the next start (keys re-tightened, stacks replaced); a boot that
crashed after swapping the database finishes cleanly; renames that cannot
cross devices fall back to copies; hostile archives are refused."""

import gzip
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

from kowan import backup as b


class _Snap:
    def snapshot(self, dest):
        with open(dest, "wb") as f:
            f.write(b"SQLite format 3\x00restored")


def tar_bytes(entries):
    """entries: [(name, bytes|None for dir|"link")]"""
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
        with tarfile.open(fileobj=gz, mode="w") as tw:
            for name, data in entries:
                info = tarfile.TarInfo(name)
                if data is None:
                    info.type = tarfile.DIRTYPE
                    tw.addfile(info)
                elif data == "link":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                    tw.addfile(info)
                else:
                    info.size = len(data)
                    tw.addfile(info, io.BytesIO(data))
    return raw.getvalue()


MANIFEST = json.dumps({"version": 1, "kind": b.MANIFEST_KIND}).encode()


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp()
        self.dst = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.src, "stacks", "shop"))
        with open(os.path.join(self.src, "stacks", "shop", "compose.yml"), "w") as f:
            f.write("services: {}")
        os.symlink("/etc/passwd", os.path.join(self.src, "stacks", "shop", "evil"))
        for k in (b.SECRET_KEY_NAME, b.JWT_KEY_NAME):
            with open(os.path.join(self.src, k), "w") as f:
                f.write("key-" + k)

    def test_backup_restore_apply(self):
        out = io.BytesIO()
        b.create_backup(_Snap(), self.src, out)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(out.getvalue()))) as t:
            names = t.getnames()
        self.assertIn("stacks/shop/compose.yml", names)
        self.assertNotIn("stacks/shop/evil", names)
        self.assertIn(b.SECRET_KEY_NAME, names)

        # The live side has old stacks and keys.
        os.makedirs(os.path.join(self.dst, "stacks", "old"))
        with open(os.path.join(self.dst, b.DB_NAME), "wb") as f:
            f.write(b"old db")
        with open(os.path.join(self.dst, b.DB_NAME + "-wal"), "wb") as f:
            f.write(b"stale wal")
        b.stage_restore(self.dst, io.BytesIO(out.getvalue()))
        self.assertTrue(b.apply_pending_restore(self.dst))
        with open(os.path.join(self.dst, b.DB_NAME), "rb") as f:
            self.assertIn(b"restored", f.read())
        self.assertFalse(os.path.exists(os.path.join(self.dst, b.DB_NAME + "-wal")))
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "stacks", "shop", "compose.yml")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "stacks", "old")))
        mode = stat.S_IMODE(os.stat(os.path.join(self.dst, b.JWT_KEY_NAME)).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.dst, b.PENDING_DIR_NAME)))
        self.assertFalse(b.apply_pending_restore(self.dst))  # nothing armed

    def test_crash_after_db_swap_finishes(self):
        b.stage_restore(self.dst, io.BytesIO(tar_bytes([(b.MANIFEST_NAME, MANIFEST), (b.DB_NAME, b"db"),
                                                         (b.SECRET_KEY_NAME, b"k")])))
        os.remove(os.path.join(self.dst, b.PENDING_DIR_NAME, b.DB_NAME))
        with self.assertLogs("kowan.backup", "WARNING"):
            self.assertTrue(b.apply_pending_restore(self.dst))
        with open(os.path.join(self.dst, b.SECRET_KEY_NAME)) as f:
            self.assertEqual(f.read(), "k")

    def test_replace_falls_back_to_copy(self):
        src_dir = os.path.join(self.src, "stacks")
        dst_dir = os.path.join(self.dst, "stacks")
        os.makedirs(dst_dir)
        src_file = os.path.join(self.src, b.SECRET_KEY_NAME)
        dst_file = os.path.join(self.dst, "key")
        with open(dst_file, "w") as f:
            f.write("old")
        with mock.patch.object(b.os, "rename", side_effect=OSError("cross-device link")):
            b._replace_path(src_dir, dst_dir)
            b._replace_path(src_file, dst_file)
        self.assertTrue(os.path.isfile(os.path.join(dst_dir, "shop", "compose.yml")))
        with open(dst_file) as f:
            self.assertTrue(f.read().startswith("key-"))

    def test_hostile_and_broken_archives(self):
        for entries, msg in (
            ([("/etc/passwd", b"x")], "absolute path"),
            ([("stacks/../../x", b"x")], "path traversal"),
            ([(b.MANIFEST_NAME, b"{nope")], "parse manifest"),
            ([(b.MANIFEST_NAME, json.dumps({"kind": "other"}).encode())], "manifest kind"),
            ([(b.DB_NAME, b"db")], "missing or invalid manifest"),
            ([(b.MANIFEST_NAME, MANIFEST)], "missing kowan.db"),
        ):
            with self.assertRaisesRegex(ValueError, msg):
                b.stage_restore(self.dst, io.BytesIO(tar_bytes(entries)))
        with self.assertRaisesRegex(ValueError, "read backup archive"):
            b.stage_restore(self.dst, io.BytesIO(b"not gzip at all"))
        # Directory entries, symlinks and unknown files: dirs made, the rest skipped.
        b.stage_restore(self.dst, io.BytesIO(tar_bytes([
            ("./", None), (b.MANIFEST_NAME, MANIFEST), (b.DB_NAME, b"db"), ("stacks/", None), ("stacks/a/", None),
            ("stacks/link", "link"), ("random.txt", b"ignored")])))
        pending = os.path.join(self.dst, b.PENDING_DIR_NAME)
        self.assertTrue(os.path.isdir(os.path.join(pending, "stacks", "a")))
        self.assertFalse(os.path.lexists(os.path.join(pending, "stacks", "link")))
        self.assertFalse(os.path.exists(os.path.join(pending, "random.txt")))
        shutil.rmtree(pending)


if __name__ == "__main__":
    unittest.main()
