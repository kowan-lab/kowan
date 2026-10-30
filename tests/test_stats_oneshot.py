"""One-shot container stats: CPU% from the delta against the previous sample.

``LocalClient.container_stats`` asks the Engine for ``one-shot=true`` (API >=
1.41), which carries no ``precpu_stats``; :class:`CpuSampleCache` keeps the
previous ``cpu_stats`` per container and takes the delta against it, as a share
of the whole host (100 = every core busy, not 100 per core). The first sample of a container (no baseline) falls back to the legacy
two-sample call.
"""

import asyncio
import unittest

from kowan.docker.local import CpuSampleCache, LocalClient


def _raw(total, system, online=4, mem=100, limit=1000):
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": total},
            "system_cpu_usage": system,
            "online_cpus": online,
        },
        "precpu_stats": {},
        "memory_stats": {"usage": mem, "limit": limit},
        "pids_stats": {"current": 3},
    }


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestCpuSampleCache(unittest.TestCase):
    def test_first_sample_has_no_baseline(self):
        c = CpuSampleCache(clock=_Clock())
        self.assertIsNone(c.compute("a", _raw(100, 1000)))
        self.assertIn("a", c)

    def test_delta_is_a_share_of_the_host(self):
        clk = _Clock()
        c = CpuSampleCache(clock=clk)
        c.compute("a", _raw(1_000, 10_000, online=4))
        clk.t += 15
        # cpu delta 500 over system delta 4000 (all 4 cpus) -> 12.5% of the host;
        # docker stats would say 50% (x4 cpus).
        self.assertAlmostEqual(c.compute("a", _raw(1_500, 14_000, online=4)), 12.5)
        clk.t += 15
        # idle: no cpu progress -> 0%
        self.assertEqual(c.compute("a", _raw(1_500, 18_000, online=4)), 0.0)

    def test_every_core_busy_is_100(self):
        clk = _Clock()
        c = CpuSampleCache(clock=clk)
        c.compute("a", _raw(0, 1000, online=8))
        clk.t += 1
        self.assertAlmostEqual(c.compute("a", _raw(1000, 2000, online=8)), 100.0)

    def test_counter_reset_after_restart_has_no_baseline(self):
        clk = _Clock()
        c = CpuSampleCache(clock=clk)
        c.compute("a", _raw(5_000, 10_000))
        clk.t += 15
        self.assertIsNone(c.compute("a", _raw(10, 20_000)))  # total went down
        clk.t += 15
        self.assertIsNotNone(c.compute("a", _raw(20, 30_000)))  # re-seeded

    def test_samples_too_close_reuse_last_value(self):
        clk = _Clock()
        c = CpuSampleCache(clock=clk)
        c.compute("a", _raw(0, 0 + 1))
        clk.t += 10
        first = c.compute("a", _raw(1_000, 4_001, online=1))
        clk.t += 0.01  # concurrent caller: delta would be noise
        self.assertEqual(c.compute("a", _raw(1_000_000, 4_002, online=1)), first)

    def test_missing_system_usage_is_not_computable(self):
        c = CpuSampleCache(clock=_Clock())
        raw = _raw(1, 0)
        self.assertIsNone(c.compute("a", raw))
        self.assertNotIn("a", c)

    def test_ttl_eviction(self):
        clk = _Clock()
        c = CpuSampleCache(ttl=60, clock=clk)
        c.compute("old", _raw(1, 1))
        clk.t += 30
        c.compute("new", _raw(1, 1))
        clk.t += 31  # "old" is now 61s stale, "new" 31s
        c.compute("other", _raw(1, 1))
        self.assertNotIn("old", c)
        self.assertIn("new", c)
        # an evicted container has no baseline again
        self.assertIsNone(c.compute("old", _raw(2, 2)))

    def test_max_entries_bound_drops_least_recent(self):
        clk = _Clock()
        c = CpuSampleCache(max_entries=3, clock=clk)
        for cid in ("a", "b", "c"):
            clk.t += 1
            c.record(cid, _raw(1, 1))
        clk.t += 1
        c.record("a", _raw(2, 2))  # refresh a -> b is now least recent
        clk.t += 1
        c.record("d", _raw(1, 1))
        self.assertEqual(len(c), 3)
        self.assertNotIn("b", c)
        for cid in ("a", "c", "d"):
            self.assertIn(cid, c)


class _StubLocal(LocalClient):
    """LocalClient with the HTTP layer stubbed: records each stats call shape."""

    def __init__(self, api="v1.43", samples=None):
        super().__init__("/nonexistent.sock")
        self._api_version = api
        self.calls = []
        self._samples = list(samples or [])

    async def _get_session(self):
        return None

    async def _request_json(self, method, path, *, params=None, json_body=None):
        self.calls.append(dict(params or {}))
        return self._samples.pop(0)


class TestContainerStatsOneShot(unittest.TestCase):
    def test_first_call_falls_back_then_uses_one_shot(self):
        legacy = _raw(1_000, 10_000, online=2)
        legacy["precpu_stats"] = {
            "cpu_usage": {"total_usage": 900},
            "system_cpu_usage": 9_000,
        }
        c = _StubLocal(samples=[_raw(1_000, 10_000, online=2), legacy, _raw(1_200, 12_000, online=2)])
        c._cpu_samples._clock = _Clock()

        async def run():
            first = await c.container_stats("x")
            c._cpu_samples._clock.t += 15
            second = await c.container_stats("x")
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(
            c.calls,
            [
                {"stream": "false", "one-shot": "true"},
                {"stream": "0"},
                {"stream": "false", "one-shot": "true"},
            ],
        )
        # legacy sample: 100/1000*100 = 10% of the host
        self.assertAlmostEqual(first.cpu_percent, 10.0)
        # one-shot vs the legacy sample's cpu_stats: 200/2000*100 = 10%
        self.assertAlmostEqual(second.cpu_percent, 10.0)
        self.assertEqual(second.mem_usage, 100)
        self.assertAlmostEqual(second.mem_percent, 10.0)
        self.assertEqual(second.pids, 3)

    def test_old_daemon_always_uses_legacy_call(self):
        c = _StubLocal(api="v1.40", samples=[_raw(1, 1), _raw(2, 2)])

        async def run():
            await c.container_stats("x")
            await c.container_stats("x")

        asyncio.run(run())
        self.assertEqual(c.calls, [{"stream": "0"}, {"stream": "0"}])
        self.assertEqual(len(c._cpu_samples), 0)


if __name__ == "__main__":
    unittest.main()
