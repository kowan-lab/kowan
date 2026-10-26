"""kowan.secrets — encrypted-credential crypto + external secret providers.

Public surface:

  * Cipher / load_or_create_key            (cipher.py) — AES-256-GCM at rest.
  * Provider registry: register / is_external / build / fields / sensitive_keys /
    types / FieldSpec / ProviderType       (provider.py).
  * Compiled-in providers vault / infisical / phase (registered on import).
  * ExternalResolver — the adapter the store's ``set_external_secrets`` expects
    (``is_external(type)`` + synchronous ``resolve(type, data, field) -> str``),
    bridging the async provider ``resolve`` into the store's synchronous secret
    resolver.

Import side effect: importing this package registers the three external
providers.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Dict

from . import provider as _provider
from .cipher import (
    KEY_FILE_NAME,
    KEY_LEN,
    Cipher,
    CipherError,
    load_or_create_key,
)
from .infisical import InfisicalType, register_infisical
from .phase import PhaseType, register_phase
from .provider import (
    FieldSpec,
    Provider,
    ProviderType,
    build,
    fields,
    is_external,
    register,
    sensitive_keys,
    types,
)
from .vault import VaultType, register_vault

# Register the compiled-in providers exactly once at import.
register_vault()
register_infisical()
register_phase()


def _run_async(coro) -> str:
    """Drive an async provider ``resolve`` to completion from synchronous code.

    The store's secret resolver (``resolve_secrets`` / ``preview_references``) is
    synchronous but the providers are async (network I/O). If no event loop is
    running in this thread we simply ``asyncio.run``; if one IS running (we are
    inside an async web handler that called into the sync store), we run the
    coroutine on a fresh loop in a dedicated thread and block for its result. This
    keeps the store's synchronous contract without a nested-loop error.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread: safe to drive one directly.
        return asyncio.run(coro)

    result: Dict[str, object] = {}

    def worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            result["value"] = loop.run_until_complete(coro)
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller thread
            result["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result["value"]  # type: ignore[return-value]


class ExternalResolver:
    """Adapter installed on the store via ``Store.set_external_secrets``.

    Exposes the synchronous interface the store's resolver expects:
    ``is_external(secret_type) -> bool`` and
    ``resolve(secret_type, data, field) -> str``. ``resolve`` builds the provider
    (gated on the plugin being enabled) and drives its async ``resolve``.
    """

    def is_external(self, secret_type: str) -> bool:
        return _provider.is_external(secret_type)

    def resolve(self, secret_type: str, data: Dict[str, str], field: str) -> str:
        prov = _provider.build(secret_type, data)
        return _run_async(prov.resolve(field or ""))


__all__ = [
    "Cipher",
    "CipherError",
    "KEY_LEN",
    "KEY_FILE_NAME",
    "load_or_create_key",
    "Provider",
    "FieldSpec",
    "ProviderType",
    "register",
    "is_external",
    "build",
    "fields",
    "sensitive_keys",
    "types",
    "VaultType",
    "InfisicalType",
    "PhaseType",
    "ExternalResolver",
]
