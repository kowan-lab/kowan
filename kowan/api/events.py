"""Docker events: a bounded recent history (JSON) and a live WebSocket stream.

Routes (mounted by :func:`register`):

    GET /api/v1/hosts/{hostID}/events      -> recent events, newest first (JSON list)
    GET /api/v1/hosts/{hostID}/events/ws   -> WebSocket, one event per text message

Both are read-only, so viewers may use them (no role gate, like logs/stats).

Query parameters:

  - since / until: unix seconds. The JSON endpoint defaults to the last hour
    and refuses to look further back than :data:`MAX_LOOKBACK`; it returns at
    most :data:`MAX_EVENTS` (the newest). The WebSocket accepts ``since`` to
    replay from a point (also clamped) before following live.
  - type: ``container``/``image``/``network``/``volume``, comma-separated for
    several. Anything else is a 400. The WebSocket also takes ``pull``: the
    progress of the host's image pulls (:mod:`kowan.docker.pulls`), one
    ``{"type": "pull", ...}`` summary per message: all of them for who sees
    the whole host, the user's own for a team member. ``type=pull`` alone
    opens no Docker events, so a team member can follow their pulls.

The Ops layer (local Engine socket or the agent over the mux) yields the raw
Engine NDJSON; this module normalises each line into a compact shape so the UI
never depends on the Engine's field names or its version-dependent quirks
(``status``/``id``/``from`` on old engines, ``Action``/``Actor`` on new ones).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aiohttp import web

from .. import jsonx
from ..docker import pulls
from ..errors import BadGateway, Validation
from ..web import json_response
from ._dockerws import _lines, _safe_close, _spawn_reader, _write_close_error, prepare_ws, resolve_ops

log = logging.getLogger("kowan.api.events")

EVENT_TYPES = ("container", "image", "network", "volume")
PULL_TYPE = "pull"
DEFAULT_LOOKBACK = 3600  # seconds
MAX_LOOKBACK = 24 * 3600
MAX_EVENTS = 500
# The Engine ends a bounded (until=...) replay on its own; this only guards
# against a wedged daemon or tunnel holding the HTTP request open forever.
HISTORY_TIMEOUT = 15.0

# Attributes worth surfacing. Docker merges every container label into the
# attribute map, so a whitelist keeps rows readable.
_KEEP_ATTRS = (
    "exitCode",
    "signal",
    "container",
    "driver",
    "destination",
    "execDuration",
    "type",
)


def _rfc3339(ts_nano: int, ts: int) -> str:
    if ts_nano:
        secs, nanos = divmod(int(ts_nano), 1_000_000_000)
    else:
        secs, nanos = int(ts or 0), 0
    dt = datetime.fromtimestamp(secs, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos // 1_000_000:03d}Z"


def normalize_event(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one Engine event document to the compact UI shape.

    Returns ``None`` for documents that are not events (e.g. an error object).
    Shape::

        {id, time, type, action, actorId, actorName, image, attributes}

    ``action`` is the verb only (``exec_start: sh -c ...`` -> ``exec_start``,
    ``health_status: healthy`` -> ``health_status``); the suffix moves to
    ``attributes.detail``. ``id`` is a stable dedup key for merging a history
    fetch with a live stream.
    """
    if not isinstance(raw, dict):
        return None
    actor = raw.get("Actor") or {}
    attrs_in: Dict[str, Any] = dict(actor.get("Attributes") or {})
    etype = str(raw.get("Type") or ("container" if raw.get("status") else "")).strip()
    action_full = str(raw.get("Action") or raw.get("status") or "").strip()
    if not etype or not action_full:
        return None
    actor_id = str(actor.get("ID") or raw.get("id") or "")

    action, _, detail = action_full.partition(":")
    action = action.strip()
    detail = detail.strip()

    attrs: Dict[str, str] = {}
    for k in _KEEP_ATTRS:
        v = attrs_in.get(k)
        if v not in (None, ""):
            attrs[k] = str(v)
    if detail:
        attrs["detail"] = detail

    name = str(attrs_in.get("name") or "")
    image = ""
    if etype == "container":
        image = str(attrs_in.get("image") or raw.get("from") or "")
    elif etype == "image":
        image = name or actor_id
    if etype == "volume" and not name:
        name = actor_id  # a volume's ID is its name

    ts_nano = int(raw.get("timeNano") or 0)
    ts = int(raw.get("time") or 0)
    return {
        "id": f"{ts_nano or ts}-{etype}-{actor_id[:24]}-{action}",
        "time": _rfc3339(ts_nano, ts),
        "type": etype,
        "action": action,
        "actorId": actor_id,
        "actorName": name.lstrip("/"),
        "image": image,
        "attributes": attrs,
    }


def parse_event_line(line: bytes) -> Optional[Dict[str, Any]]:
    """Decode + normalise one NDJSON line; ``None`` for blank/invalid lines."""
    line = line.strip()
    if not line:
        return None
    try:
        return normalize_event(jsonx.loads(line))
    except (ValueError, TypeError):
        return None


def _int_param(request: web.Request, key: str) -> Optional[int]:
    v = request.query.get(key, "").strip()
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError as err:
        raise Validation(f"invalid {key}: must be unix seconds") from err


