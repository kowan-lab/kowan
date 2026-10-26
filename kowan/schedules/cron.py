"""Timezone-aware cron next-run computation, shared by schedules and volume backups.

The workflow scheduler (``kowan/workflow/scheduler.py``) only answers "does this
minute match?" in the server's local time. Scheduled start/stop and volume
backups also need to *preview* the next run and to honour a per-entry IANA
timezone, so this module adds :func:`next_run` on top of the same 5-field parser
(``parse_cron`` is reused, not duplicated).

DST semantics (documented in docs/schedules.md and pinned by tests):

* **Spring forward** (a wall-clock time that does not exist, e.g. 02:30 on the
  night clocks jump 02:00 -> 03:00): the run happens once, at the first valid
  instant after the gap (03:00), like Vixie cron.
* **Fall back** (a wall-clock time that happens twice): the run happens once, at
  the first occurrence.

Learner note: we never iterate minute by minute over a year. We walk days, and
for each matching day only the hour/minute values the spec allows, so even a
spec that never matches (``0 0 30 2 *``) costs at most a few thousand steps
before :func:`next_run` gives up and returns ``None``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kowan.workflow.scheduler import CronParseError, parse_cron

__all__ = [
    "CronParseError",
    "parse_cron",
    "validate_cron",
    "resolve_tz",
    "next_run",
    "next_runs",
    "MAX_SEARCH_DAYS",
]

# How far ahead next_run searches before declaring the spec unsatisfiable.
MAX_SEARCH_DAYS = 366 * 5

UTC = timezone.utc


def resolve_tz(name: str) -> tzinfo:
    """Map a timezone name to a tzinfo. ``""``/``"local"`` = the server's local
    zone. Raises ``ValueError`` for an unknown IANA name."""
    n = (name or "").strip()
    if n == "" or n.lower() == "local":
        return datetime.now().astimezone().tzinfo or UTC
    if n.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(n)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ValueError(f"unknown timezone {name!r}") from err


def validate_cron(spec: str) -> None:
    """Raise :class:`CronParseError` when ``spec`` is not a valid cron spec."""
    parse_cron(spec)


def _to_instant(naive: datetime, tz: tzinfo) -> datetime:
    """Turn a local wall-clock minute into an aware UTC instant.

    A time inside a DST gap is moved to the first valid minute after the gap; an
    ambiguous time resolves to its first occurrence (fold=0)."""
    probe = naive
    for _ in range(24 * 60):  # a gap is at most a few hours
        aware = probe.replace(tzinfo=tz, fold=0)
        utc = aware.astimezone(UTC)
        if utc.astimezone(tz).replace(tzinfo=None) == probe:
            return utc
        probe = probe + timedelta(minutes=1)
    return naive.replace(tzinfo=tz).astimezone(UTC)  # pragma: no cover


def _day_matches(sched, d: date) -> bool:
    if d.month not in sched.months:
        return False
    cron_dow = (d.weekday() + 1) % 7
    dom_ok = d.day in sched.doms
    dow_ok = cron_dow in sched.dows
    if sched.dom_restricted and sched.dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_run(spec: str, after: datetime, tz_name: str = "") -> Optional[datetime]:
    """The first instant strictly after ``after`` at which ``spec`` fires in the
    given timezone, as an aware UTC datetime (``None`` if it never fires within
    :data:`MAX_SEARCH_DAYS`). ``after`` may be naive (treated as UTC) or aware."""
    sched = parse_cron(spec)
    tz = resolve_tz(tz_name)
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    after = after.astimezone(UTC)
    start_local = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)
    hours = sorted(sched.hours)
    minutes = sorted(sched.minutes)
    day = start_local.date()
    for i in range(MAX_SEARCH_DAYS):
        d = day + timedelta(days=i)
        if not _day_matches(sched, d):
            continue
        for h in hours:
            if i == 0 and h < start_local.hour:
                continue
            for m in minutes:
                if i == 0 and h == start_local.hour and m < start_local.minute:
                    continue
                inst = _to_instant(datetime(d.year, d.month, d.day, h, m), tz)
                if inst > after:
                    return inst
    return None


def next_runs(spec: str, after: datetime, tz_name: str = "", count: int = 5) -> list[datetime]:
    """The next ``count`` fire instants after ``after`` (UTC, aware)."""
    out: list[datetime] = []
    cur = after
    for _ in range(max(0, count)):
        n = next_run(spec, cur, tz_name)
        if n is None:
            break
        out.append(n)
        cur = n
    return out


def iso(t: Optional[datetime]) -> str:
    """RFC3339 UTC (``...Z``) or ``""``."""
    if t is None:
        return ""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> Optional[datetime]:
    """Parse an RFC3339 timestamp written by :func:`iso` (or any ISO form)."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC)
