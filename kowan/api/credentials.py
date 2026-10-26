"""Unified credential management over the protected ``/api/v1`` surface.

One admin-only CRUD surface for every externally-facing credential the control
plane manages centrally — image **registries**, **secrets** and notification
**channels** — replacing the former ``/registries``, ``/secrets`` and
``/channels`` modules. Every record is a :class:`~kowan.models.Credential`
discriminated by ``kind``; the sensitive payload is encrypted at rest and never
serialized in the clear (list/get return masked config; secrets expose only field
names, revealed one at a time via ``/reveal``).

Routes (all admin):

  * GET    /credentials[?kind=]              -> list (masked config for registry/channel)
  * GET    /credentials/types[?kind=]        -> provider field schemas per kind
  * POST   /credentials                      -> create (body carries ``kind``)
  * GET    /credentials/{id}                 -> one credential (masked)
  * PUT    /credentials/{id}                 -> update
  * DELETE /credentials/{id}                 -> delete
  * POST   /credentials/{id}/test            -> registry validate / channel test-send
  * POST   /credentials/{id}/reveal          -> secret only, decrypted values (audited)
  * GET    /credentials/{id}/catalog         -> registry only, repositories
  * GET    /credentials/{id}/tags?repo=      -> registry only, tags
  * POST   /credentials/resolve              -> secret ``[[...]]`` preview (masked)

Behaviour that genuinely differs per kind (registry token minting/validation,
channel send, external-secret fetch) is dispatched here but implemented in the
provider packages; the store owns storage and the merge/mask policy.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional

from aiohttp import web

from kowan import capabilities as caps
from kowan import connection_types as conn_native
from kowan import registry, secrets
from kowan.models import (
    Credential,
    CredentialKindChannel,
    CredentialKindConnection,
    CredentialKindRegistry,
    CredentialKindSecret,
    SecretTypeEnv,
    SecretTypeGeneric,
    SecretTypeGit,
    SecretTypeRegistry,
    SecretTypeToken,
    VaultDefaultMount,
    VaultFieldMount,
    normalize_registry_type,
    valid_credential_kind,
)
from kowan.models.alert import AlertLevelInfo, valid_level
from kowan.notify import provider as notify
from kowan.notify.provider import Message
from kowan.store import NotFoundError
from kowan.web import (
    ops_error_response,
    api_route,
    bind_svc,
    error_response,
    json_response,
    now_rfc3339,
    read_json,
    require_role,
    user_from,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services

log = logging.getLogger("kowan.api.credentials")

_MASKED_VALUE = "********"
# Bounds so a hung provider cannot block a request (registry validate / channel send).
_VALIDATE_TIMEOUT = 20.0
_TEST_TIMEOUT = 15.0

_BUILTIN_SECRET_TYPES = (
    SecretTypeGeneric,
    SecretTypeRegistry,
    SecretTypeGit,
    SecretTypeEnv,
    SecretTypeToken,
)


# ---------------------------------------------------------------------------
# response assembly
# ---------------------------------------------------------------------------

def _response(cred: Credential, masked: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The client-facing credential payload: the model dumped by alias, with the
    masked config spliced in for kinds that carry provider config (registry /
    channel / connection). Secrets expose only field names via ``configKeys``. A
    connection also advertises the capabilities its type implements."""
    body = cred.model_dump(by_alias=True)
    if masked is not None:
        body["config"] = masked
    if cred.kind == CredentialKindConnection:
        body["capabilities"] = sorted(caps.effective_capabilities(cred.type))
    return body


def _write(svc: "Services", cred_id: str, status: int) -> web.Response:
    """Re-read a credential and write it (masked) at ``status`` so the response
    reflects the persisted state and masked secrets."""
    try:
        cred = svc.store.get_credential(cred_id)
    except Exception as err:  # noqa: BLE001
        log.error("reload credential: %s (id=%s)", err, cred_id)
        return error_response(500, "failed to load credential")
    masked = None
    if cred.kind in (CredentialKindRegistry, CredentialKindChannel):
        try:
            masked = svc.store.credential_masked_config(cred_id)
        except Exception as err:  # noqa: BLE001
            log.error("mask credential config: %s (id=%s)", err, cred_id)
            return error_response(500, "failed to load credential config")
    return json_response(_response(cred, masked), status=status)


