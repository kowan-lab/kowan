"""The periodic digest: one message, daily or weekly, with what needs
attention but did not warrant an alert of its own.

Sections: container updates waiting, images with critical or high CVEs,
backups that failed in the period, certificates that expire soon (uptime
checks and the TLS certificates of exposed domains) and alerts still open.
Empty sections are left out; with nothing at all to say the digest is not
sent unless ``sendEmpty`` is on. It goes to the chosen channels (all enabled
ones when none is chosen) through the same providers as alerts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from kowan.notify.provider import Message

log = logging.getLogger("kowan.digest")

SECTIONS = ("updates", "cves", "backups", "certificates", "alerts")
CERT_WARN_DAYS = 21
_LIST = 10
_CHECK_EVERY = 600.0
_SETTINGS = "digest.settings"
_LAST = "digest.last"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": False, "every": "weekly", "weekday": 0, "hour": 8, "channels": [],
    "sections": {s: True for s in SECTIONS}, "sendEmpty": False,
}


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def get_settings(store: Any) -> Dict[str, Any]:
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    raw = store.get_setting(_SETTINGS)
    if raw:
        saved = json.loads(raw)
        out.update({k: v for k, v in saved.items() if k != "sections"})
        out["sections"].update(saved.get("sections") or {})
    return out


def validate_settings(body: Dict[str, Any], channel_ids: List[str]) -> Dict[str, Any]:
    unknown = set(body) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError(f"unknown field {sorted(unknown)[0]!r}")
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    out.update({k: v for k, v in body.items() if k != "sections"})
    sections = body.get("sections", {})
    if not isinstance(sections, dict) or set(sections) - set(SECTIONS):
        raise ValueError(f"sections may only name {', '.join(SECTIONS)}")
    out["sections"].update(sections)
    if out["every"] not in ("daily", "weekly"):
        raise ValueError('every must be "daily" or "weekly"')
    for key, lo, hi in (("hour", 0, 23), ("weekday", 0, 6)):
        if not isinstance(out[key], int) or isinstance(out[key], bool) or not lo <= out[key] <= hi:
            raise ValueError(f"{key} must be a whole number between {lo} and {hi}")
    for key in ("enabled", "sendEmpty"):
        if not isinstance(out[key], bool):
            raise ValueError(f"{key} must be true or false")
    if not all(isinstance(v, bool) for v in out["sections"].values()):
        raise ValueError("sections values must be true or false")
    if not isinstance(out["channels"], list) or any(c not in channel_ids for c in out["channels"]):
        raise ValueError("channels must be ids of existing channels")
    return out


def save_settings(store: Any, settings: Dict[str, Any]) -> None:
    store.set_setting(_SETTINGS, json.dumps(settings, sort_keys=True))


def last_sent(store: Any) -> Optional[Dict[str, Any]]:
    raw = store.get_setting(_LAST)
    return json.loads(raw) if raw else None


def period(settings: Dict[str, Any]) -> timedelta:
    return timedelta(days=1 if settings["every"] == "daily" else 7)


def due(settings: Dict[str, Any], last_at: str, now: datetime) -> bool:
    if not settings["enabled"] or now.hour != settings["hour"]:
        return False
    if settings["every"] == "weekly" and now.weekday() != settings["weekday"]:
        return False
    last = _parse(last_at) if last_at else None
    return last is None or now - last > timedelta(hours=20)


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


async def _updates(svc: Any, hosts: List[Any]) -> List[Dict[str, Any]]:
    from kowan.api.updates import check_host

    out: List[Dict[str, Any]] = []
    for h in hosts:
        try:
            rows = await asyncio.wait_for(check_host(svc, svc.docker.ops(h)), 60)
        except Exception as err:  # noqa: BLE001 - offline host: skip it
            log.debug("digest: updates on %s: %s", h.id, err)
            continue
        out += [{"host": h.name, "container": r["containerName"], "image": r["image"]} for r in rows if r.get("updateAvailable")]
    return out


def _cves(store: Any, host_names: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = [s for s in store.list_image_scans() if s["status"] == "ok" and (s["counts"]["critical"] or s["counts"]["high"])]
    rows.sort(key=lambda s: (-s["counts"]["critical"], -s["counts"]["high"]))
    return [{"image": s["imageRef"] or s["imageId"][:19], "host": host_names.get(s["hostId"], s["hostId"]),
             "critical": s["counts"]["critical"], "high": s["counts"]["high"], "fixable": s["fixable"]} for s in rows]


def _backups(store: Any, since: str, host_names: Dict[str, str]) -> List[Dict[str, Any]]:
    return [{"job": r["jobName"] or r["kind"], "host": host_names.get(r["hostId"], r["hostId"]), "at": r["finishedAt"] or r["startedAt"],
             "error": (r["error"] or "").splitlines()[0][:200] if r["error"] else ""}
            for r in store.list_backup_runs(limit=500) if r["status"] == "failed" and (r["finishedAt"] or r["startedAt"]) >= since]


async def _certificates(svc: Any, hosts: List[Any], now: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in svc.store.list_uptime_checks():
        exp = _parse(c.get("cert_expires_at") or "")
        if exp is not None:
            days = (exp - now).days
            if days <= CERT_WARN_DAYS:
                out.append({"name": c["target"], "where": f"uptime check {c['name']}", "daysLeft": days})
    from kowan.exposure.service import certificates

    for h in hosts:
        try:
            view = await asyncio.wait_for(certificates(svc, h), 30)
        except Exception as err:  # noqa: BLE001 - no proxy, offline host
            log.debug("digest: certificates on %s: %s", h.id, err)
            continue
        for row in view.get("domains") or []:
            cert = row.get("certificate") or {}
            days = cert.get("daysLeft")
            if isinstance(days, (int, float)) and days <= CERT_WARN_DAYS:
                out.append({"name": row["domain"], "where": f"{h.name} · {row.get('proxy', '')}", "daysLeft": int(days)})
    seen, uniq = set(), []
    for c in sorted(out, key=lambda c: c["daysLeft"]):
        if c["name"] not in seen:
            seen.add(c["name"])
            uniq.append(c)
    return uniq


def _alerts(store: Any) -> List[Dict[str, Any]]:
    return [{"title": a.title, "level": a.level, "since": a.created_at}
            for a in store.list_alerts(500) if not a.resolved]


async def build(svc: Any, settings: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """The digest's content for the period ending ``now``."""
    now = now or datetime.now(timezone.utc)
    since = _ts(now - period(settings))
    hosts = svc.store.list_hosts()
    names = {h.id: h.name for h in hosts}
    on = settings["sections"]
    sections: Dict[str, List[Dict[str, Any]]] = {}
    if on.get("updates"):
        sections["updates"] = await _updates(svc, hosts)
    if on.get("cves"):
        sections["cves"] = _cves(svc.store, names)
    if on.get("backups"):
        sections["backups"] = _backups(svc.store, since, names)
    if on.get("certificates"):
        sections["certificates"] = await _certificates(svc, hosts, now)
    if on.get("alerts"):
        sections["alerts"] = _alerts(svc.store)
    return {"since": since, "until": _ts(now), "sections": sections, "empty": not any(sections.values())}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _block(title: str, items: List[Dict[str, Any]], line: Any) -> List[str]:
    out = [f"{title} ({len(items)})"] + [f"  • {line(x)}" for x in items[:_LIST]]
    if len(items) > _LIST:
        out.append(f"  …and {len(items) - _LIST} more")
    return out


