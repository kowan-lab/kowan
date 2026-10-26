"""Safe image switch: pull -> gate (scan) -> recreate -> verify -> rollback.

Used by the background auto-updater, by the manual "Update" button
(``POST /hosts/{h}/containers/{id}/update``) and, per container, by the
deployment path. Only Ops methods every client implements are used, plus the
``recreate_with_image`` helper of ``kowan/api/updates.py`` (raw clone on the
local daemon, spec round-trip over the agent tunnel).

Rollback target: the previous image by *repo digest* (``nginx@sha256:...``)
when the old image has one, else its image id. The tag itself (``nginx:latest``)
already points at the new image after the pull, so the rolled-back container
is pinned by digest; the auto-updater remembers the original ref in its state
(``trackedRef``) and the failed digest (``badDigest``) so it neither loses
track of the tag nor retries the same broken image.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from kowan.scan.gate import BLOCK_NEVER, BLOCK_WORSE, gate_decision, parse_block_policy

log = logging.getLogger("kowan.autoupdate")

OUTCOME_UPDATED = "updated"
OUTCOME_CURRENT = "current"
OUTCOME_BLOCKED = "blocked"
OUTCOME_ROLLED_BACK = "rolled_back"
OUTCOME_FAILED = "failed"

ScanFn = Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]


@dataclass
class VerifyOptions:
    timeout: float = 120.0   # overall deadline for "running (+ healthy)"
    stable: float = 10.0     # must stay up (restart count unchanged) this long
    poll: float = 2.0


@dataclass
class Outcome:
    outcome: str
    message: str = ""
    container_id: str = ""
    from_image_id: str = ""
    to_image_id: str = ""
    from_digest: str = ""
    to_digest: str = ""
    rollback_ref: str = ""
    scan: Optional[Dict[str, Any]] = None
    current_scan: Optional[Dict[str, Any]] = None
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome, "message": self.message, "containerId": self.container_id,
            "fromImageId": self.from_image_id, "toImageId": self.to_image_id,
            "fromDigest": self.from_digest, "toDigest": self.to_digest, "scan": self.scan,
        }


async def verify_container(
    ops: Any,
    cid: str,
    opts: VerifyOptions = VerifyOptions(),
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    baseline_restarts: Optional[int] = None,
) -> Tuple[bool, str]:
    """Wait until ``cid`` is running, healthy (when it has a healthcheck) and
    stable (restart count unchanged for ``opts.stable`` seconds), or fail.

    Returns ``(ok, reason)``; never raises for container-state reasons.
    """
    start = clock()
    first = True
    up_since: Optional[float] = None
    base = baseline_restarts
    while True:
        try:
            insp = await ops.inspect_container(cid) or {}
        except Exception as err:  # noqa: BLE001
            return False, f"inspect failed: {err}"
        st = insp.get("State") or {}
        restarts = int(insp.get("RestartCount") or 0)
        if base is None:
            base = restarts
        if restarts > base:
            return False, f"container restarted {restarts - base}x after the update"
        status = str(st.get("Status") or "")
        if st.get("Restarting") or status == "restarting":
            return False, "container is restarting (crash loop)"
        if not st.get("Running"):
            if status == "created" and first:
                pass  # start in flight
            else:
                code = st.get("ExitCode")
                err = st.get("Error") or ""
                return False, f"container is {status or 'not running'} (exit code {code}){': ' + err if err else ''}"
        else:
            if up_since is None:
                up_since = clock()
            health = str(((st.get("Health") or {}).get("Status")) or "")
            if health == "unhealthy":
                return False, "healthcheck reports unhealthy"
            stable = clock() - up_since >= opts.stable
            if stable and health in ("", "none", "healthy"):
                return True, "healthy" if health == "healthy" else "running"
        first = False
        if clock() - start >= opts.timeout:
            health = str(((st.get("Health") or {}).get("Status")) or "")
            if health == "starting":
                return False, f"healthcheck still 'starting' after {int(opts.timeout)}s"
            return False, f"not stable within {int(opts.timeout)}s"
        await sleep(opts.poll)


def rollback_ref_for(old_image: Dict[str, Any], ref: Any) -> str:
    """A reference that resolves to the OLD image even after the tag moved:
    its repo digest for the same repository, else any repo digest, else id."""
    from kowan.api.updates import _repo_digest_name

    digests = [str(d) for d in (old_image.get("RepoDigests") or [])]
    for d in digests:
        name, dig = _repo_digest_name(d)
        if name and ref is not None and name == ref.name and dig:
            return d
    if digests:
        return digests[0]
    return str(old_image.get("Id") or "")


def digest_of(image: Dict[str, Any], ref: Any) -> str:
    from kowan.api.updates import compare_digests

    local, _ = compare_digests(image.get("RepoDigests") or [], ref, "")
    return local


async def apply_container_update(
    ops: Any,
    cid: str,
    *,
    image_ref: str,
    pull: Optional[Callable[[], Awaitable[None]]] = None,
    scan: Optional[ScanFn] = None,
    gate_policy: str = BLOCK_NEVER,
    verify: Optional[VerifyOptions] = VerifyOptions(),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    insp: Optional[Dict[str, Any]] = None,
) -> Outcome:
    """Switch container ``cid`` to the image ``image_ref`` resolves to now.

    ``pull`` (optional) refreshes the tag first. ``scan(image_inspect)`` returns
    a scan record (``{"counts": {...}}``) or ``None``/raises when it cannot
    scan; it is only called when ``gate_policy`` is not ``never``. ``verify``
    ``None`` skips the health verification (e.g. a stopped container).
    """
    from kowan.api.updates import parse_image_ref, recreate_with_image

    gate_policy = parse_block_policy(gate_policy)
    insp = insp or await ops.inspect_container(cid) or {}
    try:
        ref = parse_image_ref(image_ref)
    except ValueError:
        ref = None
    old_image_id = str(insp.get("Image") or "")
    was_running = bool((insp.get("State") or {}).get("Running"))
    name = str(insp.get("Name") or cid).lstrip("/")

    if pull is not None:
        await pull()

    new_img = await ops.inspect_image(image_ref) or {}
    new_image_id = str(new_img.get("Id") or "")
    try:
        old_img = await ops.inspect_image(old_image_id) if old_image_id else {}
    except Exception:  # noqa: BLE001 - old image already gone
        old_img = {}
    old_img = old_img or {}
    out = Outcome(
        outcome=OUTCOME_CURRENT, container_id=cid, from_image_id=old_image_id, to_image_id=new_image_id,
        from_digest=digest_of(old_img, ref) if ref else "", to_digest=digest_of(new_img, ref) if ref else "",
    )
    if not new_image_id:
        out.outcome, out.message = OUTCOME_FAILED, f"image {image_ref} not found after pull"
        return out
    if new_image_id == old_image_id:
        out.message = "already running the latest image"
        return out

    # -- gate ---------------------------------------------------------------
    if gate_policy != BLOCK_NEVER and scan is not None:
        new_scan: Optional[Dict[str, Any]] = None
        try:
            new_scan = await scan(new_img)
        except Exception as err:  # noqa: BLE001 - fail closed below
            out.details.append(f"scan new image: {err}")
        cur_scan: Optional[Dict[str, Any]] = None
        if gate_policy == BLOCK_WORSE and old_img:
            try:
                cur_scan = await scan(old_img)
            except Exception as err:  # noqa: BLE001
                out.details.append(f"scan current image: {err}")
        out.scan = (new_scan or {}).get("counts") if new_scan else None
        out.current_scan = (cur_scan or {}).get("counts") if cur_scan else None
        blocked, reason = gate_decision(
            gate_policy,
            out.scan if new_scan and new_scan.get("status", "ok") == "ok" else None,
            out.current_scan if cur_scan and cur_scan.get("status", "ok") == "ok" else None,
        )
        if blocked:
            out.outcome = OUTCOME_BLOCKED
            out.message = f"blocked by scan policy '{gate_policy}': {reason}"
            if out.details:
                out.message += f" ({'; '.join(out.details)})"
            return out

    # -- switch ---------------------------------------------------------------
    out.rollback_ref = rollback_ref_for(old_img, ref) if old_img else old_image_id
    try:
        new_cid = await recreate_with_image(ops, cid, insp, image_ref)
    except Exception as err:  # noqa: BLE001
        return await _recover_failed_recreate(ops, out, name, old_image_id, was_running, err, verify, clock, sleep)
    out.container_id = new_cid
    if verify is None or not was_running:
        out.outcome = OUTCOME_UPDATED
        out.message = "recreated (not verified: container was not running)" if not was_running else "recreated"
        return out

    ok, reason = await verify_container(ops, new_cid, verify, clock=clock, sleep=sleep, baseline_restarts=0)
    if ok:
        out.outcome, out.message = OUTCOME_UPDATED, f"updated and verified ({reason})"
        return out

    # -- rollback ---------------------------------------------------------------
    log.warning("autoupdate: %s failed verification (%s); rolling back to %s", name, reason, out.rollback_ref)
    try:
        new_insp = dict(await ops.inspect_container(new_cid) or {})
        # The crashed container is "not running"; the rollback must come back
        # up like the original did (recreate_with_image stops it otherwise).
        new_insp["State"] = {**(new_insp.get("State") or {}), "Running": True}
        rb_cid = await recreate_with_image(ops, new_cid, new_insp, out.rollback_ref)
    except Exception as err:  # noqa: BLE001
        out.outcome = OUTCOME_FAILED
        out.message = f"verification failed ({reason}) and rollback failed: {err}"
        return out
    out.container_id = rb_cid
    rb_ok, rb_reason = await verify_container(ops, rb_cid, verify, clock=clock, sleep=sleep, baseline_restarts=0)
    out.outcome = OUTCOME_ROLLED_BACK
    out.message = f"verification failed ({reason}); rolled back to {out.rollback_ref}" + (
        "" if rb_ok else f" (previous image is not healthy either: {rb_reason})"
    )
    return out


async def _recover_failed_recreate(ops, out: Outcome, name: str, old_image_id: str, was_running: bool,
                                   err: Exception, verify, clock, sleep) -> Outcome:
    """The switch itself failed (e.g. the new image cannot start). The remote
    path already put the old container back; the local raw clone leaves a
    created-but-not-started container on the NEW image under the same name:
    recreate that one from the previous image."""
    from kowan.api.updates import recreate_with_image

    try:
        cur = await ops.inspect_container(name) or {}
    except Exception:  # noqa: BLE001 - nothing left under that name
        cur = {}
    if cur and str(cur.get("Image") or "") == old_image_id:
        out.outcome = OUTCOME_ROLLED_BACK
        out.container_id = str(cur.get("Id") or "")
        out.message = f"switch failed ({err}); previous container restored"
        return out
    if not cur or not out.rollback_ref:
        out.outcome, out.message = OUTCOME_FAILED, f"recreate {name}: {err}"
        return out
    cur = dict(cur)
    cur["State"] = {**(cur.get("State") or {}), "Running": was_running}
    try:
        rb_cid = await recreate_with_image(ops, str(cur.get("Id") or name), cur, out.rollback_ref)
    except Exception as err2:  # noqa: BLE001
        out.outcome, out.message = OUTCOME_FAILED, f"recreate {name}: {err}; rollback failed: {err2}"
        return out
    out.container_id = rb_cid
    out.outcome = OUTCOME_ROLLED_BACK
    out.message = f"switch failed ({err}); rolled back to {out.rollback_ref}"
    if verify is not None and was_running:
        ok, reason = await verify_container(ops, rb_cid, verify, clock=clock, sleep=sleep, baseline_restarts=0)
        if not ok:
            out.message += f" (previous image is not healthy either: {reason})"
    return out
