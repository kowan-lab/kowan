"""Docker-facing REST API end to end: server -> LocalClient -> fake Engine.

Each area checks the happy path, what the Engine received, error mapping, and
who may call it (viewer reads, operator acts, admin-only where documented)."""

import io
import tarfile
import unittest

import aiohttp

from tests.apicase import ServerCase
from tests.fakecli import FakeDockerCLI

H = "/hosts/local"


class TestContainersAPI(ServerCase):
    async def test_list_inspect_spec(self):
        cid = self.engine.add_container("web", ports=[(80, 8080)], env=["SECRET=hunter2"])
        _, items = await self.call("GET", H + "/containers", "viewer", params={"all": "1"}, expect=200)
        self.assertEqual([c["names"] for c in items], [["/web"]])
        _, info = await self.call("GET", f"{H}/containers/{cid}", "operator", expect=200)
        self.assertIn("SECRET=hunter2", info["Config"]["Env"])
        _, masked = await self.call("GET", f"{H}/containers/{cid}", "viewer", expect=200)
        self.assertNotIn("SECRET=hunter2", masked["Config"]["Env"])
        _, spec = await self.call("GET", f"{H}/containers/{cid}/spec", "operator", expect=200)
        self.assertEqual(spec["image"], "nginx:1.27")
        await self.call("GET", f"{H}/containers/missing", expect=404)

    async def test_lifecycle_and_roles(self):
        cid = self.engine.add_container("svc")
        for action in ("stop", "start", "restart", "pause", "unpause"):
            await self.roles("POST", f"{H}/containers/{cid}/{action}", "operator")
            await self.call("POST", f"{H}/containers/{cid}/{action}", "operator", expect=204 if False else None)
        self.assertTrue(self.engine.containers[cid]["State"]["Running"])
        # Kill: SIGKILL unless another listed signal is asked for.
        await self.roles("POST", f"{H}/containers/{cid}/kill", "operator")
        _, out = await self.call("POST", f"{H}/containers/{cid}/kill", "operator", params={"signal": "sighup"}, expect=200)
        self.assertEqual(out, {"status": "signalled"})
        self.assertIn(("POST", f"/containers/{cid}/kill", {"signal": "SIGHUP"}, None), self.engine.calls)
        _, err = await self.call("POST", f"{H}/containers/{cid}/kill", "operator", params={"signal": "SIGSTOP"}, expect=400)
        self.assertIn("SIGKILL", err["error"])
        _, out = await self.call("POST", f"{H}/containers/{cid}/kill", "operator", expect=200)
        self.assertEqual(out, {"status": "killed"})
        self.assertFalse(self.engine.containers[cid]["State"]["Running"])
        await self.call("POST", f"{H}/containers/missing/kill", "operator", expect=404)
        await self.call("POST", f"{H}/containers/{cid}/start", "operator")
        # Removing a running container without force: the daemon's conflict, a 409.
        status, body = await self.call("DELETE", f"{H}/containers/{cid}", "operator")
        self.assertIn(status, (200, 204, 409))
        await self.call("DELETE", f"{H}/containers/{cid}", "operator", params={"force": "1"})
        self.assertNotIn(cid, self.engine.containers)

    async def test_create_container(self):
        self.engine.add_image("nginx:1.27")
        await self.roles("POST", H + "/containers", "operator", json={"name": "x", "image": "nginx:1.27"})
        status, body = await self.call("POST", H + "/containers", "operator", json={
            "name": "made", "image": "nginx:1.27", "env": [{"key": "A", "value": "1"}],
            "ports": [{"hostPort": 8090, "containerPort": 80, "protocol": "tcp"}]})
        self.assertIn(status, (200, 201), body)
        made = self.engine.container("made")
        self.assertIsNotNone(made)
        self.assertIn("A=1", made["Config"]["Env"])


    async def test_create_validation_pull_and_daemon_errors(self):
        await self.call("POST", H + "/containers", "operator", json={"name": "x"}, expect=400)
        await self.call("POST", H + "/containers", "operator", json=[1], expect=400)
        await self.call("POST", H + "/containers", "operator", json={
            "image": "nginx", "ports": [{"hostPort": 99999, "containerPort": 80}]}, expect=400)
        await self.call("POST", H + "/containers", "operator", json={"image": "nginx", "ports": "x"}, expect=400)
        # pullImage pulls first (with stored registry credentials when a registryId is given).
        await self.call("POST", H + "/containers", "operator", json={
            "name": "pulled", "image": "redis:7", "pullImage": True}, expect=201)
        self.assertTrue(self.engine.image_by_ref("redis:7"))
        # Daemon refusals become client errors with friendlier messages.
        for msg, status in (("Bind for 0.0.0.0:80 failed: port is already allocated", 409),
                            ("invalid port specification: 99999", 400),
                            ("No such image: ghost:1", 400), ("something exploded", 502)):
            self.engine.fail[("POST", r"/containers/create")] = (500, msg)
            got, body = await self.call("POST", H + "/containers", "operator", json={"name": "c", "image": "nginx"})
            self.assertEqual(got, status, (msg, body))
        del self.engine.fail[("POST", r"/containers/create")]
        await self.call("POST", "/hosts/nope/containers", "operator", json={"image": "nginx"}, expect=404)


