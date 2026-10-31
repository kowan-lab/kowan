"""Inventory export: every visible container, as CSV or JSON, by group."""

import csv
import io

from tests.apicase import ServerCase


class TestInventory(ServerCase):
    async def test_csv_and_json(self):
        labels = {"com.docker.compose.project": "shop", "com.docker.compose.service": "web"}
        self.engine.add_container("shop-web-1", "nginx:1.27", labels=labels, ports=[(80, 8080)])
        self.engine.add_container("lone", "redis:7", state="exited")
        self.engine.add_container("helper", "x:1", labels={"kowan.support": "true"})
        await self.call("PATCH", "/hosts/local", "admin", json={"tags": ["production"]}, expect=200)
        r = await self.client.get("/api/v1/inventory", headers=self.h["viewer"])
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(await r.text())))
        by = {row["container"]: row for row in rows}
        self.assertEqual(set(by), {"shop-web-1", "lone"})
        web = by["shop-web-1"]
        self.assertEqual((web["groups"], web["workload"], web["kind"], web["service"], web["ports"]),
                         ("production", "stack~local~shop", "stack", "web", "8080->80/tcp"))
        self.assertEqual((by["lone"]["kind"], by["lone"]["state"]), ("container", "exited"))
        _, data = await self.call("GET", "/inventory", "viewer", params={"format": "json", "group": "staging"}, expect=200)
        self.assertEqual(data, {"items": [], "errors": []})
        _, data = await self.call("GET", "/inventory", "viewer", params={"format": "json", "group": "production"}, expect=200)
        self.assertEqual(len(data["items"]), 2)
        await self.call("GET", "/inventory", "viewer", params={"format": "xml"}, expect=400)
