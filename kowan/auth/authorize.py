"""Central authorization: global roles x teams x resource-scoped grants.

One function decides every access question::

    authorize(principal, level, resource) -> bool

and everything else in this module is plumbing that makes sure it is asked.

Model (docs/permissions.md)
---------------------------
* Global roles stay the **ceiling**: ``viewer`` may at most *read*, ``operator``
  may at most *manage* (operate + create/delete, i.e. what operators can do
  today), ``admin`` bypasses every check.
* A principal is **scoped** once it belongs to a team or holds a grant of its
  own (an API key: once it is bound to a team or carries narrowed scopes). An
  unscoped principal is limited by its role only, exactly as before teams
  existed, so an install without teams behaves unchanged.
* A scoped principal may do ``level`` on ``resource`` only when one of its
  grants (direct, or via a team) matches the resource at that level or higher,
  and the role ceiling allows the level.
* Levels: ``read`` < ``operate`` (start/stop/restart/exec, deploy) <
  ``manage`` (create, update, delete).
* Scopes: ``all_hosts``; ``host`` (one host); ``deployment``; ``stack`` (compose
  project label on one host); ``labels`` (``key=value`` on containers, volumes
  and networks, optionally restricted to one host).

Enforcement points
------------------
* The auth middleware binds a :class:`RequestAuthz` for every authenticated
  request in a context variable (MCP runs through the same middleware).
* ``docker.Provider.ops(host)`` – the single place every API module, MCP tool,
  deployment and workflow resolves a host to Docker ``Ops`` – calls
  :func:`wrap_ops`. For a scoped principal without host-wide ``manage`` it
  returns :class:`ScopedOps`, a proxy that checks each Ops call (host-level for
  images/system/prune/events, object-level by labels for containers, volumes
  and networks) and filters list results. Unknown Ops methods fail closed.
* Store-backed objects (hosts, deployments, alerts, workflows) are checked or
  filtered by the API modules with the helpers below (:func:`check_deployment`,
  :func:`filter_hosts`, ...).
* Code with no bound context (background workers, webhooks, unit tests that
  build Ops directly) is not restricted, matching the pre-teams behaviour.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger("kowan.auth.authorize")

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

LEVEL_READ = "read"
LEVEL_OPERATE = "operate"
LEVEL_MANAGE = "manage"
LEVELS = (LEVEL_READ, LEVEL_OPERATE, LEVEL_MANAGE)
_RANK = {LEVEL_READ: 1, LEVEL_OPERATE: 2, LEVEL_MANAGE: 3}

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
# The highest level each global role may ever reach (the ceiling).
ROLE_CEILING = {ROLE_VIEWER: LEVEL_READ, ROLE_OPERATOR: LEVEL_MANAGE, ROLE_ADMIN: LEVEL_MANAGE}

SCOPE_ALL_HOSTS = "all_hosts"
SCOPE_HOST = "host"
SCOPE_DEPLOYMENT = "deployment"
SCOPE_STACK = "stack"
SCOPE_LABELS = "labels"
# Every host in a group (target = the tag), as host grants: resolved each time
# a principal loads, so tagging a host extends the grant to it at once.
SCOPE_HOST_TAG = "host_tag"
SCOPES = (SCOPE_ALL_HOSTS, SCOPE_HOST, SCOPE_DEPLOYMENT, SCOPE_STACK, SCOPE_LABELS, SCOPE_HOST_TAG)

KIND_HOST = "host"
KIND_CONTAINER = "container"
KIND_VOLUME = "volume"
KIND_NETWORK = "network"
KIND_IMAGE = "image"
KIND_DEPLOYMENT = "deployment"
KIND_STACK = "stack"
KIND_GLOBAL = "global"  # fleet-wide objects: workflows, global alert rules
_LABELLED = (KIND_CONTAINER, KIND_VOLUME, KIND_NETWORK)

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
SWARM_STACK_LABEL = "com.docker.stack.namespace"
SWARM_SERVICE_LABEL = "com.docker.swarm.service.name"
DEPLOYMENT_ID_LABEL = "kowan.deployment.id"


def level_rank(level: str) -> int:
    return _RANK.get(level or "", 0)


def valid_level(level: str) -> bool:
    return level in _RANK


def role_ceiling(role: str) -> str:
    return ROLE_CEILING.get(role or "", LEVEL_READ)


def parse_selector(target: str) -> Tuple[str, str]:
    """Split a ``key=value`` label selector (value may be empty)."""
    key, sep, value = (target or "").partition("=")
    key = key.strip()
    if not key or not sep:
        raise ValueError("label selector must be key=value")
    return key, value.strip()


# ---------------------------------------------------------------------------
# principal + resource
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    """An expanded grant as seen by :func:`authorize`."""

    scope: str
    level: str
    host_id: str = ""
    target: str = ""
    id: str = ""
    # Where the grant came from: "user", "apikey" or "team:<team id>".
    source: str = ""
    # For deployment grants: the deployment's compose project / resource name,
    # resolved at principal load so its containers and stack match too.
    resource_name: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "hostId": self.host_id,
            "target": self.target,
            "level": self.level,
            "source": self.source,
        }


@dataclass
class Principal:
    id: str
    username: str
    role: str
    kind: str = "user"  # "user" | "apikey"
    key_id: str = ""
    team_ids: Tuple[str, ...] = ()
    grants: Tuple[Grant, ...] = ()
    scoped: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN and not (self.kind == "apikey" and self.scoped)


@dataclass
class Resource:
    kind: str
    host_id: str = ""
    id: str = ""
    labels: Mapping[str, str] = field(default_factory=dict)
    stack: str = ""

    def effective_stack(self) -> str:
        if self.stack:
            return self.stack
        labels = self.labels or {}
        return (labels.get(COMPOSE_PROJECT_LABEL) or labels.get(SWARM_STACK_LABEL)
                or labels.get(SWARM_SERVICE_LABEL) or "")


def _grant_matches(g: Grant, r: Resource) -> bool:
    if g.scope == SCOPE_ALL_HOSTS:
        return True
    if r.kind == KIND_GLOBAL:
        return False
    if g.scope == SCOPE_HOST:
        return bool(r.host_id) and g.host_id == r.host_id
    if r.kind in (KIND_HOST, KIND_IMAGE):
        # Host-level objects need a host-wide grant.
        return False
    if g.scope == SCOPE_DEPLOYMENT:
        if r.kind == KIND_DEPLOYMENT:
            return r.id == g.target
        if g.host_id and r.host_id != g.host_id:
            return False
        if (r.labels or {}).get(DEPLOYMENT_ID_LABEL) == g.target:
            return True
        return bool(g.resource_name) and r.effective_stack() == g.resource_name
    if g.scope == SCOPE_STACK:
        if r.host_id != g.host_id:
            return False
        return bool(g.target) and r.effective_stack() == g.target
    if g.scope == SCOPE_LABELS:
        if r.kind not in _LABELLED:
            return False
        if g.host_id and r.host_id != g.host_id:
            return False
        try:
            key, value = parse_selector(g.target)
        except ValueError:
            return False
        labels = r.labels or {}
        return key in labels and labels.get(key, "") == value
    return False


def authorize(principal: Optional[Principal], level: str, resource: Resource) -> bool:
    """Decide whether ``principal`` may perform ``level`` on ``resource``."""
    if principal is None:
        return False
    if principal.is_admin:
        return True
    want = level_rank(level)
    if want == 0 or want > level_rank(role_ceiling(principal.role)):
        return False
    if not principal.scoped:
        return True
    for g in principal.grants:
        if level_rank(g.level) >= want and _grant_matches(g, resource):
            return True
    return False


def touches_host(principal: Optional[Principal], host_id: str) -> bool:
    """Whether the principal may see ``host_id`` at all (any grant on it)."""
    if principal is None:
        return False
    if principal.is_admin or not principal.scoped:
        return True
    for g in principal.grants:
        if g.scope == SCOPE_ALL_HOSTS:
            return True
        if g.scope == SCOPE_LABELS and not g.host_id:
            return True
        if g.host_id and g.host_id == host_id:
            return True
    return False


def host_level(principal: Optional[Principal], host_id: str) -> int:
    """Highest host-wide level rank the principal has on ``host_id`` (0 = none)."""
    if principal is None:
        return 0
    ceiling = level_rank(role_ceiling(principal.role))
    if principal.is_admin:
        return _RANK[LEVEL_MANAGE]
    if not principal.scoped:
        return ceiling
    best = 0
    for g in principal.grants:
        if g.scope == SCOPE_ALL_HOSTS or (g.scope == SCOPE_HOST and g.host_id == host_id):
            best = max(best, level_rank(g.level))
    return min(best, ceiling)


# ---------------------------------------------------------------------------
# loading a principal from the store
# ---------------------------------------------------------------------------


def _expand(rows: Iterable[Any], source: str, store: Any, dep_cache: Dict[str, Tuple[str, str]]) -> List[Grant]:
    out: List[Grant] = []
    for r in rows:
        host_id, resource_name = r.host_id, ""
        if r.scope_type == SCOPE_HOST_TAG:
            try:
                tagged = store.hosts_with_tag(r.target)
            except Exception:  # noqa: BLE001 - fail closed: no host
                tagged = []
            out += [Grant(scope=SCOPE_HOST, level=r.level, host_id=h, target="", id=r.id, source=source,
                          resource_name=f"group {r.target}") for h in tagged]
            continue
        if r.scope_type == SCOPE_DEPLOYMENT:
            info = dep_cache.get(r.target)
            if info is None:
                info = (r.host_id, "")
                try:
                    dep = store.get_deployment(r.target)
                    info = ((dep.targets or [""])[0] if dep.targets else "", dep.resource_name())
                except Exception:  # noqa: BLE001 - dangling grant: id-only match
                    pass
                dep_cache[r.target] = info
            host_id, resource_name = info
        out.append(Grant(scope=r.scope_type, level=r.level, host_id=host_id, target=r.target, id=r.id,
                         source=source, resource_name=resource_name))
    return out


def load_principal(store: Any, user: Any) -> Principal:
    """Build the :class:`Principal` for an authenticated ``CtxUser``."""
    role = getattr(user, "role", "") or ""
    uid = getattr(user, "id", "") or ""
    p = Principal(id=uid, username=getattr(user, "username", "") or "", role=role)
    key_hash = getattr(user, "key_hash", "") or ""
    if key_hash:
        p.kind = "apikey"
    if store is None:
        return p
    snap = store.authz_snapshot()
    dep_cache: Dict[str, Tuple[str, str]] = {}
    if key_hash:
        key = store.get_api_key_by_hash(key_hash)
        if key is None:
            # Revoked between verify and now: no access at all.
            p.scoped = True
            return p
        p.key_id = key.id
        own = snap.grants.get(("apikey", key.id), ())
        grants = _expand(own, "apikey", store, dep_cache)
        if key.team_id:
            p.team_ids = (key.team_id,)
            grants += _expand(snap.grants.get(("team", key.team_id), ()), "team:" + key.team_id, store, dep_cache)
        p.grants = tuple(grants)
        p.scoped = bool(key.team_id) or bool(own)
        return p
    if role == ROLE_ADMIN or snap.empty:
        return p
    team_ids = snap.memberships.get(uid, ())
    own = snap.grants.get(("user", uid), ())
    grants = _expand(own, "user", store, dep_cache)
    for tid in team_ids:
        grants += _expand(snap.grants.get(("team", tid), ()), "team:" + tid, store, dep_cache)
    p.team_ids = tuple(team_ids)
    p.grants = tuple(grants)
    p.scoped = bool(team_ids) or bool(own)
    return p


# ---------------------------------------------------------------------------
# per-request context
# ---------------------------------------------------------------------------


def request_level(method: str, path: str) -> str:
    """Default level a REST request needs, from its method and path."""
    method = (method or "GET").upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        if path.rstrip("/").endswith("/exec") or "/exec/" in path:
            return LEVEL_OPERATE
        return LEVEL_READ
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    if method == "POST" and tail in (
        "start", "stop", "restart", "pause", "unpause", "kill", "deploy", "redeploy", "sync", "run", "resolve",
    ):
        return LEVEL_OPERATE
    return LEVEL_MANAGE


class RequestAuthz:
    """Authorization state of one request (or one MCP tool call)."""

    def __init__(self, store: Any, user: Any, method: str = "GET", path: str = "") -> None:
        self._store = store
        self.user = user
        self.method = method
        self.path = path
        self.default_level = request_level(method, path)
        self._principal: Optional[Principal] = None
        # host id -> level rank granted for the rest of this request after a
        # deployment-level check (a deploy needs to pull images / create its
        # networks on the target host).
        self.elevated: Dict[str, int] = {}

    @property
    def principal(self) -> Principal:
        if self._principal is None:
            try:
                self._principal = load_principal(self._store, self.user)
            except Exception as err:  # noqa: BLE001 - fail closed
                log.error("authz: load principal failed: %s", err)
                self._principal = Principal(
                    id=getattr(self.user, "id", ""), username=getattr(self.user, "username", ""),
                    role=getattr(self.user, "role", ""), scoped=True,
                )
        return self._principal

    @property
    def unrestricted(self) -> bool:
        p = self.principal
        return p.is_admin or not p.scoped

    def allowed(self, level: str, resource: Resource) -> bool:
        if authorize(self.principal, level, resource):
            return True
        if resource.host_id and resource.kind not in (KIND_GLOBAL, KIND_DEPLOYMENT):
            got = self.elevated.get(resource.host_id, 0)
            if got and got >= level_rank(level) and level_rank(level) <= level_rank(role_ceiling(self.principal.role)):
                return True
        return False

    def host_rank(self, host_id: str) -> int:
        return max(host_level(self.principal, host_id), self.elevated.get(host_id, 0))

    def touches(self, host_id: str) -> bool:
        return touches_host(self.principal, host_id) or host_id in self.elevated


_CTX: "contextvars.ContextVar[Optional[RequestAuthz]]" = contextvars.ContextVar("kowan_authz", default=None)


def current() -> Optional[RequestAuthz]:
    return _CTX.get()


def bind(ctx: Optional[RequestAuthz]) -> contextvars.Token:
    return _CTX.set(ctx)


def unbind(token: contextvars.Token) -> None:
    try:
        _CTX.reset(token)
    except ValueError:  # reset from another context (should not happen)
        _CTX.set(None)


def bind_request(request: Any, user: Any) -> contextvars.Token:
    """Bind a :class:`RequestAuthz` for an aiohttp request's principal."""
    store = None
    try:
        from kowan.web.app import SERVICES_KEY  # lazy: kowan.web imports kowan.auth

        svc = request.app.get(SERVICES_KEY) if hasattr(request.app, "get") else None
        store = getattr(svc, "store", None)
    except Exception:  # noqa: BLE001
        store = None
    return bind(RequestAuthz(store, user, request.method, request.rel_url.path))


