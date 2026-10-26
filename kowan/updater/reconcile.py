"""Fold finished helper containers back into the ``updates`` table.

Runs on server startup and then in a loop (every 5 s while an update is
``running``, every 30 s otherwise). For each running (or recently ``unknown``)
record it looks for the helper container ``kowan-<component>-updater-<id>`` on
the target daemon:

* helper still running              -> nothing to do yet;
* helper exited                     -> parse its journal + result, rebuild the
  full record (the plan in the helper's own command line carries everything
  the orchestrator knew, so this works even after the server DB was restored
  from a snapshot), save it, remove the helper;
* agent host not connected          -> after 10 min the record becomes
  ``unknown`` (it is still reconciled if the helper is found later);
* no helper and none was launched   -> the orchestrator was interrupted (server
  restart) before touching anything: ``aborted``.

:func:`fold_result` is the pure part (unit-tested).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from . import (
    LABEL_ROLE,
    LABEL_UPDATE_ID,
    COMPONENT_AGENT,
    COMPONENT_SERVER,
    FINAL_STATUSES,
    STATUS_ABORTED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    STATUS_UPDATED,
    helper_name,
)
from . import dbsnap
from .engine import is_not_found
from .orchestrator import parse_ts, read_logs
from .plan import now_rfc3339, parse_journal, plan_from_cmd

log = logging.getLogger("kowan.updater.reconcile")

UNREACHABLE_AFTER = 10 * 60.0
NOT_LAUNCHED_AFTER = 120.0
UNKNOWN_RECHECK = 24 * 3600.0
MAX_LOG_LINES = 400


def _has_launch(phases: List[Dict[str, Any]]) -> bool:
    return any(p.get("phase") == "launch" and p.get("ok") for p in phases or [])


def fold_result(rec: Dict[str, Any], plan: Optional[Dict[str, Any]], journal_text: str,
                exit_code: Optional[int], finished_at: str = "") -> Dict[str, Any]:
    """Return a new, final record from the helper's plan + stdout + exit code."""
    out = dict(rec)
    prec = dict((plan or {}).get("record") or {})
    for key in ("hostId", "startedAt", "startedBy", "batchId", "fromImage", "fromDigest",
                "fromVersion", "toImage", "toDigest", "toVersion"):
        if not out.get(key) and prec.get(key):
            out[key] = prec[key]
    if plan:
        out.setdefault("mode", plan.get("mode"))
        if not out.get("mode"):
            out["mode"] = plan.get("mode")
    pre = list(out.get("phases") or [])
    if not _has_launch(pre):
        pre = list(prec.get("phases") or pre)
    parsed = parse_journal(journal_text)
    helper_phases = parsed["phases"]
    # Idempotence: never append the same helper journal twice.
    if helper_phases and any(p.get("phase") == "wait" for p in pre):
        pre = [p for p in pre if p.get("phase") in ("lock", "queued", "resolve", "pull", "platform",
                                                     "compare", "snapshot", "preflight", "launch")]
    out["phases"] = pre + helper_phases
    result = parsed["result"] or {}
    status = str(result.get("result") or "")
    if status not in FINAL_STATUSES or status == STATUS_UNKNOWN:
        status = STATUS_FAILED
        last = helper_phases[-1]["phase"] if helper_phases else "start"
        out["error"] = (f"the helper exited (code {exit_code}) without a result after phase "
                        f"'{last}'; check the containers on the host")
    else:
        out["error"] = str(result.get("error") or "")
    out["status"] = status
    logs: List[str] = []
    for step in result.get("operator") or []:
        logs.append(str(step))
    if result.get("logs"):
        logs.append("--- logs of the new container ---" if status != STATUS_UPDATED else "--- logs ---")
        logs.extend(str(x) for x in result["logs"])
    for w in result.get("warnings") or []:
        logs.append(f"warning: {w}")
    if status == STATUS_FAILED and parsed["other"]:
        logs.append("--- helper output ---")
        logs.extend(parsed["other"][-100:])
    out["logs"] = logs[-MAX_LOG_LINES:]
    out["finishedAt"] = finished_at or now_rfc3339()
    return out


