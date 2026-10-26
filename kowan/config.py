"""Runtime configuration for the Kowan server and agent.

Loaded from ``PORT_*`` environment variables with sensible defaults.

Interval-style values (``monitor_interval``, ``metrics_retention``, ...) are
``datetime.timedelta``. ``env_duration`` accepts either a duration string
("15s", "168h", "1h30m") or a bare integer number of seconds ("15").
"""

from __future__ import annotations

import logging
import os
import re
import secrets as _secrets
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Persisted JWT signing key, sibling to the secrets' ``secret.key``. Keeping it
# on disk (instead of regenerating per boot) means sessions survive restarts with
# no env var, and it rides along in backups so a restore keeps tokens valid.
JWT_KEY_FILE_NAME = "jwt.key"


@dataclass
class ServerConfig:
    """Configuration for the control-plane server (``kowan.server``)."""

    addr: str = ":5081"
    data_dir: str = "./data"
    jwt_secret: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    admin_user: str = "admin"
    admin_password: str = "admin"
    # Whether to seed the admin from admin_user/admin_password on first boot. Only
    # true when KOWAN_ADMIN_PASSWORD is explicitly set (headless bootstrap). When
    # false and no users exist, the first admin is created via the setup flow in
    # the browser, so a default deployment needs no env vars.
    seed_admin: bool = False
    # One-time token required by the first-run setup (KOWAN_SETUP_TOKEN); when
    # empty the server generates one in {data_dir}/setup-token. See kowan.auth.setup.
    setup_token: str = ""
    # Whether workflow script steps (bash on the control plane) may run. Hosted
    # instances turn this off with KOWAN_SERVER_SCRIPTS=off.
    server_scripts: bool = True
    # Unix socket of the local Docker Engine (KOWAN_DOCKER_SOCKET); empty means
    # /var/run/docker.sock. Rootless Docker or Podman expose theirs elsewhere.
    docker_socket: str = ""
    # Network policy (kowan/web/netpolicy.py): comma-separated addresses/CIDRs.
    # trusted_proxies may set X-Forwarded-For; the allow lists limit who reaches
    # the UI/API and the edge tunnel endpoint. Empty = no limit. proxy_header is
    # the one header a trusted proxy states the client in.
    trusted_proxies: str = ""
    proxy_header: str = "x-forwarded-for"
    ui_allow_cidrs: str = ""
    edge_allow_cidrs: str = ""
    monitor_interval: timedelta = field(default_factory=lambda: timedelta(seconds=15))

    # MetricsRetention bounds how long time-series metric samples are kept
    # before the monitor prunes them (env KOWAN_METRICS_RETENTION, default
    # 168h / 7 days).
    metrics_retention: timedelta = field(default_factory=lambda: timedelta(hours=168))

    # Retention for the tables that otherwise grow forever, enforced hourly by
    # the monitor loop (see Monitor._prune_retention). A zero/negative value
    # disables that prune.
    #   audit_retention     env KOWAN_AUDIT_RETENTION, default 90d: audit_log rows
    #                       older than this are deleted.
    #   alert_retention     env KOWAN_ALERT_RETENTION, default 30d: RESOLVED alerts
    #                       older than this are deleted (active ones are kept).
    #   workflow_runs_keep  env KOWAN_WORKFLOW_RUNS_KEEP, default 100: run history
    #                       kept per workflow (newest first).
    audit_retention: timedelta = field(default_factory=lambda: timedelta(days=90))
    alert_retention: timedelta = field(default_factory=lambda: timedelta(days=30))
    workflow_runs_keep: int = 100


    # SecretKey is the master key used to encrypt stored secrets (env
    # KOWAN_SECRET_KEY). When empty the server reads or generates a key file
    # under DataDir.
    secret_key: str = ""

    # ExternalURL is the externally-reachable base URL of this server (env
    # KOWAN_EXTERNAL_URL), e.g. "https://kowan.example.com". When set it is used
    # to build edge join commands so they point at a reachable address instead
    # of the inbound request's Host. When empty the hosts service falls back to
    # the request scheme+Host.
    external_url: str = ""