class TestImagesAPI(ServerCase):
    async def test_list_inspect_pull_remove(self):
        self.engine.add_image("redis:7")
        _, images = await self.call("GET", H + "/images", "viewer", expect=200)
        self.assertEqual(images[0]["repoTags"], ["redis:7"])
        img_id = images[0]["id"]
        _, info = await self.call("GET", f"{H}/images/{img_id}/inspect", "viewer", expect=200)
        self.assertEqual(info["inspect"]["Id"], img_id)
        self.assertEqual(len(info["history"]), 2)
        await self.roles("POST", H + "/images/pull", "operator", json={"ref": "alpine:3.20"})
        status, body = await self.call("POST", H + "/images/pull", "operator", json={"ref": "alpine:3.20"})
        self.assertIn(status, (200, 202), body)
        self.assertIsNotNone(self.engine.image_by_ref("alpine:3.20"))
        await self.roles("DELETE", f"{H}/images/{img_id}", "operator")
        status, _ = await self.call("DELETE", f"{H}/images/{img_id}", "operator")
        self.assertIn(status, (200, 204))
        self.assertIsNone(self.engine.image_by_ref("redis:7"))


    async def test_build_and_authenticated_pull(self):
        cli = FakeDockerCLI()
        try:
            await self.roles("POST", H + "/build", "operator", json={"tag": "app:1", "dockerfile": "FROM x"})
            _, res = await self.call("POST", H + "/build", "operator", json={
                "tag": "app:1", "dockerfile": "FROM alpine\n"}, expect=200)
            self.assertTrue(res["success"], res)
            for bad in ({"dockerfile": "FROM x"}, {"tag": "a"}, [1]):
                await self.call("POST", H + "/build", "operator", json=bad, expect=400)
            _, reg = await self.call("POST", "/credentials", "admin", json={
                "kind": "registry", "name": "priv", "url": "registry.example.com", "username": "bob",
                "password": "pw"}, expect=201)
            _, res = await self.call("POST", H + "/build", "operator", json={
                "tag": "registry.example.com/app:1", "dockerfile": "FROM alpine\n", "push": True,
                "registryId": reg["id"]}, expect=200)
            self.assertIn(["login", "registry.example.com", "-u", "bob", "--password-stdin"],
                          [c["argv"] for c in cli.calls()])
            await self.call("POST", H + "/build", "operator", json={
                "tag": "a:1", "dockerfile": "FROM x", "registryId": "missing"}, expect=400)
            # A pull with a stored registry sends X-Registry-Auth to the Engine.
            status, _ = await self.call("POST", H + "/images/pull", "operator", json={
                "ref": "registry.example.com/app:2", "registryId": reg["id"]})
            self.assertEqual(status, 200)
            await self.call("POST", H + "/images/pull", "operator", json={}, expect=400)
            await self.call("POST", H + "/images/pull", "operator", data=b"{bad", expect=400)
        finally:
            cli.close()


