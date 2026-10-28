"""Built-in image update detection (the "update available" badge).

Two routes, no workflow needed:

  GET  /hosts/{hostID}/images/{id}/update
       The same check for ONE image, every tag of it, asked of the registry
       now (cache bypassed, and refreshed for the host report): works for an
       image no container runs. One row per tag.
  GET  /hosts/{hostID}/updates[?refresh=1]
       For every container on the host compare the digest of the image the
       container actually runs (``RepoDigests`` of its image id) with the
       registry's current manifest digest for the SAME tag. Remote digests are
       cached in memory per normalised image ref (TTL ~10 min) and resolved with
       bounded concurrency; ``refresh=1`` bypasses the cache.
  POST /hosts/{hostID}/containers/{id}/update  (operator+)
       Pull the tag again and recreate the container with the same config.
       Returns ``{"id": <new container id>}``.

The registry side reuses the docker-image trigger's resolver
(``kowan/triggers/dockerimage.py``: manifest HEAD + Bearer handshake, no pull),
the same way ``kowan/agentupdate.py`` does for the agent badge. Credentials come
from a stored registry whose host matches the image (anonymous otherwise).

Images pinned by digest, local-only images (no ``RepoDigests``) and registry
errors never fail the whole call: that row reports ``updateAvailable=false`` with
an ``error`` string.

The pure parts (``parse_image_ref``, ``compare_digests``, ``DigestCache``) have
no I/O so ``tests/test_updates.py`` covers them offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from aiohttp import web

from kowan.errors import BadGateway, NotFound, Validation
from kowan.web import (
    ops_error_response,
    SERVICES_KEY,
    api_route,
    error_response,
    json_response,
    now_rfc3339,
    require_role,
)

log = logging.getLogger("kowan.api.updates")

# Remote digests are cached per image ref for this long (successful lookups).
CACHE_TTL = 600.0
# Failed lookups are retried sooner so a transient registry blip clears quickly.
ERROR_TTL = 60.0
# At most this many registry lookups / docker inspects run at once per request.
MAX_CONCURRENCY = 4

DOCKER_HUB = "docker.io"
DOCKER_HUB_API = "registry-1.docker.io"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageRef:
    """A parsed image reference.

    ``registry`` is the canonical host (``docker.io`` for Docker Hub),
    ``api_host`` the Registry v2 host to talk to (``registry-1.docker.io`` for
    Hub), ``repository`` the normalised path (``library/nginx`` for official Hub
    images), ``tag`` defaults to ``latest`` and ``digest`` is set when the
    reference pins one (``name@sha256:...``).
    """

    registry: str
    repository: str
    tag: str
    digest: str = ""

    @property
    def api_host(self) -> str:
        return DOCKER_HUB_API if self.registry == DOCKER_HUB else self.registry

    @property
    def name(self) -> str:
        """Canonical ``registry/repository`` used to match RepoDigests."""
        return f"{self.registry}/{self.repository}"

    @property
    def key(self) -> str:
        """Cache key: canonical ``registry/repository:tag``."""
        return f"{self.name}:{self.tag}"

    @property
    def pull_ref(self) -> str:
        """A ref safe to pull: always carries an explicit tag (an untagged
        ``fromImage`` would pull every tag of the repository)."""
        if self.registry == DOCKER_HUB:
            repo = self.repository
            if repo.startswith("library/"):
                repo = repo[len("library/"):]
            return f"{repo}:{self.tag}"
        return f"{self.registry}/{self.repository}:{self.tag}"


def parse_image_ref(ref: str) -> ImageRef:
    """Parse ``ref`` into an :class:`ImageRef` (docker's normalisation rules).

    Raises ``ValueError`` for an empty ref or a bare image id (``sha256:...`` /
    a 64-hex id), which has no registry to compare against.

        "nginx"                  -> docker.io / library/nginx : latest
        "myorg/app:1.2"          -> docker.io / myorg/app : 1.2
        "ghcr.io/o/a:v1"         -> ghcr.io / o/a : v1
        "host:5000/x"            -> host:5000 / x : latest
        "nginx@sha256:abc"       -> digest="sha256:abc"
    """
    s = (ref or "").strip()
    if not s:
        raise ValueError("empty image reference")
    if s.startswith("sha256:") or (len(s) == 64 and all(c in "0123456789abcdef" for c in s)):
        raise ValueError("image referenced by id")

    digest = ""
    at = s.find("@")
    if at >= 0:
        s, digest = s[:at], s[at + 1:]

    registry = DOCKER_HUB
    rest = s
    slash = s.find("/")
    if slash >= 0:
        first = s[:slash]
        if first == "localhost" or "." in first or ":" in first:
            registry = first
            rest = s[slash + 1:]
    if registry in ("index.docker.io", "registry-1.docker.io"):
        registry = DOCKER_HUB

    tag = "latest"
    colon = rest.rfind(":")
    if colon > rest.rfind("/"):
        tag = rest[colon + 1:] or "latest"
        rest = rest[:colon]
    rest = rest.strip("/").lower() if registry == DOCKER_HUB else rest.strip("/")
    if not rest:
        raise ValueError(f"invalid image reference {ref!r}")
    if registry == DOCKER_HUB and "/" not in rest:
        rest = "library/" + rest
    return ImageRef(registry=registry, repository=rest, tag=tag, digest=digest)


def _repo_digest_name(entry: str) -> Tuple[str, str]:
    """Split a RepoDigests entry into (canonical name, digest); ("", "") when
    malformed."""
    name, sep, dig = str(entry).partition("@")
    if not sep or not dig:
        return "", ""
    try:
        parsed = parse_image_ref(name)
    except ValueError:
        return "", ""
    return parsed.name, dig


def compare_digests(
    repo_digests: Iterable[str], ref: ImageRef, remote: str
) -> Tuple[str, bool]:
    """Compare a local image's ``RepoDigests`` with the registry digest.

    Returns ``(local_digest, update_available)``. The local digest is the entry
    for the same repository (canonical match, so ``nginx@`` matches
    ``docker.io/library/nginx``), falling back to the first entry. An update is
    available only when both digests are known and the remote one is NOT among
    the local image's digests for that repository (an image can carry several).
    """
    matching: List[str] = []
    fallback = ""
    for entry in repo_digests or []:
        name, dig = _repo_digest_name(entry)
        if not dig:
            continue
        if not fallback:
            fallback = dig
        if name == ref.name:
            matching.append(dig)
    local = matching[0] if matching else fallback
    if not local or not remote:
        return local, False
    pool = matching or [fallback]
    return local, remote not in pool


@dataclass
class CacheEntry:
    digest: str
    error: str
    checked_at: str
    stored: float


class DigestCache:
    """In-memory TTL cache of remote digests keyed by image ref.

    Concurrent lookups of the same key share one in-flight resolution, so a host
    with ten ``nginx:latest`` containers costs a single registry round trip.
    ``clock`` is injectable for tests.
    """

    def __init__(
        self,
        ttl: float = CACHE_TTL,
        error_ttl: float = ERROR_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.error_ttl = error_ttl
        self._clock = clock
        self._entries: Dict[str, CacheEntry] = {}
        self._inflight: Dict[str, "asyncio.Future[CacheEntry]"] = {}

    def get(self, key: str) -> Optional[CacheEntry]:
        e = self._entries.get(key)
        if e is None:
            return None
        ttl = self.error_ttl if e.error else self.ttl
        if self._clock() - e.stored > ttl:
            self._entries.pop(key, None)
            return None
        return e

    def put(self, key: str, digest: str, error: str = "") -> CacheEntry:
        now = self._clock()
        # Expired entries are as good as absent: drop them here, or every image
        # ref ever checked (old tags, removed images) would stay in memory.
        for k in [k for k, old in self._entries.items() if now - old.stored > (self.error_ttl if old.error else self.ttl)]:
            del self._entries[k]
        e = CacheEntry(digest=digest, error=error, checked_at=now_rfc3339(), stored=now)
        self._entries[key] = e
        return e

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    async def resolve(
        self, key: str, fetch: Callable[[], Awaitable[str]], refresh: bool = False
    ) -> CacheEntry:
        """Return the cached entry for ``key`` or run ``fetch`` (once, even under
        concurrent callers). A ``fetch`` exception is cached as an error entry."""
        if not refresh:
            cached = self.get(key)
            if cached is not None:
                return cached
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        fut: "asyncio.Future[CacheEntry]" = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            try:
                digest = await fetch()
                entry = self.put(key, digest)
            except Exception as err:  # noqa: BLE001 - reported per row
                entry = self.put(key, "", str(err) or err.__class__.__name__)
            fut.set_result(entry)
            return entry
        finally:
            self._inflight.pop(key, None)


# ---------------------------------------------------------------------------
# Registry / docker glue
# ---------------------------------------------------------------------------

_CACHE = DigestCache()


async def _remote_digest(store: Any, ref: ImageRef) -> str:
    """Resolve the registry's manifest digest for ``ref`` via the docker-image
    trigger's resolver, with stored-registry credentials when one matches."""
    from kowan.agentupdate import _registry_creds
    from kowan.triggers.dockerimage import _new_docker_image

    username, password = await _registry_creds(store, ref.registry)
    poller = _new_docker_image(
        {
            "registry": ref.api_host,
            "repository": ref.repository,
            "tag": ref.tag,
            "username": username,
            "password": password,
        }
    )
    # evaluate(state="") returns the current digest without "firing".
    _, digest = await poller.evaluate({}, "")
    return digest


def friendly_registry_error(ref: ImageRef, err: Exception) -> str:
    """Shorten the resolver's verbose errors for the UI badge tooltip."""
    msg = str(err) or err.__class__.__name__
    if "unexpected status 404" in msg:
        return f"{ref.pull_ref} not found in {ref.registry} (local-only image?)"
    if "unexpected status 401" in msg or "unexpected status 403" in msg:
        return f"{ref.registry}: access denied for {ref.repository} (private or local-only image; add registry credentials)"
    return msg


def _container_name(c: Any) -> str:
    names = getattr(c, "names", None) or []
    return (names[0] if names else getattr(c, "id", "")[:12]).lstrip("/")


async def _drain_pull(stream: Any) -> None:
    """Consume a pull's NDJSON progress stream, raising on an ``error`` line."""
    buf = b""
    err = ""
    try:
        async for chunk in stream:
            buf += chunk if isinstance(chunk, bytes) else str(chunk).encode()
            *lines, buf = buf.split(b"\n")
            for line in lines:
                err = _pull_error(line) or err
        err = _pull_error(buf) or err
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            res = close()
            if hasattr(res, "__await__"):
                await res
    if err:
        raise BadGateway("pull: " + err)


def _pull_error(line: bytes) -> str:
    line = line.strip()
    if not line:
        return ""
    try:
        msg = json.loads(line)
    except ValueError:
        return ""
    if isinstance(msg, dict) and msg.get("error"):
        return str(msg["error"])
    return ""


def _strip_image_defaults(spec: Any, image_cfg: Dict[str, Any], container_cfg: Dict[str, Any]) -> None:
    """Make the reconstructed spec inherit the NEW image's defaults.

    ``container_spec`` copies Config.Env / Config.Labels, which include values
    baked into the OLD image; re-specifying them would pin those values on the
    new image. Drop env entries and labels identical to the old image's, and put
    back the compose/kowan labels ``container_spec`` filters out so compose keeps
    tracking the container."""
    img_env = set(image_cfg.get("Env") or [])
    if img_env:
        spec.env = [
            kv for kv in spec.env
            if f"{kv.key}={kv.value}" not in img_env
        ]
    img_labels = image_cfg.get("Labels") or {}
    labels = {k: v for k, v in (spec.labels or {}).items() if img_labels.get(k) != v}
    for k, v in (container_cfg.get("Labels") or {}).items():
        if (k.startswith("com.docker.compose.") or k.startswith("kowan.")) and img_labels.get(k) != v:
            labels[k] = v
    spec.labels = labels


async def recreate_with_image(ops: Any, cid: str, insp: Dict[str, Any], image_ref: str) -> str:
    """Recreate container ``cid`` from ``image_ref`` (already pulled), keeping its
    config. Returns the new container id.

    Local daemon: the raw clone in ``LocalClient.clone_recreate`` (feeds inspect's
    Config/HostConfig back to create, so every flag survives; on failure the
    original is put back). Remote agents (no
    raw clone op on the tunnel): ``container_spec`` round-trip, stop + remove,
    create; on a failed create the old image id is used to put the container
    back before raising."""
    state = (insp.get("State") or {})
    was_running = bool(state.get("Running"))
    name = (insp.get("Name") or cid).lstrip("/")

    clone = getattr(ops, "clone_recreate", None)
    if callable(clone):
        return await clone(cid, image_ref, start=was_running)

    spec = await ops.container_spec(cid)
    old_image_id = insp.get("Image") or ""
    rollback = spec.model_copy(deep=True)
    rollback.image = old_image_id or spec.image
    try:
        old_img = await ops.inspect_image(old_image_id) if old_image_id else {}
    except Exception:  # noqa: BLE001 - keep everything when unknown
        old_img = {}
    _strip_image_defaults(spec, (old_img or {}).get("Config") or {}, insp.get("Config") or {})
    spec.image = image_ref
    spec.pull_image = False
    rollback.pull_image = False

    try:
        await ops.stop_container(cid)
    except Exception:  # noqa: BLE001 - already stopped
        pass
    await ops.remove_container(cid, True)
    try:
        return await ops.create_container(name, spec, was_running)
    except Exception as err:  # noqa: BLE001
        try:
            await ops.create_container(name, rollback, was_running)
            note = "previous container restored"
        except Exception as err2:  # noqa: BLE001
            note = f"restore failed: {err2}"
        raise BadGateway(f"recreate {name}: {err} ({note})") from err


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _svc(request: web.Request) -> Any:
    return request.app[SERVICES_KEY]


def _ops_for(svc: Any, host_id: str) -> Any:
    try:
        host = svc.store.get_host(host_id)
    except Exception as err:  # noqa: BLE001
        raise NotFound("host not found") from err
    try:
        return svc.docker.ops(host)
    except Exception as err:  # noqa: BLE001
        raise BadGateway("resolve host client: " + str(err)) from err


async def check_host(svc: Any, ops: Any, refresh: bool = False, cache: DigestCache = _CACHE) -> List[Dict[str, Any]]:
    """Build the per-container update report for one host (never raises per row)."""
    containers = await ops.list_containers(True)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    image_cache: Dict[str, "asyncio.Task[Dict[str, Any]]"] = {}

    async def inspect_image(image_id: str) -> Dict[str, Any]:
        task = image_cache.get(image_id)
        if task is None:
            async def run() -> Dict[str, Any]:
                async with sem:
                    return await ops.inspect_image(image_id) or {}
            task = asyncio.ensure_future(run())
            image_cache[image_id] = task
        return await task

    async def one(c: Any) -> Dict[str, Any]:
        image = getattr(c, "image", "") or ""
        row: Dict[str, Any] = {
            "containerId": getattr(c, "id", ""),
            "containerName": _container_name(c),
            "image": image,
            "localDigest": "",
            "remoteDigest": "",
            "updateAvailable": False,
            "checkedAt": now_rfc3339(),
        }
        try:
            ref = parse_image_ref(image)
        except ValueError as err:
            row["error"] = str(err)
            return row
        if ref.digest:
            row["localDigest"] = ref.digest
            row["error"] = "image pinned by digest"
            return row
        try:
            async with sem:
                insp = await ops.inspect_container(row["containerId"])
            img = await inspect_image((insp or {}).get("Image") or image)
        except Exception as err:  # noqa: BLE001
            row["error"] = f"inspect image: {err}"
            return row
        repo_digests = img.get("RepoDigests") or []
        if not repo_digests:
            row["error"] = "local image (no registry digest)"
            return row

        async def fetch() -> str:
            async with sem:
                try:
                    return await _remote_digest(svc.store, ref)
                except Exception as err:  # noqa: BLE001
                    raise ValueError(friendly_registry_error(ref, err)) from err

        entry = await cache.resolve(ref.key, fetch, refresh=refresh)
        row["checkedAt"] = entry.checked_at
        row["remoteDigest"] = entry.digest
        local, avail = compare_digests(repo_digests, ref, entry.digest)
        row["localDigest"] = local
        row["updateAvailable"] = avail
        if entry.error:
            row["error"] = entry.error
        return row

    return list(await asyncio.gather(*(one(c) for c in containers)))


def register(app: web.Application, svc: Any) -> None:
    api_route(app, "GET", "/hosts/{hostID}/updates", _list_updates)
    api_route(app, "GET", "/hosts/{hostID}/images/{id}/update", _image_update)
    api_route(
        app,
        "POST",
        "/hosts/{hostID}/containers/{id}/update",
        require_role("operator")(_update_container),
    )
    api_route(app, "POST", "/deployments/{id}/update", require_role("operator")(_update_deployment))
    api_route(app, "POST", "/hosts/{hostID}/stacks/{project}/update", require_role("operator")(_update_stack))


async def _list_updates(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    ops = _ops_for(svc, request.match_info["hostID"])
    refresh = (request.query.get("refresh") or "").lower() in ("1", "true", "yes")
    try:
        rows = await check_host(svc, ops, refresh=refresh)
    except Exception as err:  # noqa: BLE001 - list_containers failed
        return ops_error_response(err)
    return json_response(rows)


async def check_image(svc: Any, ops: Any, image_id: str, cache: DigestCache = _CACHE) -> List[Dict[str, Any]]:
    """Ask the registry about every tag of one image now (never raises per tag)."""
    img = await ops.inspect_image(image_id) or {}
    repo_digests = img.get("RepoDigests") or []
    tags = [t for t in (img.get("RepoTags") or []) if t and t != "<none>:<none>"]

    async def one(tag: str) -> Dict[str, Any]:
        row: Dict[str, Any] = {"tag": tag, "localDigest": "", "remoteDigest": "", "updateAvailable": False, "checkedAt": now_rfc3339()}
        try:
            ref = parse_image_ref(tag)
        except ValueError as err:
            row["error"] = str(err)
            return row
        if not repo_digests:
            row["error"] = "local image (no registry digest)"
            return row

        async def fetch() -> str:
            try:
                return await _remote_digest(svc.store, ref)
            except Exception as err:  # noqa: BLE001
                raise ValueError(friendly_registry_error(ref, err)) from err

        entry = await cache.resolve(ref.key, fetch, refresh=True)
        row["checkedAt"] = entry.checked_at
        row["remoteDigest"] = entry.digest
        row["localDigest"], row["updateAvailable"] = compare_digests(repo_digests, ref, entry.digest)
        if entry.error:
            row["error"] = entry.error
        return row

    return list(await asyncio.gather(*(one(t) for t in tags)))


async def _image_update(request: web.Request) -> web.StreamResponse:
    svc = _svc(request)
    ops = _ops_for(svc, request.match_info["hostID"])
    try:
        rows = await check_image(svc, ops, request.match_info["id"])
    except Exception as err:  # noqa: BLE001 - inspect failed (gone, host down)
        return ops_error_response(err)
    return json_response(rows)


async def _update_container(request: web.Request) -> web.StreamResponse:
    """Pull the tag again and switch the container to it, safely.

    Goes through ``kowan.autoupdate.engine.apply_container_update``: optional
    scan gate on the NEW image (policy: body ``scanBlock`` > label
    ``kowan.scan.block`` > stored container policy > global default), recreate,
    health verification and automatic rollback. A blocked update answers 409
    and leaves the container untouched; a rolled-back one answers 502. Every
    outcome is recorded in the auto-update history (trigger ``manual``).

    A container of a Kowan deployment is not recreated on its own (the next
    deploy would undo it): its whole deployment is updated instead.
    """
    svc = _svc(request)
    host_id = request.match_info["hostID"]
    ops = _ops_for(svc, host_id)
    cid = request.match_info["id"]
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            from kowan.web import read_json

            data = await read_json(request)
            body = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 - no/invalid body: defaults
            body = {}
    try:
        insp = await ops.inspect_container(cid)
    except Exception as err:  # noqa: BLE001
        return error_response(502, f"inspect container: {err}")
    insp = insp or {}
    dep = _owning_deployment(svc, host_id, (insp.get("Config") or {}).get("Labels") or {})
    if dep is not None:
        return await _deployment_update_response(svc, dep)
    payload, status = await update_container_now(svc, host_id, ops, cid, insp, str(body.get("scanBlock") or ""))
    return json_response(payload, status=status)


def _owning_deployment(svc: Any, host_id: str, labels: Dict[str, str]) -> Any:
    """The Kowan deployment a container belongs to, or None."""
    from kowan.workloads import compose_deployments, workload_of

    _, _, dep_id = workload_of(labels, host_id, "", compose_deployments(svc.store))
    if not dep_id:
        return None
    try:
        return svc.store.get_deployment(dep_id)
    except Exception:  # noqa: BLE001 - labels of a deleted deployment
        return None


async def update_container_now(svc: Any, host_id: str, ops: Any, cid: str, insp: Dict[str, Any],
                               scan_block: str = "") -> Tuple[Dict[str, Any], int]:
    """Update one container to the newest image of its tag; returns the
    payload and the HTTP status (200 updated or up to date, 409 blocked, 502
    rolled back or failed)."""
    cfg = insp.get("Config") or {}
    name = str(insp.get("Name") or cid).lstrip("/")
    labels = cfg.get("Labels") or {}
    au = getattr(svc, "autoupdater", None)
    state = svc.store.get_autoupdate_state("container", host_id, name)
    from kowan.autoupdate.runner import tracked_ref

    image = tracked_ref(cfg.get("Image") or "", state)
    try:
        ref = parse_image_ref(image)
    except ValueError as err:
        raise Validation(f"cannot update {image or cid}: {err}") from err
    if ref.digest:
        raise Validation("image is pinned by digest; change the reference to update")

    from kowan.autoupdate import engine
    from kowan.autoupdate.policy import resolve_policy
    from kowan.registry import docker_auth_for
    from kowan.scan.gate import parse_block_policy

    stored = svc.store.get_autoupdate_policy("container", host_id, name)
    defaults = au.defaults() if au is not None else {}
    eff = resolve_policy(labels, stored, defaults)
    gate = eff.scan_block
    if scan_block:
        try:
            gate = parse_block_policy(scan_block)
        except ValueError as err:
            raise Validation(str(err)) from err

    auth = await docker_auth_for(svc.store, ref.pull_ref)

    async def pull() -> None:
        try:
            await _drain_pull(await ops.pull_image(ref.pull_ref, auth))
        except BadGateway:
            raise
        except Exception as err:  # noqa: BLE001
            raise BadGateway(f"pull {ref.pull_ref}: {err}") from err

    scan = au.scan_fn(host_id, ops) if au is not None else None
    verify = au.verify_options() if au is not None else engine.VerifyOptions()
    verify.stable = min(verify.stable, 5.0)
    from kowan.autoupdate.runner import now_utc

    started = now_utc()
    out = await engine.apply_container_update(
        ops, cid, image_ref=image, pull=pull, scan=scan, gate_policy=gate, verify=verify, insp=insp,
    )
    if au is not None:
        try:
            au._finish_container(
                svc.store.get_host(host_id), name, image, eff, "manual", out, {"image": image},
                started, out.from_digest, out.to_digest, eff.notify,
            )
        except Exception as err:  # noqa: BLE001 - history is best effort
            log.warning("record manual update of %s: %s", name, err)
    payload = {"id": out.container_id or cid, "name": name, "image": image, **out.to_dict()}
    if out.outcome == engine.OUTCOME_BLOCKED:
        return {**payload, "error": out.message}, 409
    if out.outcome in (engine.OUTCOME_ROLLED_BACK, engine.OUTCOME_FAILED):
        return {**payload, "error": out.message}, 502
    log.info("container %s updated to %s (new id %s): %s", cid[:12], image, (out.container_id or "")[:12], out.message)
    return payload, 200


async def _deployment_update_response(svc: Any, dep: Any) -> web.StreamResponse:
    from kowan.auth import authorize as authz

    authz.check_deployment(dep, authz.LEVEL_OPERATE)
    au = getattr(svc, "autoupdater", None)
    if au is None:
        return error_response(503, "the updater is not running")
    try:
        res = await au.update_deployment_now(dep)
    except ValueError as err:
        raise Validation(str(err)) from err
    result = str(res.get("result") or "")
    payload = {"deploymentId": dep.id, "deployment": dep.name, "outcome": result,
               "message": res.get("message") or _deployment_message(svc, dep.id, result)}
    if result == "blocked":
        return json_response({**payload, "error": payload["message"]}, status=409)
    if result in ("rolled_back", "failed", "error"):
        return json_response({**payload, "error": payload["message"]}, status=502)
    if result == "busy":
        return json_response({**payload, "error": "an update of this deployment is already running"}, status=409)
    return json_response(payload, status=200)


def _deployment_message(svc: Any, dep_id: str, result: str) -> str:
    try:
        return str(svc.store.get_autoupdate_state("deployment", "", dep_id).get("lastMessage") or result)
    except Exception:  # noqa: BLE001
        return result


async def _update_deployment(request: web.Request) -> web.StreamResponse:
    """POST /deployments/{id}/update: every service of the deployment to the
    newest image of its tag, as one redeploy, verified and rolled back on
    failure."""
    svc = _svc(request)
    try:
        dep = svc.store.get_deployment(request.match_info["id"])
    except Exception:  # noqa: BLE001
        return error_response(404, "deployment not found")
    return await _deployment_update_response(svc, dep)


async def _update_stack(request: web.Request) -> web.StreamResponse:
    """POST /hosts/{hostID}/stacks/{project}/update: update the containers of
    a compose project or swarm stack Kowan does not manage that have a newer
    image, one by one. Kowan has no compose file for it, so each container is
    recreated with its own configuration (like the per-container Update)."""
    from kowan.workloads import stack_of

    svc = _svc(request)
    host_id = request.match_info["hostID"]
    project = request.match_info["project"]
    ops = _ops_for(svc, host_id)
    try:
        rows = await check_host(svc, ops, refresh=True)
    except Exception as err:  # noqa: BLE001
        return error_response(502, f"check updates: {err}")
    containers = {c.id: c for c in await ops.list_containers(True)}
    targets = []
    for r in rows:
        c = containers.get(r.get("containerId", ""))
        if c is None or stack_of(c.labels or {}) != project:
            continue
        if _owning_deployment(svc, host_id, c.labels or {}) is not None:
            return error_response(409, f"{project} is a Kowan deployment; update the deployment")
        if r.get("updateAvailable"):
            targets.append(c)
    results, worst = [], 200
    for c in targets:
        try:
            insp = await ops.inspect_container(c.id) or {}
            payload, status = await update_container_now(svc, host_id, ops, c.id, insp)
        except Exception as err:  # noqa: BLE001
            payload, status = {"id": c.id, "error": str(err)}, 502
        results.append({**payload, "status": status})
        worst = max(worst, status)
    return json_response({"project": project, "updated": sum(1 for r in results if r.get("outcome") == "updated"),
                          "results": results}, status=200 if worst == 200 else 207)
