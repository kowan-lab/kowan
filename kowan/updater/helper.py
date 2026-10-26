"""The one-shot self-update helper: a transactional container swap.

Runs as ``python -m kowan.<server|agent> self-update --plan <b64>`` inside a
short-lived container started from the target's CURRENT image with the Docker
socket mounted (plus, for the server, the same data mounts as the server so it
can restore the database snapshot). See ``docs/updates-design.md`` §2.

Invariants (each one has a unit test in ``tests/test_updates_helper.py``):

* Nothing is ever deleted before the replacement is proven healthy. The old
  target is renamed ``<name>-previous`` (restart policy ``no``); an older
  ``-previous`` is renamed ``<name>-retired-<id>`` and only removed at commit.
* Any failure after the target was stopped restores it: the new container is
  stopped and renamed ``<name>-failed-<id>`` (removed only once the old one is
  healthy again), the server DB snapshot is restored when the new version had a
  chance to migrate it, and the old container gets its name, its restart policy
  and is started and verified with the same rules.
* If even the restore fails, everything is left in place (both containers, the
  moved-aside database) and the result carries the manual recovery commands.
* Every Engine call has a deadline, so a wedged daemon cannot hang the helper.
* The helper never removes itself: the reconciler reads its journal and exit
  code, records them, then removes it.

Exit codes: 0 for ``updated`` / ``rolled_back`` / ``aborted``; 2 for ``failed``
(operator action needed); 1 only if the helper crashed before writing a result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import (
    LABEL_UPDATE_ID,
    MODE_ROLLBACK,
    STATUS_ABORTED,
    STATUS_FAILED,
    STATUS_ROLLED_BACK,
    STATUS_UPDATED,
)
from . import clone, dbsnap
from .engine import Engine, is_not_found
from .plan import PlanError, decode_plan, journal_line, normalize_plan, result_line
from .verify import wait_verified

_CALL_TIMEOUT = 60.0


class _Abort(Exception):
    """Raised inside a step to bail out with a given status."""


class Swap:
    """One helper run. ``await Swap(engine, plan).run()`` returns the result dict."""

    def __init__(
        self,
        engine: Engine,
        plan: Dict[str, Any],
        *,
        out: Callable[[str], None] = None,  # type: ignore[assignment]
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        restore_db: Callable[..., Any] = dbsnap.restore_snapshot,
    ) -> None:
        self.e = engine
        self.p = normalize_plan(plan)
        self._out = out or (lambda line: print(line, flush=True))
        self._sleep = sleep
        self._mono = monotonic
        self._restore_db = restore_db
        self.phases: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        uid8 = self.p["id"][:8]
        self.name = self.p["target"]
        self.prev = self.p["previous"]
        self.retired_name = f"{self.name}-retired-{uid8}"
        self.failed_name = f"{self.name}-failed-{uid8}"
        self.next_name = f"{self.name}-next-{uid8}"

    # ------------------------------------------------------------------ io --
    def phase(self, name: str, ok: bool, detail: str = "") -> None:
        self.phases.append({"phase": name, "ok": ok, "detail": detail})
        self._out(journal_line(self.p["id"], name, ok, detail))

    def _result(self, status: str, error: str = "", **extra: Any) -> Dict[str, Any]:
        res: Dict[str, Any] = {
            "result": status,
            "id": self.p["id"],
            "component": self.p["component"],
            "mode": self.p["mode"],
            "error": error,
            "warnings": self.warnings,
        }
        res.update(extra)
        res.setdefault("logs", [])
        return res

    async def _call(self, aw: Awaitable[Any], timeout: float = _CALL_TIMEOUT) -> Any:
        return await asyncio.wait_for(aw, timeout=timeout)

    async def _try(self, what: str, aw: Awaitable[Any], timeout: float = _CALL_TIMEOUT) -> bool:
        """Best-effort step: record a warning instead of raising."""
        try:
            await self._call(aw, timeout)
            return True
        except Exception as err:  # noqa: BLE001
            self.warnings.append(f"{what}: {err}")
            return False

    async def _exists(self, ref: str) -> bool:
        try:
            await self._call(self.e.inspect_container(ref))
            return True
        except Exception as err:  # noqa: BLE001
            if is_not_found(err):
                return False
            raise

    async def _verify(self, ref: str) -> tuple:
        async def _insp() -> Dict[str, Any]:
            return await self._call(self.e.inspect_container(ref))

        return await wait_verified(
            _insp,
            timeout=self.p["verifyTimeout"],
            stable_seconds=self.p["stableSeconds"],
            poll_interval=self.p["pollInterval"],
            sleep=self._sleep,
            monotonic=self._mono,
        )

    async def _logs(self, ref: str) -> List[str]:
        try:
            return list(await self._call(self.e.logs_tail(ref, self.p["logTail"])))[-self.p["logTail"]:]
        except Exception as err:  # noqa: BLE001
            return [f"(could not read logs: {err})"]

    def _db_restore(self, snapshot: str, label: str) -> bool:
        db = self.p.get("db") or {}
        aside = str(Path(db.get("backupDir") or (Path(db["path"]).parent / dbsnap.BACKUPS_DIR))
                    / f"{dbsnap.ASIDE_PREFIX}{self.p['id']}-{label}")
        try:
            info = self._restore_db(db["path"], snapshot, aside)
            moved = ", ".join((info or {}).get("moved") or []) if isinstance(info, dict) else ""
            self.phase("restore-db", True, f"restored {snapshot}" + (f"; previous files kept in {aside}" if moved else ""))
            return True
        except Exception as err:  # noqa: BLE001
            self.phase("restore-db", False, f"{snapshot}: {err}")
            return False

    def _operator_steps(self, current_holder: str, restore_name: str, policy: Dict[str, Any]) -> List[str]:
        pol = policy.get("Name") or "no"
        steps = [
            "Automatic restore failed; nothing was deleted. To recover by hand on the host:",
        ]
        if current_holder:
            steps.append(f"docker stop {current_holder}; docker rename {current_holder} {self.name}-broken")
        steps.append(f"docker rename {restore_name} {self.name}")
        steps.append(f"docker update --restart {pol} {self.name}")
        steps.append(f"docker start {self.name}")
        db = self.p.get("db")
        if db:
            snap = db.get("rollbackRestore") or ""
            if snap:
                steps.append(f"(server DB) the pre-update snapshot is {snap}; copy it over {db['path']} "
                             f"while the server is stopped and delete {db['path']}-wal/-shm")
        return steps

    # ----------------------------------------------------------------- run --
    async def run(self) -> Dict[str, Any]:
        try:
            if self.p["waitSeconds"] > 0:
                await self._sleep(self.p["waitSeconds"])
            self.phase("wait", True, f"{self.p['waitSeconds']:.0f}s")
            if self.p["mode"] == MODE_ROLLBACK:
                return await self._run_rollback()
            return await self._run_update()
        except Exception as err:  # noqa: BLE001 - last-resort guard
            self.phase("crash", False, repr(err))
            return self._result(STATUS_FAILED, f"helper error: {err}",
                                operator=self._operator_steps("", self.prev, {"Name": "unless-stopped"}))

    # --------------------------------------------------------- shared bits --
    async def _inspect_target(self) -> Dict[str, Any]:
        try:
            t = await self._call(self.e.inspect_container(self.name))
        except Exception as err:  # noqa: BLE001
            self.phase("inspect", False, f"target {self.name}: {err}")
            raise _Abort(f"target container {self.name} not found: {err}")
        if not ((t.get("State") or {}).get("Running")):
            self.phase("inspect", False, f"target {self.name} is not running")
            raise _Abort(f"target container {self.name} is not running")
        return t

    async def _restart_as_is(self, tid: str, cause: str) -> Dict[str, Any]:
        """Stop/park failed: make sure the original target runs again."""
        await self._try("start target", self.e.start(tid))
        ok, reason = await self._verify(tid)
        self.phase("restore", ok, reason)
        if ok:
            return self._result(STATUS_ROLLED_BACK, cause)
        return self._result(STATUS_FAILED, f"{cause}; target did not come back: {reason}",
                            operator=self._operator_steps("", self.name, {"Name": "unless-stopped"}))

    async def _undo_park(self, tid: str, policy: Dict[str, Any], *, parked: bool,
                         policy_changed: bool, retired: bool) -> None:
        if policy_changed:
            await self._try("restore restart policy", self.e.set_restart_policy(tid, policy))
        if parked:
            await self._try("rename target back", self.e.rename(tid, self.name))
        if retired:
            await self._try("restore previous", self.e.rename(self.retired_name, self.prev))

    # ---------------------------------------------------------- update mode --
    async def _run_update(self) -> Dict[str, Any]:
        p = self.p
        new_ref = p.get("image") or p.get("imageId")
        try:
            t = await self._inspect_target()
            try:
                new_img = await self._call(self.e.inspect_image(p.get("imageId") or new_ref))
            except Exception as err:  # noqa: BLE001
                self.phase("inspect", False, f"new image {new_ref}: {err}")
                raise _Abort(f"new image {new_ref} is not present on the host: {err}")
        except _Abort as ab:
            return self._result(STATUS_ABORTED, str(ab))
        try:
            old_img = await self._call(self.e.inspect_image(t.get("Image") or ""))
        except Exception:  # noqa: BLE001 - only used to strip image defaults
            old_img = {}
            self.warnings.append("old image not inspectable; cloning config verbatim")
        tid = str(t.get("Id") or "")
        policy = clone.restart_policy(t)
        from_image = str((t.get("Config") or {}).get("Image") or "")
        info = {"fromImage": from_image, "fromImageId": t.get("Image") or "",
                "toImage": new_ref, "toImageId": new_img.get("Id") or ""}
        self.phase("inspect", True, f"{self.name} {tid[:12]}: {from_image} -> {new_ref}")

        # stop
        try:
            await self._call(self.e.stop(tid, p["stopTimeout"]), p["stopTimeout"] + 30)
            self.phase("stop", True, f"stopped {tid[:12]}")
        except Exception as err:  # noqa: BLE001
            self.phase("stop", False, str(err))
            return dict(await self._restart_as_is(tid, f"stop failed: {err}"), **info)

        # park
        retired = parked = policy_changed = False
        try:
            if await self._exists(self.prev):
                await self._call(self.e.rename(self.prev, self.retired_name))
                retired = True
            await self._call(self.e.rename(tid, self.prev))
            parked = True
            await self._call(self.e.set_restart_policy(tid, {"Name": "no"}))
            policy_changed = True
            self.phase("park", True, f"{self.name} -> {self.prev} (restart=no)")
        except Exception as err:  # noqa: BLE001
            self.phase("park", False, str(err))
            await self._undo_park(tid, policy, parked=parked, policy_changed=policy_changed, retired=retired)
            return dict(await self._restart_as_is(tid, f"park failed: {err}"), **info)

        # create + start
        nid = ""
        started = False
        try:
            body, extras = clone.clone_body(t, (old_img or {}).get("Config") or {}, new_ref,
                                            labels={LABEL_UPDATE_ID: p["id"]})
            nid = await self._call(self.e.create(self.name, body))
            for net, ep in extras:
                await self._call(self.e.connect(net, nid, ep))
            self.phase("create", True, f"created {nid[:12]} from {new_ref}")
            await self._call(self.e.start(nid))
            started = True
            self.phase("start", True, f"started {nid[:12]}")
        except Exception as err:  # noqa: BLE001
            self.phase("start" if nid else "create", False, str(err))
            return dict(await self._rollback_update(t, nid, policy, retired, started,
                                                    f"{'start' if nid else 'create'} failed: {err}"), **info)

        # verify
        ok, reason = await self._verify(nid)
        self.phase("verify", ok, reason)
        if not ok:
            return dict(await self._rollback_update(t, nid, policy, retired, started,
                                                    f"new version not healthy: {reason}"), **info)

        # commit
        if retired:
            if await self._try("remove retired previous", self.e.remove(self.retired_name, True)):
                self.phase("cleanup", True, f"removed older {self.retired_name}")
        self.phase("commit", True, f"{self.name} now runs {new_ref}; previous kept as {self.prev}")
        return self._result(STATUS_UPDATED, "", container=nid, **info)

    async def _rollback_update(self, t: Dict[str, Any], nid: str, policy: Dict[str, Any],
                               retired: bool, started: bool, cause: str) -> Dict[str, Any]:
        p = self.p
        tid = str(t.get("Id") or "")
        self.phase("rollback", True, cause)
        logs: List[str] = []
        failed_renamed = False
        if nid:
            logs = await self._logs(nid)
            await self._try("stop new container", self.e.stop(nid, p["stopTimeout"]), p["stopTimeout"] + 30)
            await self._try("restart policy of new container", self.e.set_restart_policy(nid, {"Name": "no"}))
            if await self._try("rename new container", self.e.rename(nid, self.failed_name)):
                failed_renamed = True
            elif not await self._try("remove new container", self.e.remove(nid, True)):
                self.phase("restore", False, f"cannot free the name {self.name}")
                return self._result(STATUS_FAILED, f"{cause}; could not free {self.name} for the restore",
                                    logs=logs, operator=self._operator_steps(nid, self.prev, policy))
        db_ok = True
        db = p.get("db")
        if db and started and db.get("rollbackRestore"):
            db_ok = self._db_restore(db["rollbackRestore"], "update")
        try:
            await self._call(self.e.rename(tid, self.name))
            await self._call(self.e.set_restart_policy(tid, policy))
            await self._call(self.e.start(tid))
        except Exception as err:  # noqa: BLE001
            self.phase("restore", False, f"restart previous container: {err}")
            return self._result(STATUS_FAILED, f"{cause}; restore failed: {err}", logs=logs,
                                operator=self._operator_steps(self.failed_name if failed_renamed else "", self.prev, policy))
        ok, reason = await self._verify(tid)
        self.phase("restore", ok, f"previous container: {reason}")
        if not ok:
            return self._result(STATUS_FAILED, f"{cause}; previous version did not come back healthy: {reason}",
                                logs=logs, operator=self._operator_steps(self.name, self.failed_name if failed_renamed else self.prev, policy))
        # Proven healthy again: now it is safe to delete the broken container.
        if failed_renamed:
            await self._try("remove failed container", self.e.remove(self.failed_name, True))
        if retired:
            await self._try("restore older previous", self.e.rename(self.retired_name, self.prev))
        err = cause if db_ok else f"{cause} (WARNING: database snapshot could not be restored)"
        self.phase("cleanup", True, "removed the failed container")
        return self._result(STATUS_ROLLED_BACK, err, logs=logs, container=tid, dbRestored=db_ok)

    # -------------------------------------------------------- rollback mode --
    async def _run_rollback(self) -> Dict[str, Any]:
        p = self.p
        try:
            t = await self._inspect_target()
            try:
                prev = await self._call(self.e.inspect_container(self.prev))
            except Exception as err:  # noqa: BLE001
                self.phase("inspect", False, f"{self.prev}: {err}")
                raise _Abort(f"no previous container {self.prev} to roll back to")
        except _Abort as ab:
            return self._result(STATUS_ABORTED, str(ab))
        tid = str(t.get("Id") or "")
        pid = str(prev.get("Id") or "")
        policy = clone.restart_policy(t)
        info = {"fromImage": str((t.get("Config") or {}).get("Image") or ""), "fromImageId": t.get("Image") or "",
                "toImage": str((prev.get("Config") or {}).get("Image") or ""), "toImageId": prev.get("Image") or ""}
        self.phase("inspect", True, f"{self.name} {tid[:12]} <-> {self.prev} {pid[:12]}")

        try:
            await self._call(self.e.stop(tid, p["stopTimeout"]), p["stopTimeout"] + 30)
            self.phase("stop", True, f"stopped {tid[:12]}")
        except Exception as err:  # noqa: BLE001
            self.phase("stop", False, str(err))
            return dict(await self._restart_as_is(tid, f"stop failed: {err}"), **info)

        parked = policy_changed = swapped = False
        try:
            await self._call(self.e.rename(tid, self.next_name))
            parked = True
            await self._call(self.e.set_restart_policy(tid, {"Name": "no"}))
            policy_changed = True
            await self._call(self.e.rename(pid, self.name))
            swapped = True
            await self._call(self.e.set_restart_policy(pid, policy))
            self.phase("park", True, f"{self.name} -> {self.next_name}; {self.prev} -> {self.name}")
        except Exception as err:  # noqa: BLE001
            self.phase("park", False, str(err))
            if swapped:
                await self._try("rename previous back", self.e.rename(pid, self.prev))
            if policy_changed:
                await self._try("restore restart policy", self.e.set_restart_policy(tid, policy))
            if parked:
                await self._try("rename target back", self.e.rename(tid, self.name))
            return dict(await self._restart_as_is(tid, f"swap failed: {err}"), **info)

        db = p.get("db")
        db_changed = False
        if db and db.get("swapRestore"):
            if not self._db_restore(db["swapRestore"], "swap"):
                return dict(await self._revert_rollback(tid, pid, policy, False, [],
                                                        "could not restore the database of the previous version"), **info)
            db_changed = True
        elif db:
            self.warnings.append("no database snapshot for the previous version; keeping the current database")

        try:
            await self._call(self.e.start(pid))
            self.phase("start", True, f"started {pid[:12]}")
        except Exception as err:  # noqa: BLE001
            self.phase("start", False, str(err))
            return dict(await self._revert_rollback(tid, pid, policy, db_changed, await self._logs(pid),
                                                    f"start failed: {err}"), **info)
        ok, reason = await self._verify(pid)
        self.phase("verify", ok, reason)
        if not ok:
            return dict(await self._revert_rollback(tid, pid, policy, db_changed, await self._logs(pid),
                                                    f"previous version not healthy: {reason}"), **info)
        if await self._try("park rolled-back container", self.e.rename(tid, self.prev)):
            note = f"{self.prev} now holds the version rolled back from"
        else:
            note = f"rolled-back container kept as {self.next_name}"
        self.phase("commit", True, f"{self.name} now runs {info['toImage']}; {note}")
        return self._result(STATUS_UPDATED, "", container=pid, **info)

    async def _revert_rollback(self, tid: str, pid: str, policy: Dict[str, Any], db_changed: bool,
                               logs: List[str], cause: str) -> Dict[str, Any]:
        p = self.p
        self.phase("rollback", True, cause)
        await self._try("stop previous", self.e.stop(pid, p["stopTimeout"]), p["stopTimeout"] + 30)
        await self._try("restart policy of previous", self.e.set_restart_policy(pid, {"Name": "no"}))
        if not await self._try("rename previous back", self.e.rename(pid, self.prev)):
            self.phase("restore", False, f"cannot free the name {self.name}")
            return self._result(STATUS_FAILED, f"{cause}; could not free {self.name}", logs=logs,
                                operator=self._operator_steps(self.name, self.next_name, policy))
        db_ok = True
        db = p.get("db")
        if db_changed and db and db.get("rollbackRestore"):
            db_ok = self._db_restore(db["rollbackRestore"], "revert")
        try:
            await self._call(self.e.rename(tid, self.name))
            await self._call(self.e.set_restart_policy(tid, policy))
            await self._call(self.e.start(tid))
        except Exception as err:  # noqa: BLE001
            self.phase("restore", False, str(err))
            return self._result(STATUS_FAILED, f"{cause}; restore failed: {err}", logs=logs,
                                operator=self._operator_steps("", self.next_name, policy))
        ok, reason = await self._verify(tid)
        self.phase("restore", ok, reason)
        if not ok:
            return self._result(STATUS_FAILED, f"{cause}; current version did not come back healthy: {reason}",
                                logs=logs, operator=self._operator_steps(self.name, self.prev, policy))
        err = cause if db_ok else f"{cause} (WARNING: database could not be restored)"
        return self._result(STATUS_ROLLED_BACK, err, logs=logs, container=tid, dbRestored=db_ok)


# --------------------------------------------------------------------- CLI --


def exit_code(status: str) -> int:
    return 2 if status == STATUS_FAILED else 0


async def run_plan(plan: Dict[str, Any], socket_path: str = "/var/run/docker.sock",
                   engine: Optional[Engine] = None) -> int:
    from .engine import LocalEngine

    eng = engine or LocalEngine(socket_path)
    try:
        res = await Swap(eng, plan).run()
    finally:
        try:
            await eng.close()
        except Exception:  # noqa: BLE001
            pass
    print(result_line(res), flush=True)
    return exit_code(res["result"])


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kowan self-update",
        description="Transactional container swap with automatic rollback "
                    "(launched by the control plane; see docs/updates-design.md).",
    )
    parser.add_argument("--plan", default="", help="base64url JSON update plan")
    parser.add_argument("--socket", default="/var/run/docker.sock", help="Docker socket path")
    ns = parser.parse_args(argv)
    try:
        if not ns.plan:
            raise PlanError("--plan is required")
        plan = decode_plan(ns.plan)
    except PlanError as err:
        print(json.dumps({"result": STATUS_ABORTED, "error": f"invalid plan: {err}", "logs": []}), flush=True)
        return 0
    try:
        return asyncio.run(run_plan(plan, ns.socket))
    except Exception as err:  # noqa: BLE001
        print(json.dumps({"result": STATUS_FAILED, "id": plan.get("id"), "error": f"helper crashed: {err}",
                          "logs": []}), flush=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
