"""When the Docker daemon itself fails, every image/volume/network/container
endpoint answers 502 with the daemon's message instead of a 500 or a hang."""

import io
import unittest

import aiohttp

from tests.apicase import ServerCase

H = "/hosts/local"


class TestDaemonErrors(ServerCase):
    async def test_endpoints_map_daemon_failures(self):
        self.engine.add_image("alpine:3.20")
        img = self.engine.add_image("redis:7")
        self.engine.add_volume("v")
        cases = [
            ("GET", r"/images/json", "GET", H + "/images", None),
            ("GET", r"/images/.+/json", "GET", f"{H}/images/{img}/inspect", None),
            ("DELETE", r"/images/.+", "DELETE", f"{H}/images/{img}", None),
            ("POST", r"/images/create", "POST", H + "/images/pull", {"ref": "nginx:1"}),
            ("GET", r"/volumes$", "GET", H + "/volumes", None),
            ("GET", r"/volumes/.+", "GET", H + "/volumes/v/inspect", None),
            ("DELETE", r"/volumes/.+", "DELETE", H + "/volumes/v", None),
            ("POST", r"/volumes/create", "POST", H + "/volumes", {"name": "n"}),
            ("POST", r"/containers/create", "GET", H + "/volumes/v/browse", None),
            ("POST", r"/containers/create", "GET", H + "/volumes/v/file?path=/a.txt", None),
            ("POST", r"/containers/create", "GET", H + "/volumes/v/download?path=/a.txt", None),
            ("POST", r"/containers/create", "GET", H + "/volumes/v/archive", None),
            ("GET", r"/networks$", "GET", H + "/networks", None),
            ("GET", r"/containers/json", "GET", H + "/containers", None),
            ("GET", r"/info", "GET", H + "/system", None),
            ("GET", r"/system/df", "GET", H + "/df", None),
        ]
        for fm, fp, method, path, body in cases:
            self.engine.fail[(fm, fp)] = (500, "daemon exploded")
            status, resp = await self.call(method, path, "admin", json=body)
            del self.engine.fail[(fm, fp)]
            self.assertEqual(status, 502, (method, path, resp))
        self.engine.fail[("POST", r"/containers/create")] = (500, "daemon exploded")
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(b"x"), filename="x.txt")
        status, _ = await self.call("POST", H + "/volumes/v/upload", "admin", data=form)
        self.assertEqual(status, 502)
        del self.engine.fail[("POST", r"/containers/create")]
        # Inspect works even when the history call fails (best effort).
        self.engine.fail[("GET", r"/images/.+/history")] = (500, "nope")
        _, info = await self.call("GET", f"{H}/images/{img}/inspect", "viewer", expect=200)
        self.assertEqual(info["history"], [])


class TestPluginsNetworksAudit(ServerCase):
    async def test_plugins_and_networks_errors(self):
        await self.call("POST", H + "/plugins/install", "admin", json={}, expect=400)
        await self.call("DELETE", H + "/plugins", "admin", expect=400)
        for fm, fp, method, path, body in (
                ("GET", r"/plugins$", "GET", H + "/plugins", None),
                ("GET", r"/plugins/privileges", "POST", H + "/plugins/install", {"remote": "vieux/sshfs"}),
                ("DELETE", r"/plugins/.+", "DELETE", H + "/plugins?name=x", None),
                ("GET", r"/networks/.+", "GET", H + "/networks/abc", None),
                ("POST", r"/networks/create", "POST", H + "/networks", {"name": "n"}),
                ("DELETE", r"/networks/.+", "DELETE", H + "/networks/abc", None),
                ("POST", r"/networks/.+/connect", "POST", H + "/networks/abc/connect", {"containerId": "c"}),
                ("POST", r"/networks/.+/disconnect", "POST", H + "/networks/abc/disconnect", {"containerId": "c"})):
            self.engine.fail[(fm, fp)] = (500, "daemon exploded")
            status, resp = await self.call(method, path, "admin", json=body)
            del self.engine.fail[(fm, fp)]
            self.assertEqual(status, 502, (path, resp))
        for path in ("/networks", "/networks/abc/connect", "/networks/abc/disconnect", "/plugins/install"):
            status, _ = await self.call("POST", H + path, "admin", data=b"[1]")
            self.assertEqual(status, 400, path)
        for path, body in (("/networks", {"name": "n", "labels": "x"}), ("/volumes", {"name": "v", "labels": [1]}),
                           ("/plugins/install", {"remote": "r", "settings": "x"})):
            status, _ = await self.call("POST", H + path, "admin", json=body)
            self.assertEqual(status, 400, path)

    async def test_audit_filters(self):
        await self.call("POST", "/credentials", "admin", json={"kind": "secret", "name": "a", "fields": {"v": "1"}})
        _, page = await self.call("GET", "/audit", "admin", params={"limit": "9999", "offset": "x", "user": "root"},
                                  expect=200)
        self.assertTrue(page["items"])
        _, page = await self.call("GET", "/audit", "admin", params={"path": "/api/v1/credentials"}, expect=200)
        self.assertTrue(all("/credentials" in e["path"] for e in page["items"]))
        from unittest import mock
        with mock.patch.object(self.svc.store, "list_audit", side_effect=RuntimeError("db")):
            await self.call("GET", "/audit", "admin", expect=500)


if __name__ == "__main__":
    unittest.main()