def _masked_for(svc: "Services", cred: Credential) -> Optional[Dict[str, str]]:
    if cred.kind not in (
        CredentialKindRegistry,
        CredentialKindChannel,
        CredentialKindConnection,
    ):
        return None
    return svc.store.credential_masked_config(cred.id)


# ---------------------------------------------------------------------------
# per-kind validation + assembly
# ---------------------------------------------------------------------------

def _valid_secret_type(t: str) -> bool:
    return t in _BUILTIN_SECRET_TYPES or secrets.is_external(t)


def _validate_external_fields(secret_type: str, fields: Dict[str, str]) -> str:
    """Enforce an external secret backend's Required fields and apply the Vault KV
    mount default. May mutate ``fields``. Returns a client message or ""."""
    for f in secrets.fields(secret_type):
        if f.required and not (fields.get(f.key, "") or "").strip():
            return f"{secret_type} secret requires non-empty {f.key}"
    if secret_type == secrets.VaultType and not (fields.get(VaultFieldMount, "") or "").strip():
        fields[VaultFieldMount] = VaultDefaultMount
    return ""


def _validate_create(kind: str, body: Dict[str, Any]) -> Optional[str]:
    """Validate a create request per kind. Returns a client message or None."""
    name = str(body.get("name", "") or "")
    type_ = str(body.get("type", "") or "")
    if name == "":
        return "name is required"

    if kind == CredentialKindConnection:
        if type_ == "":
            return "type is required"
        # A known type implements a capability, or is a native type with its
        # own consumer and none (portainer: the import reads it directly).
        if not caps.capabilities_of(type_) and not conn_native.is_native(type_):
            return "unknown connection type"
        return None

    if kind == CredentialKindRegistry:
        if str(body.get("url", "") or "") == "":
            return "url is required"
        if type_ != "" and normalize_registry_type(type_) != type_:
            return "unknown registry type"
        return None

    if kind == CredentialKindSecret:
        stype = type_ or SecretTypeGeneric
        if not _valid_secret_type(stype):
            return "invalid secret type"
        fields = body.get("fields") or {}
        if not isinstance(fields, dict) or len(fields) == 0:
            return "at least one field is required"
        # A secret that delegates to a shared connection gets its sensitive backend
        # fields (address/token) from that connection, so only a STANDALONE external
        # secret must carry them itself. The Vault mount defaults live at fetch time.
        if secrets.is_external(stype) and not _conn_ref(body):
            return _validate_external_fields(stype, {str(k): str(v) for k, v in fields.items()}) or None
        return None

    if kind == CredentialKindChannel:
        if type_ == "":
            return "type is required"
        if notify.fields(type_) is None:
            return "unknown channel type"
        ml = str(body.get("minLevel", "") or "")
        if ml != "" and not valid_level(ml):
            return "minLevel must be one of info, warning, critical"
        return None

    return "unknown credential kind"


def _config_map(body: Dict[str, Any], key: str) -> Dict[str, str]:
    v = body.get(key)
    if not isinstance(v, dict):
        return {}
    return {str(k): str(val) for k, val in v.items()}


def _conn_ref(body: Dict[str, Any]) -> Dict[str, str]:
    """A ``{connectionRef: id}`` fragment for a service that delegates its auth to a
    shared connection, or ``{}`` when it embeds its own inline auth."""
    ref = body.get("connectionRef")
    return {"connectionRef": str(ref)} if ref else {}


def _apply_conn_ref(meta: Dict[str, Any], body: Dict[str, Any]) -> None:
    """On update, set/clear ``meta.connectionRef`` when the body carries the key
    (empty string detaches the service from its shared connection)."""
    if "connectionRef" in body:
        ref = str(body.get("connectionRef") or "")
        if ref:
            meta["connectionRef"] = ref
        else:
            meta.pop("connectionRef", None)


# ---------------------------------------------------------------------------
# list / get / types
# ---------------------------------------------------------------------------

