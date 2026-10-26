"""Alert delivery end to end: an alert on the event bus reaches the bell feed
(list / unread / read / read-all) and every enabled channel whose minimum level
it meets: a webhook (fake receiver) and email over SMTP (a tiny fake SMTP
server with AUTH), with per-rule channel routing."""

import asyncio
import base64
import unittest

from kowan.events import Event
from kowan.notify import Message
from kowan.notify.smtp import SMTPProvider, _build_message, _split_recipients
from tests.apicase import ServerCase
from tests.fakeservices import FakeReceiver


class FakeSMTP:
    """Just enough ESMTP: EHLO (advertising AUTH PLAIN), AUTH, MAIL, RCPT, DATA."""

    def __init__(self, user="mailer", password="mail-pw"):
        self.user, self.password = user, password
        self.messages = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, reader, writer):
        def send(line):
            writer.write((line + "\r\n").encode())

        send("220 fake ESMTP")
        mail = {"rcpt": []}
        while True:
            line = (await reader.readline()).decode().rstrip("\r\n")
            if not line:
                break
            cmd = line.split(" ", 1)[0].upper()
            if cmd in ("EHLO", "HELO"):
                send("250-fake")
                send("250 AUTH PLAIN")
            elif cmd == "AUTH":
                creds = base64.b64decode(line.split(" ")[2]).split(b"\0")
                ok = creds[1:] == [self.user.encode(), self.password.encode()]
                send("235 ok" if ok else "535 bad credentials")
            elif cmd == "STARTTLS":
                send("454 TLS not available")
            elif cmd == "MAIL":
                mail["from"] = line
                send("250 ok")
            elif cmd == "RCPT":
                mail["rcpt"].append(line)
                send("250 ok")
            elif cmd == "DATA":
                send("354 go")
                body = []
                while (data := (await reader.readline()).decode()) != ".\r\n":
                    body.append(data)
                mail["body"] = "".join(body)
                self.messages.append(dict(mail))
                mail = {"rcpt": []}
                send("250 queued")
            elif cmd == "QUIT":
                send("221 bye")
                await writer.drain()
                break
            else:
                send("250 ok")
            await writer.drain()
        writer.close()


class TestSMTP(unittest.IsolatedAsyncioTestCase):
    async def test_send_with_auth_and_errors(self):
        smtp = FakeSMTP()
        port = await smtp.start()
        try:
            p = SMTPProvider("127.0.0.1", str(port), "mailer", "mail-pw", "kowan@example.com",
                             ["ops@example.com", "dev@example.com"])
            await p.send(Message(title="Disk\r\nBcc: x@evil", body="almost full", level="warning"))
            self.assertEqual(len(smtp.messages), 1)
            m = smtp.messages[0]
            self.assertEqual(len(m["rcpt"]), 2)
            self.assertIn("almost full", m["body"])
            self.assertNotIn("\nBcc: x@evil", m["body"])  # header injection neutralised
            bad = SMTPProvider("127.0.0.1", str(port), "mailer", "wrong", "k@example.com", ["o@example.com"])
            with self.assertRaises(Exception):
                await bad.send(Message(title="t", body="b"))
        finally:
            await smtp.stop()
        self.assertEqual(_split_recipients(" a@x.io; b@y.io ,, "), ["a@x.io", "b@y.io"])
        self.assertIn(b"Subject:", _build_message("f@x.io", ["t@x.io"], Message(title="hi", body="b")))


class TestAlertDelivery(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.receiver = FakeReceiver()
        self.url = await self.receiver.start()
        self.smtp = FakeSMTP()
        port = await self.smtp.start()
        _, self.hook = await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "name": "hook", "type": "webhook", "minLevel": "warning", "enabled": True,
            "config": {"url": self.url + "/alerts"}}, expect=201)
        _, self.mail = await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "name": "mail", "type": "smtp", "minLevel": "info", "enabled": True,
            "config": {"host": "127.0.0.1", "port": str(port), "from": "kowan@example.com",
                       "to": "ops@example.com", "username": "mailer", "password": "mail-pw"}}, expect=201)

    async def asyncTearDown(self):
        await self.receiver.stop()
        await self.smtp.stop()
        await super().asyncTearDown()

    async def alert(self, level, title, channel=""):
        self.svc.bus.publish(Event(type="alert", source="monitor", payload={
            "id": title, "level": level, "title": title, "message": title + " body", "hostId": "local",
            "channelId": channel}))
        await asyncio.sleep(0.3)

    async def test_levels_routing_and_feed(self):
        await self.alert("critical", "db down")
        self.assertEqual(len(self.receiver.received), 1)
        self.assertEqual(len(self.smtp.messages), 1)
        await self.alert("info", "fyi")  # below the webhook's minimum
        self.assertEqual(len(self.receiver.received), 1)
        self.assertEqual(len(self.smtp.messages), 2)
        await self.alert("critical", "only mail", channel=self.mail["id"])
        self.assertEqual(len(self.receiver.received), 1)
        self.assertEqual(len(self.smtp.messages), 3)

        _, feed = await self.call("GET", "/notifications", "viewer", params={"limit": "10"}, expect=200)
        self.assertEqual(len(feed), 3)
        _, count = await self.call("GET", "/notifications/unread-count", "viewer", expect=200)
        self.assertEqual(count["count"], 3)
        await self.call("POST", f"/notifications/{feed[0]['id']}/read", "viewer", expect=200)
        _, count = await self.call("GET", "/notifications/unread-count", "viewer", expect=200)
        self.assertEqual(count["count"], 2)
        await self.call("POST", "/notifications/read-all", "viewer", expect=200)
        _, count = await self.call("GET", "/notifications/unread-count", "viewer", expect=200)
        self.assertEqual(count["count"], 0)
        # A notice (not an alert) lands in the feed too.
        self.svc.bus.publish(Event(type="notice", source="updates", payload={"title": "updated", "body": "x"}))
        await asyncio.sleep(0.2)
        _, feed = await self.call("GET", "/notifications", "viewer", expect=200)
        self.assertIn("notice", [n["kind"] for n in feed])


if __name__ == "__main__":
    unittest.main()
