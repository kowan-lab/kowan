"""HashiCorp Vault external secret provider.

Reads a single value from a Vault KV v2
engine via the raw HTTP API (no vault client library), using :mod:`kowan.httpcap`
with an 8s timeout (and a 1 MiB body cap) so a slow/unreachable Vault cannot
stall a deploy.

Register via :func:`register_vault` (called from the package ``__init__``). The
field keys match the ``models.VaultField*`` names so a vault secret's stored data
maps straight onto :class:`VaultConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from kowan import httpcap

from .provider import FieldSpec, Provider, register

# vault_timeout bounds a single live fetch from Vault (seconds).
VAULT_TIMEOUT = 8.0

# Cap on the answer's body.
_MAX_BODY = 1 << 20

# VaultType is the registered provider/secret type for HashiCorp Vault.
VaultType = "vault"


@dataclass
class VaultConfig:
    """Connection + reference describing where one secret value lives in a Vault
    KV v2 engine. For a vault-backed secret these fields are exactly what is
    stored (encrypted) in the secret's data map."""

    address: str = ""  # Vault base URL, e.g. "https://vault.example.com:8200"
    token: str = ""  # Vault token, presented via X-Vault-Token
    mount: str = ""  # KV v2 mount point (default "secret")
    path: str = ""  # secret path within the mount, e.g. "app/db"
    key: str = ""  # field within the secret map to return


class _VaultProvider:
    """Adapts a live Vault fetch to the :class:`Provider` interface."""

    def __init__(self, cfg: VaultConfig) -> None:
        self._cfg = cfg

    async def resolve(self, field_override: str) -> str:
        cfg = VaultConfig(**vars(self._cfg))
        if field_override:
            cfg.key = field_override
        return await fetch_vault(cfg)


def _new_vault_provider(config: Dict[str, str]) -> Provider:
    return _VaultProvider(
        VaultConfig(
            address=config.get("address", ""),
            token=config.get("token", ""),
            mount=config.get("mount", ""),
            path=config.get("path", ""),
            key=config.get("key", ""),
        )
    )


async def fetch_vault(cfg: VaultConfig) -> str:
    """Read a single value from a Vault KV v2 engine.

    Issues a GET to ``{address}/v1/{mount}/data/{path}`` with the ``X-Vault-Token``
    header and returns ``response.data.data[key]``. Returns a clear error on a
    non-200 response, a missing key, or a non-scalar value.
    """
    address = cfg.address.strip().rstrip("/")
    if not address:
        raise ValueError("vault: address is required")
    if not cfg.token:
        raise ValueError("vault: token is required")
    path = cfg.path.strip().strip("/")
    if not path:
        raise ValueError("vault: path is required")
    key = cfg.key.strip()
    if not key:
        raise ValueError("vault: key is required")
    mount = cfg.mount.strip().strip("/") or "secret"

    url = f"{address}/v1/{mount}/data/{path}"

    try:
        resp = await httpcap.request(
            "GET", url, limit=_MAX_BODY, timeout=VAULT_TIMEOUT,
            headers={"X-Vault-Token": cfg.token, "Accept": "application/json"},
        )
    except httpcap.HTTPError as e:
        raise ValueError(f"vault: request {url}: {e}") from e

    body = resp.text
    if resp.status_code != 200:
        raise ValueError(
            f"vault: GET {url}: unexpected status {resp.status_code}: {body.strip()}"
        )

    try:
        parsed = resp.json()
    except ValueError as e:
        raise ValueError(f"vault: decode response from {url}: {e}") from e

    data = ((parsed or {}).get("data") or {}).get("data") or {}
    if key not in data:
        raise ValueError(f"vault: key {key!r} not found at {mount}/{path}")

    raw = data[key]
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        # Render booleans as lowercase true/false.
        return "true" if raw else "false"
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        # Render without a trailing ".0" for integral values (e.g. a port -> "5432").
        return str(int(raw)) if raw.is_integer() else repr(raw)
    raise ValueError(
        f"vault: key {key!r} at {mount}/{path} has non-scalar value of type "
        f"{type(raw).__name__}"
    )


def register_vault() -> None:
    """Register Vault as an external secret provider. Idempotent."""
    register(
        VaultType,
        _new_vault_provider,
        [
            FieldSpec(key="address", label="Vault address", required=True,
                      placeholder="https://vault.example.com:8200"),
            FieldSpec(key="token", label="Vault token", sensitive=True, required=True,
                      placeholder="s.xxxxxxxx"),
            FieldSpec(key="mount", label="KV v2 mount", placeholder="secret"),
            FieldSpec(key="path", label="Secret path", required=True, placeholder="app/db"),
            FieldSpec(key="key", label="Key", required=True, placeholder="password"),
        ],
    )


__all__ = ["VaultType", "VaultConfig", "fetch_vault", "register_vault"]
