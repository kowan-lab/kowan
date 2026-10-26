"""Tests for the RSS/Atom poll trigger, the webhook trigger's new method field,
and the webhook method-matching predicate.

Fast stdlib unittest, NO network: the poller's HTTP fetch is stubbed by
subclassing so ``evaluate`` runs against in-memory feed bytes.

Covered surfaces:

  * ``_newest_id``           — newest-entry-id extraction for RSS (guid / link
                               fallback) and Atom (id / link href fallback);
  * seeding + fire           — an empty state seeds (fired False, state recorded);
                               a subsequent poll fires only when the newest id
                               changes;
  * webhook trigger schema   — the webhook type now advertises a "method" field
                               with the expected options;
  * ``webhook_method_matches`` — the empty=POST / "any" / case-insensitive rules.
"""

import asyncio
import unittest

from kowan import triggers
from kowan.triggers import rss
from kowan.triggers.builtin import (
    WEBHOOK_METHOD_OPTIONS,
    webhook_method_matches,
)


# --- sample feeds ----------------------------------------------------------

_RSS_WITH_GUID = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <title>Newest post</title>
      <link>https://example.com/posts/3</link>
      <guid>guid-3</guid>
    </item>
    <item>
      <title>Older post</title>
      <link>https://example.com/posts/2</link>
      <guid>guid-2</guid>
    </item>
  </channel>
</rss>
"""

_RSS_NO_GUID = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example</title>
    <item>
      <title>Newest post</title>
      <link>https://example.com/posts/9</link>
    </item>
  </channel>
</rss>
"""

_ATOM_WITH_ID = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>Newest</title>
    <id>urn:uuid:entry-newest</id>
    <link href="https://example.com/a/newest"/>
  </entry>
  <entry>
    <title>Older</title>
    <id>urn:uuid:entry-older</id>
    <link href="https://example.com/a/older"/>
  </entry>
</feed>
"""

_ATOM_NO_ID = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>Newest</title>
    <link rel="alternate" href="https://example.com/a/link-newest"/>
  </entry>
</feed>
"""

