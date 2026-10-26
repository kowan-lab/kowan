"""Telegram Bot API notification provider."""

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


class TelegramProvider(Provider):
    """Sends notifications to a Telegram chat via the Bot API."""

    def __init__(self, url: str, chat_id: str) -> None:
        self._url = url
        self._chat_id = chat_id

    async def send(self, msg: Message) -> None:
        body = {"chat_id": self._chat_id, "text": join_title_body(msg)}
        await post_json(self._url, None, body)


def _new_telegram_provider(config: Dict[str, str]) -> Provider:
    token = require_field(config, "botToken")
    chat_id = require_field(config, "chatId")
    return TelegramProvider(
        url="https://api.telegram.org/bot" + token + "/sendMessage",
        chat_id=chat_id,
    )


register(
    "telegram",
    _new_telegram_provider,
    [
        FieldSpec(
            key="botToken",
            label="Bot Token",
            sensitive=True,
            required=True,
            placeholder="123456:ABC-DEF...",
        ),
        FieldSpec(
            key="chatId",
            label="Chat ID",
            required=True,
            placeholder="-1001234567890",
        ),
    ],
)
