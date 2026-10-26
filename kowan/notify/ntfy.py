"""ntfy topic notification provider.

The message Title is sent via the "Title" header and the Body as the request
payload; an optional access token is sent as a Bearer Authorization header.
"""

from __future__ import annotations

from typing import Dict

from .http import post
from .provider import FieldSpec, Message, Provider, register, require_field


def _ntfy_priority(level: str) -> str:
    """Map a notification level to an ntfy priority header value."""
    if level == "critical":
        return "urgent"
    if level == "warning":
        return "high"
    if level == "info":
        return "default"
    return ""


class NtfyProvider(Provider):
    """Publishes notifications to an ntfy topic."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token

    async def send(self, msg: Message) -> None:
        headers: Dict[str, str] = {}
        if msg.title:
            headers["Title"] = msg.title
        prio = _ntfy_priority(msg.level)
        if prio:
            headers["Priority"] = prio
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        await post(self._url, "text/plain", headers, msg.body.encode("utf-8"))


def _new_ntfy_provider(config: Dict[str, str]) -> Provider:
    topic = require_field(config, "topic")
    server = config.get("server", "") or "https://ntfy.sh"
    server = server.rstrip("/")
    return NtfyProvider(url=server + "/" + topic, token=config.get("token", ""))


register(
    "ntfy",
    _new_ntfy_provider,
    [
        FieldSpec(key="server", label="Server", placeholder="https://ntfy.sh"),
        FieldSpec(key="topic", label="Topic", required=True, placeholder="kowan-alerts"),
        FieldSpec(key="token", label="Token", sensitive=True),
    ],
)
