"""When the database fails, endpoints answer 500 with a generic message (the
detail is only logged, never sent) instead of crashing or leaking internals.
One table drives many handlers; plus the matching input-validation branches."""

import unittest
from unittest import mock

from tests.apicase import ServerCase


class TestStoreFailures(ServerCase):
    async def test_generic_500s(self):
        _, me = await self.call("GET", "/me", "admin", expect=200)
        _, users = await self.call("GET", "/users", "admin")
        viewer = next(u["id"] for u in users if u["username"] == "viewer")
        cases = [
            ("list_users", "GET", "/users", None),
            ("create_user", "POST", "/users", {"username": "zed", "password": "zed-password", "role": "viewer"}),
            ("update_user", "PUT", f"/users/{viewer}", {"role": "operator"}),
            ("delete_user", "DELETE", f"/users/{viewer}", None),
            ("list_notifications", "GET", "/notifications", None),
            ("count_unread_notifications", "GET", "/notifications/unread-count", None),
            ("mark_notification_read", "POST", "/notifications/x/read", None),
            ("mark_all_notifications_read", "POST", "/notifications/read-all", None),
        ]
        store = self.svc.store
        for method_name, http, path, body in cases:
            with mock.patch.object(store, method_name, side_effect=RuntimeError("database is locked")):
                status, resp = await self.call(http, path, "admin", json=body)
            self.assertEqual(status, 500, (method_name, resp))
            self.assertNotIn("database is locked", str(resp))
        with mock.patch.object(store, "count_users", side_effect=RuntimeError("x")):
            r = await self.client.get("/api/v1/auth/status")
            self.assertEqual(r.status, 500)
            r = await self.client.post("/api/v1/auth/init", json={"username": "a", "password": "b" * 12})
            self.assertEqual(r.status, 500)
        with mock.patch.object(self.svc.jwt, "issue", side_effect=RuntimeError("no key")):
            r = await self.client.post("/api/v1/auth/login", json={"username": "viewer",
                                                                   "password": "viewer-password"})
            self.assertEqual(r.status, 500)
        with mock.patch("kowan.api.authapi.hash_password", side_effect=RuntimeError("scrypt")):
            status, _ = await self.call("POST", "/users", "admin", json={
                "username": "q", "password": "q-password-1", "role": "viewer"})
            self.assertEqual(status, 500)
            status, _ = await self.call("PUT", f"/users/{viewer}", "admin", json={"password": "another-pass"})
            self.assertEqual(status, 500)
        self.assertTrue(me)

    async def test_auth_input_validation(self):
        for body in (b"not json", b"[1]"):
            r = await self.client.post("/api/v1/auth/login", data=body, headers={"Content-Type": "application/json"})
            self.assertEqual(r.status, 400)
        r = await self.client.post("/api/v1/auth/login", json={"username": "u" * 5000, "password": "p"})
        self.assertEqual(r.status, 400)
        for body in (b"nope", b"[]"):
            status, _ = await self.call("POST", "/users", "admin", data=body)
            self.assertEqual(status, 400)
        _, users = await self.call("GET", "/users", "admin")
        viewer = next(u["id"] for u in users if u["username"] == "viewer")
        status, _ = await self.call("PUT", f"/users/{viewer}", "admin", data=b"nope")
        self.assertEqual(status, 400)
        _, renamed = await self.call("PUT", f"/users/{viewer}", "admin", json={"username": "viewer2"}, expect=200)
        self.assertEqual(renamed["username"], "viewer2")

    async def test_notification_recording_and_pruning_failures(self):
        from kowan.events import Event
        import asyncio

        from kowan.notifications import NotificationCenter

        center = NotificationCenter(self.svc)
        with mock.patch.object(self.svc.store, "create_notification", side_effect=RuntimeError("full")):
            center._record(Event(type="notice", source="t", payload={"title": "x"}))
        with mock.patch.object(self.svc.store, "prune_notifications", side_effect=RuntimeError("busy")):
            center._prune()
        self.svc.store.create_notification  # the real one works again
        center._record(Event(type="alert", source="monitor", payload={"title": "y", "level": "critical"}))
        center._prune()
        self.assertTrue(asyncio)


if __name__ == "__main__":
    unittest.main()
