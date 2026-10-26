"""``LocalClient`` against a fake Docker Engine (tests/fakeengine.py) over a real
unix socket: the whole HTTP path runs (requests, version negotiation, error
mapping, stream demuxing, tar handling) without touching the machine's Docker."""

import asyncio
import io
import json
import tarfile
import unittest

from kowan.docker.local import LocalClient
from kowan.errors import BadGateway, Conflict, NotFound, Validation
from kowan.models import ContainerSpec, KeyValue, PortMapping, VolumeMapping
from kowan.models.container import IPAMConfig, NetworkCreateOptions, VolumeCreateOptions
from kowan.models.volumeplugin import PluginInstallOptions
from tests.fakeengine import FakeEngine


async def _read_all(stream) -> bytes:
    out = bytearray()
    try:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            out.extend(chunk)
    finally:
        await stream.close()
    return bytes(out)


class EngineCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = FakeEngine()
        path = await self.engine.start()
        self.client = LocalClient(path)

    async def asyncTearDown(self):
        await self.client.close()
        await self.engine.stop()


class TestContainers(EngineCase):
    async def test_ping_and_version_negotiation(self):
        await self.client.ping()
        await self.client.list_containers(True)
        # The client clamps to what the daemon supports and uses that prefix.
        self.assertTrue(any(c[1] == "/containers/json" for c in self.engine.calls))

    async def test_list_and_inspect(self):
        web = self.engine.add_container("web", ports=[(80, 8080)], labels={"app": "web"})
        self.engine.add_container("old", state="exited")
        running = await self.client.list_containers(False)
        everyone = await self.client.list_containers(True)
        self.assertEqual([c.names for c in running], [["/web"]])
        self.assertEqual(len(everyone), 2)
        self.assertEqual((running[0].ports[0].private_port, running[0].ports[0].public_port), (80, 8080))
        self.assertEqual(running[0].labels, {"app": "web"})
        info = await self.client.inspect_container(web)
        self.assertEqual(info["Name"], "/web")

    async def test_lifecycle(self):
        cid = self.engine.add_container("svc")
        await self.client.stop_container(cid)
        self.assertFalse(self.engine.containers[cid]["State"]["Running"])
        await self.client.start_container(cid)
        await self.client.restart_container(cid)
        await self.client.pause_container(cid)
        self.assertTrue(self.engine.containers[cid]["State"]["Paused"])
        await self.client.unpause_container(cid)
        await self.client.stop_container_timeout(cid, 3)
        self.assertIn(("POST", f"/containers/{cid}/stop", {"t": "3"}, None), self.engine.calls)
        await self.client.rename_container(cid, "svc2")
        self.assertEqual(self.engine.containers[cid]["Name"], "/svc2")
        await self.client.update_container_config(cid, {"RestartPolicy": {"Name": "always"}})
        self.assertEqual(self.engine.containers[cid]["HostConfig"]["RestartPolicy"], {"Name": "always"})
        await self.client.remove_container(cid, True)
        self.assertNotIn(cid, self.engine.containers)

    async def test_errors_are_typed(self):
        # A refusal by the daemon is the caller's problem (409/400), not a
        # broken upstream (502).
        with self.assertRaises(NotFound):
            await self.client.inspect_container("missing")
        cid = self.engine.add_container("busy")
        with self.assertRaises(Conflict):
            await self.client.remove_container(cid, False)
        self.engine.fail[("POST", r"/containers/create")] = (400, "invalid reference format")
        with self.assertRaises(Validation):
            await self.client.create_container("x", ContainerSpec(image="nginx:1.27"), False)
        self.engine.fail[("GET", r"/info")] = (500, "boom")
        with self.assertRaises(BadGateway):
            await self.client.system_info()

    async def test_logs_are_demultiplexed(self):
        cid = self.engine.add_container("logger", logs=["one", "two", "three"])
        data = await _read_all(await self.client.container_logs(cid, False, 2))
        text = data.decode()
        self.assertNotIn("\x01\x00", text)  # stdcopy headers stripped
        self.assertIn("two", text)
        self.assertNotIn("one", text)
        self.assertEqual(await self.client.container_logs_tail(cid, 0), ["one", "two", "three"])

    async def test_stats(self):
        cid = self.engine.add_container("busy")
        s = await self.client.container_stats(cid)
        self.assertGreater(s.cpu_percent, 0)
        self.assertGreater(s.mem_usage, 0)
        self.assertEqual(s.mem_limit, 1 << 30)
        self.assertEqual((s.net_rx, s.net_tx, s.blk_read, s.blk_write), (1000, 2000, 300, 400))

    async def test_spec_round_trip_and_create(self):
        spec = ContainerSpec(
            image="nginx:1.27", env=[KeyValue(key="MODE", value="prod")],
            ports=[PortMapping(hostPort=8081, containerPort=80, protocol="tcp")],
            volumes=[VolumeMapping(source="data", target="/data")], labels={"team": "a"},
            restartPolicy="always",
        )
        self.engine.add_image("nginx:1.27")
        cid = await self.client.create_container("made", spec, True)
        c = self.engine.containers[cid]
        self.assertTrue(c["State"]["Running"])
        self.assertIn("MODE=prod", c["Config"]["Env"])
        back = await self.client.container_spec(cid)
        self.assertEqual(back.image, "nginx:1.27")
        self.assertEqual([(p.host_port, p.container_port) for p in back.ports], [(8081, 80)])
        self.assertIn(("MODE", "prod"), [(kv.key, kv.value) for kv in back.env])

    async def test_exec_and_status(self):
        cid = self.engine.add_container("sh")
        self.engine.exec_handler = lambda c, argv: (0, b"hello\n") if argv[:1] == ["greet"] else None
        self.assertEqual(await self.client.exec(cid, ["greet"]), "hello\n")
        st = await self.client.container_exec_status(cid, ["greet"], "0")
        self.assertEqual(st.get("exitCode", st.get("ExitCode", 0)), 0)

    async def test_events_stream(self):
        self.engine.emit("container", "start", "abc", {"name": "web"})
        stream = await self.client.events(until=1, filters={"type": ["container"]})
        data = await _read_all(stream)
        self.assertIn(b'"start"', data)


