"""Password sign-in is throttled: failures per address and per username, a
success clears the username, and the setup token cannot be guessed at speed."""

import datetime
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from kowan.auth import ratelimit
from kowan.auth.ratelimit import LoginLimiter
from kowan.config import ServerConfig
from kowan.server import build_app


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestLimiter(unittest.TestCase):
    def test_pair_limit(self):
        lim = LoginLimiter(clock=Clock())
        for _ in range(ratelimit.MAX_FAILURES_PER_PAIR):
            self.assertIsNone(lim.retry_after("10.0.0.1", "Admin"))
            lim.failed("10.0.0.1", "Admin")
        self.assertIsNotNone(lim.retry_after("10.0.0.1", "admin"))
        # Another machine is not affected by this one's failures.
        self.assertIsNone(lim.retry_after("10.0.0.2", "admin"))

    def test_window_expires(self):
        clock = Clock()
        lim = LoginLimiter(clock=clock)
        for _ in range(ratelimit.MAX_FAILURES_PER_PAIR):
            lim.failed("10.0.0.1", "ops")
        clock.t += ratelimit.WINDOW_SECONDS + 1
        self.assertIsNone(lim.retry_after("10.0.0.1", "ops"))

    def test_per_address(self):
        lim = LoginLimiter(clock=Clock())
        for i in range(ratelimit.MAX_FAILURES_PER_ADDRESS):
            lim.failed("10.0.0.9", f"user{i}")
        self.assertIsNotNone(lim.retry_after("10.0.0.9", "someone-else"))
        self.assertIsNone(lim.retry_after("10.0.0.10", "someone-else"))

    def test_ipv6_counted_per_64(self):
        lim = LoginLimiter(clock=Clock())
        for i in range(ratelimit.MAX_FAILURES_PER_ADDRESS):
            lim.failed(f"2001:db8:1:2::{i + 1:x}", f"user{i}")
        self.assertIsNotNone(lim.retry_after("2001:db8:1:2::ffff", "x"))
        self.assertIsNone(lim.retry_after("2001:db8:1:3::1", "x"))

    def test_distributed_attack_cannot_lock_out_a_known_address(self):
        lim = LoginLimiter(clock=Clock())
        lim.succeeded("192.0.2.10", "admin")  # the real admin signed in before
        for i in range(ratelimit.MAX_FAILURES_PER_USERNAME):
            lim.failed(f"198.51.{i // 200}.{i % 200}", "admin")
        self.assertIsNotNone(lim.retry_after("203.0.113.1", "admin"))
        self.assertIsNone(lim.retry_after("192.0.2.10", "admin"))

    def test_lookups_do_not_grow_memory(self):
        lim = LoginLimiter(clock=Clock())
        for i in range(1000):
            lim.retry_after(f"10.0.{i // 250}.{i % 250}", f"random-{i}")
        self.assertEqual((len(lim._by_address), len(lim._by_username), len(lim._by_pair)), (0, 0, 0))


def _cfg():
    return ServerConfig(addr="127.0.0.1:0", data_dir=tempfile.mkdtemp(), jwt_secret="x" * 48,
                        monitor_interval=datetime.timedelta(seconds=3600),
                        metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="")


class TestEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_setup_token_guessing_is_throttled(self):
        statuses = []
        for _ in range(ratelimit.MAX_FAILURES_PER_ADDRESS + 1):
            r = await self.client.post("/api/v1/auth/init", json={
                "username": "root", "password": "supersecret", "setupToken": "guess"})
            statuses.append(r.status)
        self.assertEqual(statuses[-1], 429)
        self.assertTrue(all(s == 403 for s in statuses[:-1]))

    async def test_login_is_throttled_with_retry_after(self):
        await self.client.post("/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": self.svc.setup_token})
        for _ in range(ratelimit.MAX_FAILURES_PER_PAIR):
            r = await self.client.post("/api/v1/auth/login", json={"username": "root", "password": "wrong-password"})
            self.assertEqual(r.status, 401)
        # Even the right password waits now: the attacker cannot tell.
        r = await self.client.post("/api/v1/auth/login", json={"username": "root", "password": "supersecret"})
        self.assertEqual(r.status, 429)
        self.assertGreater(int(r.headers["Retry-After"]), 0)

    async def test_short_passwords_are_refused_for_new_users(self):
        r = await self.client.post("/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": self.svc.setup_token})
        h = {"Authorization": "Bearer " + (await r.json())["token"]}
        r = await self.client.post("/api/v1/users", headers=h, json={"username": "x", "password": "123", "role": "viewer"})
        self.assertEqual(r.status, 400)


if __name__ == "__main__":
    unittest.main()