async def _list(svc: "Services", request: web.Request) -> web.Response:
    kind = request.query.get("kind", "") or None
    if kind is not None and not valid_credential_kind(kind):
        return error_response(400, "unknown credential kind")
    try:
        creds = svc.store.list_credentials(kind)
    except Exception as err:  # noqa: BLE001
        log.error("list credentials: %s", err)
        return error_response(500, "failed to list credentials")
    out = []
    for cred in creds:
        try:
            out.append(_response(cred, _masked_for(svc, cred)))
        except Exception as err:  # noqa: BLE001
            log.error("mask credential config: %s (id=%s)", err, cred.id)
            return error_response(500, "failed to load credential config")
    return json_response(out)


async def _get(svc: "Services", request: web.Request) -> web.Response:
    cid = request.match_info.get("id", "")
    try:
        cred = svc.store.get_credential(cid)
    except NotFoundError:
        return error_response(404, "credential not found")
    try:
        return json_response(_response(cred, _masked_for(svc, cred)))
    except Exception as err:  # noqa: BLE001
        log.error("mask credential config: %s (id=%s)", err, cid)
        return error_response(500, "failed to load credential config")


async def _types(svc: "Services", request: web.Request) -> web.Response:
    """Provider field schemas. With ``?kind=`` returns that kind's list; otherwise a
    dict keyed by kind so the UI can render every tab from one call."""
    def reg_types():
        return [ts.model_dump(by_alias=True) for ts in registry.types()]

    def sec_types():
        return [t.to_dict() for t in secrets.types()]

    def ch_types():
        return notify.types()

    def conn_types():
        # Every connection-capable type with the capabilities it implements + its
        # field schema, so the UI renders one create form and capability badges.
        # The raw ecr/acr registry types are hidden — they are surfaced as the
        # cloud identities aws/azure (which reuse their field schemas), because a
        # cloud connection is more than a registry (it also does secret/message/
        # object-store, derived from cloudIdentity).
        reg_by_type = {d.get("type", ""): d for d in reg_types()}
        out = []
        for d in reg_types() + sec_types() + ch_types():
            t = d.get("type", "")
            if t in ("ecr", "acr") or not caps.capabilities_of(t):
                continue
            out.append({
                "type": t,
                "capabilities": sorted(caps.effective_capabilities(t)),
                "fields": d.get("fields", []),
            })
        # Cloud identities reuse the ECR/ACR field schemas.
        for cloud, facet in (("aws", "ecr"), ("azure", "acr")):
            out.append({
                "type": cloud,
                "capabilities": sorted(caps.effective_capabilities(cloud)),
                "fields": reg_by_type.get(facet, {}).get("fields", []),
            })
        # Connection-native types (git hosts, api-token, basic-auth) own their schema.
        for d in conn_native.types():
            t = d["type"]
            out.append({
                "type": t,
                "capabilities": sorted(caps.effective_capabilities(t)),
                "fields": d.get("fields", []),
            })
        out.sort(key=lambda e: e["type"])
        return out

    kind = request.query.get("kind", "")
    if kind == CredentialKindConnection:
        return json_response(conn_types())
    if kind == CredentialKindRegistry:
        return json_response(reg_types())
    if kind == CredentialKindSecret:
        return json_response(sec_types())
    if kind == CredentialKindChannel:
        return json_response(ch_types())
    if kind:
        return error_response(400, "unknown credential kind")
    return json_response(
        {
            CredentialKindConnection: conn_types(),
            CredentialKindRegistry: reg_types(),
            CredentialKindSecret: sec_types(),
            CredentialKindChannel: ch_types(),
        }
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

async def _create(svc: "Services", request: web.Request) -> web.Response:
    try:
        body = await read_json(request)
    except Exception:  # noqa: BLE001
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")

    kind = str(body.get("kind", "") or "")
    if not valid_credential_kind(kind):
        return error_response(400, "unknown credential kind")

    msg = _validate_create(kind, body)
    if msg is not None:
        return error_response(400, msg)

    now = now_rfc3339()
    name = str(body.get("name", "") or "")

    if kind == CredentialKindConnection:
        # A reusable typed credential. `config` holds the secret fields (-> data);
        # `meta` holds non-secret identity fields (username, region, apiBaseUrl…).
        meta = body.get("meta")
        meta = {str(k): v for k, v in meta.items()} if isinstance(meta, dict) else {}
        cred = Credential(
            id=str(uuid.uuid4()), kind=kind, type=str(body.get("type", "") or ""),
            name=name, meta=meta, createdAt=now, updatedAt=now,
        )
        try:
            svc.store.create_credential(cred, _config_map(body, "config"))
        except Exception as err:  # noqa: BLE001
            log.error("create connection credential: %s", err)
            return error_response(500, "failed to create credential")
        return _write(svc, cred.id, 201)

    if kind == CredentialKindRegistry:
        password = str(body.get("password", "") or "")
        config = _config_map(body, "config")
        data = dict(config)
        if password:
            data["password"] = password
        cred = Credential(
            id=str(uuid.uuid4()), kind=kind,
            type=normalize_registry_type(str(body.get("type", "") or "")),
            name=name,
            meta={
                "url": str(body.get("url", "") or ""),
                "username": str(body.get("username", "") or ""),
                "insecure": bool(body.get("insecure", False)),
                "verified": False, "lastError": "", "hasPassword": password != "",
                **_conn_ref(body),
            },
            createdAt=now, updatedAt=now,
        )
        try:
            svc.store.create_credential(cred, data)
        except Exception as err:  # noqa: BLE001
            log.error("create registry credential: %s", err)
            return error_response(500, "failed to create credential")
        await _registry_validate_and_persist(svc, cred.id)
        return _write(svc, cred.id, 201)

    if kind == CredentialKindSecret:
        stype = str(body.get("type", "") or "") or SecretTypeGeneric
        fields = {str(k): str(v) for k, v in (body.get("fields") or {}).items()}
        if secrets.is_external(stype) and not _conn_ref(body):
            _validate_external_fields(stype, fields)  # applies Vault mount default
        cred = Credential(
            id=str(uuid.uuid4()), kind=kind, type=stype, name=name,
            meta={"description": str(body.get("description", "") or ""), **_conn_ref(body)},
            createdAt=now, updatedAt=now,
        )
        try:
            svc.store.create_credential(cred, fields)
        except Exception as err:  # noqa: BLE001
            log.error("create secret credential: %s", err)
            return error_response(500, "failed to create credential")
        return _write(svc, cred.id, 201)

    # channel
    config = _config_map(body, "config")
    cred = Credential(
        id=str(uuid.uuid4()), kind=kind, type=str(body.get("type", "") or ""), name=name,
        meta={
            "enabled": bool(body.get("enabled", False)),
            "minLevel": str(body.get("minLevel", "") or "") or AlertLevelInfo,
            **_conn_ref(body),
        },
        createdAt=now, updatedAt=now,
    )
    try:
        svc.store.create_credential(cred, config)
    except Exception as err:  # noqa: BLE001
        log.error("create channel credential: %s", err)
        return error_response(500, "failed to create credential")
    return _write(svc, cred.id, 201)


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

async def _update(svc: "Services", request: web.Request) -> web.Response:
    cid = request.match_info.get("id", "")
    try:
        existing = svc.store.get_credential(cid)
    except NotFoundError:
        return error_response(404, "credential not found")

    try:
        body = await read_json(request)
    except Exception:  # noqa: BLE001
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")

    kind = existing.kind
    now = now_rfc3339()

    if kind == CredentialKindConnection:
        name = str(body.get("name", "") or "") or existing.name
        meta = body.get("meta")
        meta = {str(k): v for k, v in meta.items()} if isinstance(meta, dict) else dict(existing.meta)
        cred = Credential(
            id=cid, kind=kind,
            type=str(body.get("type", "") or "") or existing.type,
            name=name, meta=meta, updatedAt=now,
        )
        config = _config_map(body, "config") if body.get("config") is not None else None
        try:
            svc.store.update_credential(cred, config, replace=False)
        except Exception as err:  # noqa: BLE001
            log.error("update connection credential: %s", err)
            return error_response(500, "failed to update credential")
        return _write(svc, cid, 200)

    if kind == CredentialKindRegistry:
        msg = _validate_create(kind, {**body, "name": body.get("name", existing.name)})
        if msg is not None:
            return error_response(400, msg)
        meta = dict(existing.meta)
        meta["url"] = str(body.get("url", "") or "")
        meta["username"] = str(body.get("username", "") or "")
        meta["insecure"] = bool(body.get("insecure", False))
        _apply_conn_ref(meta, body)
        cred = Credential(
            id=cid, kind=kind,
            type=normalize_registry_type(str(body.get("type", "") or "")),
            name=str(body.get("name", "") or ""), meta=meta, updatedAt=now,
        )
        password = str(body.get("password", "") or "")
        # config absent -> leave provider config untouched; present -> merge.
        config = _config_map(body, "config") if "config" in body else None
        try:
            svc.store.update_registry_credential(cred, password=password, config=config)
        except Exception as err:  # noqa: BLE001
            log.error("update registry credential: %s", err)
            return error_response(500, "failed to update credential")
        await _registry_validate_and_persist(svc, cid)
        return _write(svc, cid, 200)

    if kind == CredentialKindSecret:
        name = str(body.get("name", "") or "")
        if name == "":
            return error_response(400, "name is required")
        stype = str(body.get("type", "") or "") or existing.type
        if not _valid_secret_type(stype):
            return error_response(400, "invalid secret type")
        fields = {str(k): str(v) for k, v in (body.get("fields") or {}).items()}
        meta = {"description": str(body.get("description", "") or "")}
        if existing.connection_ref:
            meta["connectionRef"] = existing.connection_ref
        _apply_conn_ref(meta, body)
        # A workload's own secret (kowan/deployment/envsecrets.py): its rows
        # reference it by name, so it keeps both its owner and its name.
        owner = (existing.meta or {}).get("deploymentId")
        if owner:
            if name != existing.name:
                return error_response(400, "this secret belongs to a workload, whose variables reference it by name: it cannot be renamed")
            meta["deploymentId"] = owner
        # Skip the external-backend required-field check when the secret delegates
        # to a shared connection (the connection carries the sensitive backend).
        if secrets.is_external(stype) and len(fields) > 0 and not meta.get("connectionRef"):
            emsg = _validate_external_fields(stype, fields)
            if emsg:
                return error_response(400, emsg)
        cred = Credential(
            id=cid, kind=kind, type=stype, name=name, meta=meta, updatedAt=now,
        )
        try:
            svc.store.update_credential(cred, fields, replace=True)
        except Exception as err:  # noqa: BLE001
            log.error("update secret credential: %s", err)
            return error_response(500, "failed to update credential")
        return _write(svc, cid, 200)

    # channel — partial update; type immutable.
    meta = dict(existing.meta)
    name = existing.name
    if "name" in body:
        name = str(body.get("name") or "")
        if name == "":
            return error_response(400, "name is required")
    if "type" in body and str(body.get("type") or "") != existing.type:
        return error_response(400, "channel type cannot be changed")
    if "enabled" in body:
        meta["enabled"] = bool(body.get("enabled"))
    if "minLevel" in body:
        ml = str(body.get("minLevel") or "")
        if not valid_level(ml):
            return error_response(400, "minLevel must be one of info, warning, critical")
        meta["minLevel"] = ml
    _apply_conn_ref(meta, body)
    cred = Credential(id=cid, kind=kind, type=existing.type, name=name, meta=meta, updatedAt=now)
    config = _config_map(body, "config") if body.get("config") is not None else None
    try:
        svc.store.update_credential(cred, config, replace=False)
    except Exception as err:  # noqa: BLE001
        log.error("update channel credential: %s", err)
        return error_response(500, "failed to update credential")
    return _write(svc, cid, 200)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

async def _delete(svc: "Services", request: web.Request) -> web.Response:
    cid = request.match_info.get("id", "")
    try:
        svc.store.get_credential(cid)
    except NotFoundError:
        return error_response(404, "credential not found")
    try:
        svc.store.delete_credential(cid)
    except Exception as err:  # noqa: BLE001
        log.error("delete credential: %s", err)
        return error_response(500, "failed to delete credential")
    return json_response({"status": "deleted"})


# ---------------------------------------------------------------------------
# registry-only: validate/test + catalog + tags
# ---------------------------------------------------------------------------

async def _validate_provider(provider: Any) -> Dict[str, Any]:
    """Run a registry provider's ``validate()`` under a timeout, into a result dict."""
    try:
        insecure, err = await asyncio.wait_for(provider.validate(), timeout=_VALIDATE_TIMEOUT)
    except asyncio.TimeoutError:
        return {"verified": False, "insecure": False, "error": "validation timed out"}
    except Exception as exc:  # noqa: BLE001
        return {"verified": False, "insecure": False, "error": str(exc)}
    if err:
        return {"verified": False, "insecure": insecure, "error": err}
    return {"verified": True, "insecure": insecure, "error": ""}


async def _registry_validate_and_persist(svc: "Services", cred_id: str) -> Dict[str, Any]:
    # Build the docker-registry-auth provider through the capability dispatch so a
    # registry delegating to a shared connection validates the CONNECTION's creds,
    # not its own (empty) inline data.
    from kowan import connections

    try:
        provider = await connections.resolve_service(
            svc.store, cred_id, caps.CAP_DOCKER_REGISTRY_AUTH
        )
    except Exception as err:  # noqa: BLE001
        res = {"verified": False, "insecure": False, "error": str(err)}
    else:
        res = await _validate_provider(provider)
    try:
        svc.store.update_credential_status(cred_id, res["verified"], res["insecure"], res["error"])
    except Exception as err:  # noqa: BLE001
        log.error("persist registry status: %s (id=%s)", err, cred_id)
    return res


async def _test(svc: "Services", request: web.Request) -> web.Response:
    """Dispatch a connectivity test by kind: registry validates its credentials
    (result persisted to the badges); channel builds its provider and sends a test
    message. Always 200 so the frontend shows the outcome inline."""
    cid = request.match_info.get("id", "")
    try:
        cred = svc.store.get_credential(cid)
    except NotFoundError:
        return error_response(404, "credential not found")

    if cred.kind == CredentialKindRegistry:
        res = await _registry_validate_and_persist(svc, cid)
        body = {"verified": res["verified"], "insecure": res["insecure"]}
        if res.get("error"):
            body["error"] = res["error"]
        return json_response(body)

    if cred.kind == CredentialKindChannel:
        # Build the send provider through the capability dispatch so a channel that
        # delegates to a shared connection test-sends with the CONNECTION's transport,
        # not its own (empty) inline config — mirroring the notifier's live path.
        from kowan import connections

        try:
            provider = await connections.resolve_service(
                svc.store, cid, caps.CAP_MESSAGE_SEND
            )
        except Exception as err:  # noqa: BLE001
            return json_response({"ok": False, "error": str(err)})
        msg = Message(
            title="Kowan test",
            body=f'This is a test notification from kowan for channel "{cred.name}".',
            level=AlertLevelInfo,
        )
        try:
            await asyncio.wait_for(provider.send(msg), timeout=_TEST_TIMEOUT)
        except Exception as err:  # noqa: BLE001
            return json_response({"ok": False, "error": str(err)})
        return json_response({"ok": True})

    return error_response(400, "test is not supported for this credential kind")


async def _browse_provider(svc: "Services", cid: str):
    """Resolve the dockerRegistryAuth provider for a registry id through the
    capability dispatch, so a registry delegating to a shared CONNECTION browses
    with the connection's credentials (ECR/ACR/GCP mint a token) — not the
    registry's own empty inline data. This is the same resolution the validate/test
    path uses; browsing historically built the provider from ``registry_view``,
    which ignores ``connectionRef`` and so never saw the cloud credentials."""
    from kowan import connections

    return await connections.resolve_service(
        svc.store, cid, caps.CAP_DOCKER_REGISTRY_AUTH
    )


async def _catalog(svc: "Services", request: web.Request) -> web.Response:
    cid = request.match_info.get("id", "")
    try:
        svc.store.get_credential(cid)
    except NotFoundError:
        return error_response(404, "registry not found")
    try:
        provider = await _browse_provider(svc, cid)
        repos = await provider.list_repositories()
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"repositories": repos if repos else []})


