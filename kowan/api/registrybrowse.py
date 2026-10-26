"""Live registry browse endpoints for a stored registry CONNECTION.

Two read-only helpers the workflow docker-image trigger editor uses to fill its
repository/tag selector from the registry a connection points at, so the operator
picks an image instead of typing a full reference with inline credentials:

  * GET /credentials/{id}/registry/repositories        -> {"repositories": [...]}
  * GET /credentials/{id}/registry/tags?repository=REPO -> {"tags": [...]}

Both resolve the credential id through the capability dispatch
(``connections.resolve_service(store, id, dockerRegistryAuth)``) to obtain the
registry Provider, then ask it to browse via ``provider.list_repositories()`` /
``provider.list_tags(repo)``. The provider picks the right protocol per type:
generic/ghcr/gcp use the Docker Registry HTTP API v2 (``/v2/_catalog`` and
``/v2/{repo}/tags/list``), while ECR and ACR use their cloud-native listing APIs
(ECR has no ``_catalog``). ``resolve_service`` follows a registry's
``connectionRef``, so a registry backed by a shared cloud connection browses with
that connection's credentials — and a bare connection id resolves directly too.

BEST EFFORT by design: not every registry supports listing (Docker Hub has no
``_catalog``), and credentials may lack the scope. Rather than surface a 5xx to a
UI selector, these endpoints log the failure and return an empty list — the
operator can still type the value into the CreatableSelect. They require the
operator role because they reach an external registry with the stored connection's
credentials.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

from aiohttp import web

from kowan import capabilities as caps
from kowan.store import NotFoundError
from kowan.web import (
    api_route,
    bind_svc,
    error_response,
    json_response,
    require_role,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services

log = logging.getLogger("kowan.api.registrybrowse")


async def _resolve_provider(svc: "Services", cid: str):
    """Resolve a registry/connection id into its ``dockerRegistryAuth`` Provider.

    Uses ``resolve_service`` so a registry delegating to a shared connection (or a
    bare connection id) yields a provider bound to the right credentials — a cloud
    connection mints short-lived creds and knows how to list natively. Raises
    ``NotFoundError`` when the credential is missing and ``ValueError`` when it
    cannot serve dockerRegistryAuth."""
    from kowan import connections

    return await connections.resolve_service(
        svc.store, cid, caps.CAP_DOCKER_REGISTRY_AUTH
    )


async def _repositories(svc: "Services", request: web.Request) -> web.Response:
    """List repositories for the connection's registry (provider-native).

    Empty list (never an error) when the registry does not support listing, rejects
    the credentials, or is unreachable — the UI selector stays usable by letting the
    operator type the repository."""
    cid = request.match_info.get("id", "")
    try:
        provider = await _resolve_provider(svc, cid)
    except NotFoundError:
        return error_response(404, "credential not found")
    except Exception as err:  # noqa: BLE001 - cannot serve dockerRegistryAuth
        log.info("registry repositories: resolve credential failed id=%s err=%s", cid, err)
        return json_response({"repositories": []})

    try:
        repos = await provider.list_repositories()
    except Exception as err:  # noqa: BLE001 - registry/httpx error; best effort
        log.info("registry repositories: list failed id=%s err=%s", cid, err)
        return json_response({"repositories": []})

    return json_response({"repositories": _string_list(repos)})


async def _tags(svc: "Services", request: web.Request) -> web.Response:
    """List a repository's tags for the connection's registry (provider-native).

    Empty list (never an error) on any registry/credential/transport failure."""
    cid = request.match_info.get("id", "")
    repo = (request.query.get("repository", "") or "").strip().strip("/")
    if repo == "":
        return error_response(400, "repository is required")

    try:
        provider = await _resolve_provider(svc, cid)
    except NotFoundError:
        return error_response(404, "credential not found")
    except Exception as err:  # noqa: BLE001 - cannot serve dockerRegistryAuth
        log.info("registry tags: resolve credential failed id=%s err=%s", cid, err)
        return json_response({"tags": []})

    try:
        tag_list = await provider.list_tags(repo)
    except Exception as err:  # noqa: BLE001 - registry/httpx error; best effort
        log.info("registry tags: list failed id=%s repo=%s err=%s", cid, repo, err)
        return json_response({"tags": []})

    return json_response({"tags": _string_list(tag_list)})


def _string_list(value: Optional[object]) -> List[str]:
    """Coerce a registry-response array field into a list of strings, tolerating a
    missing/null/non-list value (returns [])."""
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def register(app: web.Application, svc: "Services") -> None:
    """Mount the registry-browse routes under ``/api/v1``.

    Operator role: they reach an external registry with the stored connection's
    credentials, so they are gated above viewer but below the admin-only credential
    CRUD."""
    h = bind_svc(svc)
    operator = require_role("operator")

    api_route(
        app, "GET", "/credentials/{id}/registry/repositories", operator(h(_repositories))
    )
    api_route(app, "GET", "/credentials/{id}/registry/tags", operator(h(_tags)))


__all__ = ["register"]
