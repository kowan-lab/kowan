"""Cron scheduler for workflows.

Runs enabled workflows on their cron schedules. The allowed-deps list forbids
apscheduler, so this is a small hand-rolled asyncio cron with a standard 5-field
parser (minute hour day-of-month month day-of-week; no seconds field) plus the ``@hourly``/``@daily``/... macros.

The scheduler is reloaded whenever workflows change so the live schedule always
reflects the stored state. Every enabled workflow contributes each of its
enabled "cron" trigger's ``cron`` config expressions, each scheduling an
independent run (OR semantics). Invalid specs are logged and skipped — they
never crash the scheduler.

Learner notes: a single ``asyncio`` loop sleeps until the next whole minute, then
fires every entry whose spec matches that minute. This matches cron's
minute-resolution semantics without a per-entry timer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Set

from kowan import triggers
from kowan.models import WorkflowTriggerCron

log = logging.getLogger("kowan.workflow.scheduler")

_RUN_TIMEOUT = 60 * 60.0


# Field bounds for the standard 5-field cron format (min, max) inclusive.
_FIELD_BOUNDS = [
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),   # day of week (0 = Sunday)
]

# Named cron macros (@hourly, @daily, ...).
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronParseError(ValueError):
    """Raised when a cron spec cannot be parsed."""


@dataclass
class _Schedule:
    """A parsed cron spec: the allowed value set per field.
    ``dom_restricted``/``dow_restricted`` capture cron's special OR rule between
    day-of-month and day-of-week."""

    minutes: Optional[Set[int]] = None
    hours: Optional[Set[int]] = None
    doms: Optional[Set[int]] = None
    months: Optional[Set[int]] = None
    dows: Optional[Set[int]] = None
    dom_restricted: bool = False
    dow_restricted: bool = False
    # The last wall-clock minute this schedule fired, so a minute never
    # double-fires even if the loop ticks twice within it.
    last_fired_minute: Optional[datetime] = None

    def matches(self, t: datetime) -> bool:
        """Whether wall-clock minute ``t`` matches this schedule (minute
        resolution)."""
        if t.minute not in self.minutes:  # type: ignore[operator]
            return False
        if t.hour not in self.hours:  # type: ignore[operator]
            return False
        if t.month not in self.months:  # type: ignore[operator]
            return False
        # cron's day rule: if BOTH dom and dow are restricted, the entry fires
        # when EITHER matches; otherwise both must match (an unrestricted field
        # is always "*").
        # Python weekday(): Monday=0..Sunday=6; cron uses Sunday=0..Saturday=6.
        cron_dow = (t.weekday() + 1) % 7
        dom_ok = t.day in self.doms  # type: ignore[operator]
        dow_ok = cron_dow in self.dows  # type: ignore[operator]
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok


def parse_cron(spec: str) -> _Schedule:
    """Parse a cron spec into a :class:`_Schedule`.

    Accepts the standard 5-field format and the ``@hourly``/``@daily``/...
    macros. Field syntax supports ``*``, ``*/n``, ``a``, ``a-b``, ``a-b/n`` and
    comma lists thereof, and day-of-week/day-of-month name-independent numeric
    values. Raises :class:`CronParseError` on anything invalid.
    """
    spec = spec.strip()
    if spec == "":
        raise CronParseError("empty spec")

    if spec.startswith("@"):
        macro = _MACROS.get(spec)
        if macro is None:
            raise CronParseError(f"unknown macro {spec!r}")
        spec = macro

    fields = spec.split()
    if len(fields) != 5:
        raise CronParseError(
            f"expected 5 fields, got {len(fields)} in {spec!r}"
        )

    minute = _parse_field(fields[0], *_FIELD_BOUNDS[0])
    hour = _parse_field(fields[1], *_FIELD_BOUNDS[1])
    dom = _parse_field(fields[2], *_FIELD_BOUNDS[2])
    month = _parse_field(fields[3], *_FIELD_BOUNDS[3])
    dow = _parse_field(fields[4], *_FIELD_BOUNDS[4])
    # cron treats day-of-week 7 as Sunday too; normalise 7 -> 0.
    if 7 in dow:
        dow = set(dow)
        dow.discard(7)
        dow.add(0)

    return _Schedule(
        minutes=minute,
        hours=hour,
        doms=dom,
        months=month,
        dows=dow,
        dom_restricted=fields[2].strip() != "*",
        dow_restricted=fields[4].strip() != "*",
    )


def _parse_field(field: str, lo: int, hi: int) -> Set[int]:
    """Expand one cron field into the set of matching integers in [lo, hi]."""
    values: Set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if part == "":
            raise CronParseError(f"empty list element in {field!r}")
        step = 1
        if "/" in part:
            rng, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise CronParseError(f"invalid step in {part!r}")
            if step <= 0:
                raise CronParseError(f"non-positive step in {part!r}")
        else:
            rng = part

        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise CronParseError(f"invalid range {rng!r}")
        else:
            try:
                start = end = int(rng)
            except ValueError:
                raise CronParseError(f"invalid value {rng!r}")

        # Day-of-week allows 7 as an alias for Sunday; widen the upper bound.
        eff_hi = hi if hi != 6 else 7
        if start < lo or end > eff_hi or start > end:
            raise CronParseError(f"value out of range [{lo},{hi}]: {part!r}")
        for v in range(start, end + 1, step):
            values.add(v)
    return values


@dataclass
class _Entry:
    """One scheduled run: a parsed schedule bound to a workflow id/name."""

    schedule: _Schedule
    workflow_id: str
    workflow_name: str


class Scheduler:
    """Runs enabled workflows on their cron schedules. Reloaded whenever workflows change."""

    def __init__(self, engine, store) -> None:
        self._engine = engine
        self._store = store
        self._lock = asyncio.Lock()
        self._entries: List[_Entry] = []
        self._task: Optional[asyncio.Task] = None
        self._started = False
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Load the current schedule and begin firing. Safe to call once;
        subsequent schedule changes go through :meth:`reload`. Cancel the
        returned loop by cancelling the scheduler task (see :meth:`stop`)."""
        async with self._lock:
            if self._started:
                return
            self._started = True
        await self.reload()
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        """Cancel the scheduler loop."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def reload(self) -> None:
        """Rebuild the schedule from the store: each enabled "cron" trigger of an
        enabled workflow becomes an entry. Invalid specs are logged and skipped.
        Called on start and after any workflow create/update/delete."""
        try:
            workflows = self._store.list_workflows()
        except Exception as err:  # noqa: BLE001
            log.error("scheduler: list workflows err=%s", err)
            return

        entries: List[_Entry] = []
        loaded = 0
        for wf in workflows:
            if not wf.enabled:
                continue
            # Collect every cron expression for this workflow: each enabled
            # "cron" trigger's cron config. OR semantics.
            specs: List[str] = []
            for t in wf.triggers:
                if t.enabled and t.type == triggers.TypeCron:
                    spec = (t.config.get("cron") or "").strip()
                    if spec != "":
                        specs.append(spec)

            for spec in specs:
                try:
                    schedule = parse_cron(spec)
                except CronParseError as err:
                    log.warning(
                        "scheduler: invalid cron spec, skipping err=%s workflow=%s name=%s cron=%s",
                        err, wf.id, wf.name, spec,
                    )
                    continue
                entries.append(
                    _Entry(schedule=schedule, workflow_id=wf.id, workflow_name=wf.name)
                )
                loaded += 1

        async with self._lock:
            self._entries = entries
        log.info("scheduler reloaded scheduled=%d total=%d", loaded, len(workflows))

    async def _loop(self) -> None:
        """Tick once per minute (aligned to the whole minute) and fire every
        matching entry. Mirrors cron's minute-resolution scheduling."""
        try:
            while True:
                now = datetime.now()
                # Sleep until the next whole minute.
                secs_to_minute = 60 - now.second - now.microsecond / 1_000_000
                await asyncio.sleep(secs_to_minute if secs_to_minute > 0 else 0.5)
                tick = datetime.now()
                async with self._lock:
                    entries = list(self._entries)
                for entry in entries:
                    sched = entry.schedule
                    # Fire at most once per matching minute per entry.
                    minute = tick.replace(second=0, microsecond=0)
                    if sched.last_fired_minute == minute:
                        continue
                    if sched.matches(tick):
                        sched.last_fired_minute = minute
                        self._fire(entry)
        except asyncio.CancelledError:
            raise

    def _fire(self, entry: _Entry) -> None:
        """Kick off a detached cron run through the engine."""

        async def _run() -> None:
            try:
                await asyncio.wait_for(
                    self._engine.run(entry.workflow_id, WorkflowTriggerCron, None),
                    timeout=_RUN_TIMEOUT,
                )
            except Exception as err:  # noqa: BLE001
                log.error(
                    "scheduler: cron run failed err=%s workflow=%s name=%s",
                    err, entry.workflow_id, entry.workflow_name,
                )

        task = asyncio.ensure_future(_run())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