def _takes_args(fn) -> bool:
    import inspect

    return len([p for p in inspect.signature(fn).parameters.values() if p.default is p.empty]) > 0


class TestImages(EngineCase):
    async def test_list_inspect_history_remove(self):
        iid = self.engine.add_image("redis:7", size=5000)
        images = await self.client.list_images()
        self.assertEqual(images[0].repo_tags, ["redis:7"])
        self.assertEqual((await self.client.inspect_image("redis:7"))["Id"], iid)
        layers = await self.client.image_history("redis:7")
        self.assertEqual(len(layers), 2)
        await self.client.remove_image("redis:7", False)
        self.assertFalse(self.engine.images)

    async def test_pull_streams_progress(self):
        data = await _read_all(await self.client.pull_image("alpine:3.20", ""))
        self.assertIn(b"Downloaded newer image", data)
        self.assertIsNotNone(self.engine.image_by_ref("alpine:3.20"))

    async def test_prune(self):
        self.engine.add_image("unused:1")
        used = self.engine.add_container("keeper", "kept:1")
        self.engine.containers[used]["State"].update(Running=False, Status="exited")
        self.assertGreaterEqual(await self.client.prune_images(True), 0)
        self.assertIsNone(self.engine.image_by_ref("unused:1"))
        self.assertGreaterEqual(await self.client.prune_containers(), 0)


class TestVolumes(EngineCase):
    async def test_crud(self):
        v = await self.client.create_volume(VolumeCreateOptions(name="data", labels={"k": "v"}))
        self.assertEqual(v.name, "data")
        self.assertEqual([x.name for x in await self.client.list_volumes()], ["data"])
        self.assertEqual((await self.client.inspect_volume("data"))["Name"], "data")
        await self.client.remove_volume("data", False)
        self.assertFalse(self.engine.volumes)
        self.engine.add_volume("orphan")
        self.assertGreaterEqual(await self.client.prune_volumes(), 0)

    async def test_browse_read_download_upload(self):
        self.engine.add_image("alpine:3.20")
        self.engine.add_volume("site", {"/index.html": b"<h1>hi</h1>", "/assets/app.css": b"body{}"})
        files = await self.client.browse_volume("site", "/")
        names = sorted(f.name for f in files)
        self.assertEqual(names, ["assets", "index.html"])
        self.assertEqual(await self.client.read_volume_file("site", "/index.html"), b"<h1>hi</h1>")
        raw = await _read_all(await self.client.download_volume_file("site", "/index.html"))
        self.assertEqual(raw, b"<h1>hi</h1>")
        archive = await _read_all(await self.client.download_volume_archive("site", "/assets"))
        self.assertTrue(archive.startswith(b"PK"))  # a zip
        # helper containers are cleaned up
        self.assertFalse([c for c in self.engine.containers.values() if c["State"]["Running"]])


