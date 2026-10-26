"""GitOps sync service: fetch -> parse/validate -> plan -> (approve) -> apply.

One :class:`GitOpsService` per control plane (``svc.gitops``). Each sync has
its own shallow checkout under ``<data>/gitops/<sync id>`` (fetched with the
deployment git helpers, so stored ``gitFetch`` connections work unchanged) and
an ``asyncio.Lock`` so refresh/apply of one sync never overlap.

Triggers:
  * manual refresh / apply from the UI or API;
  * the interval loop (``interval_sec`` > 0): refresh when due, then apply
    automatically when ``auto_apply`` is on and the plan is clean;
  * the push webhook ``/hooks/gitops/{token}`` (GitHub/Gitea HMAC or GitLab
    token, verified with the sync's secret), same as the interval path.

Apply always works on the checkout the plan was computed from (the commit the
operator saw); passing that commit makes a stale approval fail with 409.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets as _secrets
import shutil
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Optional

from kowan.deployment import gitsource
from kowan.errors import Conflict, NotFound, Validation

from . import engine
from .kinds import Context
from .resources import scan_tree

log = logging.getLogger("kowan.gitops")

_LOOP_TICK = float(os.environ.get("KOWAN_GITOPS_TICK", "15"))
MIN_INTERVAL = 30


def _now() -> str:
    from kowan.web import now_rfc3339

    return now_rfc3339()


def new_token() -> str:
    return _secrets.token_hex(16)


def _age_seconds(ts: str) -> float:
    from datetime import datetime, timezone

    if not ts:
        return float("inf")
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - t).total_seconds()


def status_of(pr: Optional[engine.PlanResult], error: str = "") -> str:
    if error or pr is None or not pr.ok:
        return "error"
    return "drift" if pr.has_changes else "in_sync"


class GitOpsService:
    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self.store = svc.store
        self._locks: Dict[str, asyncio.Lock] = {}
        self._bg: set = set()

    # -- helpers -----------------------------------------------------------
    def _lock(self, sync_id: str) -> asyncio.Lock:
        lk = self._locks.get(sync_id)
        if lk is None:
            lk = self._locks[sync_id] = asyncio.Lock()
        return lk

    def checkout_dir(self, sync_id: str) -> str:
        cfg = getattr(self.svc, "config", None)
        data_dir = getattr(cfg, "data_dir", "") or "./data"
        return os.path.join(data_dir, "gitops", sync_id)

    def remove_checkout(self, sync_id: str) -> None:
        shutil.rmtree(self.checkout_dir(sync_id), ignore_errors=True)

    def get(self, sync_id: str) -> Dict[str, Any]:
        try:
            return self.store.get_gitops_sync(sync_id)
        except Exception as err:  # noqa: BLE001
            raise NotFound("sync not found") from err

    async def git_auth(self, sync: Dict[str, Any]) -> Optional[gitsource.GitAuth]:
        cid = sync.get("credential_id") or ""
        if not cid:
            return None
        from kowan import capabilities, connections

        try:
            prov = await connections.resolve(self.store, cid, capabilities.CAP_GIT_FETCH)
            username, token = prov.git_auth(sync["repo_url"])
        except Exception as err:  # noqa: BLE001
            raise Validation(f"git credential {cid!r}: {err}") from err
        return gitsource.GitAuth(username=username or "", token=token or "")

    def _ctx(self, sync: Dict[str, Any], commit: str) -> Context:
        return Context(svc=self.svc, checkout=self.checkout_dir(sync["id"]), commit=commit)

    async def _plan_from_checkout(self, sync: Dict[str, Any], commit: str) -> engine.PlanResult:
        dest = self.checkout_dir(sync["id"])
        resources, errors = await asyncio.to_thread(scan_tree, dest, sync.get("path") or "")
        return await engine.plan(self._ctx(sync, commit), sync["id"], resources, errors,
                                 prune=bool(sync.get("prune", True)), generated_at=_now())

    def _record(self, sync: Dict[str, Any], *, trigger: str, action: str, status: str, commit: str,
                plan: Any = None, result: Any = None, error: str = "", started: str = "") -> Dict[str, Any]:
        run = {"id": str(uuid.uuid4()), "sync_id": sync["id"], "trigger": trigger, "action": action,
               "status": status, "commit_sha": commit, "plan": plan, "result": result, "error": error,
               "started_at": started or _now(), "finished_at": _now()}
        self.store.create_gitops_run(run)
        try:
            self.store.prune_gitops_runs(sync["id"], 100)
        except Exception:  # noqa: BLE001
            pass
        return run

    # -- refresh ------------------------------------------------------------
    async def refresh(self, sync_id: str, *, trigger: str = "manual") -> Dict[str, Any]:
        """Fetch the repo and recompute the plan. Returns the updated sync."""
        async with self._lock(sync_id):
            return await self._refresh_locked(sync_id, trigger)

    async def _refresh_locked(self, sync_id: str, trigger: str) -> Dict[str, Any]:
        sync = self.get(sync_id)
        started = _now()
        prev_status, prev_commit = sync.get("status", ""), sync.get("last_commit", "")
        try:
            auth = await self.git_auth(sync)
            commit = await gitsource.clone(sync["repo_url"], sync.get("ref") or "main",
                                           self.checkout_dir(sync_id), auth)
            pr = await self._plan_from_checkout(sync, commit)
            err = ""
        except Exception as e:  # noqa: BLE001 - git/credential failure
            commit, pr, err = sync.get("last_commit", ""), None, str(e)
        sync = self.get(sync_id)
        sync["last_checked_at"] = _now()
        sync["last_error"] = err or ("; ".join(e["message"] for e in pr.plan["errors"][:3]) if pr and not pr.ok else "")
        sync["status"] = status_of(pr, err)
        if pr is not None:
            sync["plan"] = pr.plan
            sync["last_commit"] = commit
        sync["updated_at"] = _now()
        self.store.update_gitops_sync(sync)
        if trigger != "interval" or sync["status"] != prev_status or sync["last_commit"] != prev_commit:
            self._record(sync, trigger=trigger, action="refresh", status=sync["status"], commit=sync["last_commit"],
                         plan=pr.plan if pr else None, error=sync["last_error"], started=started)
        return sync

    # -- apply ------------------------------------------------------------------
    async def apply(self, sync_id: str, *, trigger: str = "manual", expected_commit: str = "") -> Dict[str, Any]:
        async with self._lock(sync_id):
            return await self._apply_locked(sync_id, trigger, expected_commit)

    async def _apply_locked(self, sync_id: str, trigger: str, expected_commit: str) -> Dict[str, Any]:
        sync = self.get(sync_id)
        commit = sync.get("last_commit") or ""
        if not commit or not os.path.isdir(self.checkout_dir(sync_id)):
            raise Conflict("nothing fetched yet: refresh the sync first")
        if expected_commit and expected_commit != commit:
            raise Conflict("the plan is stale (the repository moved): refresh and review again")
        started = _now()
        pr = await self._plan_from_checkout(sync, commit)
        if not pr.ok:
            sync["plan"], sync["status"] = pr.plan, "error"
            sync["last_error"] = "; ".join(e["message"] for e in pr.plan["errors"][:3])
            sync["updated_at"] = _now()
            self.store.update_gitops_sync(sync)
            run = self._record(sync, trigger=trigger, action="apply", status="failed", commit=commit,
                               plan=pr.plan, error="the plan has errors", started=started)
            raise Validation("the plan has errors; fix them in the repository before applying")
        results = await engine.apply(self._ctx(sync, commit), sync_id, pr, now=_now())
        status = engine.run_status(results) if results else "applied"
        after = await self._plan_from_checkout(sync, commit)
        sync = self.get(sync_id)
        sync["plan"] = after.plan
        sync["status"] = status_of(after)
        failed = [r for r in results if r["status"] == "failed"]
        sync["last_error"] = "; ".join(f"{r['kind']} {r['name']}: {r['error']}" for r in failed[:3])
        sync["last_applied_commit"] = commit
        sync["last_applied_at"] = _now()
        sync["updated_at"] = _now()
        self.store.update_gitops_sync(sync)
        run = self._record(sync, trigger=trigger, action="apply", status=status, commit=commit,
                           plan=pr.plan, result=results, error=sync["last_error"], started=started)
        return run

    # -- background ---------------------------------------------------------------
    async def refresh_and_maybe_apply(self, sync_id: str, trigger: str) -> None:
        try:
            sync = await self.refresh(sync_id, trigger=trigger)
            if sync.get("auto_apply") and sync.get("status") == "drift":
                await self.apply(sync_id, trigger=trigger, expected_commit=sync.get("last_commit", ""))
        except Exception as err:  # noqa: BLE001
            log.warning("gitops: %s sync %s failed: %s", trigger, sync_id, err)

    def spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def tick(self) -> int:
        """One pass of the interval loop; returns how many syncs were refreshed."""
        n = 0
        for s in self.store.list_gitops_syncs():
            iv = int(s.get("interval_sec") or 0)
            if not s.get("enabled", True) or iv <= 0:
                continue
            if _age_seconds(s.get("last_checked_at", "")) < max(iv, MIN_INTERVAL):
                continue
            if self._lock(s["id"]).locked():
                continue
            n += 1
            await self.refresh_and_maybe_apply(s["id"], "interval")
        return n

    async def run_loop(self) -> None:
        log.info("gitops loop started (tick=%ss)", _LOOP_TICK)
        try:
            while True:
                await asyncio.sleep(_LOOP_TICK)
                try:
                    from kowan import features

                    if not features.enabled(self.store, "gitops"):
                        continue  # turned off in Settings
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    log.error("gitops tick failed: %s", err)
        except asyncio.CancelledError:
            for t in list(self._bg):
                t.cancel()
            raise

    # -- webhook --------------------------------------------------------------------
    def evaluate_webhook(self, sync: Dict[str, Any], headers, body: bytes) -> gitsource.PushDecision:
        """Reuse the deployment push evaluator: a push to the sync's ref that
        touches its path (or any push when the payload lists no files)."""
        rel = (sync.get("path") or "").strip().strip("/")
        git = SimpleNamespace(
            webhook_secret=sync.get("webhook_secret") or "",
            auto_deploy=bool(sync.get("enabled", True)),
            ref=sync.get("ref") or "main",
            path=(rel + "/_") if rel else "_",
        )
        return gitsource.evaluate_push(git, headers, body)