class Reconciler:
    def __init__(self, svc: Any, manager: Any, *, fast: float = 5.0, slow: float = 30.0,
                 clock=time.time) -> None:
        self.svc = svc
        self.m = manager
        self.fast = fast
        self.slow = slow
        self.clock = clock
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # --------------------------------------------------------------- loop --
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            busy = False
            try:
                busy = await self.reconcile_once(boot=first)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                log.warning("updates: reconcile failed: %s", err)
            first = False
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.fast if busy else self.slow)
            except asyncio.TimeoutError:
                pass

    # -------------------------------------------------------------- once --
    async def reconcile_once(self, boot: bool = False) -> bool:
        """One pass. Returns True while some update is still running."""
        store = self.svc.store
        now = self.clock()
        recs = store.list_updates_by_status([STATUS_RUNNING, STATUS_UNKNOWN])
        busy = False
        for rec in recs:
            if rec["status"] == STATUS_UNKNOWN:
                fin = parse_ts(rec.get("finishedAt") or rec.get("startedAt") or "")
                if fin is None or now - fin > UNKNOWN_RECHECK:
                    continue
            if self.m.live(rec["id"]):
                busy = True
                continue
            try:
                still = await self._one(rec, now)
            except Exception as err:  # noqa: BLE001
                log.warning("updates: reconcile %s: %s", rec["id"], err)
                still = rec["status"] == STATUS_RUNNING
            busy = busy or (still and rec["status"] == STATUS_RUNNING)
        try:
            busy = await self._orphans_local() or busy
        except Exception as err:  # noqa: BLE001
            log.debug("updates: orphan scan: %s", err)
        return busy

    async def _orphans_local(self) -> bool:
        """Helpers on the local daemon whose record is missing or already final.

        A server DB restore can drop the record of the very update that is
        being performed (the snapshot predates it); the helper still carries
        the plan, so the record is rebuilt from it here. Exited preflight
        containers left by an interrupted orchestrator are removed too.
        """
        ops = self.m.local_ops()
        if ops is None:
            return False
        containers = await asyncio.wait_for(ops.list_containers(True), timeout=15)
        busy = False
        for c in containers:
            labels = getattr(c, "labels", None) or {}
            role = str(labels.get(LABEL_ROLE) or "")
            uid = str(labels.get(LABEL_UPDATE_ID) or "")
            if not uid or not (role.endswith("-updater") or role.endswith("-preflight")):
                continue
            if self.m.live(uid):
                continue
            exited = getattr(c, "state", "") in ("exited", "dead")
            if role.endswith("-preflight"):
                if exited:
                    try:
                        await ops.remove_container(c.id, True)
                    except Exception:  # noqa: BLE001
                        pass
                continue
            try:
                rec: Optional[Dict[str, Any]] = self.svc.store.get_update(uid)
            except Exception:  # noqa: BLE001 - store NotFoundError
                rec = None
            if rec is not None and rec["status"] in (STATUS_RUNNING, STATUS_UNKNOWN):
                continue  # the main pass owns it
            if not exited:
                busy = True
                continue
            if rec is not None:
                try:  # final record, leftover helper
                    await ops.remove_container(c.id, True)
                except Exception:  # noqa: BLE001
                    pass
                continue
            insp = await ops.inspect_container(c.id)
            plan = plan_from_cmd((insp.get("Config") or {}).get("Cmd")) or {}
            prec = plan.get("record") or {}
            skeleton = {
                "id": uid, "component": plan.get("component") or role.split("-")[0],
                "hostId": prec.get("hostId"), "mode": plan.get("mode") or "update",
                "status": STATUS_RUNNING, "phases": [], "logs": [], "error": "",
                "startedAt": prec.get("startedAt") or now_rfc3339(), "finishedAt": "",
            }
            await self._fold_helper(skeleton, ops, insp)
        return busy

    def _ops_for(self, rec: Dict[str, Any]) -> Optional[Any]:
        if rec["component"] == COMPONENT_SERVER:
            return self.m.local_ops()
        try:
            host = self.svc.store.get_host(rec.get("hostId") or "")
        except Exception:  # noqa: BLE001 - host deleted
            return None
        if not self.m.host_reachable(host):
            return None
        try:
            return self.m.host_ops(host)
        except Exception:  # noqa: BLE001
            return None

    def _mark(self, rec: Dict[str, Any], status: str, error: str) -> None:
        if rec["status"] == status:
            return
        rec = dict(rec)
        rec["status"] = status
        rec["error"] = rec.get("error") or error
        rec["finishedAt"] = now_rfc3339()
        self.svc.store.save_update(rec)
        log.warning("updates: %s %s -> %s: %s", rec["component"], rec["id"], status, error)

    async def _one(self, rec: Dict[str, Any], now: float) -> bool:
        """Reconcile one record; returns True when it is still in flight."""
        started = parse_ts(rec.get("startedAt") or "") or now
        age = now - started
        ops = self._ops_for(rec)
        last_phase = (rec.get("phases") or [{}])[-1].get("phase", "") if rec.get("phases") else ""
        if ops is None:
            if rec["status"] == STATUS_RUNNING and age > UNREACHABLE_AFTER:
                what = "the agent did not reconnect" if rec["component"] == COMPONENT_AGENT else "Docker unreachable"
                self._mark(rec, STATUS_UNKNOWN, f"{what} within {int(UNREACHABLE_AFTER // 60)} min "
                                                f"(last known phase: {last_phase or 'none'})")
                return False
            return True
        name = helper_name(rec["component"], rec["id"])
        try:
            insp = await asyncio.wait_for(ops.inspect_container(name), timeout=15)
        except Exception as err:  # noqa: BLE001
            if not is_not_found(err):
                return True  # transient; try again later
            insp = None
        if insp is None:
            if rec["status"] != STATUS_RUNNING:
                return False
            if not _has_launch(rec.get("phases") or []):
                if age > NOT_LAUNCHED_AFTER:
                    self._mark(rec, STATUS_ABORTED, "interrupted before the helper was launched "
                                                    "(the server restarted); nothing was changed")
                    return False
                return True
            if age > UNREACHABLE_AFTER:
                self._mark(rec, STATUS_UNKNOWN, f"the helper container {name} disappeared "
                                                f"(last known phase: {last_phase or 'launch'})")
                return False
            return True
        state = insp.get("State") or {}
        if state.get("Status") not in ("exited", "dead"):
            return True
        return await self._fold_helper(rec, ops, insp)

    async def _fold_helper(self, rec: Dict[str, Any], ops: Any, insp: Dict[str, Any]) -> bool:
        """The helper ``insp`` exited: record its outcome, remove it."""
        name = str(insp.get("Name") or "").lstrip("/")
        state = insp.get("State") or {}
        try:
            text = await asyncio.wait_for(read_logs(ops, insp.get("Id") or name, 0), timeout=30)
        except Exception as err:  # noqa: BLE001
            log.warning("updates: helper logs %s: %s", name, err)
            return True
        plan = plan_from_cmd((insp.get("Config") or {}).get("Cmd"))
        code = state.get("ExitCode")
        final = fold_result(rec, plan, text, code)
        self.svc.store.save_update(final)
        try:
            self.svc.store.prune_updates(final["component"], final.get("hostId"), 50)
        except Exception:  # noqa: BLE001
            pass
        log.warning("updates: %s %s %s finished: %s %s", final["component"], final.get("mode"),
                    final["id"], final["status"], final.get("error") or "")
        try:
            await ops.remove_container(insp.get("Id") or name, True)
        except Exception as err:  # noqa: BLE001
            log.info("updates: remove helper %s: %s", name, err)
        if final["component"] == COMPONENT_SERVER:
            self._prune_snapshots()
        self._notify(final)
        return False

    def _prune_snapshots(self) -> None:
        try:
            producer = self.svc.store.last_update(COMPONENT_SERVER, None, [STATUS_UPDATED])
            keep = {producer["id"]} if producer else set()
            dbsnap.prune_snapshots(self.m.data_dir(), keep=3, protect=keep)
        except Exception as err:  # noqa: BLE001
            log.info("updates: snapshot pruning: %s", err)

    def _notify(self, rec: Dict[str, Any]) -> None:
        """Best-effort bell notification for the outcome."""
        try:
            create = getattr(self.svc.store, "create_notification", None)
            if create is None:
                return
            from ..models.notification import Notification
        except Exception:  # noqa: BLE001
            return
        try:
            comp = "Server" if rec["component"] == COMPONENT_SERVER else "Agent"
            level = {STATUS_UPDATED: "info", "rolled_back": "warning", STATUS_ABORTED: "warning"}.get(
                rec["status"], "critical")
            verb = "rollback" if rec.get("mode") == "rollback" else "update"
            title = f"{comp} {verb} {rec['status'].replace('_', ' ')}"
            import uuid as _uuid

            create(Notification(
                id=str(_uuid.uuid4()), kind="notice", level=level, title=title,
                body=rec.get("error") or f"now running {rec.get('toImage')} {rec.get('toVersion') or ''}".strip(),
                hostId=rec.get("hostId") or "", source="updates", refId=rec["id"],
                read=False, createdAt=now_rfc3339(),
            ))
        except Exception as err:  # noqa: BLE001
            log.debug("updates: notification skipped: %s", err)