@dataclass
class AgentConfig:
    """Configuration for a host agent (``kowan.agent``).

    ``mode`` selects the transport: "agent" (default) runs an HTTPS listener
    that the control plane dials, while "edge" dials OUT to the control plane at
    ``server_url`` and serves the agent API over a persistent reverse tunnel
    authenticated by ``edge_token``.
    """

    addr: str = ":9001"
    token: str = ""
    tls_cert: str = ""
    tls_key: str = ""

    # mode is "agent" (server->agent, default) or "edge" (agent->server).
    mode: str = "agent"
    # server_url is the control-plane base URL the edge agent dials, e.g.
    # "https://kowan.example.com". Used only in edge mode.
    server_url: str = ""
    # edge_token is the per-host edge enrollment token presented to the control
    # plane when connecting the reverse tunnel. Used only in edge mode.
    edge_token: str = ""
    # Direct mode: addresses/CIDRs allowed to connect (the control plane's),
    # KOWAN_AGENT_ALLOW_CIDRS. Empty = any address that has the token.
    allow_cidrs: str = ""
    # Unix socket of the Docker Engine on this host (KOWAN_DOCKER_SOCKET).
    docker_socket: str = ""


# Suffix multipliers for duration units, in seconds: "ns", "us"/"µs", "ms",
# "s", "m", "h", plus "d" (24h) so retention windows can be written as "90d".
_UNIT_SECONDS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}

# Matches one number + unit component of a duration string, e.g. "168h",
# "1h30m", "1.5s".
_DURATION_COMPONENT = re.compile(r"(?P<value>[0-9]*\.?[0-9]+)(?P<unit>ns|us|µs|ms|s|m|h|d)")


def env_or(key: str, default: str) -> str:
    """Return ``os.environ[key]`` if set and non-empty, else ``default``
    (an empty string is treated as unset)."""
    v = os.environ.get(key, "")
    return v if v != "" else default


def _parse_go_duration(value: str) -> timedelta | None:
    """Parse a duration string ("15s", "168h", "1h30m") into a timedelta.

    Returns None if the string is not a valid duration.
    """
    s = value.strip()
    if s == "":
        return None
    sign = 1
    if s[0] in "+-":
        if s[0] == "-":
            sign = -1
        s = s[1:]
    if s == "":
        return None
    # Special case: "0" is accepted with no unit.
    if s == "0":
        return timedelta(0)

    total = 0.0
    pos = 0
    matched_any = False
    for m in _DURATION_COMPONENT.finditer(s):
        if m.start() != pos:
            # Gap between components => invalid.
            return None
        total += float(m.group("value")) * _UNIT_SECONDS[m.group("unit")]
        pos = m.end()
        matched_any = True
    if not matched_any or pos != len(s):
        return None
    return timedelta(seconds=sign * total)


def env_int(key: str, default: int) -> int:
    """Read an integer from the environment; empty/unparseable -> ``default``."""
    v = os.environ.get(key, "").strip()
    if v == "":
        return default
    try:
        return int(v)
    except ValueError:
        logger.warning(
            "invalid integer in environment; using default",
            extra={"key": key, "value": v, "default": default},
        )
        return default


def env_duration(key: str, default: timedelta) -> timedelta:
    """Read a duration from the environment.

    Accepts a duration string (e.g. "15s", "168h") or a bare integer
    interpreted as seconds (e.g. "15"). An empty or unparseable value falls back
    to ``default`` with a warning.
    """
    v = os.environ.get(key, "")
    if v == "":
        return default
    d = _parse_go_duration(v)
    if d is not None:
        return d
    try:
        secs = int(v)
        return timedelta(seconds=secs)
    except ValueError:
        pass
    logger.warning(
        "invalid duration in environment; using default",
        extra={"key": key, "value": v, "default": str(default)},
    )
    return default


def _random_secret() -> str:
    """Return a hex-encoded 32-byte random secret."""
    return _secrets.token_hex(32)