class TestNetworks(EngineCase):
    async def test_crud_and_connect(self):
        net = await self.client.create_network(NetworkCreateOptions(
            name="apps", driver="bridge", labels={"k": "v"}, ipam=[IPAMConfig(subnet="10.9.0.0/24")]))
        self.assertEqual(net.name, "apps")
        names = [n.name for n in await self.client.list_networks()]
        self.assertIn("apps", names)
        cid = self.engine.add_container("web")
        await self.client.connect_container(net.id, cid)
        inspected = await self.client.inspect_network(net.id)
        self.assertEqual(len(inspected.containers), 1)
        await self.client.disconnect_container(net.id, cid, False)
        await self.client.remove_network(net.id)
        self.assertIsNone(self.engine.network("apps"))
        await self.client.prune_networks()


class TestSystemAndPlugins(EngineCase):
    async def test_info_and_disk_usage(self):
        self.engine.add_container("web")
        info = await self.client.system_info()
        self.assertEqual(info.containers_running, 1)
        du = await self.client.disk_usage()
        self.assertGreaterEqual(du.build_cache_size, 0)

    async def test_swarm(self):
        self.assertFalse(await self.client.swarm_active())
        self.engine.swarm = True
        self.assertTrue(await self.client.swarm_active())
        self.assertEqual(len(await self.client.list_swarm_nodes()), 1)

    async def test_plugins(self):
        await self.client.install_plugin(PluginInstallOptions(remote="vieux/sshfs:latest", alias="sshfs"))
        plugins = await self.client.list_plugins()
        self.assertEqual(len(plugins), 1)
        await self.client.remove_plugin(plugins[0].name, True)
        self.assertFalse(self.engine.plugins)


class TestContainerFiles(EngineCase):
    async def test_stat_list_read_and_put(self):
        cid = self.engine.add_container("web", files={"/etc/app.conf": b"x=1", "/var/www/index.html": b"hi"})
        st = await self.client.container_path_stat(cid, "/etc/app.conf")
        self.assertEqual(st.get("size"), 3)
        listing = await self.client.container_list_dir(cid, "/var/www", 100)
        self.assertIn("index.html", [e["name"] for e in listing["entries"]])
        self.assertEqual(await self.client.container_read_file(cid, "/etc/app.conf", 1024), b"x=1")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("new.txt")
            info.size = 3
            tf.addfile(info, io.BytesIO(b"new"))

        async def body():
            yield buf.getvalue()

        class _Stream:
            def __init__(self):
                self._it = body().__aiter__()

            async def read(self, n=-1):
                try:
                    return await self._it.__anext__()
                except StopAsyncIteration:
                    return b""

            async def close(self):
                return None

        await self.client.container_put_archive(cid, "/etc", _Stream())
        self.assertEqual(self.engine.fs[cid].get("/etc/new.txt"), b"new")
        tar_bytes = await _read_all(await self.client.container_get_archive(cid, "/etc"))
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
            self.assertIn("etc/app.conf", tf.getnames())


if __name__ == "__main__":
    unittest.main()