async def _tags(svc: "Services", request: web.Request) -> web.Response:
    cid = request.match_info.get("id", "")
    try:
        svc.store.get_credential(cid)
    except NotFoundError:
        return error_response(404, "registry not found")
    repo = request.query.get("repo", "")
    if repo == "":
        return error_response(400, "repo is required")
    try:
        provider = await _browse_provider(svc, cid)
        tag_list = await provider.list_tags(repo)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"repository": repo, "tags": tag_list if tag_list else []})


# ---------------------------------------------------------------------------
# secret-only: reveal + resolve preview
# ---------------------------------------------------------------------------

def _mask_sensitive(secret_type: str, data: Dict[str, str]) -> Dict[str, str]:
    sensitive = secrets.sensitive_keys(secret_type)
    return {k: (_MASKED_VALUE if sensitive.get(k) else v) for k, v in data.items()}


async def _reveal(svc: "Services", request: web.Request) -> web.Response:
    """Return the decrypted values of a secret credential (the only endpoint that
    exposes values; audited). External backends mask their sensitive fields."""
    cid = request.match_info.get("id", "")
    try:
        cred = svc.store.get_credential(cid)
    except NotFoundError:
        return error_response(404, "credential not found")
    if cred.kind != CredentialKindSecret:
        return error_response(400, "reveal is only supported for secrets")
    try:
        data = svc.store.credential_config(cid)
    except Exception as err:  # noqa: BLE001
        log.error("reveal secret: %s secret=%s", err, cred.name)
        return error_response(500, "failed to reveal secret")
    if secrets.is_external(cred.type):
        data = _mask_sensitive(cred.type, data)
    user = user_from(request)
    log.info(
        "secret revealed: user=%s userId=%s secret=%s secretId=%s",
        getattr(user, "username", ""), getattr(user, "id", ""), cred.name, cred.id,
    )
    return json_response({"fields": data})


