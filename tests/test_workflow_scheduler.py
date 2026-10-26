"""The workflow cron scheduler: spec parsing (macros, ranges, steps, lists,
Sunday as 7, dom/dow OR semantics, errors), reload from the store skipping bad
specs and disabled workflows, and a firing that starts a cron run."""

import asyncio
import unittest
from datetime import datetime
from unittest import mock

from kowan.workflow import scheduler as sch
from kowan.workflow.scheduler import CronParseError, parse_cron
from tests.apicase import ServerCase


class TestCronParsing(unittest.TestCase):
    def test_specs(self):
        s = parse_cron("*/15 9-17 * * 1-5")
        self.assertTrue(s.matches(datetime(2026, 10, 26, 9, 30)))    # Monday
        self.assertFalse(s.matches(datetime(2026, 10, 25, 9, 30)))   # Sunday
        self.assertFalse(s.matches(datetime(2026, 10, 26, 9, 31)))
        self.assertTrue(parse_cron("0 0 * * 7").matches(datetime(2026, 10, 25, 0, 0)))  # 7 = Sunday
        # Both dom and dow restricted: either matches (cron's OR rule).
        either = parse_cron("0 12 1 * 1")
        self.assertTrue(either.matches(datetime(2026, 10, 26, 12, 0)))  # a Monday, not the 1st
        self.assertTrue(either.matches(datetime(2026, 10, 1, 12, 0)))   # the 1st, a Thursday
        self.assertTrue(parse_cron("@daily").matches(datetime(2026, 1, 1, 0, 0)))
        self.assertTrue(parse_cron("5,10 0 1-31/2 * *").matches(datetime(2026, 1, 3, 0, 10)))
        for bad in ("", "@sometimes", "* * *", "61 * * * *", "*/0 * * * *", "*/x * * * *", "a-b * * * *",
                    "x * * * *", "1,,2 * * * *", "5-1 * * * *"):
            with self.assertRaises(CronParseError, msg=bad):
                parse_cron(bad)


class TestScheduler(ServerCase):
    async def test_reload_and_fire(self):
        steps = [{"name": "b", "type": "backup", "config": {}}]
        _, ok = await self.call("POST", "/workflows", "admin", json={
            "name": "nightly", "enabled": True, "steps": steps,
            "triggers": [{"type": "cron", "enabled": True, "config": {"cron": "* * * * *"}}]}, expect=201)
        await self.call("POST", "/workflows", "admin", json={
            "name": "off", "enabled": False, "steps": steps,
            "triggers": [{"type": "cron", "enabled": True, "config": {"cron": "* * * * *"}}]}, expect=201)
        scheduler = self.svc.workflow_scheduler
        # A bad spec stored by other means is skipped on reload.
        wf = self.svc.store.get_workflow(ok["id"])
        wf.triggers[0].config["cron"] = "bogus"
        self.svc.store.update_workflow(wf)
        await scheduler.reload()
        self.assertEqual(scheduler._entries, [])
        wf.triggers[0].config["cron"] = "* * * * *"
        self.svc.store.update_workflow(wf)
        await scheduler.reload()
        self.assertEqual([e.workflow_name for e in scheduler._entries], ["nightly"])

        scheduler._fire(scheduler._entries[0])
        for _ in range(100):
            _, runs = await self.call("GET", f"/workflows/{ok['id']}/runs", "operator", expect=200)
            if runs:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(runs[0]["trigger"], "cron")

        # The minute loop: no real minute to wait for.
        real_sleep = asyncio.sleep
        calls = []

        async def fast_sleep(seconds):
            calls.append(seconds)
            if len(calls) > 2:
                raise asyncio.CancelledError
            await real_sleep(0)

        fired = []
        with mock.patch.object(sch.asyncio, "sleep", fast_sleep), \
                mock.patch.object(scheduler, "_fire", lambda e: fired.append(e)):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler._loop()
        self.assertEqual(len(fired), 1)  # once per minute, even if the loop ticks twice

        with mock.patch.object(self.svc.store, "list_workflows", side_effect=RuntimeError("db")):
            await scheduler.reload()  # logged, keeps the previous schedule
        self.assertEqual(len(scheduler._entries), 1)


if __name__ == "__main__":
    unittest.main()