def _types(request: web.Request, extra: tuple = ()) -> List[str]:
    raw = request.query.get("type", "").strip()
    types = [t.strip().lower() for t in raw.split(",") if t.strip()]
    allowed = EVENT_TYPES + extra
    bad = [t for t in types if t not in allowed]
    if bad:
        raise Validation(
            f"invalid type {bad[0]!r}: expected one of {', '.join(allowed)}"
        )
    return sorted(set(types))


def _filters(request: web.Request) -> Dict[str, List[str]]:
    types = _types(request)
    return {"type": types} if types else {}


def _clamp_since(since: Optional[int], now: int, default: int) -> int:
    if since is None or since <= 0:
        return now - default
    return max(since, now - MAX_LOOKBACK)


async def recent_events(request: web.Request) -> web.Response:
    """GET /hosts/{hostID}/events — bounded history, newest first."""
    ops = resolve_ops(request)
    now = int(time.time())
    until = _int_param(request, "until")
    if until is None or until <= 0 or until > now:
        until = now
    since = _clamp_since(_int_param(request, "since"), now, DEFAULT_LOOKBACK)
    if since >= until:
        raise Validation("since must be before until")
    filters = _filters(request)

    try:
        stream = await ops.events(since, until, filters or None)
    except NotImplementedError as err:
        raise BadGateway("events are not supported by this host") from err
    except Exception as err:  # noqa: BLE001
        raise BadGateway(str(err)) from err

    out: List[Dict[str, Any]] = []

    async def _collect() -> None:
        async for line in _lines(stream):
            ev = parse_event_line(line)
            if ev is not None:
                out.append(ev)
                if len(out) > MAX_EVENTS * 2:
                    del out[: len(out) - MAX_EVENTS]

    try:
        await asyncio.wait_for(_collect(), timeout=HISTORY_TIMEOUT)
    except asyncio.TimeoutError:
        log.debug("events history timed out; returning %d partial events", len(out))
    except Exception as err:  # noqa: BLE001
        if not out:
            raise BadGateway(str(err)) from err
    finally:
        await _safe_close(stream)

    out = out[-MAX_EVENTS:]
    out.reverse()
    return json_response(out)


async def events_ws(request: web.Request) -> web.WebSocketResponse:
    """GET /hosts/{hostID}/events/ws — live events, one JSON object per message."""
    # Resolve and validate BEFORE upgrading so errors are plain HTTP responses.
    ops = resolve_ops(request)
    types = _types(request, (PULL_TYPE,))
    docker_types = [t for t in types if t != PULL_TYPE]
    follow_pulls = PULL_TYPE in types
    since_q = _int_param(request, "since")
    since: Optional[int] = None
    if since_q:
        since = _clamp_since(since_q, int(time.time()), DEFAULT_LOOKBACK)

    ws = await prepare_ws(request)
    if follow_pulls and not docker_types:
        await _forward_pulls_only(request, ws)
        return ws
    try:
        stream = await ops.events(since, None, {"type": docker_types} if docker_types else None)
    except Exception as err:  # noqa: BLE001 - report to the socket then close
        msg = "events are not supported by this host" if isinstance(err, NotImplementedError) else str(err)
        await _write_close_error(ws, msg)
        return ws

    reader = _spawn_reader(ws, lambda: _safe_close(stream))
    forwarder = _forward_pulls(request, ws) if follow_pulls else None
    # While a client follows the live stream, image events (pull/tag/untag/
    # delete/import/load, incl. ones done outside Kowan) drop the host's cached
    # image list (kowan/docker/imagecache.py) so the next list is fresh.
    invalidate_images = getattr(ops, "invalidate_image_cache", None)
    try:
        async for line in _lines(stream):
            ev = parse_event_line(line)
            if ev is None:
                continue
            if invalidate_images is not None and ev.get("type") == "image":
                invalidate_images()
            try:
                await ws.send_str(jsonx.dumps(ev).decode("utf-8"))
            except (ConnectionResetError, RuntimeError):
                break
        if not ws.closed:
            await ws.close(code=1000)
    except Exception as err:  # noqa: BLE001
        log.debug("events stream ended: %s", err)
    finally:
        reader.cancel()
        if forwarder is not None:
            forwarder.cancel()
        await _safe_close(stream)
    return ws


def _forward_pulls(request: web.Request, ws: web.WebSocketResponse) -> "asyncio.Task[None]":
    """Send the host's pull summaries to ``ws`` until cancelled."""
    host_id = request.match_info.get("hostID", "")
    only = pulls.visible_to(host_id)

    async def run() -> None:
        q = pulls.TRACKER.subscribe(host_id, only)
        try:
            while not ws.closed:
                snap = await q.get()
                await ws.send_str(jsonx.dumps(snap).decode("utf-8"))
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            pulls.TRACKER.unsubscribe(host_id, q)

    return asyncio.ensure_future(run())


async def _forward_pulls_only(request: web.Request, ws: web.WebSocketResponse) -> None:
    """``type=pull`` alone: no Docker events, the pulls until the client goes."""
    gone = asyncio.Event()
    reader = _spawn_reader(ws, gone.set)
    forwarder = _forward_pulls(request, ws)
    try:
        await gone.wait()
    finally:
        reader.cancel()
        forwarder.cancel()
        if not ws.closed:
            await ws.close(code=1000)


def register(app: web.Application, svc) -> None:
    """Mount the events routes (read-only: viewers allowed)."""
    from ..web import api_route

    api_route(app, "GET", "/hosts/{hostID}/events", recent_events)
    api_route(app, "GET", "/hosts/{hostID}/events/ws", events_ws)
