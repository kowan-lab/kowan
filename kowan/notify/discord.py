"""Discord webhook notification provider."""

from __future__ import annotations

from typing import Dict

from .http import post_json
from .provider import (
    FieldSpec,
    Message,
    Provider,
    join_title_body,
    register,
    require_field,
)


class DiscordProvider(Provider):
    """Posts notifications to a Discord webhook."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def send(self, msg: Message) -> None:
        await post_json(self._url, None, {"content": join_title_body(msg)})


def _new_discord_provider(config: Dict[str, str]) -> Provider:
    return DiscordProvider(require_field(config, "url"))


register(
    "discord",
    _new_discord_provider,
    [
        FieldSpec(
            key="url",
            label="Webhook URL",
            sensitive=True,
            required=True,
            placeholder="https://discord.com/api/webhooks/...",
        )
    ],
)