@contextlib.contextmanager
def default_level(level: str):
    """Temporarily set the default level of the bound context (MCP tools)."""
    ctx = current()
    if ctx is None:
        yield
        return
    prev = ctx.default_level
    ctx.default_level = level
    try:
        yield
    finally:
        ctx.default_level = prev


# ---------------------------------------------------------------------------
# helpers for API modules (all no-ops without a bound context)
# ---------------------------------------------------------------------------


def _forbidden(message: str):
    from kowan.errors import Forbidden

    return Forbidden(message)


def allowed(level: str, resource: Resource) -> bool:
    ctx = current()
    return True if ctx is None else ctx.allowed(level, resource)


def check(level: Optional[str], resource: Resource, message: str = "insufficient permissions") -> None:
    ctx = current()
    if ctx is None:
        return
    if not ctx.allowed(level or ctx.default_level, resource):
        raise _forbidden(message)


def host_visible(host_id: str) -> bool:
    ctx = current()
    return True if ctx is None else ctx.touches(host_id)


def check_host(host_id: str) -> None:
    """Raise Forbidden unless the principal may see the host at all."""
    if not host_visible(host_id):
        raise _forbidden("insufficient permissions for this host")


def filter_hosts(hosts: Sequence[Any]) -> List[Any]:
    ctx = current()
    if ctx is None or ctx.unrestricted:
        return list(hosts)
    return [h for h in hosts if ctx.touches(getattr(h, "id", ""))]


