"""RSS / Atom feed polling trigger.

Mirrors the structure of ``gitcommit.py`` / ``dockerimage.py``: a :class:`Poller`
that fetches a feed URL with :mod:`kowan.httpcap` and parses it with the stdlib
``xml.etree.ElementTree`` (no third-party feed library). It fires whenever the
newest entry id changes.

Newest-id resolution (feeds list newest-first, so the first entry wins):

  - RSS: ``<item>`` -> ``<guid>`` if present, else ``<link>``.
  - Atom: ``<entry>`` -> ``<id>`` if present, else ``<link href=...>``.

Seeding contract (shared by every poll trigger): when ``state`` is empty, record
the current newest id and return ``fired=False`` so attaching the trigger never
fires for feed items that predate it. Afterwards it fires only when the newest id
differs from the stored one. Fetch/parse failures raise ``ValueError`` (surfaced
on the trigger as ``lastError``), leaving stored state untouched.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, Optional, Tuple

from kowan import httpcap

from . import FieldSpec, KindPoll, Poller, TypeRSS, register

# Bounds the single feed fetch. The poller already wraps evaluate in a 25s
# budget; this is a tighter per-call cap, matching the other pollers.
_RSS_TIMEOUT = 20.0

# Caps the feed body we read. A few MiB is generous for a feed while bounding
# memory against a hostile/huge response.
_RSS_MAX_BODY = 4 << 20

# The Atom namespace. RSS 2.0 has no namespace on its elements; Atom lives under
# this one, so we match tags namespace-agnostically (see _local).
_ATOM_NS = "http://www.w3.org/2005/Atom"


class _RSSPoller(Poller):
    """Parsed config for one rss trigger: just the feed URL."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def evaluate(self, config: Dict[str, str], state: str) -> Tuple[bool, str]:
        """Fetch + parse the feed and report whether the newest entry changed.

        Seeding (state == "") records the current newest id without firing;
        afterwards it fires only when the newest id differs from ``state``.
        """
        body = await self._fetch()
        newest = _newest_id(body)
        if newest == "":
            raise ValueError(f"rss: no entries found in feed {self.url}")
        if state == "":
            # Seed: record the current newest id without firing for pre-existing
            # items.
            return False, newest
        return newest != state, newest

    async def _fetch(self) -> bytes:
        """GET the feed URL and return the (capped) raw body bytes. Raises
        ``ValueError`` on a transport error or a non-2xx status."""
        try:
            resp = await httpcap.request(
                "GET", self.url, limit=_RSS_MAX_BODY, timeout=_RSS_TIMEOUT, follow_redirects=True,
                headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
            )
        except httpcap.HTTPError as err:
            raise ValueError(f"rss: fetch {self.url}: {err}") from err
        body = resp.content
        if resp.status_code < 200 or resp.status_code >= 300:
            snippet = body.decode("utf-8", "replace").strip()
            raise ValueError(
                f"rss: {self.url}: unexpected status {resp.status_code}: {snippet}"
            )
        return body


def _local(tag: str) -> str:
    """Strip an ``{namespace}`` prefix from an ElementTree tag so RSS (no
    namespace) and Atom (namespaced) elements can be matched by local name."""
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _find_child(elem: ET.Element, name: str) -> Optional[ET.Element]:
    """First direct child of ``elem`` whose local tag name equals ``name``."""
    for child in elem:
        if _local(child.tag) == name:
            return child
    return None


def _newest_id(data: bytes) -> str:
    """Parse RSS or Atom bytes and return the newest entry's id.

    RSS item id = ``<guid>`` text, else ``<link>`` text. Atom entry id =
    ``<id>`` text, else the ``href`` of a ``<link>`` (preferring rel="alternate"
    / no rel). Returns "" when the feed carries no entries. Raises ``ValueError``
    on malformed XML.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as err:
        raise ValueError(f"rss: parse feed: {err}") from err

    root_name = _local(root.tag)

    # Atom: <feed><entry>...  (the root itself is <feed>).
    if root_name == "feed":
        entry = _find_child(root, "entry")
        if entry is None:
            return ""
        return _atom_entry_id(entry)

    # RSS 2.0: <rss><channel><item>...  RSS 1.0/RDF: <rdf:RDF>...<item>.
    channel = _find_child(root, "channel")
    container = channel if channel is not None else root
    item = _find_child(container, "item")
    if item is None:
        # RSS 1.0 lists <item> as a sibling of <channel> under the RDF root.
        item = _find_child(root, "item")
    if item is None:
        return ""
    return _rss_item_id(item)


def _rss_item_id(item: ET.Element) -> str:
    """Newest-id value for one RSS ``<item>``: guid text, else link text."""
    guid = _find_child(item, "guid")
    if guid is not None and (guid.text or "").strip():
        return guid.text.strip()
    link = _find_child(item, "link")
    if link is not None and (link.text or "").strip():
        return link.text.strip()
    return ""


def _atom_entry_id(entry: ET.Element) -> str:
    """Newest-id value for one Atom ``<entry>``: id text, else a link href."""
    ident = _find_child(entry, "id")
    if ident is not None and (ident.text or "").strip():
        return ident.text.strip()
    # Atom links carry the URL in the href attribute; prefer an alternate/no-rel
    # link, otherwise fall back to the first link with an href.
    fallback = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if not href:
            continue
        rel = (child.get("rel") or "").strip()
        if rel in ("", "alternate"):
            return href
        if fallback == "":
            fallback = href
    return fallback


def _new_rss(config: Dict[str, str]) -> Poller:
    url = (config.get("url") or "").strip()
    if not url:
        raise ValueError("rss: url is required")
    return _RSSPoller(url=url)


register(
    TypeRSS,
    KindPoll,
    "RSS feed",
    "Fires when a new entry appears in an RSS or Atom feed, polled over HTTP (no third-party feed library).",
    _new_rss,
    [
        FieldSpec(key="url", label="Feed URL", required=True, placeholder="https://example.com/feed.xml"),
    ],
)
