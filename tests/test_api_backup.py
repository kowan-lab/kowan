"""Control-plane backup and restore through the API: download an archive,
stage it (raw body and multipart), refuse bad archives and traversal entries,
then apply the staged restore the way the next start does."""

import gzip
import io
import json
import os
import tarfile
import unittest

import aiohttp

from kowan import backup
from tests.apicase import ServerCase


def _archive(entries):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        with tarfile.open(fileobj=gz, mode="w") as tw:
            for name, data in entries:
                info = tarfile.TarInfo(name)
                if data is None:
                    info.type = tarfile.DIRTYPE
                    tw.addfile(info)
                else:
                    info.size = len(data)
                    tw.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestBackupApi(ServerCase):
    async def test_round_trip(self):
        await self.roles("GET", "/backup", "admin")
        status, archive = await self.call("GET", "/backup", "admin", expect=200)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as t:
            names = t.getnames()
        self.assertIn("manifest.json", names)
        self.assertIn("kowan.db", names)

        await self.roles("POST", "/restore", "admin", data=archive)
        _, res = await self.call("POST", "/restore", "admin", data=archive, expect=200)
        self.assertIn("restart", res["status"])
        pending = os.path.join(self.data_dir, backup.PENDING_DIR_NAME)
        self.assertTrue(os.path.exists(os.path.join(pending, backup.RESTORE_MARKER)))
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(archive), filename="b.tar.gz")
        await self.call("POST", "/restore", "admin", data=form, expect=200)
        form = aiohttp.FormData()
        form.add_field("other", io.BytesIO(b"x"), filename="x.txt")  # multipart without "file"
        await self.call("POST", "/restore", "admin", data=form, expect=400)
        # A bad upload leaves the staged restore alone.
        await self.call("POST", "/restore", "admin", data=b"garbage", expect=400)

        # The next start swaps the staged files in.
        self.assertTrue(backup.apply_pending_restore(self.data_dir))
        self.assertFalse(os.path.exists(pending))
        self.assertFalse(backup.apply_pending_restore(self.data_dir))

    async def test_bad_archives(self):
        manifest = json.dumps({"kind": "kowan-backup"}).encode()
        for entries in ([("kowan.db", b"x")],                                       # no manifest
                        [("manifest.json", manifest)],                              # no db
                        [("manifest.json", b'{"kind": "other"}'), ("kowan.db", b"x")],
                        [("manifest.json", b"{nope"), ("kowan.db", b"x")]):
            await self.call("POST", "/restore", "admin", data=_archive(entries), expect=400)
        await self.call("POST", "/restore", "admin", data=b"not gzip", expect=400)
        # Traversal or absolute entries refuse the whole archive.
        for evil in ("../evil", "/etc/passwd", "stacks/../../x"):
            await self.call("POST", "/restore", "admin", expect=400, data=_archive(
                [("manifest.json", manifest), ("kowan.db", b"db"), (evil, b"x")]))
        # Unknown entries are skipped, stacks/ is kept.
        ok = _archive([("manifest.json", manifest), ("kowan.db", b"db"), ("stacks", None),
                       ("stacks/app/compose.yml", b"services: {}"), ("unknown.txt", b"x")])
        await self.call("POST", "/restore", "admin", data=ok, expect=200)
        pending = os.path.join(self.data_dir, backup.PENDING_DIR_NAME)
        self.assertTrue(os.path.exists(os.path.join(pending, "stacks", "app", "compose.yml")))
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "evil")))
        self.assertFalse(os.path.exists(os.path.join(pending, "unknown.txt")))


if __name__ == "__main__":
    unittest.main()
