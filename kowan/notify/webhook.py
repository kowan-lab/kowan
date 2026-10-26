"""Generic webhook notification provider.

POSTs the notification as a JSON object ``{title, body, level}`` to a configured
URL with optional static headers.
"""

from __future__ import annotations

from typing import Dict, Optional

from .http import post_json
from .provider import FieldSpec, Message, Provider, register, require_field


def _parse_headers(s: str) -> Optional[Dict[str, str]]:
    """Parse a ``"K:V;K2:V2"`` string into a header map.

    Whitespace around keys/values is trimmed; malformed entries (no colon, empty
    key) are skipped. Returns ``None`` when the result is empty.
    """
    if not s:
        return None
    out: Dict[str, str] = {}
    for pair in s.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        k, sep, v = pair.partition(":")
        k = k.strip()
        if not sep or k == "":
            continue
        out[k] = v.strip()
    return out or None


class WebhookProvider(Provider):
    """POSTs a JSON ``{title, body, level}`` payload to a URL with static headers."""

    def __init__(self, url: str, headers: Optional[Dict[str, str]]) -> None:
        self._url = url
        self._headers = headers

    async def send(self, msg: Message) -> None:
        body = {"title": msg.title, "body": msg.body, "level": msg.level}
        await post_json(self._url, self._headers, body)


def _new_webhook_provider(config: Dict[str, str]) -> Provider:
    url = require_field(config, "url")
    return WebhookProvider(url=url, headers=_parse_headers(config.get("headers", "")))


register(
    "webhook",
    _new_webhook_provider,
    [
        FieldSpec(key="url", label="URL", required=True, placeholder="https://example.com/hook"),
        FieldSpec(
            key="headers",
            label="Headers",
            placeholder="Authorization:Bearer x;X-Env:prod",
        ),
    ],
)
