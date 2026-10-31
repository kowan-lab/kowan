"""Synchronous SQLite-backed store for the Kowan control plane.

SQLite is single-writer, so the store uses the stdlib :mod:`sqlite3` module with
one shared connection guarded by a re-entrant lock so concurrent callers never
trip "database is locked".

Aggregation pattern (IMPORTANT for the parallel store-modules agent)
-------------------------------------------------------------------
CRUD is implemented as **mixin classes**, one per domain module, each defining
methods on ``self`` that use ``self._execute`` / ``self._query`` / ``self._one``
helpers (all defined on :class:`_StoreBase`).  The concrete :class:`Store`
inherits from every mixin plus ``_StoreBase``.  New modules add a mixin and get
folded into ``Store`` here.  Method names are snake_case verbs on the entity
(e.g. ``get_user``, ``list_api_keys``).

Shared plumbing every mixin relies on:
  * ``self._conn``       — the sqlite3.Connection (row_factory = sqlite3.Row).
  * ``self._lock``       — threading.RLock guarding all access.
  * ``self._cipher``     — optional AES-GCM cipher for secret/auth-provider blobs.
  * ``self._execute(sql, params=())``          -> sqlite3.Cursor (write, commits).
  * ``self._query(sql, params=())``            -> list[sqlite3.Row] (read).
  * ``self._one(sql, params=())``              -> sqlite3.Row | None (read).
  * ``self._encrypt_data(dict)`` / ``self._decrypt_data(blob)`` — blob helpers.
  * ``bool_to_int(bool)`` — module-level 0/1 helper.

All queries use ``?`` placeholders; SQL is NEVER string-formatted with values.

Schema ownership: every ``CREATE TABLE``/``CREATE INDEX`` for the whole app is
defined by :data:`_SCHEMA` below.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Iterable, Optional, Sequence

from ._common import (
    MASK_VALUE,
    CipherNotConfiguredError,
    NotFoundError,
    StoreError,
    bool_to_int,
)
from .alerts import _AlertMixin
from .apikeys import _APIKeyMixin
from .audit import _AuditMixin
from .credentials import _CredentialMixin
from .deployments import _DeploymentMixin
from .hosts import _HostMixin
from .metrics import _MetricMixin
from .notifications import _NotificationMixin
from .settings import _SettingsMixin
from .templates import _TemplateMixin
from .updates import UPDATES_SCHEMA, _UpdateMixin
from .users import _UserMixin
from .workflows import _WorkflowMixin
from .scans import _ScanMixin
from .autoupdate import _AutoUpdateMixin
from .teams import _TeamMixin
from .sso import _SSOMixin
from .volbackups import _VolBackupMixin
from .schedules import _ScheduleMixin
from .gitops import _GitOpsMixin
from .sessions import _SessionMixin
from .uptime import UPTIME_SCHEMA, _UptimeMixin
from .runbooks import RUNBOOKS_SCHEMA, _RunbookMixin
from .alertrunbooks import ALERT_RUNBOOKS_SCHEMA, _AlertRunbookMixin
from .crashes import CRASHES_SCHEMA, _CrashMixin
from .workload_metrics import WORKLOAD_METRICS_SCHEMA, _WorkloadMetricMixin


# _SCHEMA is the COMPLETE DDL for the whole application: every CREATE TABLE /
# CREATE INDEX in its final shape. There are no migrations; new tables and
# columns go straight into these CREATE statements.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    role          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    email         TEXT NOT NULL DEFAULT '',
    auth_source   TEXT NOT NULL DEFAULT 'local',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hosts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'agent',
    address    TEXT NOT NULL DEFAULT '',
    token      TEXT NOT NULL DEFAULT '',
    last_seen  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
-- Host groups: tags such as production or customer-x (a table of its own, so
-- an existing database gets it). Grants, schedules, runbooks and auto-update
-- policies can target every host with a tag.
CREATE TABLE IF NOT EXISTS host_tags (
    host_id TEXT NOT NULL,
    tag     TEXT NOT NULL,
    PRIMARY KEY (host_id, tag)
);

CREATE TABLE IF NOT EXISTS deployments (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    type           TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    targets        TEXT NOT NULL DEFAULT '[]',
    replicas       INTEGER NOT NULL DEFAULT 0,
    spec           TEXT,
    compose        TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '',
    redeploy_token TEXT NOT NULL DEFAULT '',
    protected      INTEGER NOT NULL DEFAULT 0,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    git_source      TEXT,
    deployed_commit TEXT NOT NULL DEFAULT '',
    last_sync_at    TEXT NOT NULL DEFAULT '',
    preview         TEXT,
    promote_to      TEXT NOT NULL DEFAULT ''
);

-- A compose deployment's stack variables, as a JSON list of {key, value}. A
-- table of its own so databases created before it gain it without a migration.
CREATE TABLE IF NOT EXISTS deployment_variables (
    deployment_id TEXT PRIMARY KEY,
    variables     TEXT NOT NULL DEFAULT '[]'
);

-- Numbered snapshots of what a deployment runs (kowan/deployment/revisions.py):
-- recorded on every save that changes it, kept up to revisions.KEEP each.
CREATE TABLE IF NOT EXISTS deployment_revisions (
    deployment_id TEXT NOT NULL,
    number        INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    author        TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    config        TEXT NOT NULL,
    deployed_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (deployment_id, number)
);

CREATE TABLE IF NOT EXISTS alerts (
    id         TEXT PRIMARY KEY,
    rule_id    TEXT NOT NULL DEFAULT '',
    host_id    TEXT NOT NULL DEFAULT '',
    level      TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL DEFAULT '',
    resource   TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    resolved   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL,
    operator     TEXT NOT NULL DEFAULT '>',
    threshold    REAL NOT NULL DEFAULT 0,
    severity     TEXT NOT NULL DEFAULT '',
    for_duration INTEGER NOT NULL DEFAULT 0,
    host_id      TEXT NOT NULL DEFAULT '',
    match        TEXT NOT NULL DEFAULT '',
    label_key    TEXT NOT NULL DEFAULT '',
    label_value  TEXT NOT NULL DEFAULT '',
    channel_id   TEXT NOT NULL DEFAULT '',
    auto_resolve INTEGER NOT NULL DEFAULT 1,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'notice',
    level      TEXT NOT NULL DEFAULT 'info',
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    host_id    TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    ref_id     TEXT NOT NULL DEFAULT '',
    read       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications (created_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Unified credential store: image registries, secrets and notification
-- channels, one row type discriminated by `kind`. `meta` is JSON text holding
-- the non-sensitive kind-specific attributes; `data` is the AES-256-GCM sealed
-- field map (registry password + provider config / channel config / secret
-- fields).
CREATE TABLE IF NOT EXISTS credentials (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    data       BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS workflows (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    steps       BLOB,
    triggers    BLOB,
    variables   BLOB,
    enabled     INTEGER NOT NULL DEFAULT 0,
    run_token   TEXT NOT NULL DEFAULT '',
    protected   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id          TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    steps       BLOB,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

-- list_runs: WHERE workflow_id = ? ORDER BY started_at DESC LIMIT n, and the
-- cascade delete in delete_workflow.
CREATE INDEX IF NOT EXISTS idx_workflow_runs_wf ON workflow_runs (workflow_id, started_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id       TEXT PRIMARY KEY,
    time     TEXT NOT NULL,
    user_id  TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    action   TEXT NOT NULL,
    path     TEXT NOT NULL DEFAULT '',
    status   INTEGER NOT NULL DEFAULT 0,
    ip       TEXT NOT NULL DEFAULT '',
    detail   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_log_time ON audit_log (time DESC);

CREATE TABLE IF NOT EXISTS metrics (
    host_id     TEXT NOT NULL,
    time        TEXT NOT NULL,
    running     INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    images      INTEGER NOT NULL DEFAULT 0,
    cpu_percent REAL NOT NULL DEFAULT 0,
    mem_used    INTEGER NOT NULL DEFAULT 0,
    mem_total   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_metrics_host_time ON metrics (host_id, time);
-- prune_metrics deletes by time alone, which the (host_id, time) index cannot
-- serve: without this every hourly prune was a full table scan under the lock.
CREATE INDEX IF NOT EXISTS idx_metrics_time ON metrics (time);

-- list_alerts: ORDER BY created_at DESC LIMIT n.
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);
-- prune_resolved_alerts: WHERE resolved = 1 AND created_at < ? (retention).
-- audit_log (time) and workflow_runs (workflow_id, started_at) prunes reuse the
-- indexes above.
CREATE INDEX IF NOT EXISTS idx_alerts_resolved_created ON alerts (resolved, created_at);

CREATE TABLE IF NOT EXISTS api_keys (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    hash       TEXT NOT NULL,
    prefix     TEXT NOT NULL DEFAULT '',
    role       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used  TEXT NOT NULL DEFAULT '',
    team_id    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (hash);

-- Admin-created app templates (built-ins ship in templates_builtin.json and are
-- never stored). spec = raw container spec JSON (may hold ${VAR} tokens), NULL
-- for compose; inputs = JSON array of input declarations.
CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    icon        TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL,
    spec        BLOB,
    compose     TEXT NOT NULL DEFAULT '',
    inputs      TEXT NOT NULL DEFAULT '[]',
    website     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT ''
);


-- Image vulnerability scans, one row per image digest (kowan/store/scans.py).
CREATE TABLE IF NOT EXISTS image_scans (
    image_id        TEXT PRIMARY KEY,
    image_ref       TEXT NOT NULL DEFAULT '',
    repo_digests    TEXT NOT NULL DEFAULT '[]',
    host_id         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'ok',
    error           TEXT NOT NULL DEFAULT '',
    scanner         TEXT NOT NULL DEFAULT 'trivy',
    scanner_version TEXT NOT NULL DEFAULT '',
    os              TEXT NOT NULL DEFAULT '',
    critical        INTEGER NOT NULL DEFAULT 0,
    high            INTEGER NOT NULL DEFAULT 0,
    medium          INTEGER NOT NULL DEFAULT 0,
    low             INTEGER NOT NULL DEFAULT 0,
    unknown         INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    fixable         INTEGER NOT NULL DEFAULT 0,
    findings        TEXT NOT NULL DEFAULT '[]',
    scanned_at      TEXT NOT NULL DEFAULT '',
    duration_ms     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_image_scans_host ON image_scans (host_id, scanned_at DESC);

-- Auto-update policies / per-object state / history (kowan/store/autoupdate.py).
CREATE TABLE IF NOT EXISTS autoupdate_policies (
    kind        TEXT NOT NULL,
    host_id     TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'off',
    win         TEXT NOT NULL DEFAULT '',
    min_age     TEXT NOT NULL DEFAULT '',
    scan_block  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, host_id, target)
);
CREATE TABLE IF NOT EXISTS autoupdate_state (
    kind        TEXT NOT NULL,
    host_id     TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, host_id, target)
);
CREATE TABLE IF NOT EXISTS autoupdate_history (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    host_id     TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    image       TEXT NOT NULL DEFAULT '',
    from_digest TEXT NOT NULL DEFAULT '',
    to_digest   TEXT NOT NULL DEFAULT '',
    mode        TEXT NOT NULL DEFAULT '',
    trig        TEXT NOT NULL DEFAULT 'auto',
    outcome     TEXT NOT NULL,
    message     TEXT NOT NULL DEFAULT '',
    scan        TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_autoupdate_history_time ON autoupdate_history (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_autoupdate_history_obj ON autoupdate_history (kind, host_id, target, started_at DESC);

-- Volume backups with restic (kowan/store/volbackups.py). Repository secrets
-- are AES-GCM sealed like credentials.data.
CREATE TABLE IF NOT EXISTS backup_repos (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL,
    host_id     TEXT NOT NULL DEFAULT '',
    config      TEXT NOT NULL DEFAULT '{}',
    secrets     BLOB,
    initialized INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS backup_jobs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    host_id     TEXT NOT NULL,
    repo_id     TEXT NOT NULL,
    selector    TEXT NOT NULL DEFAULT '{}',
    schedule    TEXT NOT NULL DEFAULT '',
    timezone    TEXT NOT NULL DEFAULT '',
    retention   TEXT NOT NULL DEFAULT '{}',
    hooks       TEXT NOT NULL DEFAULT '{}',
    tags        TEXT NOT NULL DEFAULT '[]',
    enabled     INTEGER NOT NULL DEFAULT 1,
    alert_id    TEXT NOT NULL DEFAULT '',
    last_run_at TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS backup_runs (
    id           TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL DEFAULT '',
    job_name     TEXT NOT NULL DEFAULT '',
    repo_id      TEXT NOT NULL DEFAULT '',
    host_id      TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'backup',
    trigger      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    volumes      TEXT NOT NULL DEFAULT '[]',
    snapshots    TEXT NOT NULL DEFAULT '[]',
    stats        TEXT NOT NULL DEFAULT '{}',
    bytes_added  INTEGER NOT NULL DEFAULT 0,
    duration     REAL NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    log          TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_backup_runs_job ON backup_runs (job_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_backup_runs_started ON backup_runs (started_at DESC);

-- Scheduled start/stop (kowan/store/schedules.py). Label-derived schedules are
-- not stored; only their runs (schedule_id 'label:...').
CREATE TABLE IF NOT EXISTS schedules (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    target_type  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    host_id      TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL,
    cron         TEXT NOT NULL,
    timezone     TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    only_if      TEXT NOT NULL DEFAULT '{}',
    last_run_at  TEXT NOT NULL DEFAULT '',
    last_status  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_schedules_target ON schedules (target_type, target_id);

CREATE TABLE IF NOT EXISTS schedule_runs (
    id            TEXT PRIMARY KEY,
    schedule_id   TEXT NOT NULL,
    schedule_name TEXT NOT NULL DEFAULT '',
    target_type   TEXT NOT NULL DEFAULT '',
    target_id     TEXT NOT NULL DEFAULT '',
    host_id       TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL DEFAULT '',
    trigger       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    message       TEXT NOT NULL DEFAULT '',
    scheduled_for TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_schedule_runs_sched ON schedule_runs (schedule_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_started ON schedule_runs (started_at DESC);

-- GitOps syncs, managedBy markers and run history (kowan/store/gitops.py).
CREATE TABLE IF NOT EXISTS gitops_syncs (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    repo_url            TEXT NOT NULL,
    ref                 TEXT NOT NULL DEFAULT 'main',
    path                TEXT NOT NULL DEFAULT '',
    credential_id       TEXT NOT NULL DEFAULT '',
    interval_sec        INTEGER NOT NULL DEFAULT 0,
    auto_apply          INTEGER NOT NULL DEFAULT 0,
    prune               INTEGER NOT NULL DEFAULT 1,
    enabled             INTEGER NOT NULL DEFAULT 1,
    webhook_token       TEXT NOT NULL DEFAULT '',
    webhook_secret      TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT '',
    last_commit         TEXT NOT NULL DEFAULT '',
    last_checked_at     TEXT NOT NULL DEFAULT '',
    last_applied_commit TEXT NOT NULL DEFAULT '',
    last_applied_at     TEXT NOT NULL DEFAULT '',
    last_error          TEXT NOT NULL DEFAULT '',
    plan                TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS gitops_managed (
    kind         TEXT NOT NULL,
    resource_id  TEXT NOT NULL,
    sync_id      TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    applied_hash TEXT NOT NULL DEFAULT '',
    commit_sha   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_gitops_managed_sync ON gitops_managed (sync_id);

CREATE TABLE IF NOT EXISTS gitops_runs (
    id          TEXT PRIMARY KEY,
    sync_id     TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    commit_sha  TEXT NOT NULL DEFAULT '',
    plan        TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_gitops_runs_sync ON gitops_runs (sync_id, started_at DESC);

-- Teams, memberships and resource-scoped grants (kowan/store/teams.py,
-- docs/permissions.md). team_members.source: 'manual' or 'sso:<provider id>'.
CREATE TABLE IF NOT EXISTS teams (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id  TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    source   TEXT NOT NULL DEFAULT 'manual',
    added_at TEXT NOT NULL,
    PRIMARY KEY (team_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members (user_id);

CREATE TABLE IF NOT EXISTS grants (
    id           TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    scope_type   TEXT NOT NULL,
    host_id      TEXT NOT NULL DEFAULT '',
    target       TEXT NOT NULL DEFAULT '',
    level        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_grants_subject ON grants (subject_type, subject_id);

-- Session revocation (kowan/store/sessions.py): a per-user version stamped into
-- each JWT, and single revoked token ids kept until they would expire.
CREATE TABLE IF NOT EXISTS user_sessions (
    user_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti TEXT PRIMARY KEY,
    exp INTEGER NOT NULL
);

-- Single sign-on providers (client secret AES-GCM sealed in `secret`) and
-- external identity links (kowan/store/sso.py, docs/sso.md).
CREATE TABLE IF NOT EXISTS sso_providers (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    type         TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    icon         TEXT NOT NULL DEFAULT '',
    issuer       TEXT NOT NULL DEFAULT '',
    client_id    TEXT NOT NULL DEFAULT '',
    secret       BLOB,
    scopes       TEXT NOT NULL DEFAULT '',
    config       TEXT NOT NULL DEFAULT '{}',
    enabled      INTEGER NOT NULL DEFAULT 1,
    managed      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS user_identities (
    provider_id TEXT NOT NULL,
    subject     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    email       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    last_login  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (provider_id, subject)
);

CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities (user_id);
"""