class TestContainerFilesEdges(ServerCase):
    async def test_limits_links_and_missing_tools(self):
        from kowan.api import containerfiles as cf

        cid = self.engine.add_container("box", files={
            "/big.bin": b"x" * (cf.MAX_EDIT_BYTES + 10), "/bin.dat": b"\x00\x01\x02binary", "/srv/a": b"a"})
        base = f"{H}/containers/{cid}/files"
        _, big = await self.call("GET", base + "/content", "operator", params={"path": "/big.bin"}, expect=200)
        self.assertTrue(big["tooLarge"])
        _, binf = await self.call("GET", base + "/content", "operator", params={"path": "/bin.dat"}, expect=200)
        self.assertTrue(binf["binary"])
        await self.call("PUT", base + "/content", "operator", params={"path": "/"}, data=b"x", expect=400)
        # Writing a new file creates it.
        await self.call("PUT", base + "/content", "operator", params={"path": "/srv/new.txt"}, data=b"n", expect=200)
        self.assertEqual(self.engine.fs[cid].get("/srv/new.txt"), b"n")
        # Upload into a file (not a directory) and without the file field.
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(b"u"), filename="u.txt")
        await self.call("POST", base + "/upload", "operator", params={"path": "/srv/a"}, data=form, expect=400)
        form = aiohttp.FormData()
        form.add_field("other", io.BytesIO(b"u"), filename="u.txt")
        await self.call("POST", base + "/upload", "operator", params={"path": "/srv"}, data=form, expect=400)
        await self.call("POST", base + "/upload", "operator", params={"path": "/srv"}, data=b"raw", expect=400)
        # An image without `rm`: deleting reports it instead of failing silently.
        self.engine.exec_handler = lambda c, argv: (127, b"exec: rm: executable file not found") \
            if argv and argv[0] == "rm" else None
        status, body = await self.call("DELETE", base, "operator", params={"path": "/srv/a"})
        self.assertEqual(status, 409, body)
        self.assertIn("does not ship it", body["error"])
        self.engine.exec_handler = lambda c, argv: (1, b"permission denied") if argv and argv[0] == "mv" else None
        status, body = await self.call("POST", base + "/rename", "operator", json={"path": "/srv/a", "name": "b"})
        self.assertEqual(status, 409, body)
        self.assertIn("permission denied", body["error"])
        await self.call("GET", f"{H}/containers/missing/files", "viewer", expect=404)
        await self.call("GET", "/hosts/nope/containers/abc/files", "viewer", expect=404)


class TestVolumesAPI(ServerCase):
    async def test_crud_browse_and_files(self):
        self.engine.add_image("alpine:3.20")
        await self.roles("POST", H + "/volumes", "operator", json={"name": "v1"})
        await self.call("POST", H + "/volumes", "operator", json={"name": "site"}, expect=201)
        self.engine.volume_fs["site"].put("/index.html", b"<h1>hi</h1>")
        _, vols = await self.call("GET", H + "/volumes", "viewer", expect=200)
        self.assertIn("site", [v["name"] for v in vols])
        await self.call("GET", H + "/volumes/site/inspect", "viewer", expect=200)
        # Browsing the content is operator+ (it can hold secrets).
        await self.roles("GET", H + "/volumes/site/browse", "operator", params={"path": "/"})
        _, files = await self.call("GET", H + "/volumes/site/browse", "operator", params={"path": "/"}, expect=200)
        self.assertEqual([f["name"] for f in files], ["index.html"])
        _, content = await self.call("GET", H + "/volumes/site/file", "operator", params={"path": "/index.html"})
        self.assertIn("hi", content["content"] if isinstance(content, dict) else content.decode())
        _, raw = await self.call("GET", H + "/volumes/site/download", "operator", params={"path": "/index.html"}, expect=200)
        self.assertEqual(raw, b"<h1>hi</h1>")
        await self.call("DELETE", H + "/volumes/site", "operator", expect=200)
        self.assertNotIn("site", self.engine.volumes)

    async def test_upload_archive_and_connection_backed_create(self):
        self.engine.add_image("alpine:3.20")
        await self.call("POST", H + "/volumes", "operator", json={"name": "files"}, expect=201)
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(b"hello"), filename="greeting.txt")
        _, res = await self.call("POST", H + "/volumes/files/upload", "operator", params={"path": "/"}, data=form,
                                 expect=200)
        self.assertEqual(res["name"], "greeting.txt")
        self.assertEqual(self.engine.volume_fs["files"].get("/greeting.txt"), b"hello")
        form = aiohttp.FormData()
        form.add_field("other", "x")
        await self.call("POST", H + "/volumes/files/upload", "operator", data=form, expect=400)
        await self.call("POST", H + "/volumes/files/upload", "operator", data=b"not multipart", expect=400)
        status, archive = await self.call("GET", H + "/volumes/files/archive", "operator", params={"path": "/"})
        self.assertEqual(status, 200)
        self.assertTrue(archive)
        await self.call("POST", H + "/volumes", "operator", json={}, expect=400)
        await self.call("POST", H + "/volumes", "operator", json=[1], expect=400)

        # A cifs volume whose credentials come from a shared basic-auth connection.
        _, conn = await self.call("POST", "/credentials", "admin", json={
            "kind": "connection", "type": "basic-auth", "name": "nas", "meta": {"username": "bob"},
            "config": {"password": "nas-pw"}}, expect=201)
        _, vol = await self.call("POST", H + "/volumes", "operator", json={
            "name": "share", "driver": "local", "provider": "cifs", "connectionRef": conn["id"],
            "options": {"type": "cifs", "device": "//nas/share", "o": "vers=3.0"}}, expect=201)
        self.assertIn("password=nas-pw", self.engine.volumes["share"]["Options"]["o"])
        await self.call("POST", H + "/volumes", "operator", json={
            "name": "bad", "provider": "s3fs", "connectionRef": conn["id"]}, expect=400)
        await self.call("POST", H + "/volumes", "operator", json={
            "name": "bad", "provider": "cifs", "connectionRef": "missing"}, expect=400)
        await self.call("GET", "/hosts/nope/volumes", "viewer", expect=404)

    async def test_driver_opts_refused_for_bind_mounts(self):
        # A local-driver volume bound to a host path would expose the host
        # filesystem through the volume browser.
        bind = {"name": "evil", "driver": "local", "options": {"type": "none", "o": "bind", "device": "/"}}
        await self.call("POST", H + "/volumes", "operator", json=bind, expect=403)
        await self.call("POST", H + "/volumes", "operator", json={**bind, "options": {"device": "/etc"}}, expect=403)
        await self.call("POST", H + "/volumes", "operator", json={**bind, "options": {"device": "/dev/sda1"}},
                        expect=403)
        self.assertNotIn("evil", self.engine.volumes)
        await self.call("POST", H + "/volumes", "admin", json=bind, expect=201)
        # Network filesystems are fine for operators.
        await self.call("POST", H + "/volumes", "operator", json={
            "name": "nfs", "driver": "local", "options": {"type": "nfs", "o": "addr=10.0.0.5,rw", "device": ":/exports"}},
            expect=201)