def deployment_resource(dep: Any) -> Resource:
    targets = getattr(dep, "targets", None) or []
    try:
        stack = dep.resource_name()
    except Exception:  # noqa: BLE001
        stack = ""
    return Resource(KIND_DEPLOYMENT, host_id=targets[0] if targets else "", id=getattr(dep, "id", ""), stack=stack)


def deployment_allowed(dep: Any, level: str = LEVEL_READ) -> bool:
    ctx = current()
    if ctx is None:
        return True
    return ctx.allowed(level, deployment_resource(dep))


def check_deployment(dep: Any, level: Optional[str] = None) -> None:
    """Check access to a deployment record; on success the request may act on
    the deployment's host at that level (pull images, create its network...)."""
    ctx = current()
    if ctx is None:
        return
    lvl = level or ctx.default_level
    res = deployment_resource(dep)
    if not ctx.allowed(lvl, res):
        raise _forbidden("insufficient permissions for this deployment")
    if res.host_id and not ctx.unrestricted:
        # Running a deployment (operate) replaces its containers, pulls its
        # images and creates its networks/volumes: that is manage-level work on
        # the target host, bounded by the fixed deployment code path of this
        # request (still capped by the role ceiling in ``allowed``).
        rank = _RANK[LEVEL_MANAGE] if level_rank(lvl) >= _RANK[LEVEL_OPERATE] else level_rank(lvl)
        ctx.elevated[res.host_id] = max(ctx.elevated.get(res.host_id, 0), rank)


