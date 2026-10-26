"""The LocalClient suite again, but through the tunnel:

    RemoteClient --mux stream--> agent serve_agent_ops --> LocalClient --> fake Engine

Every op, its arguments, results, streams and errors must survive the agent
protocol, so the remote path behaves exactly like the local one."""

import asyncio
import unittest

from kowan import agentproto
from kowan.agent_ops_server import serve_agent_ops
from kowan.docker.local import LocalClient
from kowan.docker.remote import make_remote_client
from kowan.errors import NotFound
from kowan.mux.session import Session
from kowan.transport.memory import memory_pair
from tests import test_local_client as local
from tests.fakecli import FakeDockerCLI
from tests.fakeengine import FakeEngine


class _Remote:
    """A RemoteClient plus the in-memory tunnel and agent behind it."""

    def __init__(self, socket: str) -> None:
        ta, tb = memory_pair()
        self._cp = Session(ta, is_client=True).start()
        self._agent = Session(tb, is_client=False).start()
        self.local = LocalClient(socket)
        self._serve = asyncio.ensure_future(serve_agent_ops(self._agent, self.local))
        self.remote = make_remote_client(lambda: self._cp.open_stream())

    # LocalClient-only helpers (server self-update runs on the local host only).
    LOCAL_ONLY = {"stop_container_timeout", "container_logs_tail", "rename_container", "update_container_config"}

    def __getattr__(self, name):
        return getattr(self.local if name in self.LOCAL_ONLY else self.remote, name)

    async def close(self) -> None:
        self._serve.cancel()
        await self._cp.close()
        await self._agent.close()
        await self.local.close()


class RemoteCase(local.EngineCase):
    async def asyncSetUp(self):
        self.engine = FakeEngine()
        self.client = _Remote(await self.engine.start())


def _remote(cls):
    name = "Remote" + cls.__name__[len("Test"):]
    return type(name, (RemoteCase, cls), {"__module__": __name__, "__qualname__": name, "__doc__": cls.__doc__})


RemoteContainers = _remote(local.TestContainers)
RemoteImages = _remote(local.TestImages)
RemoteVolumes = _remote(local.TestVolumes)
RemoteNetworks = _remote(local.TestNetworks)
RemoteSystemAndPlugins = _remote(local.TestSystemAndPlugins)
RemoteContainerFiles = _remote(local.TestContainerFiles)

class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        out, self._data = (self._data, b"") if n < 0 else (self._data[:n], self._data[n:])
        return out


class TestRemoteOnly(RemoteCase):
    """Ops the local suite doesn't reach, and the protocol's own error paths."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine.add_image("alpine:3.22")  # the helper image for volume access

    async def test_uploads_relabel_and_compose(self):
        self.engine.add_volume("data")
        await self.client.upload_volume_file("data", "/", "hello.txt", 5, _Bytes(b"hello"))
        self.assertEqual(await self.client.read_volume_file("data", "/hello.txt"), b"hello")

        cid = self.engine.add_container("web", labels={"old": "1"})
        self.engine.add_network("edge")
        new_id = await self.client.container_relabel(cid, {"new": "2"}, ["old"], ["edge"], [])
        labels = self.engine.containers[new_id]["Config"]["Labels"]
        self.assertEqual((labels.get("new"), "old" in labels), ("2", False))

        cli = FakeDockerCLI()
        try:
            await self.client.compose_up("shop", "services:\n  web:\n    image: nginx\n")
            await self.client.compose_down("shop")
            await self.client.swarm_deploy("st", "services: {}\n")
            await self.client.swarm_remove("st")
            argv = [c["argv"] for c in cli.calls()]
            self.assertTrue(any(a[:3] == ["compose", "-p", "shop"] for a in argv), argv)
            self.assertTrue(any(a[:2] == ["stack", "deploy"] for a in argv), argv)
        finally:
            cli.close()

    async def test_protocol_errors(self):
        # An unknown op, a broken header and a streaming op that fails up front.
        stream = await self.client._cp.open_stream()
        with self.assertRaises(agentproto.ProtocolError):
            await agentproto.call(stream, "format_disk", {})
        stream = await self.client._cp.open_stream()
        await stream.write(b"not json\n")
        line = await agentproto._read_line(stream)
        self.assertIn(b"bad request header", line)
        with self.assertRaises(NotFound):
            await self.client.container_logs("missing", False, 0)
        with self.assertRaises(NotFound):
            await self.client.inspect_volume("missing")


class TestRemoteExec(RemoteCase):
    async def test_interactive_exec_over_the_tunnel(self):
        from unittest import mock

        from tests.test_websockets import EchoShell

        shell = EchoShell()

        async def attach(self_, id, cmd, tty):
            return shell

        with mock.patch.object(LocalClient, "exec_attach", attach):
            stream = await self.client.exec_attach("c1", ["/bin/sh"], True)
            self.assertEqual(await asyncio.wait_for(stream.read(64), 3), b"$ ")
            await stream.write(b"ls\n")
            self.assertEqual(await asyncio.wait_for(stream.read(64), 3), b"echo:ls\n")
            await stream.close()
            await stream.close()  # idempotent
        for _ in range(50):
            if shell.closed:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(shell.closed)

        async def refuse(self_, id, cmd, tty):
            from kowan.errors import NotFound
            raise NotFound("No such container: c2")

        with mock.patch.object(LocalClient, "exec_attach", refuse):
            with self.assertRaises(NotFound):
                await self.client.exec_attach("c2", ["/bin/sh"], True)


# Don't run the local classes a second time from this module.
del local


if __name__ == "__main__":
    unittest.main()
