"""A small timezone-aware cron runner with a missed-run policy.

Shared by scheduled start/stop (:mod:`kowan.schedules.service`) and volume
backups (:mod:`kowan.volbackup.service`). The workflow scheduler stays as it is
(minute-match in server-local time); this runner adds what those two features
need on top of the same cron parser:

* a per-entry IANA timezone and a computed ``next_due`` instant (also used for
  the "next run" previews in the UI);
* a **missed-run policy**: a run that is more than ``grace`` late (default 10
  minutes; e.g. the server was down or the event loop stalled) is not executed
  late, it is reported through ``on_skip`` and the entry moves on to its next
  future slot. Only the oldest missed slot is reported, never a burst;
* an **overlap guard**: if the previous run of the same entry is still going,
  the new slot is skipped (reported via ``on_skip``) instead of stacking runs.

Entries are plain :class:`CronEntry` values produced by a loader callable, so
the runner knows nothing about stores: :meth:`reload` re-reads them whenever
the owner's data changes. ``last_run`` lets a restart pick up where it left
off: the next due slot is computed from the last recorded run, so a slot that
passed while the server was down is detected (and skipped or run, per the
grace window).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .cron import CronParseError, next_run

log = logging.getLogger("kowan.schedules.runner")

UTC = timezone.utc
DEFAULT_GRACE = timedelta(minutes=10)
# Upper bound on one sleep, so a clock jump or a reload never leaves the loop
# asleep for hours.
_MAX_SLEEP = 30.0


@dataclass
class CronEntry:
    key: str
    spec: str
    tz: str = ""
    # Last recorded run (fired or skipped); None = never ran.
    last_run: Optional[datetime] = None
    # Reference point for an entry that never ran (usually its creation time).
    anchor: Optional[datetime] = None
    payload: Any = None


@dataclass
class _State:
    entry: CronEntry
    next_due: Optional[datetime]
    sig: tuple = field(default_factory=tuple)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CronRunner:
    def __init__(
        self,
        name: str,
        load: Callable[[], List[CronEntry]],
        fire: Callable[[CronEntry, datetime], Awaitable[None]],
        on_skip: Optional[Callable[[CronEntry, datetime, str], None]] = None,
        *,
        grace: timedelta = DEFAULT_GRACE,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.name = name
        self._load = load
        self._fire = fire
        self._on_skip = on_skip
        self.grace = grace
        self._clock = clock
        self._states: Dict[str, _State] = {}
        self._running: Dict[str, asyncio.Task] = {}
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self.reload()
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        t, self._task = self._task, None
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for task in list(self._running.values()):
            task.cancel()

    async def drain(self) -> None:
        """Wait for every in-flight run (tests)."""
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)

    # -- schedule -----------------------------------------------------------
    def reload(self) -> None:
        """Re-read entries. An unchanged entry (same spec/tz/last_run) keeps its
        computed slot; new or edited ones get a fresh ``next_due``."""
        try:
            entries = self._load()
        except Exception as err:  # noqa: BLE001
            log.error("%s: load entries: %s", self.name, err)
            return
        now = self._clock()
        states: Dict[str, _State] = {}
        for e in entries:
            sig = (e.spec, e.tz)
            old = self._states.get(e.key)
            if old is not None and old.sig == sig:
                old.entry = e
                states[e.key] = old
                continue
            ref = e.last_run or e.anchor or now
            try:
                due = next_run(e.spec, ref, e.tz)
            except (CronParseError, ValueError) as err:
                log.warning("%s: entry %s invalid (%s), skipped", self.name, e.key, err)
                continue
            states[e.key] = _State(entry=e, next_due=due, sig=sig)
        self._states = states
        self._wake.set()

    def next_due(self, key: str) -> Optional[datetime]:
        st = self._states.get(key)
        return st.next_due if st else None

    def entries(self) -> List[CronEntry]:
        return [s.entry for s in self._states.values()]

    # -- tick ---------------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> List[str]:
        """Process every due entry at ``now``; return the keys that fired.
        Late-by-more-than-grace slots are skipped (``on_skip``)."""
        now = now or self._clock()
        fired: List[str] = []
        for key, st in list(self._states.items()):
            due = st.next_due
            if due is None or due > now:
                continue
            late = now - due
            if late > self.grace:
                mins = int(late.total_seconds() // 60)
                self._skip(st.entry, due, f"missed: {mins} min late (more than {int(self.grace.total_seconds() // 60)} min)")
            elif key in self._running and not self._running[key].done():
                self._skip(st.entry, due, "skipped: previous run still in progress")
            else:
                fired.append(key)
                self._spawn(st.entry, due)
            try:
                st.next_due = next_run(st.entry.spec, max(now, due), st.entry.tz)
            except (CronParseError, ValueError):
                st.next_due = None
        return fired

    def _skip(self, entry: CronEntry, due: datetime, reason: str) -> None:
        log.info("%s: %s %s at %s", self.name, entry.key, reason, due.isoformat())
        entry.last_run = due
        if self._on_skip is not None:
            try:
                self._on_skip(entry, due, reason)
            except Exception as err:  # noqa: BLE001
                log.error("%s: on_skip %s: %s", self.name, entry.key, err)

    def _spawn(self, entry: CronEntry, due: datetime) -> None:
        entry.last_run = due

        async def _run() -> None:
            try:
                await self._fire(entry, due)
            except Exception as err:  # noqa: BLE001
                log.error("%s: run %s failed: %s", self.name, entry.key, err)

        task = asyncio.ensure_future(_run())
        self._running[entry.key] = task

        def _done(t: asyncio.Task, key: str = entry.key) -> None:
            if self._running.get(key) is t:
                self._running.pop(key, None)

        task.add_done_callback(_done)

    async def _loop(self) -> None:
        while True:
            now = self._clock()
            dues = [s.next_due for s in self._states.values() if s.next_due is not None]
            delay = _MAX_SLEEP
            if dues:
                delay = min(delay, max(0.2, (min(dues) - now).total_seconds()))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            try:
                self.tick()
            except Exception as err:  # noqa: BLE001
                log.error("%s: tick: %s", self.name, err)
