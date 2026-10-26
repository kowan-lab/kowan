"""The bell's notification center runs with the app and records bus events."""

import asyncio
import unittest

from aiohttp import web

from kowan import notifications
from kowan.events import Event, MemoryBus


class _Store:
    def __init__(self):
        self.items = []

    def create_notification(self, n):
        self.items.append(n)

    def prune_notifications(self, *_a, **_k):
        return 0


class _Svc:
    def __init__(self):
        self.store = _Store()
        self.bus = MemoryBus()


class NotificationCenterLifecycleTests(unittest.TestCase):
    def test_alert_reaches_the_feed_after_startup(self):
        async def run():
            svc = _Svc()
            app = web.Application()
            notifications.register(app, svc)
            runner = web.AppRunner(app)
            await runner.setup()  # fires on_startup
            await asyncio.sleep(0)
            svc.bus.publish(Event(type="alert", source="monitor", payload={"level": "critical", "title": "db down", "message": "exit 137", "id": "a1"}))
            for _ in range(50):
                if svc.store.items:
                    break
                await asyncio.sleep(0.01)
            await runner.cleanup()  # fires on_cleanup
            return svc.store.items

        items = asyncio.run(run())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "db down")
        self.assertEqual(items[0].body, "exit 137")


if __name__ == "__main__":
    unittest.main()