class _StoreBase:
    """Connection lifecycle + shared query/crypto plumbing for the mixins."""

    def __init__(self, path: str) -> None:
        # check_same_thread=False lets the single shared connection be used from
        # any thread/asyncio executor; _lock serializes access (sqlite is a
        # single writer).
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._cipher: Optional[Any] = None
        # Injected external-secret resolver (secrets.is_external / secrets.build),
        # wired later by the secrets/plugins layer. Kept out of the store to avoid
        # an import cycle. None => external secrets are treated as unresolvable.
        self._external_secrets: Optional[Any] = None
        self._init_db(path)

    def _init_db(self, path: str) -> None:
        with self._lock:
            # foreign_keys ON, WAL journal, and a busy_timeout so a momentarily
            # locked db retries instead of erroring (_lock already serializes
            # access; busy_timeout is the belt-and-braces here).
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            # In WAL mode NORMAL is corruption-safe (only the last commits can be
            # lost on power failure) and skips an fsync per commit, which FULL
            # (the default) pays on every metric/audit/last_seen write.
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(_SCHEMA)
            # Self-update history table (kowan/store/updates.py).
            self._conn.executescript(UPDATES_SCHEMA)
            # Uptime checks, results and incidents (kowan/store/uptime.py).
            self._conn.executescript(UPTIME_SCHEMA)
            # Resource history per workload (kowan/store/workload_metrics.py).
            self._conn.executescript(WORKLOAD_METRICS_SCHEMA)
            # Runbooks and their runs (kowan/store/runbooks.py).
            self._conn.executescript(RUNBOOKS_SCHEMA)
            self._conn.executescript(ALERT_RUNBOOKS_SCHEMA)
            self._conn.executescript(CRASHES_SCHEMA)
            self._conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def use_cipher(self, cipher: Any) -> None:
        """Attach the AES-256-GCM cipher used to seal secret/auth-provider blobs.

        Must be called once after construction
        before any secret or auth-provider method is used. ``cipher`` must expose
        ``encrypt(bytes) -> bytes`` and ``decrypt(bytes) -> bytes``.
        """
        self._cipher = cipher

    def set_external_secrets(self, resolver: Any) -> None:
        """Install the external-secret resolver used by :meth:`resolve_secrets`
        and :meth:`preview_references`. ``resolver`` must expose
        ``is_external(secret_type: str) -> bool`` and
        ``resolve(secret_type, data, field) -> str``. Kept injectable to avoid an
        import cycle with the secrets/plugins layer."""
        self._external_secrets = resolver

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def snapshot(self, dest_path: str) -> None:
        """Write a consistent point-in-time copy via SQLite ``VACUUM INTO``.

        ``dest_path`` must not already exist; SQLite refuses to overwrite.
        """
        if self._path and self._path != ":memory:" and not self._path.startswith("file:"):
            # Use a dedicated read connection: WAL gives it a consistent snapshot
            # without taking self._lock, so a backup (run in a worker thread)
            # no longer freezes every store call on the event loop for the
            # whole VACUUM INTO.
            try:
                conn = sqlite3.connect(self._path)
                try:
                    conn.execute("PRAGMA busy_timeout=5000;")
                    conn.execute("VACUUM INTO ?", (dest_path,))
                finally:
                    conn.close()
                return
            except sqlite3.Error as e:
                raise StoreError(f"vacuum into {dest_path!r}: {e}") from e
        with self._lock:
            try:
                self._conn.execute("VACUUM INTO ?", (dest_path,))
                self._conn.commit()
            except sqlite3.Error as e:
                raise StoreError(f"vacuum into {dest_path!r}: {e}") from e

    # -- query helpers -----------------------------------------------------
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Run a write statement and commit. Returns the cursor (for rowcount)."""
        with self._lock:
            try:
                cur = self._conn.execute(sql, tuple(params))
                self._conn.commit()
                return cur
            except sqlite3.Error as e:
                raise StoreError(str(e)) from e

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Run a read statement returning all rows."""
        with self._lock:
            try:
                cur = self._conn.execute(sql, tuple(params))
                return cur.fetchall()
            except sqlite3.Error as e:
                raise StoreError(str(e)) from e

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        """Run a read statement returning the first row or None."""
        with self._lock:
            try:
                cur = self._conn.execute(sql, tuple(params))
                return cur.fetchone()
            except sqlite3.Error as e:
                raise StoreError(str(e)) from e

    # -- secret blob crypto (shared by secrets + auth providers) ----------
    def _require_cipher(self) -> Any:
        if self._cipher is None:
            raise CipherNotConfiguredError("store: secrets cipher not configured")
        return self._cipher

    def _encrypt_data(self, data: Optional[dict[str, str]]) -> bytes:
        """JSON-encode the field map and seal it with the attached cipher.

        A ``None`` map is sealed as ``{}``.
        """
        cipher = self._require_cipher()
        if data is None:
            data = {}
        try:
            plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise StoreError(f"marshal secret data: {e}") from e
        try:
            return cipher.encrypt(plaintext)
        except Exception as e:  # noqa: BLE001 — surface any cipher failure wrapped
            raise StoreError(f"encrypt secret data: {e}") from e

    def _decrypt_data(self, blob: Optional[bytes]) -> dict[str, str]:
        """Open an encrypted blob into its field map."""
        cipher = self._require_cipher()
        try:
            plaintext = cipher.decrypt(bytes(blob) if blob is not None else b"")
        except Exception as e:  # noqa: BLE001
            raise StoreError(f"decrypt secret data: {e}") from e
        try:
            data = json.loads(plaintext)
        except (TypeError, ValueError) as e:
            raise StoreError(f"unmarshal secret data: {e}") from e
        if not isinstance(data, dict):
            raise StoreError("unmarshal secret data: not an object")
        return {str(k): str(v) for k, v in data.items()}


class Store(
    _RunbookMixin,
    _AlertRunbookMixin,
    _CrashMixin,
    _UserMixin,
    _HostMixin,
    _APIKeyMixin,
    _DeploymentMixin,
    _MetricMixin,
    _CredentialMixin,
    _AlertMixin,
    _AuditMixin,
    _NotificationMixin,
    _SettingsMixin,
    _TemplateMixin,
    _UpdateMixin,
    _WorkflowMixin,
    _ScanMixin,
    _AutoUpdateMixin,
    _TeamMixin,
    _SSOMixin,
    _VolBackupMixin,
    _ScheduleMixin,
    _GitOpsMixin,
    _SessionMixin,
    _UptimeMixin,
    _WorkloadMetricMixin,
    _StoreBase,
):
    """SQLite-backed control-plane store.

    Constructed with the full database file path (normally
    ``<data_dir>/kowan.db``). All CRUD methods
    are contributed by the per-module mixins; see the module docstring for the
    aggregation pattern.
    """


def open_store(path: str) -> Store:
    """Open (creating if necessary) the database at ``path``."""
    return Store(path)