def _cert_left(x: Dict[str, Any]) -> str:
    return f"expired {-x['daysLeft']} days ago" if x["daysLeft"] < 0 else f"{x['daysLeft']} days left"


def render(d: Dict[str, Any]) -> Message:
    """Plain text that reads well in mail, chat and push alike."""
    s = d["sections"]
    blocks: List[List[str]] = []
    summary: List[str] = []
    if s.get("updates"):
        summary.append(_plural(len(s["updates"]), "update"))
        blocks.append(_block("Updates waiting", s["updates"], lambda x: f"{x['container']} on {x['host']}: {x['image']}"))
    if s.get("cves"):
        summary.append(_plural(len(s["cves"]), "vulnerable image"))
        blocks.append(_block("Images with critical or high vulnerabilities", s["cves"],
                             lambda x: f"{x['image']} ({x['host']}): {x['critical']} critical, {x['high']} high, {x['fixable']} fixable"))
    if s.get("backups"):
        summary.append(_plural(len(s["backups"]), "failed backup"))
        blocks.append(_block("Failed backups", s["backups"],
                             lambda x: f"{x['job']} on {x['host']} at {x['at']}" + (f": {x['error']}" if x["error"] else "")))
    if s.get("certificates"):
        summary.append(_plural(len(s["certificates"]), "certificate") + " expiring")
        blocks.append(_block(f"Certificates expiring within {CERT_WARN_DAYS} days", s["certificates"],
                             lambda x: f"{x['name']} ({x['where']}): {_cert_left(x)}"))
    if s.get("alerts"):
        summary.append(_plural(len(s["alerts"]), "open alert"))
        blocks.append(_block("Alerts still open", s["alerts"], lambda x: f"[{x['level']}] {x['title']} since {x['since']}"))
    if not blocks:
        return Message(title="Kowan digest: all clear", body="Nothing needs attention.", level="info")
    urgent = s.get("backups") or s.get("certificates") or any(x["critical"] for x in s.get("cves") or [])
    return Message(title="Kowan digest: " + ", ".join(summary), body="\n\n".join("\n".join(b) for b in blocks),
                   level="warning" if urgent else "info")


