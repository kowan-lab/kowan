"""Every notification provider builds the right HTTP request (an httpcap.mock
handler plays Slack/Discord/Telegram/ntfy/webhook) and reports a failing
endpoint; required fields are enforced."""

import json
import unittest

from kowan import httpcap
from kowan.errors import Validation
from kowan.notify import Message
from kowan.notify import provider as notify


class TestProviders(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        notify.load_builtins()
        self.requests = []
        self.status = 200

    def transport(self):
        def handler(request):
            self.requests.append(request)
            return httpcap.Reply(self.status, text="ok" if self.status < 400 else "boom")

        return httpcap.mock(handler)

    async def send(self, ptype, config, level="critical"):
        p = notify.build(ptype, config)
        with self.transport():
            await p.send(Message(title="Disk full", body="95% used", level=level))
        return self.requests[-1]

    async def test_each_provider(self):
        r = await self.send("slack", {"url": "https://hooks.slack.test/x"})
        self.assertIn("Disk full", json.loads(r.content)["text"])
        r = await self.send("discord", {"url": "https://discord.test/api/webhooks/1"})
        self.assertIn("95% used", json.loads(r.content)["content"])
        r = await self.send("telegram", {"botToken": "123:abc", "chatId": "-100"})
        self.assertEqual(str(r.url), "https://api.telegram.org/bot123:abc/sendMessage")
        self.assertEqual(json.loads(r.content)["chat_id"], "-100")
        for level, prio in (("critical", "urgent"), ("warning", "high"), ("info", "default")):
            r = await self.send("ntfy", {"server": "https://ntfy.test/", "topic": "kowan", "token": "tk"}, level)
            self.assertEqual((str(r.url), r.headers["Priority"]), ("https://ntfy.test/kowan", prio))
            self.assertEqual(r.headers["Authorization"], "Bearer tk")
        r = await self.send("ntfy", {"topic": "kowan"}, level="")
        self.assertEqual(str(r.url), "https://ntfy.sh/kowan")
        self.assertNotIn("Priority", r.headers)
        r = await self.send("webhook", {"url": "https://hook.test/in", "headers": "X-Env: prod; bad; :x;Auth:Bearer t"})
        self.assertEqual((r.headers["X-Env"], r.headers["Auth"]), ("prod", "Bearer t"))
        self.assertEqual(json.loads(r.content)["level"], "critical")

    async def test_failures_and_required_fields(self):
        self.status = 500
        with self.assertRaises(Exception):
            await self.send("slack", {"url": "https://hooks.slack.test/x"})
        for ptype, cfg in (("slack", {}), ("telegram", {"botToken": "x"}), ("ntfy", {}), ("webhook", {}),
                           ("discord", {})):
            with self.assertRaises(Validation, msg=ptype):
                notify.build(ptype, cfg)


if __name__ == "__main__":
    unittest.main()