def filter_deployments(deps: Sequence[Any]) -> List[Any]:
    ctx = current()
    if ctx is None or ctx.unrestricted:
        return list(deps)
    return [d for d in deps if ctx.allowed(LEVEL_READ, deployment_resource(d))]


def host_allowed(host_id: str, level: str = LEVEL_READ) -> bool:
    """Whether the caller holds ``level`` on the whole of ``host_id`` (role
    ceiling and host-wide grants; a grant on single objects is not enough)."""
    ctx = current()
    return True if ctx is None else ctx.host_rank(host_id) >= level_rank(level)


def global_allowed(level: str = LEVEL_READ) -> bool:
    ctx = current()
    return True if ctx is None else ctx.allowed(level, Resource(KIND_GLOBAL))


def check_global(level: Optional[str] = None, message: str = "insufficient permissions: needs access to all hosts") -> None:
    ctx = current()
    if ctx is None:
        return
    if not ctx.allowed(level or ctx.default_level, Resource(KIND_GLOBAL)):
        raise _forbidden(message)


def host_item_visible(host_id: str) -> bool:
    """For host-tagged records (alerts, notifications, rules): visible when the
    record has no host and the principal sees everything, or the host is seen."""
    ctx = current()
    if ctx is None or ctx.unrestricted:
        return True
    if not host_id:
        return ctx.allowed(LEVEL_READ, Resource(KIND_GLOBAL))
    return ctx.touches(host_id)