_EMPTY_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Empty</title></channel></rss>
"""


class _StubbedRSSPoller(rss._RSSPoller):
    """An rss poller whose network fetch is replaced by fixed in-memory bytes,
    so ``evaluate`` runs with no real HTTP. Successive polls can be simulated by
    reassigning ``self.body``."""

    def __init__(self, body: bytes) -> None:
        super().__init__(url="https://example.com/feed.xml")
        self.body = body

    async def _fetch(self) -> bytes:  # type: ignore[override]
        return self.body


def _run(coro):
    return asyncio.run(coro)


class TestNewestIDExtraction(unittest.TestCase):
    """`_newest_id` picks the first (newest) entry and the right id source."""

    def test_rss_prefers_guid(self):
        self.assertEqual(rss._newest_id(_RSS_WITH_GUID), "guid-3")

    def test_rss_falls_back_to_link(self):
        self.assertEqual(rss._newest_id(_RSS_NO_GUID), "https://example.com/posts/9")

    def test_atom_prefers_id(self):
        self.assertEqual(rss._newest_id(_ATOM_WITH_ID), "urn:uuid:entry-newest")

    def test_atom_falls_back_to_link_href(self):
        self.assertEqual(
            rss._newest_id(_ATOM_NO_ID), "https://example.com/a/link-newest"
        )

    def test_empty_feed_returns_blank(self):
        self.assertEqual(rss._newest_id(_EMPTY_RSS), "")

    def test_malformed_xml_raises(self):
        with self.assertRaises(ValueError):
            rss._newest_id(b"<not-xml")


class TestSeedingAndFire(unittest.TestCase):
    """The empty-state seeding rule and the subsequent change-fires rule."""

    def test_empty_state_seeds_without_firing(self):
        poller = _StubbedRSSPoller(_RSS_WITH_GUID)
        fired, new_state = _run(poller.evaluate({}, ""))
        self.assertFalse(fired)
        self.assertEqual(new_state, "guid-3")

    def test_unchanged_newest_does_not_fire(self):
        poller = _StubbedRSSPoller(_RSS_WITH_GUID)
        fired, new_state = _run(poller.evaluate({}, "guid-3"))
        self.assertFalse(fired)
        self.assertEqual(new_state, "guid-3")

    def test_new_newest_id_fires(self):
        # First poll seeds against the two-item feed.
        poller = _StubbedRSSPoller(_RSS_WITH_GUID)
        fired, state = _run(poller.evaluate({}, ""))
        self.assertFalse(fired)
        self.assertEqual(state, "guid-3")

        # A newer item appears at the top: same poller, different body.
        poller.body = _RSS_WITH_GUID.replace(b"guid-3", b"guid-4").replace(
            b"posts/3", b"posts/4"
        )
        fired, new_state = _run(poller.evaluate({}, state))
        self.assertTrue(fired)
        self.assertEqual(new_state, "guid-4")

    def test_atom_seed_then_fire(self):
        poller = _StubbedRSSPoller(_ATOM_WITH_ID)
        fired, state = _run(poller.evaluate({}, ""))
        self.assertFalse(fired)
        self.assertEqual(state, "urn:uuid:entry-newest")

        poller.body = _ATOM_WITH_ID.replace(b"entry-newest", b"entry-brandnew")
        fired, new_state = _run(poller.evaluate({}, state))
        self.assertTrue(fired)
        self.assertEqual(new_state, "urn:uuid:entry-brandnew")

    def test_empty_feed_evaluate_raises(self):
        poller = _StubbedRSSPoller(_EMPTY_RSS)
        with self.assertRaises(ValueError):
            _run(poller.evaluate({}, ""))


class TestRegistration(unittest.TestCase):
    """The rss type is registered as a poll trigger with a required url field."""

    def test_rss_registered_as_poll(self):
        self.assertTrue(triggers.is_registered(triggers.TypeRSS))
        self.assertEqual(triggers.kind(triggers.TypeRSS), triggers.KindPoll)

    def test_rss_url_field(self):
        flds = triggers.fields(triggers.TypeRSS)
        keys = {f.key for f in flds}
        self.assertIn("url", keys)
        url_field = next(f for f in flds if f.key == "url")
        self.assertTrue(url_field.required)

    def test_build_requires_url(self):
        with self.assertRaises(ValueError):
            triggers.build(triggers.TypeRSS, {})
        # A url builds fine.
        poller = triggers.build(triggers.TypeRSS, {"url": "https://x/feed"})
        self.assertIsInstance(poller, rss._RSSPoller)


class TestWebhookMethodField(unittest.TestCase):
    """The webhook trigger now advertises a `method` field with a fixed option
    list that serializes via FieldSpec.to_dict."""

    def test_webhook_has_method_field(self):
        flds = triggers.fields(triggers.TypeWebhook)
        keys = {f.key for f in flds}
        self.assertIn("method", keys)

    def test_method_field_options(self):
        method_field = next(
            f for f in triggers.fields(triggers.TypeWebhook) if f.key == "method"
        )
        self.assertEqual(
            method_field.options, ["any", "GET", "POST", "PUT", "PATCH", "DELETE"]
        )
        self.assertEqual(WEBHOOK_METHOD_OPTIONS, method_field.options)

    def test_options_serialized_in_to_dict(self):
        method_field = next(
            f for f in triggers.fields(triggers.TypeWebhook) if f.key == "method"
        )
        d = method_field.to_dict()
        self.assertEqual(
            d["options"], ["any", "GET", "POST", "PUT", "PATCH", "DELETE"]
        )

    def test_options_absent_when_unset(self):
        # A plain field (no options) must not emit an "options" key, so existing
        # fields serialize byte-for-byte as before.
        url_field = next(
            f for f in triggers.fields(triggers.TypeRSS) if f.key == "url"
        )
        self.assertNotIn("options", url_field.to_dict())


class TestWebhookMethodMatches(unittest.TestCase):
    """The empty=POST / any / case-insensitive predicate."""

    def test_empty_configured_is_post(self):
        self.assertTrue(webhook_method_matches("", "POST"))
        self.assertTrue(webhook_method_matches("", "post"))
        self.assertFalse(webhook_method_matches("", "GET"))
        self.assertFalse(webhook_method_matches("   ", "GET"))

    def test_any_matches_everything(self):
        for m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            self.assertTrue(webhook_method_matches("any", m))
        self.assertTrue(webhook_method_matches("ANY", "delete"))

    def test_exact_case_insensitive(self):
        self.assertTrue(webhook_method_matches("GET", "get"))
        self.assertTrue(webhook_method_matches("get", "GET"))
        self.assertTrue(webhook_method_matches("Delete", "DELETE"))
        self.assertFalse(webhook_method_matches("GET", "POST"))


if __name__ == "__main__":
    unittest.main()