class TestMoreContainerPaths(EngineCase):
    async def test_stats_stream_is_ndjson(self):
        cid = self.engine.add_container("busy")
        body = await _read_all(await self.client.container_stats_stream(cid))
        samples = [json.loads(line) for line in body.splitlines() if line.strip()]
        self.assertEqual(len(samples), 3)  # the blank and garbled lines are skipped
        self.assertIn("cpuPercent", samples[0])

    async def test_create_with_limits_gpus_and_networks(self):
        self.engine.add_image("nginx:1.27")
        self.engine.add_network("front")
        self.engine.add_network("back")
        spec = ContainerSpec(image="nginx:1.27", cpuLimit=1.5, memoryLimitMB=64, memoryReservationMB=32,
                             gpus="2", networks=["front", "back"], pullImage=True)
        cid = await self.client.create_container("limited", spec, True)
        hc = self.engine.containers[cid]["HostConfig"]
        self.assertEqual(hc["NanoCpus"], 1_500_000_000)
        self.assertEqual(hc["Memory"], 64 << 20)
        self.assertEqual(hc["DeviceRequests"][0]["Count"], 2)
        self.assertEqual(set(self.engine.containers[cid]["NetworkSettings"]["Networks"]), {"front", "back"})
        self.assertTrue(any(c[1].endswith("/images/create") for c in self.engine.calls))
        # The spec read back from inspect carries the limits.
        got = await self.client.container_spec(cid)
        self.assertEqual(got.cpu_limit, 1.5)
        with self.assertRaises(Validation):
            await self.client.create_container("bad-gpus", ContainerSpec(image="nginx:1.27", gpus="many"), False)

    async def test_relabel_refuses_primary_network_detach(self):
        cid = self.engine.add_container("app", networks=["front"])
        primary = next(iter(self.engine.containers[cid]["NetworkSettings"]["Networks"]))
        with self.assertRaises(Validation):
            await self.client.container_relabel(cid, {}, [], [], [primary])

    def test_registry_auth_helpers(self):
        from kowan.docker.local import build_registry_auth, decode_registry_auth, registry_host_from_ref

        self.assertEqual(build_registry_auth("", "", ""), "")
        tok = build_registry_auth("bob", "pw", "reg.example")
        self.assertEqual(decode_registry_auth(tok), ("bob", "pw", "reg.example"))
        self.assertEqual(decode_registry_auth(""), ("", "", ""))
        for ref, host in (("nginx", "docker.io"), ("library/nginx:1", "docker.io"),
                          ("ghcr.io/a/b:1", "ghcr.io"), ("localhost/x", "localhost"),
                          ("reg:5000/x@sha256:abc", "reg:5000")):
            self.assertEqual(registry_host_from_ref(ref), host, ref)


class TestRollbacksAndErrorMapping(EngineCase):
    async def test_relabel_rolls_back_when_a_network_cannot_be_joined(self):
        self.engine.add_network("front")
        cid = self.engine.add_container("app", labels={"v": "1"}, networks=["bridge", "front"])
        self.engine.fail[("POST", r"/networks/.+/connect")] = (500, "network is gone")
        with self.assertRaises(BadGateway):
            await self.client.container_relabel(cid, {"v": "2"}, [], ["front"], [])
        # The original is back under its name, running, with its old labels.
        restored = self.engine.container("app")
        self.assertEqual(restored["Id"], cid)
        self.assertTrue(restored["State"]["Running"])
        self.assertEqual(restored["Config"]["Labels"]["v"], "1")
        self.assertEqual(len([c for c in self.engine.containers.values() if c["Name"].startswith("/app")]), 1)
        with self.assertRaises(NotFound):
            await self.client.container_relabel("missing", {}, [], [], [])

    async def test_clone_recreate_rolls_back_when_create_fails(self):
        self.engine.add_image("nginx:1.28")
        cid = self.engine.add_container("web", "nginx:1.27", networks=["bridge"])
        self.engine.fail[("POST", r"/containers/create")] = (500, "no space left on device")
        with self.assertRaises(BadGateway):
            await self.client.clone_recreate("web", "nginx:1.28")
        restored = self.engine.container("web")
        self.assertEqual(restored["Id"], cid)
        self.assertTrue(restored["State"]["Running"])

    async def test_archive_errors_are_typed(self):
        cid = self.engine.add_container("ro", files={"/etc/a": b"x"})
        for status, err in ((400, Validation), (403, Conflict), (409, Conflict), (500, BadGateway)):
            self.engine.fail[("PUT", r"/containers/.+/archive")] = (status, "read-only file system")
            with self.assertRaises(err, msg=status):
                await self.client.container_put_archive(cid, "/etc", _Bytes(b""))
        del self.engine.fail[("PUT", r"/containers/.+/archive")]
        with self.assertRaises(Validation):
            await self.client.container_read_file(cid, "/etc", 1024)  # a directory, not a file

    async def test_plugin_install_edge_cases(self):
        with self.assertRaises(Validation):
            await self.client.install_plugin(PluginInstallOptions(remote=" "))
        await self.client.install_plugin(PluginInstallOptions(remote="vieux/sshfs:latest", alias="sshfs",
                                                              settings={"DEBUG": "1"}))
        # Installing it again is not an error.
        await self.client.install_plugin(PluginInstallOptions(remote="vieux/sshfs:latest", alias="sshfs"))
        self.engine.fail[("POST", r"/plugins/pull")] = (404, "plugin not found")
        with self.assertRaises(NotFound):
            await self.client.install_plugin(PluginInstallOptions(remote="ghost/plugin:1"))


class _Bytes:
    def __init__(self, data):
        self._d = data

    async def read(self, n=-1):
        out, self._d = (self._d, b"") if n is None or n < 0 else (self._d[:n], self._d[n:])
        return out
