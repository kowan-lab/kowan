"""Run a short-lived restic helper container on a host and collect its output.

Only the generic Docker ``Ops`` surface is used (create/start/inspect/logs/
remove, image inspect/pull), so the same code drives the local daemon and any
remote agent over the tunnel: no new agent RPC is needed.

Learner note: Ops has no "wait" call, so completion is detected by polling
``inspect_container`` once a second. That costs one tiny request per second
for the duration of a backup, which is negligible next to restic's own I/O.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from kowan.models import ContainerSpec, KeyValue, VolumeMapping

from .restic import RESTIC_IMAGE, strip_timestamps

log = logging.getLogger("kowan.volbackup.executor")

HELPER_LABEL = "kowan.volbackup.helper"
# Bound on log bytes read back from a helper (a huge `ls` must not OOM us).
_MAX_LOG_BYTES = 32 * 1024 * 1024


@dataclass
class HelperResult:
    exit_code: int
    lines: List[str]
    timed_out: bool = False


async def ensure_image(ops, image: str = RESTIC_IMAGE) -> None:
    """Pull ``image`` on the host unless it is already present."""
    try:
        info = await ops.inspect_image(image)
        if info:
            return
    except Exception:  # noqa: BLE001 - not present (or remote error): pull
        pass
    stream = await ops.pull_image(image, "")
    try:
        async for _ in stream:
            pass
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            res = close()
            if hasattr(res, "__await__"):
                await res


async def _read_logs(ops, cid: str) -> List[str]:
    stream = await ops.container_logs(cid, False, 0)
    buf = bytearray()
    try:
        async for chunk in stream:
            if chunk:
                buf.extend(chunk)
                if len(buf) > _MAX_LOG_BYTES:
                    break
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            res = close()
            if hasattr(res, "__await__"):
                await res
    return strip_timestamps(buf.decode("utf-8", "replace").splitlines())


async def run_helper(
    ops,
    script: str,
    *,
    env: Dict[str, str],
    binds: List[Tuple[str, str, bool]],
    network: str = "",
    timeout: float = 6 * 3600,
    label: str = "",
    image: str = RESTIC_IMAGE,
    poll: float = 1.0,
) -> HelperResult:
    """Create + start the helper, wait for it to exit, return exit code + log
    lines. The container is always force-removed afterwards."""
    await ensure_image(ops, image)
    name = "kowan-restic-" + uuid.uuid4().hex[:10]
    spec = ContainerSpec(
        image=image,
        entrypoint=["/bin/sh", "-c"],
        command=[script],
        env=[KeyValue(key=k, value=v) for k, v in env.items()],
        volumes=[VolumeMapping(source=s, target=t, read_only=ro) for s, t, ro in binds],
        networks=[network] if network else [],
        labels={HELPER_LABEL: label or "1"},
    )
    cid = await ops.create_container(name, spec, False)
    timed_out = False
    exit_code = -1
    try:
        await ops.start_container(cid)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            info = await ops.inspect_container(cid)
            state = (info or {}).get("State") or {}
            if not state.get("Running", False) and state.get("Status") not in ("created", "restarting"):
                exit_code = int(state.get("ExitCode", -1))
                break
            if loop.time() > deadline:
                timed_out = True
                break
            await asyncio.sleep(poll)
        lines = await _read_logs(ops, cid)
    finally:
        try:
            await ops.remove_container(cid, True)
        except Exception as err:  # noqa: BLE001
            log.warning("volbackup: remove helper %s: %s", name, err)
    if timed_out:
        lines.append(f"@@kowan timeout after {int(timeout)}s")
    return HelperResult(exit_code=exit_code, lines=lines, timed_out=timed_out)
