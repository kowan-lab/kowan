"""Unified credential CRUD + secret interpolation.

Provides :class:`_CredentialMixin`, folded into :class:`kowan.store.store.Store`.
It replaces the three near-identical mixins that used to back image registries
(``registries.py``), secrets (``references.py``) and notification channels
(``channels.py``): one ``credentials`` table, one encrypt/decrypt/mask/merge path.

A credential row is ``(id, kind, type, name, meta, data, created_at, updated_at)``:

  * ``meta`` — JSON text, the non-sensitive kind-specific attributes.
  * ``data`` — an AES-256-GCM sealed field map (the sensitive values). Never
    returned in the clear except via the reveal path; only field NAMES leak.

Sensitivity (which ``data`` keys to mask on read / preserve on blank update) is
resolved by a single injected resolver keyed by ``(kind, type)`` — see
:meth:`set_credential_sensitivity` — keeping the store free of a dependency on the
registry/notify/secrets provider packages (avoiding an import cycle).

The behaviour that genuinely differs per kind stays in the provider packages
(registry token minting, notify send, external-secret fetch); this layer only
owns storage and the thin adapters those consumers need:

  * registry — :meth:`registry_view` / :meth:`registry_view_by_host` reconstruct a
    :class:`~kowan.models.Registry` for :func:`kowan.registry.new_provider`, plus
    :meth:`update_credential_status` for validation results.
  * secret   — :meth:`resolve_secrets` / :meth:`preview_references` interpolate
    ``[[name]]`` tokens (external backends resolved live via the injected
    resolver, exactly as before).
  * channel  — :meth:`credential_config` feeds the notify provider builder.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from kowan.models import Registry, ReferencePreview
from kowan.models.credential import (
    CredentialKindConnection,
    CredentialKindRegistry,
    CredentialKindSecret,
    Credential,
)

from ._common import (
    CipherNotConfiguredError,
    NotFoundError,
    config_keys,
    mask_config,
    merge_config,
)

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object

_log = logging.getLogger("kowan.store.credentials")

# Reserved key under which a basic-auth registry password is stored inside the
# credential's encrypted `data` map. Registry provider config keys (ecr/gcp/acr)
# never collide with it, and it is excluded from the exposed config-key set so it
# is tracked only via meta.hasPassword. Empty when the registry uses cloud auth.
_REG_PASSWORD_KEY = "password"

# Docker Hub is reachable under several host spellings; they all mean the same
# registry, so fold them to one canonical host for credential matching.
_DOCKERHUB_HOSTS = frozenset(
    {
        "docker.io",
        "index.docker.io",
        "registry-1.docker.io",
        "registry.hub.docker.com",
    }
)


def _canon_host(url: str) -> str:
    """Reduce a registry URL/host to a canonical ``host[:port]`` for matching.

    Drops any scheme and path (so ``https://reg.io/``, ``reg.io/`` and ``reg.io``
    all match) and folds the Docker Hub aliases to ``docker.io``. Used to match an
    image reference's host — always bare, e.g. ``123.dkr.ecr.eu-west-1.amazonaws.com``
    — against a stored registry's URL, which the operator may have entered with a
    scheme or trailing slash. Without this, an ECR (or any) registry whose URL was
    saved as ``https://…`` would never match, and the pull would fall back to an
    anonymous (failing) pull.
    """
    s = url.strip()
    i = s.find("://")
    if i >= 0:
        s = s[i + 3:]
    slash = s.find("/")
    if slash >= 0:
        s = s[:slash]
    s = s.strip().lower()
    return "docker.io" if s in _DOCKERHUB_HOSTS else s

_CREDENTIAL_COLUMNS = "id, kind, type, name, meta, data, created_at, updated_at"


# -- reference interpolation helpers (moved verbatim from references.py) -------

# Reference source prefixes.
REF_SOURCE_SECRET = "secret"
REF_SOURCE_PLUGIN = "plugin"

# Reference preview statuses.
REF_STATUS_RESOLVED = "resolved"
REF_STATUS_UNRESOLVED = "unresolved"
REF_STATUS_ERROR = "error"

# Matches [[NAME]], [[NAME.FIELD]] or [[SOURCE::NAME(.FIELD)]] (non-greedy inner).
_SECRET_TOKEN_RE = re.compile(r"\[\[\s*([A-Za-z0-9_.:-]+?)\s*\]\]")


@dataclass
class _ParsedRef:
    source: str = ""
    name: str = ""
    field: str = ""
    has_field: bool = False
    recognized: bool = False


def _parse_ref(expr: str) -> _ParsedRef:
    """Split a token's inner expression into optional ``SOURCE::`` prefix, secret
    name and optional ``.FIELD``. Only ``secret``/``plugin`` (plus the bare form)
    are recognized."""
    source = ""
    rest = expr
    i = expr.find("::")
    if i >= 0:
        source = expr[:i].strip().lower()
        rest = expr[i + 2 :].strip()
    if source not in ("", REF_SOURCE_SECRET, REF_SOURCE_PLUGIN):
        return _ParsedRef(recognized=False)
    name, sep, field = rest.partition(".")
    has_field = sep == "."
    if name == "":
        return _ParsedRef(recognized=False)
    return _ParsedRef(
        source=source, name=name, field=field, has_field=has_field, recognized=True
    )


@dataclass
class _ResolvedSecret:
    secret_type: str
    data: dict[str, str]


def _mask_preview(v: str) -> str:
    """Obscure a resolved value for display (dot = U+2022 BULLET)."""
    n = len(v)
    if n == 0:
        return ""
    if n <= 4:
        return "•" * n
    dots = n - 4
    if dots > 8:
        dots = 8
    return v[:2] + ("•" * dots) + v[-2:]


class _CredentialMixin(_Base):
    # -- sensitivity resolver ------------------------------------------------
    def set_credential_sensitivity(
        self, fn: Callable[[str, str], dict[str, bool]]
    ) -> None:
        """Install the resolver deciding which ``data`` keys are sensitive.

        ``fn(kind, type) -> dict[str, bool]``. The server wires this at startup to
        dispatch across the registry / notify / secrets provider sensitive-key
        schemas. Sensitive keys are masked on read and preserved on update when the
        supplied value is blank or the mask placeholder.
        """
        self._cred_sensitivity = fn

    def _sensitive_keys(self, kind: str, type_: str) -> dict[str, bool]:
        fn = getattr(self, "_cred_sensitivity", None)
        if fn is None:
            return {}
        m = fn(kind, type_)
        return m if m else {}

    # -- row scan ------------------------------------------------------------
    def _row_to_credential(self, row: sqlite3.Row) -> Credential:
        data = self._decrypt_data(row["data"])
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return Credential(
            id=row["id"],
            kind=row["kind"],
            type=row["type"],
            name=row["name"],
            meta=meta,
            configKeys=self._exposed_keys(row["kind"], data),
            createdAt=row["created_at"],
            updatedAt=row["updated_at"],
        )

    @staticmethod
    def _exposed_keys(kind: str, data: dict[str, str]) -> list[str]:
        """Field names to expose (registry hides the reserved basic-auth password)."""
        if kind == CredentialKindRegistry:
            return config_keys({k: v for k, v in data.items() if k != _REG_PASSWORD_KEY})
        return config_keys(data)

    @staticmethod
    def _dump_meta(meta: dict) -> str:
        return json.dumps(meta or {}, separators=(",", ":"))

    # -- generic CRUD --------------------------------------------------------
    def create_credential(self, cred: Credential, data: dict[str, str]) -> None:
        """Insert a credential, encrypting ``data`` into the sealed blob. ``cred``
        supplies id/kind/type/name/meta/timestamps; ``cred.config_keys`` is ignored
        (names come from ``data``)."""
        blob = self._encrypt_data(data)
        self._execute(
            "INSERT INTO credentials "
            "(id, kind, type, name, meta, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cred.id, cred.kind, cred.type, cred.name,
                self._dump_meta(cred.meta), blob, cred.created_at, cred.updated_at,
            ),
        )

    def get_credential(self, id: str) -> Credential:
        """Return the credential with ``id`` (metadata + field names, no values)."""
        self._require_cipher()
        row = self._one(
            f"SELECT {_CREDENTIAL_COLUMNS} FROM credentials WHERE id = ?", (id,)
        )
        if row is None:
            raise NotFoundError("get credential: no rows in result set")
        return self._row_to_credential(row)

    def list_credentials(self, kind: Optional[str] = None) -> list[Credential]:
        """Return all credentials (optionally of one kind) ordered by name."""
        self._require_cipher()
        if kind:
            rows = self._query(
                f"SELECT {_CREDENTIAL_COLUMNS} FROM credentials WHERE kind = ? "
                "ORDER BY name",
                (kind,),
            )
        else:
            rows = self._query(
                f"SELECT {_CREDENTIAL_COLUMNS} FROM credentials ORDER BY kind, name"
            )
        return [self._row_to_credential(r) for r in rows]

    def delete_credential(self, id: str) -> None:
        """Remove a credential by id."""
        self._require_cipher()
        self._execute("DELETE FROM credentials WHERE id = ?", (id,))

    def credential_config(self, id: str) -> dict[str, str]:
        """Return the fully decrypted ``data`` map for a credential. Must never be
        serialized to clients in the clear (reveal path only)."""
        self._require_cipher()
        row = self._one("SELECT data FROM credentials WHERE id = ?", (id,))
        if row is None:
            raise NotFoundError("get credential config: no rows in result set")
        return self._decrypt_data(row["data"])

    def credential_masked_config(self, id: str) -> dict[str, str]:
        """Return a credential's exposed config with sensitive values masked (the
        reserved registry password key is never included)."""
        cred = self.get_credential(id)
        data = self.credential_config(id)
        if cred.kind == CredentialKindRegistry:
            data = {k: v for k, v in data.items() if k != _REG_PASSWORD_KEY}
        return mask_config(self._sensitive_keys(cred.kind, cred.type), data)

    # -- update: per-kind data policy ---------------------------------------
    def update_credential(
        self, cred: Credential, data: Optional[dict[str, str]], *, replace: bool
    ) -> None:
        """Update a channel/secret credential's metadata and, per policy, its data.

        ``replace=True`` (secrets): a non-empty ``data`` replaces the stored payload
        wholesale; an empty/None ``data`` keeps it. ``replace=False`` (channels):
        ``data`` is merged over the stored map (blank/masked sensitive values keep
        their existing value); a None ``data`` leaves the payload untouched. Not for
        registries — use :meth:`update_registry_credential`.
        """
        self._require_cipher()
        if data is None or (replace and not data):
            self._execute(
                "UPDATE credentials SET type = ?, name = ?, meta = ?, updated_at = ? "
                "WHERE id = ?",
                (cred.type, cred.name, self._dump_meta(cred.meta), cred.updated_at, cred.id),
            )
            return
        if replace:
            new_data = data
        else:
            existing = self.credential_config(cred.id)
            new_data = merge_config(
                self._sensitive_keys(cred.kind, cred.type), existing, data
            )
        blob = self._encrypt_data(new_data)
        self._execute(
            "UPDATE credentials SET type = ?, name = ?, meta = ?, data = ?, "
            "updated_at = ? WHERE id = ?",
            (cred.type, cred.name, self._dump_meta(cred.meta), blob, cred.updated_at, cred.id),
        )

    def count_credentials_by_type(self, kind: str, type_: str) -> int:
        """Return how many credentials of a given kind+type exist."""
        row = self._one(
            "SELECT COUNT(*) AS n FROM credentials WHERE kind = ? AND type = ?",
            (kind, type_),
        )
        return row["n"] if row is not None else 0

    # -- registry-kind adapters ---------------------------------------------
    def update_registry_credential(
        self, cred: Credential, *, password: str, config: Optional[dict[str, str]]
    ) -> None:
        """Update a registry credential. An empty ``password`` keeps the stored one;
        ``config`` None leaves the provider config untouched, else it is merged
        (blank/masked sensitive keys preserved). ``meta.hasPassword`` is refreshed.
        """
        self._require_cipher()
        existing = self.credential_config(cred.id)
        old_pw = existing.get(_REG_PASSWORD_KEY, "")
        old_cfg = {k: v for k, v in existing.items() if k != _REG_PASSWORD_KEY}
        new_pw = password if password else old_pw
        if config is None:
            new_cfg = old_cfg
        else:
            new_cfg = merge_config(
                self._sensitive_keys(cred.kind, cred.type), old_cfg, config
            )
        data = dict(new_cfg)
        if new_pw:
            data[_REG_PASSWORD_KEY] = new_pw
        cred.meta["hasPassword"] = new_pw != ""
        blob = self._encrypt_data(data)
        self._execute(
            "UPDATE credentials SET type = ?, name = ?, meta = ?, data = ?, "
            "updated_at = ? WHERE id = ?",
            (cred.type, cred.name, self._dump_meta(cred.meta), blob, cred.updated_at, cred.id),
        )

    def update_credential_status(
        self, id: str, verified: bool, insecure: bool, last_err: str
    ) -> None:
        """Persist a registry validation result into ``meta`` (best-effort, does not
        touch credentials or config)."""
        cred = self.get_credential(id)
        cred.meta["verified"] = bool(verified)
        cred.meta["insecure"] = bool(insecure)
        cred.meta["lastError"] = last_err
        self._execute(
            "UPDATE credentials SET meta = ? WHERE id = ?",
            (self._dump_meta(cred.meta), id),
        )

    def registry_view(self, id: str) -> Registry:
        """Reconstruct the :class:`~kowan.models.Registry` view of a registry
        credential (decrypted) for :func:`kowan.registry.new_provider`."""
        cred = self.get_credential(id)
        if cred.kind != CredentialKindRegistry:
            raise NotFoundError("get registry: no rows in result set")
        return self._registry_from(cred, self.credential_config(id))

    def registry_view_by_host(self, host: str) -> Registry:
        """Reconstruct the registry view whose URL matches ``host``.

        Matching is on the CANONICAL host (scheme/path stripped, Docker Hub
        aliases folded) so an image ref's bare host resolves the credential
        regardless of how the registry URL was entered — see :func:`_canon_host`.
        """
        self._require_cipher()
        target = _canon_host(host)
        for cred in self.list_credentials(CredentialKindRegistry):
            if _canon_host(cred.url) == target:
                return self._registry_from(cred, self.credential_config(cred.id))
        raise NotFoundError("get registry: no rows in result set")

    def _registry_from(self, cred: Credential, data: dict[str, str]) -> Registry:
        pw = data.get(_REG_PASSWORD_KEY, "")
        cfg = {k: v for k, v in data.items() if k != _REG_PASSWORD_KEY}
        return Registry(
            id=cred.id,
            name=cred.name,
            url=cred.url,
            type=cred.type,
            username=cred.username,
            password=pw,
            hasPassword=pw != "",
            insecure=cred.insecure,
            verified=cred.verified,
            lastError=cred.last_error,
            config=cfg,
            configKeys=config_keys(cfg),
            createdAt=cred.created_at,
        )

    # -- secret-kind resolution (external backends via injected resolver) ----
    def _external_is_external(self, secret_type: str) -> bool:
        r = self._external_secrets
        if r is None:
            return False
        return bool(r.is_external(secret_type))

    def _external_resolve(
        self, secret_type: str, data: dict[str, str], field: str
    ) -> str:
        r = self._external_secrets
        if r is None:
            raise LookupError(
                f"external secret resolver not configured for type {secret_type!r}"
            )
        return r.resolve(secret_type, data, field)

    def _load_resolved_secret(self, name: str) -> _ResolvedSecret:
        """Read a secret credential by name, returning its type + decrypted data.

        If the secret delegates its backend to a shared connection
        (``meta.connectionRef`` — many secret refs pointing at one Vault/Infisical
        backend), the connection supplies the reusable, sensitive backend data
        (address + token) and the secret's own fields (the ref-specific selectors:
        mount/path/key) overlay it; the provider is then built from the
        CONNECTION's type. Otherwise the secret's own inline type + data are used
        directly. This is the same pattern a registry uses to delegate its
        auth to a connection — same "connection = shared credential" model, applied
        to the secret-resolution seam."""
        self._require_cipher()
        row = self._one(
            "SELECT type, data, meta FROM credentials WHERE kind = ? AND name = ?",
            (CredentialKindSecret, name),
        )
        if row is None:
            raise NotFoundError(f"load secret {name!r}: no rows in result set")
        data = self._decrypt_data(row["data"])
        ref = ""
        try:
            ref = str((json.loads(row["meta"] or "{}")).get("connectionRef", "") or "")
        except Exception:  # noqa: BLE001 — bad meta => treat as a standalone secret
            ref = ""
        if ref:
            conn = self._one(
                "SELECT type, data FROM credentials WHERE kind = ? AND id = ?",
                (CredentialKindConnection, ref),
            )
            if conn is not None:
                # Connection provides the backend (address/token); the secret's own
                # selectors win on any overlap.
                merged = {**self._decrypt_data(conn["data"]), **data}
                return _ResolvedSecret(secret_type=conn["type"], data=merged)
        return _ResolvedSecret(secret_type=row["type"], data=data)

    def _resolve_value(
        self, rs: _ResolvedSecret, field: str, has_field: bool
    ) -> str:
        """Compute the concrete value for an already-loaded secret reference."""
        if self._external_is_external(rs.secret_type):
            field_override = field if has_field else ""
            return self._external_resolve(rs.secret_type, rs.data, field_override)
        if has_field:
            if field not in rs.data:
                raise LookupError(f"field {field!r} not found")
            return rs.data[field]
        if "value" in rs.data:
            return rs.data["value"]
        if len(rs.data) == 1:
            return next(iter(rs.data.values()))
        raise LookupError(
            f'ambiguous bare reference ({len(rs.data)} fields, no "value")'
        )

    def resolve_secrets(self, text: str) -> str:
        """Replace ``[[name]]`` / ``[[name.field]]`` tokens in ``text`` with
        decrypted secret values. Unrecognized/unknown tokens are left unchanged; with
        no cipher attached ``text`` is returned unchanged."""
        if self._cipher is None:
            return text
        if "[[" not in text:
            return text

        cache: dict[str, Optional[_ResolvedSecret]] = {}

        def repl(m: "re.Match[str]") -> str:
            token = m.group(0)
            ref = _parse_ref(m.group(1))
            if not ref.recognized:
                return token

            if ref.name in cache:
                rs = cache[ref.name]
            else:
                try:
                    rs = self._load_resolved_secret(ref.name)
                except Exception:  # noqa: BLE001 — unknown/read error => leave token
                    cache[ref.name] = None
                    return token
                cache[ref.name] = rs
            if rs is None:
                return token

            if ref.source == REF_SOURCE_PLUGIN and not self._external_is_external(
                rs.secret_type
            ):
                return token

            try:
                return self._resolve_value(rs, ref.field, ref.has_field)
            except Exception as e:  # noqa: BLE001 — non-fatal, leave token unresolved
                _log.warning(
                    "resolve secrets: leaving token unchanged: name=%s type=%s token=%s error=%s",
                    ref.name, rs.secret_type, token, e,
                )
                return token

        return _SECRET_TOKEN_RE.sub(repl, text)

    def preview_references(self, text: str) -> list[ReferencePreview]:
        """Resolve every ``[[...]]`` token in ``text`` and return a structured,
        MASKED per-token report (admin tool). Mirrors :meth:`resolve_secrets`; for an
        external secret it runs a live fetch that doubles as a connectivity check."""
        out: list[ReferencePreview] = []
        if self._cipher is None:
            raise CipherNotConfiguredError("store: secrets cipher not configured")

        cache: dict[str, Optional[_ResolvedSecret]] = {}
        seen: set[str] = set()

        for m in _SECRET_TOKEN_RE.finditer(text):
            token = m.group(0)
            if token in seen:
                continue
            seen.add(token)

            ref = _parse_ref(m.group(1))
            p = ReferencePreview(
                token=token, source=ref.source, name=ref.name, field=ref.field
            )
            if not ref.recognized:
                p.status = REF_STATUS_UNRESOLVED
                p.message = "not a recognized reference (unknown source prefix)"
                out.append(p)
                continue

            if ref.name in cache:
                rs = cache[ref.name]
            else:
                try:
                    rs = self._load_resolved_secret(ref.name)
                except Exception:  # noqa: BLE001
                    rs = None
                cache[ref.name] = rs
            if rs is None:
                p.status = REF_STATUS_UNRESOLVED
                p.message = f"no secret named {ref.name!r}"
                out.append(p)
                continue

            p.secret_type = rs.secret_type
            p.external = self._external_is_external(rs.secret_type)

            if ref.source == REF_SOURCE_PLUGIN and not p.external:
                p.status = REF_STATUS_UNRESOLVED
                p.message = (
                    f"plugin:: requires an external secret, but {ref.name!r} is type "
                    f"{rs.secret_type!r}"
                )
                out.append(p)
                continue

            try:
                val = self._resolve_value(rs, ref.field, ref.has_field)
            except Exception as e:  # noqa: BLE001
                p.status = REF_STATUS_ERROR
                p.message = str(e)
                out.append(p)
                continue

            p.status = REF_STATUS_RESOLVED
            p.length = len(val)
            p.masked = _mask_preview(val)
            out.append(p)

        return out
