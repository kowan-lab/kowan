"""Kowan control-plane server bootstrap.

Wires the whole control plane together
and serves it over aiohttp:

  * loads config, opens the SQLite store, builds the secrets cipher and seeds
    the admin user + the fixed ``local`` host row;
  * builds the shared singletons (event bus, JWT issuer, API-key verifier, the
    edge tunnel Hub, the local Docker client and the per-host Ops Provider) into
    the :class:`kowan.web.Services` DI container;
  * mounts every api module (``register(app, svc)``) plus the monitor api under
    ``/api/v1`` via :func:`kowan.web.create_app`;
  * mounts the PUBLIC edge tunnel endpoint ``GET /edge/connect`` (an aiohttp
    WebSocket the edge agent dials into), the webhook receiver and the SPA;
  * starts the background workers (notifier, monitor; the workflow engine /
    scheduler / poller start themselves via the workflows api's on_startup hook).

Run with ``python3 -m kowan.server``. Honors ``config.addr`` (host:port, default
``:5081``) and optional TLS (``config.tls_cert`` / ``config.tls_key``).

aiohttp's ``web.run_app`` installs the SIGINT/SIGTERM handlers and runs the
app's startup/cleanup callbacks, so each subsystem starts and stops through
``on_startup``/``on_cleanup`` hooks (registered here and inside the workflows
api) rather than through explicitly managed background tasks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from pathlib import Path

from aiohttp import web

from .auth import hash_password, new_api_key_verifier, new_jwt
from .config import ServerConfig, load_or_create_jwt_secret, load_server_config
from .docker import LocalClient, Provider
from .events import MemoryBus
from .secrets import Cipher, ExternalResolver, load_or_create_key
from .store import Store
from .tunnel import Hub, establish_server_side
from .transport.aiohttp_ws import HANDSHAKE_TIMEOUT, MAX_MESSAGE_SIZE, AiohttpWSTransport
from .web import (
    Services,
    add_spa,
    add_webhooks,
    create_app,
)

log = logging.getLogger("kowan.server")

# The fixed id of the local Docker daemon host row.
LOCAL_HOST_ID = "local"


# ---------------------------------------------------------------------------
# The ordered list of api-module register callables mounted under /api/v1.
#
# Order matters only where a
# module attaches a singleton a later one reads (deployments -> workflows), and
# where a specific public hook prefix must be registered before the generic
# /hooks catch-all (add_webhooks is called last, after create_app).
# ---------------------------------------------------------------------------

def _registrars() -> list:
    from .api import (
        apikeys,
        audit,
        authapi,
        backup,
        containers,
        credentials,
        deployments,
        events as events_api,
        exec as exec_api,
        hosts,
        images,
        logs,
        networks,
        plugins as plugins_api,
        registrybrowse,
        serverupdate,
        stats,
        system,
        volumes,
        workflows,
    )
    from .api import updates as updates_api
    from .api import templates as templates_api
    from .api import scan as scan_api
    from .api import autoupdate as autoupdate_api
    from .api import compose_validate as compose_validate_api
    from .api import gitops as gitops_api
    from .api import volbackups as volbackups_api
    from .api import schedules as schedules_api
    from .api import containerfiles as containerfiles_api
    from .api import exposure as exposure_api
    from .api import netmap as netmap_api
    from .monitor import api as monitor_api
    from . import notifications
    from . import mcp as mcp_api
    from .api import teams as teams_api
    from .api import sso as sso_api
    from .api import netaccess as netaccess_api
    from .api import uptime as uptime_api
    from .api import logarchive as logarchive_api
    from .api import workloadres as workloadres_api
    from .api import imports as imports_api
    from .api import inline_secrets as inline_secrets_api
    from .api import features as features_api
    from .api import cleanup as cleanup_api
    from .api import digest as digest_api

    return [
        # platform features on/off: its middleware gates the routes of the rest.
        features_api.register,
        # auth + users + external auth (public login/callback routes included).
        authapi.register,
        # Settings > Network access (UI-managed address allowlists).
        netaccess_api.register,
        # hosts CRUD + edge enrollment.
        hosts.register,
        # Docker ops surfaces (each mounts its own routes against the provider).
        containers.register,
        images.register,
        exec_api.register,
        logs.register,
        stats.register,
        events_api.register,  # docker events: recent history + live WS
        networks.register,
        volumes.register,
        system.register,
        plugins_api.register,
        # container file browser (archive API), exposure/HTTPS, network map.
        containerfiles_api.register,
        exposure_api.register,
        netmap_api.register,
        # deployments MUST come before workflows: it attaches svc.deployments,
        # which the workflow engine's default wiring reads.
        deployments.register,
        # monitor: alerts / alert rules / metrics.
        monitor_api.register,
        # uptime checks (HTTP/TCP probes) and the public status page.
        uptime_api.register,
        # resource history per workload (written by the monitor).
        workloadres_api.register,
        # retained, searchable container logs (logs.db).
        logarchive_api.register,
        # import stacks from Portainer and compose folders (after deployments).
        imports_api.register,
        # write secret values into a deployment's compose.
        inline_secrets_api.register,
        # guided and scheduled disk cleanup.
        cleanup_api.register,
        # the periodic notification digest.
        digest_api.register,
        # unified credentials: registries + secrets + notification channels.
        credentials.register,
        # live registry browse (repositories/tags) for a registry connection,
        # used by the workflow docker-image trigger image selector.
        registrybrowse.register,
        # control-plane self-update: check for a newer server image + recreate.
        serverupdate.register,
        # workflows: attaches + starts the engine / scheduler / poller.
        workflows.register,
        backup.register,
        audit.register,
        apikeys.register,
        # notifications: bell feed (list / unread-count / read / read-all).
        notifications.register,
        # image update detection + pull/recreate (update-available badge).
        updates_api.register,
        # app-template catalog (built-in + custom) and input rendering.
        templates_api.register,
        # image vulnerability scans (Trivy on the target host).
        scan_api.register,
        # auto-update policies + background updater (starts its own loop).
        autoupdate_api.register,
        # compose validation (POST /compose/validate) + GitOps syncs/export.
        compose_validate_api.register,
        gitops_api.register,
        # volume backups (restic) + scheduled start/stop; each starts its own
        # CronRunner from an on_startup hook. After deployments (svc.deployments).
        volbackups_api.register,
        schedules_api.register,
        # MCP server (Streamable HTTP at /mcp, outside /api/v1; own auth+audit).
        # After deployments/workflows: its tools use svc.deployments / svc.workflow_engine.
        mcp_api.register,
        # teams / grants / effective permissions and single sign-on (OIDC,
        # GitHub) incl. the public /api/v1/auth/sso/* login routes.
        teams_api.register,
        sso_api.register,
    ]


# ---------------------------------------------------------------------------
# build_app
# ---------------------------------------------------------------------------


async def build_app(config: ServerConfig) -> tuple[web.Application, Services]:
    """Build the aiohttp Application and its :class:`Services` container.

    Performs the full non-serving bootstrap (store, cipher, admin seed, local
    host, DI singletons, route mounting, edge tunnel + webhooks + SPA, and the
    background-worker startup hooks) but does NOT start listening — that is
    :func:`main`'s job (or a test's, via ``aiohttp.test_utils``).

    CRITICAL: this must succeed WITHOUT a live Docker daemon. The ``LocalClient``
    is constructed lazily-safe (it never touches ``/var/run/docker.sock`` at
    construction — only per request), and its construction is additionally
    guarded so a missing socket path can never fail the bootstrap.
    """
    # 1) Data dir + store + cipher + admin seed + local host. -----------------
    os.makedirs(config.data_dir, exist_ok=True)

    # Apply a staged restore (if one is armed) BEFORE opening the database, so the
    # file swap never races an open SQLite handle: deliberately placed between
    # the data-dir mkdir and the store open.
    from .backup import apply_pending_restore

    if apply_pending_restore(config.data_dir):
        log.info("applied staged restore from a previous /restore request")

    db_path = str(Path(config.data_dir) / "kowan.db")
    store = Store(db_path)

    # Secrets cipher: resolve the master key (env / key file / generate) and
    # attach it so secret + auth-provider CRUD and [[name]] interpolation work.
    secret_key = load_or_create_key(config.data_dir, config.secret_key)
    cipher = Cipher(secret_key)
    store.use_cipher(cipher)

    # External secret providers (vault / infisical / phase) resolver, kept out of
    # the store to avoid an import cycle.
    store.set_external_secrets(ExternalResolver())

    # Unified credential sensitivity resolver: which encrypted `data` keys to mask
    # on read / preserve on blank update, dispatched by (kind, type) across the
    # registry / notify / secrets provider schemas. Kept out of the store to avoid
    # an import cycle with those provider packages.
    from . import connection_types as _connection_types
    from . import registry as _registry_pkg
    from . import secrets as _secrets_pkg
    from .models import (
        CredentialKindChannel,
        CredentialKindRegistry,
        CredentialKindSecret,
    )
    from .notify import provider as _notify_provider

    from .models import CredentialKindConnection, normalize_registry_type

    def _credential_sensitivity(kind: str, type_: str) -> dict:
        if kind == CredentialKindRegistry:
            return _registry_pkg.sensitive_keys(type_)
        if kind == CredentialKindChannel:
            return _notify_provider.sensitive_keys(type_)
        if kind == CredentialKindSecret:
            return _secrets_pkg.sensitive_keys(type_)
        if kind == CredentialKindConnection:
            # A connection's sensitivity is decided by its type's owning family.
            # Connection-native types (git hosts / api-token / basic-auth) own their
            # schema; cloud identities reuse the ECR/ACR/GCR field schema (aws->ecr,
            # azure->acr); a registry-family basic login has a sensitive password
            # stored in `data`; secret/notify types carry their own sensitive keys.
            if _connection_types.is_native(type_):
                return _connection_types.sensitive_keys(type_)
            cloud = {"aws": "ecr", "azure": "acr", "gcp": "gcp"}
            reg_t = cloud.get(type_, type_)
            if normalize_registry_type(reg_t) == reg_t:
                s = dict(_registry_pkg.sensitive_keys(reg_t))
                s.setdefault("password", True)
                return s
            if _notify_provider.fields(type_) is not None:
                return _notify_provider.sensitive_keys(type_)
            if _secrets_pkg.is_external(type_):
                return _secrets_pkg.sensitive_keys(type_)
            return {}
        return {}

    store.set_credential_sensitivity(_credential_sensitivity)

    # 2) Shared singletons. ---------------------------------------------------
    bus = MemoryBus()
    # Resolve the JWT secret from the env var or a persisted keyfile (generating
    # + saving one on first boot), so sessions survive restarts with no env.
    jwt = new_jwt(load_or_create_jwt_secret(config.data_dir, config.jwt_secret))

    # API-key verifier: adapt the store's verify_api_key into the
    # (role, username, ok) callback the auth middleware expects.
    def _verify_api_key(secret: str):
        try:
            key, ok = store.verify_api_key(secret)
        except Exception as err:  # noqa: BLE001
            log.error("api key verification failed: %s", err)
            return "", "", False
        if not ok or key is None:
            return "", "", False
        return key.role, "key:" + key.name, True

    keys = new_api_key_verifier(_verify_api_key)

    # Edge tunnel hub. A registered session IS the host's liveness signal; the
    # control-plane drives every host by opening streams (server->agent), so the
    # hub only needs to hold sessions and dial out on demand.
    hub = Hub()

    # Local Docker client + the per-host Ops provider. Constructing LocalClient
    # NEVER touches the socket (the aiohttp session is created lazily on first
    # request), but guard it anyway so build_app cannot fail on a boxed
    # environment without /var/run/docker.sock.
    local = _new_local_client(config.docker_socket)
    provider = Provider(local=local, local_host_id=LOCAL_HOST_ID, edge=hub)

    # 3) DI container. --------------------------------------------------------
    svc = Services(
        config=config,
        store=store,
        jwt=jwt,
        keys=keys,
        bus=bus,
        cipher=cipher,
        docker=provider,
        hub=hub,
        audit=_store_audit_recorder(store),
    )

    # 4) Seed the admin user + ensure the local host row. These need the store
    # fully wired (cipher attached) and are idempotent.
    _seed_admin(store, config)
    _arm_setup_token(svc, store, config)

    from .api.hosts import ensure_local_host

    ensure_local_host(svc)

    # 5) Build the app with every api module mounted under /api/v1. -----------
    app = create_app(svc, registrars=_registrars())

    # 6) Public edge tunnel endpoint. Mounted OUTSIDE /api/v1 (like /hooks): it
    # authenticates with the edge enrollment token in the Authorization header,
    # not a JWT.
    app.router.add_get("/edge/connect", _edge_connect_handler(svc))

    # 7) Public webhook receiver + SPA. The specific /hooks/deploy and
    # /hooks/workflow prefixes were registered as public routes by their api
    # modules; add_webhooks mounts the generic /hooks catch-all AFTER them so
    # they take precedence, and add_spa is the final catch-all for the frontend.
    add_webhooks(app, _webhook_receiver)
    add_spa(app)

    # 8) Background workers whose lifetime is the app's. The workflow engine /
    # scheduler / poller register their own on_startup/on_cleanup inside
    # workflows.register; here we add the notifier + monitor.
    _register_background_workers(app, svc)

    return app, svc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _new_local_client(socket_path: str = ""):
    """Construct the local Docker client, never failing on a missing socket.

    LocalClient.__init__ only records the socket path (the aiohttp session is
    created lazily on first request), so this cannot raise in practice; the
    guard is belt-and-braces so build_app is robust on a host with no Docker.
    """
    try:
        return LocalClient(socket_path) if socket_path else LocalClient()
    except Exception as err:  # noqa: BLE001
        log.warning("docker: local client unavailable at startup: %s", err)
        return None


def _store_audit_recorder(store: Store):
    """Adapt the store into the AuditRecorder the audit middleware expects.

    The middleware calls ``recorder.record(entry)``; the store stamps the Time
    field itself.
    """
    create = getattr(store, "create_audit", None)
    if create is None:
        return None

    from .models import AuditEntry

    class _Recorder:
        def record(self, entry):
            # The audit middleware / login recorder hand us a camelCase dict;
            # the store's create_audit wants an AuditEntry model (it stamps
            # .time). Convert by alias so both agree.
            if isinstance(entry, dict):
                entry = AuditEntry.model_validate(entry)
            create(entry)

    return _Recorder()


def _arm_setup_token(svc: Services, store: Store, config: ServerConfig) -> None:
    """Require a one-time token on first-run setup while no user exists."""
    from .auth.setup import ensure_setup_token

    try:
        initialized = store.count_users() > 0
    except Exception as err:  # noqa: BLE001
        log.error("setup token: count users failed: %s", err)
        initialized = False
    token = ensure_setup_token(config.data_dir, initialized, config.setup_token)
    svc.attach("setup_token", token)
    if token and not config.setup_token:
        log.warning(
            "first-run setup: open the web UI and enter this setup token to create the admin: %s "
            "(also in %s/setup-token)", token, config.data_dir,
        )


def _seed_admin(store: Store, config: ServerConfig) -> None:
    """Create the initial admin user from env, if requested and no users exist.

    Only runs when ``KOWAN_ADMIN_PASSWORD`` was explicitly set (config.seed_admin)
    — a headless/automated bootstrap. Otherwise the first admin is created via the
    browser setup flow (POST /auth/init), so a default deployment needs no env.
    Seeds only when the users table is empty, so it never clobbers real accounts.
    """
    if not config.seed_admin:
        return

    from .models import User
    from .models.user import AuthSourceLocal, RoleAdmin

    try:
        if store.count_users() > 0:
            return
    except Exception as err:  # noqa: BLE001
        log.error("seed admin: count users failed: %s", err)
        return

    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    admin = User(
        id=str(uuid.uuid4()),
        username=config.admin_user,
        role=RoleAdmin,
        password_hash=hash_password(config.admin_password),
        authSource=AuthSourceLocal,
        createdAt=now,
    )
    store.create_user(admin)
    log.warning(
        "control plane starting; seeded default admin user %r — "
        "change the admin password after first login",
        config.admin_user,
    )


def _edge_connect_handler(svc: Services):
    """Build the aiohttp handler for the public edge tunnel endpoint.

    An edge agent dials ``GET /edge/connect`` and presents its enrollment token
    in the ``Authorization: Bearer <token>`` header. We accept the WebSocket,
    wrap it in an :class:`AiohttpWSTransport`, run the SERVER side of the tunnel
    handshake (the agent dialed us, so we VERIFY the token -> host id), register
    the resulting mux session on the Hub, and keep the socket open until the
    session ends.
    """
    store = svc.store
    hub = svc.hub

    def _verify(token: str):
        """Resolve an edge enrollment token to its host id, or None.

        Only hosts in edge mode may connect.
        """
        if not token:
            return None
        try:
            host = store.get_host_by_token(token)
        except Exception:  # noqa: BLE001 - NotFoundError => reject
            return None
        from .models.host import HostModeEdge

        if getattr(host, "mode", "") != HostModeEdge:
            return None
        return host.id

    async def handler(request: web.Request) -> web.StreamResponse:
        # Authenticate BEFORE upgrading, so an unauthenticated dialer gets a
        # plain 401 rather than a WebSocket. The token is re-verified inside the
        # handshake too (the mux only trusts the on-wire hello).
        authz = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authz.startswith(prefix) or not _verify(authz[len(prefix):].strip()):
            raise web.HTTPUnauthorized(text="invalid edge token")

        # heartbeat: detect a silently dropped edge link (NAT timeout, agent
        # host power loss) so the session is unregistered and ops fail fast
        # instead of hanging on a dead tunnel; the agent then redials.
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=MAX_MESSAGE_SIZE)
        await ws.prepare(request)
        from .web.wsregistry import track as _track_ws

        _track_ws(request, ws)  # closed on shutdown so SIGTERM never hangs

        transport = AiohttpWSTransport(ws)
        try:
            host_id, session = await asyncio.wait_for(
                establish_server_side(transport, dialed=False, verify=_verify),
                HANDSHAKE_TIMEOUT,
            )
        except Exception as err:  # noqa: BLE001 - handshake refused / closed
            log.info("edge: handshake failed: %s", err)
            await transport.close()
            return ws

        await hub.register(host_id, session)
        log.info("edge tunnel connected host=%s", host_id)
        try:
            # Keep the socket open until the mux session ends.
            await session.wait_closed()
        finally:
            await hub.unregister(host_id, session)
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("edge tunnel disconnected host=%s", host_id)
        return ws

    return handler


async def _webhook_receiver(request: web.Request) -> web.StreamResponse:
    """Generic ``/hooks`` catch-all.

    The Kowan control plane's real webhooks are the per-deployment redeploy hook
    (``/hooks/deploy/{token}``) and the per-workflow run hook
    (``/hooks/workflow/{token}``), each registered as a more-specific public
    route that takes precedence over this catch-all. Any other ``/hooks/*`` path
    is unknown, so we 404 it.
    """
    raise web.HTTPNotFound()


# Application keys for the background services started with the app.
NOTIFIER_KEY = web.AppKey("kowan_notifier", object)
MONITOR_KEY = web.AppKey("kowan_monitor", object)
MONITOR_TASK_KEY = web.AppKey("kowan_monitor_task", object)
DIALER_KEY = web.AppKey("kowan_dialer", object)
UPDATE_RECONCILER_KEY = web.AppKey("kowan_update_reconciler", object)


def _register_background_workers(app: web.Application, svc: Services) -> None:
    """Start the notifier + monitor on app startup and stop them on cleanup.

    The workflow engine / scheduler / poller manage their own lifecycle via the
    workflows api's on_startup/on_cleanup, so they are intentionally NOT started
    here (double-registration would run them twice).
    """
    import asyncio

    from .dialer import StoaDialer
    from .monitor.monitor import Monitor
    from .notify.notifier import Notifier

    async def _on_startup(_app: web.Application) -> None:
        notifier = Notifier(svc)
        await notifier.start()  # spawns its own task and returns
        _app[NOTIFIER_KEY] = notifier

        monitor = Monitor(svc)
        # Monitor.start() runs the loop to completion, so run it detached.
        monitor_task = asyncio.ensure_future(monitor.start())
        _app[MONITOR_KEY] = monitor
        _app[MONITOR_TASK_KEY] = monitor_task

        # Outbound tunnels to agent-mode (stoa) hosts. Edge hosts dial in; these
        # are dialed out from here and registered on the same hub.
        dialer = StoaDialer(svc)
        await dialer.start()  # spawns the supervisor task and returns
        _app[DIALER_KEY] = dialer

        # Self-update reconciliation (kowan/updater): folds finished helper
        # containers into the `updates` history — including the helper that just
        # swapped THIS server in (or rolled it back). Runs once now, then loops.
        try:
            from .serverupdate import _manager
            from .updater.reconcile import Reconciler

            reconciler = Reconciler(svc, _manager(svc))
            reconciler.start()
            _app[UPDATE_RECONCILER_KEY] = reconciler
        except Exception as err:  # noqa: BLE001 - never block startup
            log.error("updates: reconciler not started: %s", err)

    async def _on_cleanup(_app: web.Application) -> None:
        reconciler = _app.get(UPDATE_RECONCILER_KEY)
        if reconciler is not None:
            await reconciler.stop()
        notifier = _app.get(NOTIFIER_KEY)
        if notifier is not None:
            await notifier.stop()
        dialer = _app.get(DIALER_KEY)
        if dialer is not None:
            await dialer.stop()
        monitor = _app.get(MONITOR_KEY)
        if monitor is not None:
            monitor.stop()
        task = _app.get(MONITOR_TASK_KEY)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Close the local Docker client's pooled aiohttp session (otherwise
        # aiohttp logs "Unclosed client session" and the socket fds leak).
        local = getattr(svc.docker, "_local", None)
        if local is not None:
            try:
                await local.close()
            except Exception:  # noqa: BLE001 - best-effort on shutdown
                pass

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def _split_addr(addr: str) -> tuple[str | None, int]:
    """Split a ``host:port`` listen address into ``(host, port)``.

    ``":5081"`` -> ``(None, 5081)`` binds all interfaces (aiohttp uses host=None
    for that).
    """
    addr = addr or ":5081"
    if ":" not in addr:
        # No colon at all: treat the whole thing as a port if numeric, else host.
        try:
            return None, int(addr)
        except ValueError:
            return addr, 5081
    host, _, port = addr.rpartition(":")
    host = host or None
    try:
        return host, int(port)
    except ValueError:
        return host, 5081


def _tls_context(config: ServerConfig) -> ssl.SSLContext | None:
    """Build a server TLS context when both cert and key are configured."""
    if config.tls_cert and config.tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(config.tls_cert, config.tls_key)
        return ctx
    return None


def main() -> None:
    """Load config, build the app, and serve until interrupted.

    Runnable as ``python3 -m kowan.server``. aiohttp's ``web.run_app`` installs
    the SIGINT/SIGTERM handlers and drives the app's startup/cleanup callbacks
    for a graceful shutdown.
    """
    import sys

    args = sys.argv[1:]
    # `healthcheck` is the image HEALTHCHECK: answer before any logging setup.
    if args and args[0] == "healthcheck":
        from kowan.updater.preflight import server_healthcheck

        raise SystemExit(server_healthcheck(args[1:]))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # One-shot sub-commands of the self-update system (docs/updates-design.md),
    # dispatched before config load:
    #   self-update --plan <b64>  the transactional swap helper container
    #   preflight [--db PATH]     smoke test of a NEW image (never the live DB)
    if args and args[0] in ("self-update", "preflight"):
        from kowan.updater.preflight import dispatch

        raise SystemExit(dispatch("server", args))

    config = load_server_config()

    import asyncio

    from kowan.eventloop import new_event_loop

    # uvloop when installed (optional dep, see kowan/eventloop.py), else asyncio.
    loop = new_event_loop()
    asyncio.set_event_loop(loop)
    app, _svc = loop.run_until_complete(build_app(config))

    host, port = _split_addr(config.addr)
    ssl_ctx = _tls_context(config)

    log.warning(
        "control plane listening host=%s port=%s tls=%s",
        host or "0.0.0.0",
        port,
        ssl_ctx is not None,
    )
    # Open websockets are closed in on_shutdown (kowan.web.wsregistry); the
    # timeout only bounds stragglers so a stop never waits for Docker's SIGKILL.
    from .web.accesslog import RedactingAccessLogger

    web.run_app(
        app, host=host, port=port, ssl_context=ssl_ctx, loop=loop, shutdown_timeout=10.0,
        access_log_class=RedactingAccessLogger,
    )


if __name__ == "__main__":
    main()
