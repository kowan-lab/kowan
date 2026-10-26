"""Container file browser: path normalisation, async tar parsing/building, and
the archive ops travelling over the agent tunnel (fake Ops, no Docker)."""

import asyncio
import base64
import io
import json
import tarfile
import unittest

from kowan.agent_ops_server import serve_agent_ops
from kowan.api.containerfiles import decode_text
from kowan.docker import containerfs as cfs
from kowan.docker.remote import make_remote_client
from kowan.errors import AppError, NotFound, Validation
from kowan.mux.session import Session
from kowan.transport.memory import memory_pair


class Chunks:
    """AsyncByteStream yielding fixed-size chunks (exercises buffering)."""

    def __init__(self, data: bytes, size: int = 7) -> None:
        self.data, self.size = data, size

    async def read(self, n: int = -1) -> bytes:
        out, self.data = self.data[: self.size], self.data[self.size:]
        return out


def make_tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tf.addfile(ti)
            elif kind == "link":
                ti.type = tarfile.SYMTYPE
                ti.linkname = data
                tf.addfile(ti)
            else:
                ti.size = len(data)
                ti.mode = 0o640
                ti.uid, ti.gid = 33, 44
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class TestPaths(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(cfs.normalize_path(""), "/")
        self.assertEqual(cfs.normalize_path("/"), "/")
        self.assertEqual(cfs.normalize_path("etc//nginx/./conf.d/"), "/etc/nginx/conf.d")
        self.assertEqual(cfs.normalize_path("/a b/ü"), "/a b/ü")

    def test_traversal_rejected(self):
        for bad in ("/../etc", "/etc/../../root", "..", "/a/..", "/a/\x00b", "/a\nb", 5, "/" + "x" * 5000):
            with self.assertRaises(Validation, msg=repr(bad)):
                cfs.normalize_path(bad)

    def test_names(self):
        self.assertEqual(cfs.validate_name(" new.txt "), "new.txt")
        for bad in ("", ".", "..", "a/b", "a\x00", "x" * 300, None):
            with self.assertRaises(Validation):
                cfs.validate_name(bad)
        self.assertEqual(cfs.join("/etc", "hosts"), "/etc/hosts")
        self.assertEqual(cfs.parent("/etc/hosts"), "/etc")
        self.assertEqual(cfs.parent("/etc"), "/")

    def test_resolve_link(self):
        self.assertEqual(cfs.resolve_link("/lib", "usr/lib"), "/usr/lib")
        self.assertEqual(cfs.resolve_link("/a/b/c", "../../x"), "/x")
        self.assertEqual(cfs.resolve_link("/a", "../../../etc"), "/etc")  # clamped at /
        self.assertEqual(cfs.resolve_link("/a/b", "/abs/t"), "/abs/t")

    def test_go_mode(self):
        self.assertEqual(cfs.go_mode_string((1 << 31) | 0o755), "drwxr-xr-x")
        self.assertEqual(cfs.go_mode_string((1 << 27) | 0o777), "lrwxrwxrwx")
        self.assertEqual(cfs.go_mode_string(0o4755 & 0o777 | (1 << 23)), "-rwsr-xr-x")
        st = {"name": "x", "size": 9, "mode": 0o644, "mtime": "2024-05-01T10:00:00.123456789Z", "linkTarget": ""}
        e = cfs.entry_from_stat("/x", st)
        self.assertEqual((e["kind"], e["size"], e["modTime"]), ("file", 9, 1714557600))

    def test_decode_stat_header(self):
        import base64, json
        raw = base64.b64encode(json.dumps({"name": "etc", "mode": 2147484141}).encode()).decode().rstrip("=")
        self.assertEqual(cfs.decode_path_stat(raw)["name"], "etc")
        with self.assertRaises(Validation):
            cfs.decode_path_stat("!!notbase64")

    def test_decode_text(self):
        self.assertEqual(decode_text(b"hi\n"), ("hi\n", False))
        self.assertEqual(decode_text(b"\x00\x01"), ("", True))
        self.assertEqual(decode_text(b"\xff\xfe"), ("", True))


class TestTar(unittest.IsolatedAsyncioTestCase):
    async def test_list_children_of_dir(self):
        long = "d" * 150
        data = make_tar([
            ("etc", "dir", b""),
            ("etc/hosts", "file", b"127.0.0.1 localhost\n"),
            ("etc/nginx", "dir", b""),
            ("etc/nginx/nginx.conf", "file", b"x" * 2000),
            ("etc/" + long, "file", b"long"),
            ("etc/link", "link", "../usr"),
            ("etc/implicit/deep/file", "file", b"z"),
        ])
        entries, truncated = await cfs.list_tar_children(Chunks(data), "/etc", limit=100, max_scan_bytes=1 << 30)
        self.assertFalse(truncated)
        names = [e["name"] for e in entries]
        self.assertEqual(names, ["implicit", "nginx", long, "hosts", "link"])
        hosts = next(e for e in entries if e["name"] == "hosts")
        self.assertEqual((hosts["size"], hosts["path"], hosts["mode"]), (20, "/etc/hosts", "-rw-r-----"))
        link = next(e for e in entries if e["name"] == "link")
        self.assertTrue(link["isLink"])
        self.assertEqual(link["linkTarget"], "../usr")

    async def test_root_listing_and_limit(self):
        data = make_tar([(".", "dir", b""), ("./a", "file", b"1"), ("./b", "dir", b""), ("./b/c", "file", b"2"), ("./d", "file", b"")])
        entries, truncated = await cfs.list_tar_children(Chunks(data, 100), "/", limit=100, max_scan_bytes=1 << 30)
        self.assertEqual([e["name"] for e in entries], ["b", "a", "d"])
        entries, truncated = await cfs.list_tar_children(Chunks(data, 100), "/", limit=1, max_scan_bytes=1 << 30)
        self.assertTrue(truncated)
        self.assertEqual(len(entries), 1)

    async def test_first_file_and_streaming(self):
        payload = bytes(range(256)) * 50
        data = make_tar([("blob.bin", "file", payload)])
        h, content = await cfs.first_regular_file(Chunks(data, 333), 1 << 20)
        self.assertEqual((h.name, h.uid, h.gid, content), ("blob.bin", 33, 44, payload))
        out = b"".join([c async for c in cfs.iter_first_file(Chunks(data, 1000))])
        self.assertEqual(out, payload)
        with self.assertRaises(Validation):
            await cfs.first_regular_file(Chunks(data), 10)

    async def test_builders_roundtrip(self):
        t = cfs.tar_file_bytes("new.txt", b"hello", mode=0o600, uid=1000, gid=1001, uname="app")
        with tarfile.open(fileobj=io.BytesIO(t)) as tf:
            m = tf.getmember("new.txt")
            self.assertEqual((m.mode, m.uid, m.gid, m.uname), (0o600, 1000, 1001, "app"))
            self.assertEqual(tf.extractfile(m).read(), b"hello")
        d = cfs.tar_dir("folder", uid=5)
        with tarfile.open(fileobj=io.BytesIO(d)) as tf:
            self.assertTrue(tf.getmember("folder").isdir())
        stream = cfs.TarFileStream("big.bin", 100000, Chunks(b"a" * 100000, 4096), uid=7)
        raw = b""
        while True:
            c = await stream.read(8192)
            if not c:
                break
            raw += c
        self.assertEqual(len(raw) % 512, 0)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            self.assertEqual(tf.extractfile("big.bin").read(), b"a" * 100000)

    async def test_truncated_upload_detected(self):
        stream = cfs.TarFileStream("x", 10, Chunks(b"abc"))
        await stream.read()
        await stream.read()
        with self.assertRaises(Validation):
            await stream.read()


class FakeFsOps:
    def __init__(self):
        self.put = None

    async def container_path_stat(self, id, path):
        if path == "/missing":
            raise NotFound("no such file or directory: /missing")
        return {"name": "x", "path": path, "kind": "file", "isDir": False, "size": 3}

    async def container_list_dir(self, id, path, limit):
        return {"path": path, "entries": [{"name": "a"}], "truncated": False, "method": "archive", "limit": limit}

    async def container_read_file(self, id, path, max_bytes):
        if max_bytes < 3:
            raise AppError("file is too large to open", status=413)
        return b"abc"

    async def container_get_archive(self, id, path):
        return Chunks(make_tar([("f", "file", b"payload")]), 100)

    async def container_put_archive(self, id, path, content):
        buf = b""
        while True:
            c = await content.read(1000)
            if not c:
                break
            buf += c
        self.put = (id, path, buf)

    async def container_exec_status(self, id, cmd, user=""):
        return {"exitCode": 127 if cmd[0] == "rm" else 0, "output": " ".join(cmd) + " as " + user}

    async def container_relabel(self, id, set_labels, remove_labels, connect, disconnect):
        return f"new:{sorted(set_labels)}:{remove_labels}:{connect}:{disconnect}"


class TestArchiveOpsOverTunnel(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip(self):
        ta, tb = memory_pair()
        cp = Session(ta, is_client=True).start()
        agent = Session(tb, is_client=False).start()
        ops = FakeFsOps()
        task = asyncio.ensure_future(serve_agent_ops(agent, ops))
        remote = make_remote_client(lambda: cp.open_stream())
        try:
            self.assertEqual((await remote.container_path_stat("c", "/etc"))["path"], "/etc")
            with self.assertRaises(AppError) as ctx:
                await remote.container_path_stat("c", "/missing")
            self.assertEqual(int(ctx.exception.status), 404)
            self.assertEqual((await remote.container_list_dir("c", "/", 5))["limit"], 5)
            self.assertEqual(await remote.container_read_file("c", "/f", 10), b"abc")
            with self.assertRaises(AppError) as ctx:
                await remote.container_read_file("c", "/f", 1)
            self.assertEqual(int(ctx.exception.status), 413)
            body = await remote.container_get_archive("c", "/f")
            got = b"".join([c async for c in cfs.iter_first_file(body)])
            await body.close()
            self.assertEqual(got, b"payload")
            tar = cfs.tar_file_bytes("n.txt", b"x" * 70000)
            await remote.container_put_archive("c", "/data", Chunks(tar, 4096))
            self.assertEqual(ops.put, ("c", "/data", tar))
            res = await remote.container_exec_status("c", ["ls", "/"], "0")
            self.assertEqual(res, {"exitCode": 0, "output": "ls / as 0"})
            rid = await remote.container_relabel("c", {"a": "1"}, ["b"], ["net"], [])
            self.assertEqual(rid, "new:['a']:['b']:['net']:[]")
        finally:
            task.cancel()
            await cp.close()
            await agent.close()


if __name__ == "__main__":
    unittest.main()


class TestTarFormats(unittest.IsolatedAsyncioTestCase):
    """GNU long names/links, PAX headers, special files and corrupt input."""

    def gnu_tar(self):
        buf = io.BytesIO()
        long_name = "srv/" + "deep/" * 30 + "file.txt"
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
            ti = tarfile.TarInfo("srv")
            ti.type = tarfile.DIRTYPE
            tf.addfile(ti)
            ti = tarfile.TarInfo(long_name)
            ti.size = 3
            tf.addfile(ti, io.BytesIO(b"abc"))
            ti = tarfile.TarInfo("srv/link")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/" + "x" * 150
            tf.addfile(ti)
            for name, typ in (("srv/fifo", tarfile.FIFOTYPE), ("srv/dev", tarfile.CHRTYPE),
                              ("srv/blk", tarfile.BLKTYPE)):
                ti = tarfile.TarInfo(name)
                ti.type = typ
                tf.addfile(ti)
            ti = tarfile.TarInfo("srv/suid")
            ti.mode = 0o4755 | 0o2000 | 0o1000
            ti.size = 0
            tf.addfile(ti, io.BytesIO(b""))
        return buf.getvalue(), long_name

    async def test_gnu_long_names_and_special_files(self):
        data, long_name = self.gnu_tar()
        entries, truncated = await cfs.list_tar_children(Chunks(data, 100), "/srv", limit=50,
                                                                 max_scan_bytes=1 << 20)
        kinds = {e["name"]: e["kind"] for e in entries}
        self.assertEqual(kinds["deep"], "dir")  # synthesised from the long nested name
        self.assertEqual(kinds["link"], "symlink")
        self.assertEqual(next(e for e in entries if e["name"] == "link")["linkTarget"], "/" + "x" * 150)
        self.assertEqual(kinds["fifo"], "pipe")
        self.assertEqual(kinds["dev"], "device")
        self.assertIn("s", next(e for e in entries if e["name"] == "suid")["mode"])
        self.assertFalse(truncated)
        hdr, body = await cfs.first_regular_file(Chunks(data, 64), 1024)
        self.assertEqual((hdr.name, body), (long_name, b"abc"))
        _, truncated = await cfs.list_tar_children(Chunks(data, 100), "/srv", limit=2,
                                                           max_scan_bytes=1 << 20)
        self.assertTrue(truncated)
        _, truncated = await cfs.list_tar_children(Chunks(data, 100), "/srv", limit=50, max_scan_bytes=10)
        self.assertTrue(truncated)

    async def test_pax_size_mtime_and_corruption(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
            ti = tarfile.TarInfo("d/ünïcode-" + "n" * 120)
            ti.size = 2
            ti.mtime = 1_700_000_000.5
            ti.pax_headers = {"mtime": "1700000000.5"}
            tf.addfile(ti, io.BytesIO(b"hi"))
        entries, _ = await cfs.list_tar_children(Chunks(buf.getvalue(), 33), "/d", limit=10,
                                                         max_scan_bytes=1 << 20)
        self.assertEqual(entries[0]["modTime"], 1_700_000_000)
        with self.assertRaises(Validation):
            await cfs.list_tar_children(Chunks(b"x" * 512 + b"\0" * 1024), "/d", limit=10,
                                                max_scan_bytes=1 << 20)
        self.assertEqual(await cfs.first_regular_file(Chunks(b""), 10), (None, b""))
        self.assertIsNone(await cfs.first_header(Chunks(b"\0" * 1024)))

    def test_modes_names_and_stat(self):
        self.assertEqual(cfs.go_mode_kind(1 << 26), "device")   # Go ModeDevice
        self.assertEqual(cfs.go_mode_kind(1 << 25), "pipe")     # Go ModeNamedPipe
        self.assertEqual(cfs.go_mode_kind(1 << 24), "socket")   # Go ModeSocket
        with self.assertRaises(Validation):
            cfs.validate_name("bad\x01name")
        with self.assertRaises(Validation):
            cfs.decode_path_stat("")
        with self.assertRaises(Validation):
            cfs.decode_path_stat("not base64!")
        st = cfs.decode_path_stat(base64.b64encode(json.dumps(
            {"name": "a", "size": 1, "mode": 420, "mtime": "garbage"}).encode()).decode())
        self.assertEqual(cfs.entry_from_stat("/a", st)["modTime"], 0)