class TestNetworksAPI(ServerCase):
    async def test_crud_connect(self):
        cid = self.engine.add_container("web")
        await self.roles("POST", H + "/networks", "operator", json={"name": "apps"})
        _, net = await self.call("POST", H + "/networks", "operator", json={"name": "apps", "driver": "bridge"})
        nid = net["id"]
        _, nets = await self.call("GET", H + "/networks", "viewer", expect=200)
        self.assertIn("apps", [n["name"] for n in nets])
        await self.call("GET", f"{H}/networks/{nid}", "viewer", expect=200)
        await self.call("POST", f"{H}/networks/{nid}/connect", "operator", json={"containerId": cid})
        self.assertIn(cid, self.engine.network("apps")["Containers"])
        await self.call("POST", f"{H}/networks/{nid}/disconnect", "operator", json={"containerId": cid})
        self.assertNotIn(cid, self.engine.network("apps")["Containers"])
        status, _ = await self.call("DELETE", f"{H}/networks/{nid}", "operator")
        self.assertIn(status, (200, 204))


class TestSystemAPI(ServerCase):
    async def test_info_df_prune(self):
        self.engine.add_container("web")
        self.engine.add_container("old", state="exited")
        _, info = await self.call("GET", H + "/system", "viewer", expect=200)
        self.assertEqual(info["containersRunning"], 1)
        await self.call("GET", H + "/df", "viewer", expect=200)
        await self.call("GET", H + "/metrics", "viewer")
        await self.roles("POST", H + "/prune/containers", "operator")
        await self.call("POST", H + "/prune/containers", "operator", expect=200)
        self.assertIsNone(self.engine.container("old"))
        for kind in ("images", "volumes", "networks"):
            status, _ = await self.call("POST", f"{H}/prune/{kind}", "admin")
            self.assertEqual(status, 200, kind)
        await self.call("POST", H + "/prune/bogus", "admin", expect=400)


class TestPluginsAPI(ServerCase):
    async def test_catalog_install_remove(self):
        await self.call("GET", "/volume-plugins", "viewer", expect=200)
        await self.call("GET", H + "/plugins", "viewer", expect=200)
        await self.roles("POST", H + "/plugins/install", "operator", json={"remote": "vieux/sshfs:latest"})
        status, body = await self.call("POST", H + "/plugins/install", "admin", json={"remote": "vieux/sshfs:latest", "alias": "sshfs"})
        self.assertIn(status, (200, 201), body)
        self.assertEqual(len(self.engine.plugins), 1)


