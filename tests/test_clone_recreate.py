"""``LocalClient.clone_recreate``: used by manual and automatic container image
updates. It clones the container onto the new image through the shared
``kowan.updater.clone.clone_body``, so the old image's defaults do not leak."""

import asyncio
import unittest
from unittest import mock

from kowan.docker.local import LocalClient
from kowan.errors import BadGateway


class TestCloneRecreate(unittest.TestCase):
    def test_clone_onto_new_image_drops_old_image_defaults(self):
        client = LocalClient(socket_path="/nonexistent.sock")
        inspect = {
            "Id": "0123456789abcdef",
            "Name": "/web",
            "Image": "sha256:old",
            "Config": {
                "Image": "nginx:1.26",
                # image default + user override
                "Env": ["PATH=/usr/bin", "NGINX_VERSION=1.26", "MODE=prod"],
                "Cmd": ["nginx", "-g", "daemon off;"],
                "Hostname": "0123456789ab",
                "Labels": {"org.opencontainers.image.version": "1.26", "app": "web"},
            },
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "bridge"},
            "NetworkSettings": {"Networks": {"bridge": {}, "extra": {"Aliases": ["web"]}}},
            "Mounts": [],
        }
        old_image = {"Config": {
            "Env": ["PATH=/usr/bin", "NGINX_VERSION=1.26"],
            "Cmd": ["nginx", "-g", "daemon off;"],
            "Labels": {"org.opencontainers.image.version": "1.26"},
        }}

        req_json = mock.AsyncMock(side_effect=[inspect, old_image, {"Id": "newcontainer"}])
        req = mock.AsyncMock(return_value=(204, b""))
        connect = mock.AsyncMock()
        with mock.patch.object(LocalClient, "_request_json", new=req_json), \
             mock.patch.object(LocalClient, "_request", new=req), \
             mock.patch.object(LocalClient, "connect_container_endpoint", new=connect):
            cid = asyncio.run(client.clone_recreate("web", "nginx:1.27"))

        self.assertEqual(cid, "newcontainer")
        create = req_json.await_args_list[2]
        self.assertEqual(create.args[1], "/containers/create")
        self.assertEqual(create.kwargs["params"], {"name": "web"})
        body = create.kwargs["json_body"]
        self.assertEqual(body["Image"], "nginx:1.27")
        # Only the user's own env/labels survive; image defaults follow the new image.
        self.assertEqual(body.get("Env"), ["MODE=prod"])
        self.assertNotIn("Cmd", body)
        self.assertEqual(body.get("Labels"), {"app": "web"})
        self.assertNotIn("Hostname", body)
        self.assertEqual(body["HostConfig"]["RestartPolicy"], {"Name": "unless-stopped"})

        methods = [(c.args[0], c.args[1]) for c in req.await_args_list]
        self.assertEqual(methods, [
            ("POST", "/containers/0123456789abcdef/rename"),
            ("POST", "/containers/0123456789abcdef/stop"),
            ("POST", "/containers/newcontainer/start"),
            ("DELETE", "/containers/0123456789abcdef"),
        ])
        self.assertEqual([c.args[0] for c in connect.await_args_list], ["extra"])

    def test_failed_start_restores_original(self):
        # The case that lost a container: the clone is created but cannot start
        # (here: its compose network is gone). The original must come back.
        client = LocalClient(socket_path="/nonexistent.sock")
        inspect = {
            "Id": "0123456789abcdef", "Name": "/db", "Image": "sha256:old",
            "State": {"Running": True},
            "Config": {"Image": "postgres:16-alpine"},
            "HostConfig": {"NetworkMode": "proj_default"},
            "NetworkSettings": {"Networks": {}}, "Mounts": [],
        }
        req_json = mock.AsyncMock(side_effect=[inspect, {"Config": {}}, {"Id": "clone"}])

        async def request(method, path, **kw):
            if path == "/containers/clone/start":
                raise BadGateway("failed to set up container networking: network proj_default not found")
            return (204, b"")

        req = mock.AsyncMock(side_effect=request)
        with mock.patch.object(LocalClient, "_request_json", new=req_json), \
             mock.patch.object(LocalClient, "_request", new=req):
            with self.assertRaises(BadGateway):
                asyncio.run(client.clone_recreate("db", "postgres:16-alpine"))

        calls = [(c.args[0], c.args[1], c.kwargs.get("params")) for c in req.await_args_list]
        self.assertEqual(calls[-3:], [
            ("DELETE", "/containers/clone", {"force": "true"}),
            ("POST", "/containers/0123456789abcdef/rename", {"name": "db"}),
            ("POST", "/containers/0123456789abcdef/start", None),
        ])
        self.assertNotIn(("DELETE", "/containers/0123456789abcdef", {"force": "true"}), calls)

    def test_stopped_container_is_not_started(self):
        client = LocalClient(socket_path="/nonexistent.sock")
        inspect = {"Id": "abc", "Name": "/web", "Image": "sha256:old", "State": {"Running": False},
                   "Config": {}, "HostConfig": {}, "NetworkSettings": {"Networks": {}}, "Mounts": []}
        req_json = mock.AsyncMock(side_effect=[inspect, {"Config": {}}, {"Id": "new"}])
        req = mock.AsyncMock(return_value=(204, b""))
        with mock.patch.object(LocalClient, "_request_json", new=req_json), \
             mock.patch.object(LocalClient, "_request", new=req):
            asyncio.run(client.clone_recreate("web", "nginx:1.27", start=False))
        paths = [c.args[1] for c in req.await_args_list]
        self.assertNotIn("/containers/new/start", paths)


if __name__ == "__main__":
    unittest.main()
