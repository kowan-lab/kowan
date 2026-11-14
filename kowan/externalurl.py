"""The address people and agents reach this server at (Settings → General).

Used for the SSO redirect URI, the agent join commands, the MCP origin check,
secure cookies and HSTS. It lives in the ``settings`` table and is edited from
the UI; ``svc.config.external_url`` holds the current value so readers keep a
plain attribute (no query per request), and saving updates both.

``KOWAN_EXTERNAL_URL`` is only the first value: read once, while nothing is
saved yet, so an existing install keeps working after the upgrade. From then on
the setting wins and the variable can be dropped.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger("kowan.externalurl")

SETTING = "external_url"

# A saved "" means "cleared on purpose": the env seed must not come back.
_CLEARED = "-"


def normalize(raw: str) -> Tuple[str, Optional[str]]:
    """``(value, error)``: an http(s) origin, optionally with a path prefix,
    without the trailing slash. ``""`` is valid and means "not set"."""
    text = (raw or "").strip().rstrip("/")
    if text == "":
        return "", None
    try:
        u = urlsplit(text)
    except ValueError:
        return "", "not a valid URL"
    if u.scheme not in ("http", "https"):
        return "", "must start with http:// or https://"
    if not u.hostname:
        return "", "is missing the host name"
    if u.query or u.fragment:
        return "", "must not carry a query or a #fragment"
    if u.username or u.password:
        return "", "must not carry a user name or password"
    try:
        u.port  # noqa: B018 - raises on a port out of range
    except ValueError:
        return "", "has an invalid port"
    return text, None


def load(store: Any, env_value: str) -> str:
    """The value to run with: the saved one, else the env seed (saved then)."""
    saved = store.get_setting(SETTING)
    if saved == _CLEARED:
        return ""
    if saved:
        return saved
    seed, err = normalize(env_value)
    if err:
        log.error("KOWAN_EXTERNAL_URL %s: ignored", err)
        return ""
    if seed:
        store.set_setting(SETTING, seed)
        log.info("external URL taken from KOWAN_EXTERNAL_URL and saved in the settings; "
                 "it is now changed from Settings → General")
    return seed


def save(svc: Any, value: str) -> None:
    """Persist an already-normalized value and apply it at once."""
    svc.store.set_setting(SETTING, value or _CLEARED)
    svc.config.external_url = value
