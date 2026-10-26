"""Slack incoming-webhook notification provider."""

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


class SlackProvider(Provider):
    """Posts notifications to a Slack incoming webhook."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def send(self, msg: Message) -> None:
        await post_json(self._url, None, {"text": join_title_body(msg)})


def _new_slack_provider(config: Dict[str, str]) -> Provider:
    return SlackProvider(require_field(config, "url"))


register(
    "slack",
    _new_slack_provider,
    [
        FieldSpec(
            key="url",
            label="Webhook URL",
            sensitive=True,
            required=True,
            placeholder="https://hooks.slack.com/services/...",
        )
    ],
)