async def _resolve(svc: "Services", request: web.Request) -> web.Response:
    """Resolve the ``[[...]]`` tokens in the supplied text and return a masked,
    per-token preview (admin tool; external backends fetched live then masked)."""
    body = await read_json(request)
    text = ""
    if isinstance(body, dict):
        text = str(body.get("text", "") or "")
    try:
        refs = await asyncio.to_thread(svc.store.preview_references, text)
    except Exception as err:  # noqa: BLE001
        log.error("resolve references: %s", err)
        return error_response(500, "failed to resolve references")
    user = user_from(request)
    log.info(
        "references previewed: user=%s userId=%s count=%d",
        getattr(user, "username", ""), getattr(user, "id", ""), len(refs),
    )
    return json_response({"references": [r.model_dump(by_alias=True) for r in refs]})


# ---------------------------------------------------------------------------
# route registration
# ---------------------------------------------------------------------------

def register(app: web.Application, svc: "Services") -> None:
    """Mount the credential routes under ``/api/v1``. Every route is admin-only
    because credentials carry secrets."""
    h = bind_svc(svc)
    admin = require_role("admin")

    api_route(app, "GET", "/credentials", admin(h(_list)))
    api_route(app, "GET", "/credentials/types", admin(h(_types)))
    api_route(app, "POST", "/credentials/resolve", admin(h(_resolve)))
    api_route(app, "POST", "/credentials", admin(h(_create)))
    api_route(app, "GET", "/credentials/{id}", admin(h(_get)))
    api_route(app, "PUT", "/credentials/{id}", admin(h(_update)))
    api_route(app, "DELETE", "/credentials/{id}", admin(h(_delete)))
    api_route(app, "POST", "/credentials/{id}/test", admin(h(_test)))
    api_route(app, "POST", "/credentials/{id}/reveal", admin(h(_reveal)))
    api_route(app, "GET", "/credentials/{id}/catalog", admin(h(_catalog)))
    api_route(app, "GET", "/credentials/{id}/tags", admin(h(_tags)))


__all__ = ["register"]