def load_or_create_jwt_secret(data_dir: str, env_secret: str) -> str:
    """Resolve the JWT signing secret: env var, else a persisted keyfile, else generate.

    Priority mirrors :func:`kowan.secrets.cipher.load_or_create_key`:
      * a non-empty ``env_secret`` (KOWAN_JWT_SECRET) wins;
      * otherwise ``{data_dir}/jwt.key`` is read if present;
      * otherwise a fresh secret is generated and written there (0600).
    Persisting it means sessions survive restarts with no env var, and the file
    is included in backups so a restore keeps existing tokens valid.
    """
    if env_secret:
        return env_secret
    path = Path(data_dir) / JWT_KEY_FILE_NAME
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    secret = _random_secret()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.encode("utf-8"))
    finally:
        os.close(fd)
    logger.info(
        "generated a JWT signing key at %s (persisted; sessions survive restarts)",
        path,
    )
    return secret


def load_server_config() -> ServerConfig:
    """Build a ServerConfig from environment variables, applying defaults.

    If no JWT secret is provided a random one is generated and a warning is
    logged (tokens will not survive a restart in that case).
    """
    c = ServerConfig(
        addr=env_or("KOWAN_ADDR", ":5081"),
        data_dir=env_or("KOWAN_DATA_DIR", "./data"),
        jwt_secret=os.environ.get("KOWAN_JWT_SECRET", ""),
        tls_cert=os.environ.get("KOWAN_TLS_CERT", ""),
        tls_key=os.environ.get("KOWAN_TLS_KEY", ""),
        admin_user=env_or("KOWAN_ADMIN_USER", "admin"),
        admin_password=env_or("KOWAN_ADMIN_PASSWORD", "admin"),
        # Only seed the admin from env when a password is explicitly provided;
        # otherwise the first admin is created through the browser setup flow.
        seed_admin=bool(os.environ.get("KOWAN_ADMIN_PASSWORD")),
        setup_token=os.environ.get("KOWAN_SETUP_TOKEN", ""),
        server_scripts=os.environ.get("KOWAN_SERVER_SCRIPTS", "on").strip().lower() not in ("off", "false", "0", "no"),
        docker_socket=os.environ.get("KOWAN_DOCKER_SOCKET", ""),
        trusted_proxies=os.environ.get("KOWAN_TRUSTED_PROXIES", ""),
        ui_allow_cidrs=os.environ.get("KOWAN_UI_ALLOW_CIDRS", ""),
        proxy_header=os.environ.get("KOWAN_PROXY_HEADER", "x-forwarded-for"),
        edge_allow_cidrs=os.environ.get("KOWAN_EDGE_ALLOW_CIDRS", ""),
        monitor_interval=env_duration("KOWAN_MONITOR_INTERVAL", timedelta(seconds=15)),
        metrics_retention=env_duration("KOWAN_METRICS_RETENTION", timedelta(hours=168)),
        audit_retention=env_duration("KOWAN_AUDIT_RETENTION", timedelta(days=90)),
        alert_retention=env_duration("KOWAN_ALERT_RETENTION", timedelta(days=30)),
        workflow_runs_keep=env_int("KOWAN_WORKFLOW_RUNS_KEEP", 100),
        secret_key=os.environ.get("KOWAN_SECRET_KEY", ""),
        external_url=os.environ.get("KOWAN_EXTERNAL_URL", ""),
    )

    # The JWT secret is resolved from a persisted keyfile at bootstrap (see
    # load_or_create_jwt_secret, which needs data_dir), not here — so it is left
    # as the raw env value ("" when unset).
    return c


def load_agent_config() -> AgentConfig:
    """Build an AgentConfig from environment variables, applying defaults.

    In edge mode (KOWAN_AGENT_MODE=edge) the agent dials KOWAN_SERVER_URL and
    authenticates with KOWAN_AGENT_TOKEN (reused as the edge enrollment token).
    """
    token = os.environ.get("KOWAN_AGENT_TOKEN", "")
    return AgentConfig(
        addr=env_or("KOWAN_AGENT_ADDR", ":9001"),
        token=token,
        tls_cert=os.environ.get("KOWAN_AGENT_TLS_CERT", ""),
        tls_key=os.environ.get("KOWAN_AGENT_TLS_KEY", ""),
        mode=env_or("KOWAN_AGENT_MODE", "agent"),
        server_url=os.environ.get("KOWAN_SERVER_URL", ""),
        edge_token=token,
        allow_cidrs=os.environ.get("KOWAN_AGENT_ALLOW_CIDRS", ""),
        docker_socket=os.environ.get("KOWAN_DOCKER_SOCKET", ""),
    )