def filter_host_items(items: Sequence[Any], attr: str = "host_id") -> List[Any]:
    ctx = current()
    if ctx is None or ctx.unrestricted:
        return list(items)
    return [it for it in items if host_item_visible(getattr(it, attr, "") or "")]


# ---------------------------------------------------------------------------
# the Ops proxy
# ---------------------------------------------------------------------------

# method -> (mode, kind, level)
#   touch   : allowed when the host is visible at all
#   host    : needs a host-wide grant at level
#   hostlist: like host, but returns [] instead of raising
#   list    : filter the returned items by label/stack grants
#   obj     : first positional arg is the object id; check the object
#   create  : check the object described by the arguments (labels)
#   stack   : first positional arg is a compose/swarm project name
_OPS_RULES: Dict[str, Tuple[str, str, str]] = {
    "ping": ("touch", KIND_HOST, LEVEL_READ),
    "swarm_active": ("touch", KIND_HOST, LEVEL_READ),
    "inspect_image": ("touch", KIND_IMAGE, LEVEL_READ),
    "image_history": ("touch", KIND_IMAGE, LEVEL_READ),
    "list_containers": ("list", KIND_CONTAINER, LEVEL_READ),
    "inspect_container": ("obj", KIND_CONTAINER, LEVEL_READ),
    "container_spec": ("obj", KIND_CONTAINER, LEVEL_READ),
    "container_logs": ("obj", KIND_CONTAINER, LEVEL_READ),
    "container_stats": ("obj", KIND_CONTAINER, LEVEL_READ),
    "container_stats_stream": ("obj", KIND_CONTAINER, LEVEL_READ),
    "start_container": ("obj", KIND_CONTAINER, LEVEL_OPERATE),
    "stop_container": ("obj", KIND_CONTAINER, LEVEL_OPERATE),
    "restart_container": ("obj", KIND_CONTAINER, LEVEL_OPERATE),
    "pause_container": ("obj", KIND_CONTAINER, LEVEL_OPERATE),
    "unpause_container": ("obj", KIND_CONTAINER, LEVEL_OPERATE),
    "kill_container": ("obj", KIND_CONTAINER, LEVEL_OPERATE),
    "exec": ("obj", KIND_CONTAINER, LEVEL_OPERATE),
    "exec_attach": ("obj", KIND_CONTAINER, LEVEL_OPERATE),
    "remove_container": ("obj", KIND_CONTAINER, LEVEL_MANAGE),
    "create_container": ("create", KIND_CONTAINER, LEVEL_MANAGE),
    "list_volumes": ("list", KIND_VOLUME, LEVEL_READ),
    "inspect_volume": ("obj", KIND_VOLUME, LEVEL_READ),
    "browse_volume": ("obj", KIND_VOLUME, LEVEL_OPERATE),
    "read_volume_file": ("obj", KIND_VOLUME, LEVEL_OPERATE),
    "download_volume_file": ("obj", KIND_VOLUME, LEVEL_OPERATE),
    "download_volume_archive": ("obj", KIND_VOLUME, LEVEL_OPERATE),
    "upload_volume_file": ("obj", KIND_VOLUME, LEVEL_MANAGE),
    "remove_volume": ("obj", KIND_VOLUME, LEVEL_MANAGE),
    "create_volume": ("create", KIND_VOLUME, LEVEL_MANAGE),
    "list_networks": ("list", KIND_NETWORK, LEVEL_READ),
    "inspect_network": ("obj", KIND_NETWORK, LEVEL_READ),
    "connect_container": ("obj", KIND_NETWORK, LEVEL_MANAGE),
    "disconnect_container": ("obj", KIND_NETWORK, LEVEL_MANAGE),
    "remove_network": ("obj", KIND_NETWORK, LEVEL_MANAGE),
    "create_network": ("create", KIND_NETWORK, LEVEL_MANAGE),
    "compose_up": ("stack", KIND_STACK, LEVEL_MANAGE),
    "compose_down": ("stack", KIND_STACK, LEVEL_MANAGE),
    "swarm_deploy": ("stack", KIND_STACK, LEVEL_MANAGE),
    "swarm_remove": ("stack", KIND_STACK, LEVEL_MANAGE),
    "update_service_image": ("stack", KIND_STACK, LEVEL_MANAGE),
    "list_images": ("hostlist", KIND_IMAGE, LEVEL_READ),
    "list_plugins": ("hostlist", KIND_HOST, LEVEL_READ),
    "list_swarm_nodes": ("host", KIND_HOST, LEVEL_READ),
    "system_info": ("host", KIND_HOST, LEVEL_READ),
    "disk_usage": ("host", KIND_HOST, LEVEL_READ),
    "events": ("host", KIND_HOST, LEVEL_READ),
    "pull_image": ("host", KIND_IMAGE, LEVEL_MANAGE),
    "build_image": ("host", KIND_IMAGE, LEVEL_MANAGE),
    "remove_image": ("host", KIND_IMAGE, LEVEL_MANAGE),
    "install_plugin": ("host", KIND_HOST, LEVEL_MANAGE),
    "remove_plugin": ("host", KIND_HOST, LEVEL_MANAGE),
    "prune_containers": ("host", KIND_HOST, LEVEL_MANAGE),
    "prune_images": ("host", KIND_IMAGE, LEVEL_MANAGE),
    "prune_volumes": ("host", KIND_HOST, LEVEL_MANAGE),
    "prune_networks": ("host", KIND_HOST, LEVEL_MANAGE),
    "prune_build_cache": ("host", KIND_HOST, LEVEL_MANAGE),
}


