"""Retention for the append-only tables: audit_log, alerts, workflow_runs.

Without this they grow forever (every mutating API call writes an audit row,
every rule breach an alert, every run a run row). :func:`prune_retention` applies
the three policies configured in :class:`kowan.config.ServerConfig`
(``KOWAN_AUDIT_RETENTION``, ``KOWAN_ALERT_RETENTION``, ``KOWAN_WORKFLOW_RUNS_KEEP``);
the monitor loop calls it about once an hour, in a worker thread so a large
first prune never stalls the event loop. Each delete is index-backed and
chunked (see the store's ``prune_*`` methods), so the store lock is only held
briefly at a time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

log = logging.getLogger("kowan.store.retention")

DEFAULT_AUDIT_RETENTION = timedelta(days=90)
DEFAULT_ALERT_RETENTION = timedelta(days=30)
DEFAULT_WORKFLOW_RUNS_KEEP = 100


def _rfc3339(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _secs(value: Any) -> float:
    if value is None:
        return 0.0
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


def prune_retention(
    store: Any,
    *,
    audit_retention: Any = DEFAULT_AUDIT_RETENTION,
    alert_retention: Any = DEFAULT_ALERT_RETENTION,
    workflow_runs_keep: int = DEFAULT_WORKFLOW_RUNS_KEEP,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Apply all three retention policies; return rows deleted per table.

    A non-positive retention / keep disables that policy. Each policy is
    independent: one failing (logged) doesn't skip the others.
    """
    now = now or datetime.now(timezone.utc)
    out = {"audit": 0, "alerts": 0, "workflow_runs": 0}

    # getattr: a store without a given prune method (tests' fakes) just skips it.
    audit_secs = _secs(audit_retention)
    if audit_secs > 0 and hasattr(store, "prune_audit"):
        try:
            out["audit"] = store.prune_audit(_rfc3339(now - timedelta(seconds=audit_secs)))
        except Exception as err:  # noqa: BLE001
            log.error("retention: prune audit log: %s", err)

    alert_secs = _secs(alert_retention)
    if alert_secs > 0 and hasattr(store, "prune_resolved_alerts"):
        try:
            out["alerts"] = store.prune_resolved_alerts(
                _rfc3339(now - timedelta(seconds=alert_secs))
            )
        except Exception as err:  # noqa: BLE001
            log.error("retention: prune resolved alerts: %s", err)

    if workflow_runs_keep and workflow_runs_keep > 0 and hasattr(store, "prune_workflow_runs"):
        try:
            out["workflow_runs"] = store.prune_workflow_runs(int(workflow_runs_keep))
        except Exception as err:  # noqa: BLE001
            log.error("retention: prune workflow runs: %s", err)

    if any(out.values()):
        log.info(
            "retention: pruned audit=%d alerts=%d workflow_runs=%d",
            out["audit"], out["alerts"], out["workflow_runs"],
        )
    return out
