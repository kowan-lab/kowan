"""Access to the shared self-update manager (:mod:`kowan.updater`).

The transactional server and agent updates (lock, pull, preflight, DB
snapshot, helper swap with automatic rollback) live in :mod:`kowan.updater`;
the REST handlers (``kowan/api/serverupdate.py``, the agent section of
``kowan/api/hosts.py``) and the startup reconciler reach the one
:class:`~kowan.updater.orchestrator.UpdateManager` through :func:`_manager`.
"""

from __future__ import annotations

from typing import Any


def _manager(svc: Any) -> Any:
    """The shared :class:`kowan.updater.orchestrator.UpdateManager` (lazily attached)."""
    try:
        return svc.updates
    except AttributeError:
        from kowan.updater.orchestrator import UpdateManager

        mgr = UpdateManager(svc)
        svc.attach("updates", mgr)
        return mgr