# Harmless synchronous helpers that touch no Docker state.
_OPS_PASSTHROUGH = frozenset({"invalidate_image_cache"})


def _labels_of(obj: Any) -> Dict[str, str]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        cfg = obj.get("Config")
        if isinstance(cfg, dict) and isinstance(cfg.get("Labels"), dict):
            return cfg["Labels"]
        for k in ("Labels", "labels"):
            if isinstance(obj.get(k), dict):
                return obj[k]
        return {}
    labels = getattr(obj, "labels", None)
    return labels if isinstance(labels, dict) else {}


def _arg(args: tuple, kwargs: dict, index: int, *names: str) -> Any:
    if len(args) > index:
        return args[index]
    for n in names:
        if n in kwargs:
            return kwargs[n]
    return None


class ScopedOps:
    """Proxy over an ``Ops`` that enforces a scoped principal's grants."""

    def __init__(self, inner: Any, host_id: str, ctx: RequestAuthz) -> None:
        self._inner = inner
        self._host_id = host_id
        self._ctx = ctx

    def __repr__(self) -> str:  # pragma: no cover
        return f"ScopedOps({self._inner!r}, host={self._host_id!r})"

    def _deny(self, what: str):
        return _forbidden(f"insufficient permissions: {what}")

    def _host_ok(self, level: str) -> bool:
        return self._ctx.allowed(level, Resource(KIND_HOST, host_id=self._host_id))

    def _obj_ok(self, kind: str, level: str, labels: Mapping[str, str], stack: str = "") -> bool:
        return self._ctx.allowed(level, Resource(kind, host_id=self._host_id, labels=labels or {}, stack=stack))

    async def _object_labels(self, kind: str, ref: Any) -> Dict[str, str]:
        if kind == KIND_CONTAINER:
            return _labels_of(await self._inner.inspect_container(ref))
        if kind == KIND_VOLUME:
            return _labels_of(await self._inner.inspect_volume(ref))
        if kind == KIND_NETWORK:
            return _labels_of(await self._inner.inspect_network(ref))
        return {}

    def __getattr__(self, name: str) -> Any:
        if name == "inner":
            return self  # never hand out the unrestricted Ops underneath
        target = getattr(self._inner, name)
        if name in _OPS_PASSTHROUGH:
            return target
        if name.startswith("_"):
            raise AttributeError(name)
        if not callable(target):
            return target
        rule = _OPS_RULES.get(name)
        if rule is None:
            # Unknown operation: require host-wide manage (fail closed).
            rule = ("host", KIND_HOST, LEVEL_MANAGE)
        mode, kind, level = rule

        async def call(*args: Any, **kwargs: Any) -> Any:
            if not self._ctx.touches(self._host_id):
                raise self._deny("host")
            if mode == "touch":
                return await target(*args, **kwargs)
            if mode in ("host", "hostlist"):
                if self._host_ok(level):
                    return await target(*args, **kwargs)
                if mode == "hostlist":
                    return []
                raise self._deny(f"{name.replace('_', ' ')} needs {level} on the host")
            if self._host_ok(level):
                return await target(*args, **kwargs)
            if mode == "list":
                items = await target(*args, **kwargs)
                return [it for it in (items or []) if self._obj_ok(kind, level, _labels_of(it))]
            if mode == "obj":
                ref = _arg(args, kwargs, 0, "id", "name", "network_id")
                labels = await self._object_labels(kind, ref)
                if not self._obj_ok(kind, level, labels):
                    raise self._deny(f"{level} on this {kind}")
                return await target(*args, **kwargs)
            if mode == "create":
                spec = _arg(args, kwargs, 1, "spec") if kind == KIND_CONTAINER else _arg(args, kwargs, 0, "opts")
                if not self._obj_ok(kind, level, _labels_of(spec)):
                    raise self._deny(f"create {kind} outside your scope")
                return await target(*args, **kwargs)
            if mode == "stack":
                project = _arg(args, kwargs, 0, "project", "stack") or ""
                if not self._obj_ok(KIND_STACK, level, {}, stack=str(project)):
                    raise self._deny(f"{level} on stack {project}")
                return await target(*args, **kwargs)
            raise self._deny(name)  # pragma: no cover

        call.__name__ = name
        return call


