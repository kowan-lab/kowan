"""Persisted kowanctl configuration.

Config lives at ``~/.kowan/config.yaml`` and carries the server URL, a bearer
token, and an optional admin-managed API key ("pk_..."). Configuration sources
are merged with this precedence:

    command-line flag > ~/.kowan/config.yaml > environment variable
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Config:
    """The persisted kowanctl configuration (~/.kowan/config.yaml)."""

    server: str = ""
    token: str = ""
    # api_key is an admin-managed API key secret ("pk_..."). When set it
    # authenticates requests in place of (or alongside) the bearer token.
    api_key: str = ""


def config_path() -> Path:
    """Return the path to the kowanctl config file (~/.kowan/config.yaml)."""
    return Path.home() / ".kowan" / "config.yaml"


def load_config() -> Config:
    """Read ~/.kowan/config.yaml.

    A missing file is not an error: it yields an empty Config.
    """
    path = config_path()
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    except OSError as e:  # pragma: no cover
        raise RuntimeError(f"read config {path}: {e}") from e

    try:
        raw = yaml.safe_load(data) or {}
    except yaml.YAMLError as e:
        raise RuntimeError(f"parse config {path}: {e}") from e
    if not isinstance(raw, dict):
        raw = {}

    return Config(
        server=str(raw.get("server", "") or ""),
        token=str(raw.get("token", "") or ""),
        api_key=str(raw.get("apiKey", "") or ""),
    )


def save_config(cfg: Config) -> None:
    """Write the configuration to ~/.kowan/config.yaml.

    Creates the ~/.kowan directory (mode 0700) if needed and writes the file
    with 0600 permissions, because it contains a bearer token. The ``apiKey``
    key is omitted when empty.
    """
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    doc: dict[str, str] = {"server": cfg.server, "token": cfg.token}
    if cfg.api_key:
        doc["apiKey"] = cfg.api_key

    data = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    # Write atomically-ish with restrictive perms (0600).
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
    except OSError as e:  # pragma: no cover
        raise RuntimeError(f"write config {path}: {e}") from e
    # Ensure perms even if the file pre-existed with a looser mode.
    os.chmod(path, 0o600)


def first_non_empty(*vals: str) -> str:
    """Return the first argument that is not empty after trimming."""
    for v in vals:
        if v is not None and v.strip() != "":
            return v.strip()
    return ""


@dataclass
class Resolved:
    """Effective server/token/api-key after merging all configuration sources."""

    server: str = ""
    token: str = ""
    api_key: str = ""


def resolve_config(
    flag_server: str = "",
    flag_token: str = "",
    flag_api_key: str = "",
) -> Resolved:
    """Merge flags, config file, and environment by precedence.

    Precedence per field: flag > config file > environment variable
    (KOWAN_SERVER / KOWAN_TOKEN / KOWAN_API_KEY).
    """
    cfg = load_config()
    return Resolved(
        server=first_non_empty(flag_server, cfg.server, os.getenv("KOWAN_SERVER", "")),
        token=first_non_empty(flag_token, cfg.token, os.getenv("KOWAN_TOKEN", "")),
        api_key=first_non_empty(
            flag_api_key, cfg.api_key, os.getenv("KOWAN_API_KEY", "")
        ),
    )
