"""One-time setup token for the first-run ``POST /auth/init``.

Without it, whoever reaches a fresh server first becomes its admin, which is root
on the Docker host. The token proves the person creating the admin can read the
server's logs or data directory, like Jupyter's first-run token.

Resolution: ``KOWAN_SETUP_TOKEN`` (for automated provisioning) wins; otherwise
``{data_dir}/setup-token`` is reused, or generated (0600) so restarts keep it
stable. The file is deleted once the first admin exists.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

log = logging.getLogger("kowan.auth.setup")

SETUP_TOKEN_FILE_NAME = "setup-token"


def _path(data_dir: str) -> Path:
    return Path(data_dir) / SETUP_TOKEN_FILE_NAME


def ensure_setup_token(data_dir: str, initialized: bool, env_token: str = "") -> Optional[str]:
    """Return the token that unlocks first-run setup, or None once initialized."""
    if initialized:
        discard_setup_token(data_dir)
        return None
    if env_token:
        return env_token
    path = _path(data_dir)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return token


def discard_setup_token(data_dir: str) -> None:
    try:
        _path(data_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError as err:
        log.warning("could not remove %s: %s", _path(data_dir), err)


def setup_token_matches(expected: Optional[str], given: object) -> bool:
    if not expected or not isinstance(given, str) or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), given.strip().encode("utf-8"))