def wrap_ops(host: Any, ops: Any) -> Any:
    """Return ``ops`` or a :class:`ScopedOps` proxy for the bound principal."""
    ctx = current()
    if ctx is None or ops is None:
        return ops
    try:
        if ctx.unrestricted:
            return ops
        host_id = getattr(host, "id", "") or ""
        if ctx.host_rank(host_id) >= _RANK[LEVEL_MANAGE]:
            return ops
        return ScopedOps(ops, host_id, ctx)
    except Exception as err:  # noqa: BLE001 - fail closed
        log.error("authz: wrap ops failed: %s", err)
        return ScopedOps(ops, getattr(host, "id", ""), ctx)


def check_host_item(host_id: str, level: Optional[str] = None) -> None:
    """Check a host-tagged config record (alert rule, ...): host-wide access on
    its host, or all-hosts access when it has none."""
    ctx = current()
    if ctx is None:
        return
    lvl = level or ctx.default_level
    res = Resource(KIND_HOST, host_id=host_id) if host_id else Resource(KIND_GLOBAL)
    if not ctx.allowed(lvl, res):
        raise _forbidden("insufficient permissions for this host")


# ---------------------------------------------------------------------------
# records that name their target (schedules, volume backups, auto-update)
# ---------------------------------------------------------------------------

