"""SMTP email notification provider.

Delivery uses stdlib :mod:`smtplib`, which is blocking, so :meth:`SMTPProvider.send`
runs it on a worker thread via :func:`asyncio.to_thread` to avoid stalling the
event loop.

Security: every value interpolated into a mail header (subject/from/to/level) has
CR/LF stripped to prevent header injection — e.g. an alert rule name containing
``"\\r\\n"`` smuggling extra headers or a second body (see :func:`_header_safe`).
"""

from __future__ import annotations

import asyncio
import smtplib
from typing import Dict, List

from .provider import FieldSpec, Message, Provider, register, require_field

# Separators recognised when splitting the recipient list.
_RECIPIENT_SEPS = (",", ";", " ", "\t", "\n")


def _header_safe(v: str) -> str:
    """Strip CR/LF from a value bound for an email header.

    Prevents header injection: newlines would otherwise let operator-settable
    text (subject/from/to) smuggle extra headers or a second body.
    """
    return v.replace("\r", " ").replace("\n", " ")


def _split_recipients(to: str) -> List[str]:
    """Parse a comma/space/semicolon separated address list."""
    out: List[str] = []
    token = ""
    for ch in to:
        if ch in _RECIPIENT_SEPS:
            if token:
                out.append(token)
            token = ""
        else:
            token += ch
    if token:
        out.append(token)
    return [t.strip() for t in out if t.strip()]


def _build_message(from_addr: str, to: List[str], msg: Message) -> bytes:
    """Compose an RFC 5322 message body for a notification.

    All header values are CR/LF-sanitised.
    """
    level = msg.level or "info"
    subject = _header_safe(f"[kowan][{level.upper()}] {msg.title}")
    recipients = [_header_safe(addr) for addr in to]

    lines = [
        f"From: {_header_safe(from_addr)}\r\n",
        f"To: {', '.join(recipients)}\r\n",
        f"Subject: {subject}\r\n",
        'MIME-Version: 1.0\r\n',
        'Content-Type: text/plain; charset="utf-8"\r\n',
        "\r\n",
        f"Level: {_header_safe(level)}\r\n",
        "\r\n",
        msg.body,
        "\r\n",
    ]
    return "".join(lines).encode("utf-8")


class SMTPProvider(Provider):
    """Sends notifications via email over SMTP using :mod:`smtplib`."""

    def __init__(
        self,
        host: str,
        port: str,
        username: str,
        password: str,
        from_addr: str,
        to: List[str],
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from = from_addr
        self._to = to

    def _send_sync(self, body: bytes) -> None:
        """Blocking SMTP delivery, run on a worker thread from :meth:`send`."""
        with smtplib.SMTP(self._host, int(self._port)) as client:
            # Opportunistic STARTTLS + auth when credentials are configured,
            # matching net/smtp.SendMail's PlainAuth-when-username path.
            if self._username:
                try:
                    client.starttls()
                except smtplib.SMTPException:
                    pass
                client.login(self._username, self._password)
            client.sendmail(self._from, self._to, body)

    async def send(self, msg: Message) -> None:
        body = _build_message(self._from, self._to, msg)
        try:
            await asyncio.to_thread(self._send_sync, body)
        except (smtplib.SMTPException, OSError) as err:
            from kowan.errors import BadGateway

            raise BadGateway(f"send mail: {err}") from err


def _new_smtp_provider(config: Dict[str, str]) -> Provider:
    host = require_field(config, "host")
    from_addr = require_field(config, "from")
    to = _split_recipients(config.get("to", ""))
    if not to:
        from kowan.errors import Validation

        raise Validation("missing required field 'to'")
    port = config.get("port", "") or "25"
    return SMTPProvider(
        host=host,
        port=port,
        username=config.get("username", ""),
        password=config.get("password", ""),
        from_addr=from_addr,
        to=to,
    )


register(
    "smtp",
    _new_smtp_provider,
    [
        FieldSpec(key="host", label="Host", required=True, placeholder="smtp.example.com"),
        FieldSpec(key="port", label="Port", placeholder="587"),
        FieldSpec(key="username", label="Username", placeholder="user@example.com"),
        FieldSpec(key="password", label="Password", sensitive=True),
        FieldSpec(key="from", label="From", required=True, placeholder="alerts@example.com"),
        FieldSpec(
            key="to",
            label="To",
            required=True,
            placeholder="ops@example.com, oncall@example.com",
        ),
    ],
)
