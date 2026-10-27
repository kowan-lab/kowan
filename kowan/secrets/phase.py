"""Phase (phase.dev) external secret provider.

Phase exposes a public REST API that
returns secret values in plaintext to a holder of a valid service/user token, so
resolution is a single authenticated GET — no client-side decryption. Bounded by
an 8s timeout and a 1 MiB body cap via :mod:`kowan.httpcap`.
"""

from __future__ import annotations

from typing import Dict

from kowan import httpcap

from .provider import FieldSpec, Provider, register

# phase_timeout bounds the single live HTTP read of one resolution (seconds).
PHASE_TIMEOUT = 8.0

# Cap on the answer's body.
_MAX_BODY = 1 << 20

# PhaseType is the registered provider/secret type for Phase.
PhaseType = "phase"

# Phase Cloud API base, used when a secret omits an explicit address.
PHASE_DEFAULT_ADDRESS = "https://api.phase.dev"


class _PhaseProvider:
    """Holds the connection + reference for one Phase secret."""

    def __init__(
        self,
        address: str,
        token: str,
        app_id: str,
        environment: str,
        path: str,
        key: str,
    ) -> None:
        self._address = address
        self._token = token
        self._app_id = app_id
        self._environment = environment
        self._path = path
        self._key = key

    async def resolve(self, field_override: str) -> str:
        """Read a single secret value from the Phase REST API. A non-empty
        ``field_override`` (from ``[[NAME.FIELD]]``) selects a different secret
        name."""
        if not self._token:
            raise ValueError("phase: token is required")
        if not self._app_id or not self._environment:
            raise ValueError("phase: appId and environment are required")
        name = field_override.strip() or self._key.strip()
        if not name:
            raise ValueError("phase: secret name (key) is required")

        endpoint = self._address + "/v1/secrets/"
        params = {
            "app_id": self._app_id,
            "env": self._environment,
            "path": self._path,
            "key": name,
        }
        try:
            resp = await httpcap.request(
                "GET", endpoint, limit=_MAX_BODY, timeout=PHASE_TIMEOUT,
                params=params,
                headers={"Authorization": "Bearer " + self._token, "Accept": "application/json"},
            )
        except httpcap.HTTPError as e:
            raise ValueError(f"phase: read {endpoint}: {e}") from e

        if resp.status_code != 200:
            raise ValueError(
                f"phase: read {name!r}: unexpected status {resp.status_code}: {resp.text.strip()}"
            )

        try:
            secrets_list = resp.json()
        except ValueError as e:
            raise ValueError(f"phase: decode read response: {e}") from e

        # The list endpoint returns an array of secret objects; even with the key
        # filter we match by key explicitly so a path/tag collision can't hand back
        # the wrong value.
        if isinstance(secrets_list, list):
            for s in secrets_list:
                if isinstance(s, dict) and s.get("key") == name:
                    return s.get("value", "") or ""
        raise ValueError(
            f"phase: secret {name!r} not found in app {self._app_id!r} "
            f"env {self._environment!r} path {self._path!r}"
        )


def _new_phase_provider(config: Dict[str, str]) -> Provider:
    address = config.get("address", "").strip().rstrip("/") or PHASE_DEFAULT_ADDRESS
    path = config.get("path", "").strip() or "/"
    return _PhaseProvider(
        address=address,
        token=config.get("token", ""),
        app_id=config.get("appId", ""),
        environment=config.get("environment", ""),
        path=path,
        key=config.get("key", ""),
    )


def register_phase() -> None:
    """Register Phase as an external secret provider. Idempotent."""
    register(
        PhaseType,
        _new_phase_provider,
        [
            FieldSpec(key="address", label="API address", placeholder=PHASE_DEFAULT_ADDRESS),
            FieldSpec(key="token", label="Token", sensitive=True, required=True,
                      placeholder="pss_service:v2:…"),
            FieldSpec(key="appId", label="App ID", required=True, placeholder="phase application id"),
            FieldSpec(key="environment", label="Environment", required=True, placeholder="production"),
            FieldSpec(key="path", label="Secret path", placeholder="/"),
            FieldSpec(key="key", label="Secret name", required=True, placeholder="DATABASE_URL"),
        ],
    )


__all__ = ["PhaseType", "register_phase"]