# A target is (kind, host id, ref): a deployment id; a compose project on a
# host; a container or volume name on a host; a ``key=value`` selector on a
# host (KIND_SELECTOR).
Target = Tuple[str, str, str]
KIND_SELECTOR = "selector"


class TargetAccess:
    """Access to store records that point at a workload by name instead of
    holding it: schedules, volume backup jobs and runs, auto-update policies.

    Deployments, stacks and selectors are decided from the record alone.
    Containers and volumes are decided by their labels, read once per host for
    the request through the caller's own (scoped) Docker view; a host-wide grant
    skips that. A record with no host needs all-hosts access, and a target that
    cannot be resolved (gone, host offline) is denied: fail closed.

    Used for lists (:meth:`filter`) and for writes (:meth:`check`), so a scoped
    user can neither see nor create automation that the background runners
    would then carry out, unrestricted, outside their grants.
    """

    def __init__(self, store: Any, docker: Any) -> None:
        self._store = store
        self._docker = docker
        self._ctx = current()
        # (kind, host id) -> {name: labels} of what the caller may read there.
        self._objects: Dict[Tuple[str, str], Dict[str, Dict[str, str]]] = {}

    @property
    def unrestricted(self) -> bool:
        return self._ctx is None or self._ctx.unrestricted

    async def _named(self, kind: str, host_id: str) -> Dict[str, Dict[str, str]]:
        key = (kind, host_id)
        if key not in self._objects:
            named: Dict[str, Dict[str, str]] = {}
            try:
                ops = self._docker.ops(self._store.get_host(host_id))
                if kind == KIND_CONTAINER:
                    for c in await ops.list_containers(True) or []:
                        for n in getattr(c, "names", None) or []:
                            named[str(n).lstrip("/")] = _labels_of(c)
                else:
                    for v in await ops.list_volumes() or []:
                        named[getattr(v, "name", "") or ""] = _labels_of(v)
            except Exception as err:  # noqa: BLE001 - fail closed
                log.debug("authz: list %ss on %s: %s", kind, host_id, err)
            self._objects[key] = named
        return self._objects[key]

    async def allowed(self, target: Target, level: str = LEVEL_READ) -> bool:
        ctx = self._ctx
        if ctx is None or ctx.unrestricted:
            return True
        kind, host_id, ref = target
        if kind == KIND_DEPLOYMENT:
            try:
                dep = self._store.get_deployment(ref)
            except Exception:  # noqa: BLE001 - gone: only fleet-wide access sees it
                return ctx.allowed(level, Resource(KIND_GLOBAL))
            return ctx.allowed(level, deployment_resource(dep))
        if not host_id:
            return ctx.allowed(level, Resource(KIND_GLOBAL))
        if ctx.host_rank(host_id) >= level_rank(level):
            return True
        if not ctx.touches(host_id):
            return False
        if kind == KIND_STACK:
            return ctx.allowed(level, Resource(KIND_STACK, host_id=host_id, stack=ref))
        if kind == KIND_SELECTOR:
            try:
                key, value = parse_selector(ref)
            except ValueError:
                return False
            return ctx.allowed(level, Resource(KIND_VOLUME, host_id=host_id, labels={key: value}))
        if kind in (KIND_CONTAINER, KIND_VOLUME):
            labels = (await self._named(kind, host_id)).get(ref)
            return labels is not None and ctx.allowed(level, Resource(kind, host_id=host_id, labels=labels))
        return False

    async def allowed_all(self, targets: Iterable[Target], level: str = LEVEL_READ) -> bool:
        for t in targets:
            if not await self.allowed(t, level):
                return False
        return True

    async def filter(self, items: Iterable[Any], targets: Any, level: str = LEVEL_READ) -> List[Any]:
        """The items whose every target (``targets(item)``) is allowed."""
        items = list(items)
        if self.unrestricted:
            return items
        return [it for it in items if await self.allowed_all(targets(it), level)]

    async def check(self, targets: Iterable[Target], level: Optional[str] = None,
                    message: str = "insufficient permissions for this target") -> None:
        ctx = self._ctx
        if ctx is None:
            return
        if not await self.allowed_all(targets, level or ctx.default_level):
            raise _forbidden(message)