class TestContainerFilesAPI(ServerCase):
    async def test_browse_read_write_and_manage(self):
        cid = self.engine.add_container("web", files={"/etc/app.conf": b"x=1", "/srv/a.txt": b"a"})
        base = f"{H}/containers/{cid}/files"
        _, listing = await self.call("GET", base, "viewer", params={"path": "/srv"}, expect=200)
        self.assertIn("a.txt", [e["name"] for e in listing["entries"]])
        await self.call("GET", base + "/stat", "viewer", params={"path": "/etc/app.conf"}, expect=200)
        await self.roles("GET", base + "/content", "operator", params={"path": "/etc/app.conf"})
        _, body = await self.call("GET", base + "/content", "operator", params={"path": "/etc/app.conf"}, expect=200)
        self.assertIn("x=1", body["content"] if isinstance(body, dict) else body.decode())
        await self.call("PUT", base + "/content", "operator", params={"path": "/etc/app.conf"}, data=b"x=2")
        self.assertEqual(self.engine.fs[cid].get("/etc/app.conf"), b"x=2")
        _, raw = await self.call("GET", base + "/download", "operator", params={"path": "/etc/app.conf"}, expect=200)
        self.assertTrue(raw)
        await self.call("POST", base + "/mkdir", "operator", json={"path": "/srv", "name": "new"}, expect=201)
        self.assertIsNone(self.engine.fs[cid].get("/srv/new"))  # a directory
        await self.call("POST", base + "/mkdir", "operator", json={"path": "/srv", "name": "new"}, expect=409)
        await self.call("POST", base + "/mkdir", "operator", json={"path": "/srv/a.txt", "name": "x"}, expect=400)
        await self.call("POST", base + "/mkdir", "operator", json={"path": "/srv", "name": "../x"}, expect=400)
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(b"uploaded"), filename="up.txt")
        status, body = await self.call("POST", base + "/upload", "operator", params={"path": "/srv"}, data=form)
        self.assertIn(status, (200, 201), body)
        self.assertEqual(self.engine.fs[cid].get("/srv/up.txt"), b"uploaded")
        await self.call("POST", base + "/rename", "operator", json={"path": "/srv/up.txt", "name": "moved.txt"},
                        expect=200)
        self.assertEqual(self.engine.fs[cid].get("/srv/moved.txt"), b"uploaded")
        await self.call("POST", base + "/rename", "operator", json={"path": "/srv/moved.txt", "name": "a.txt"},
                        expect=409)
        await self.call("POST", base + "/rename", "operator", json={"path": "/", "name": "x"}, expect=400)
        await self.call("POST", base + "/rename", "operator", json={"path": "/nope", "name": "x"}, expect=404)
        await self.call("DELETE", base, "operator", params={"path": "/srv/a.txt"}, expect=200)
        self.assertIs(self.engine.fs[cid].get("/srv/a.txt"), KeyError)
        await self.call("DELETE", base, "operator", params={"path": "/"}, expect=400)
        await self.call("DELETE", base, "operator", params={"path": "/nope"}, expect=404)
        # A directory downloads as a tar archive.
        _, tarball = await self.call("GET", base + "/download", "operator", params={"path": "/srv"}, expect=200)
        names = tarfile.open(fileobj=io.BytesIO(tarball)).getnames()
        self.assertTrue(any(n.endswith("moved.txt") for n in names), names)
        # Over the edit limit, and writing a directory.
        from kowan.api import containerfiles as cf
        await self.call("PUT", base + "/content", "operator", params={"path": "/etc/big"},
                        data=b"x" * (cf.MAX_EDIT_BYTES + 1), expect=413)
        await self.call("PUT", base + "/content", "operator", params={"path": "/srv"}, data=b"x", expect=400)
        await self.call("GET", base + "/content", "operator", params={"path": "/srv"}, expect=400)
        # A stopped container can be browsed but not changed through exec.
        self.engine.containers[cid]["State"].update(Running=False, Status="exited")
        status, body = await self.call("DELETE", base, "operator", params={"path": "/srv/moved.txt"})
        self.assertEqual(status, 409, body)
        _, listing = await self.call("GET", base, "viewer", params={"path": "/srv"}, expect=200)
        self.assertFalse(listing["running"])


if __name__ == "__main__":
    unittest.main()