async def send(svc: Any, settings: Dict[str, Any], msg: Message) -> Dict[str, List[str]]:
    from kowan import capabilities as caps
    from kowan import connections
    from kowan.models.credential import CredentialKindChannel

    sent, failed = [], []
    for ch in svc.store.list_credentials(CredentialKindChannel):
        if not ch.enabled or (settings["channels"] and ch.id not in settings["channels"]):
            continue
        try:
            provider = await connections.resolve_service(svc.store, ch.id, caps.CAP_MESSAGE_SEND)
            await provider.send(msg)
            sent.append(ch.name)
        except Exception as err:  # noqa: BLE001
            log.error("digest: send via %s: %s", ch.name, err)
            failed.append(f"{ch.name}: {err}")
    return {"sent": sent, "failed": failed}


async def run(svc: Any, now: Optional[datetime] = None, *, force: bool = False) -> Dict[str, Any]:
    """Build and send the digest; records the result. ``force`` sends even
    when there is nothing to say (the "send now" button)."""
    now = now or datetime.now(timezone.utc)
    settings = get_settings(svc.store)
    d = await build(svc, settings, now)
    record: Dict[str, Any] = {"at": _ts(now), "sent": [], "failed": [], "skipped": False}
    if d["empty"] and not settings["sendEmpty"] and not force:
        record["skipped"] = True
    else:
        record.update(await send(svc, settings, render(d)))
    svc.store.set_setting(_LAST, json.dumps(record))
    return record


class Scheduler:
    def __init__(self, svc: Any) -> None:
        self._svc = svc
        self._task: Optional[asyncio.Task] = None

    async def tick(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        settings = get_settings(self._svc.store)
        last = last_sent(self._svc.store) or {}
        if not due(settings, last.get("at", ""), now):
            return False
        await run(self._svc, now)
        return True

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as err:  # noqa: BLE001
                log.error("digest: %s", err)
            await asyncio.sleep(_CHECK_EVERY)
