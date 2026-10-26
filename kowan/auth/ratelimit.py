"""Brute-force protection for password sign-in.

Failed attempts are counted in a sliding window three ways:

* per address and username: one machine guessing one account;
* per address: one machine guessing many accounts (IPv6 grouped by /64, since a
  single client usually owns a whole /64);
* per username, across addresses: a distributed guess at one account. This
  limit is high, and an address the user has already signed in from is exempt,
  so an attacker cannot lock the real admin out by failing on purpose.

On top of that, password hashing runs a few at a time: scrypt costs ~50 ms of
CPU and 32 MiB, so an unbounded flood would starve every other request.

State is in memory: the server is a single process, and a restart only gives an
attacker a fresh window.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import deque
from typing import Deque, Dict, Optional, Set

WINDOW_SECONDS = 10 * 60
MAX_FAILURES_PER_PAIR = 10
MAX_FAILURES_PER_ADDRESS = 20
MAX_FAILURES_PER_USERNAME = 100
# Concurrent password hashes, and how many may queue behind them before new
# attempts are turned away.
HASH_CONCURRENCY = 4
HASH_QUEUE_LIMIT = 32
# Distinct keys remembered per table, and good addresses remembered per user.
MAX_TRACKED_KEYS = 10_000
MAX_KNOWN_ADDRESSES = 20


def address_key(address: str) -> str:
    """The unit an address is counted in: itself for IPv4, its /64 for IPv6."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class LoginLimiter:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._by_pair: Dict[str, Deque[float]] = {}
        self._by_address: Dict[str, Deque[float]] = {}
        self._by_username: Dict[str, Deque[float]] = {}
        self._known: Dict[str, Set[str]] = {}
        self._hashing = asyncio.Semaphore(HASH_CONCURRENCY)
        self._waiting = 0

    def _recent(self, table: Dict[str, Deque[float]], key: str) -> Deque[float]:
        """Failures of ``key`` still inside the window (a read: keys with none
        are dropped, so guessing random names cannot grow the table)."""
        q = table.get(key)
        if q is None:
            return deque()
        now = self._clock()
        while q and now - q[0] > WINDOW_SECONDS:
            q.popleft()
        if not q:
            del table[key]
        return q

    def _record(self, table: Dict[str, Deque[float]], key: str, now: float) -> None:
        self._recent(table, key)
        table.setdefault(key, deque()).append(now)
        # Bound memory under a flood of distinct keys: forget the stalest.
        if len(table) > MAX_TRACKED_KEYS:
            oldest = min(table, key=lambda k: table[k][-1])
            del table[oldest]

    def _wait(self, q: Deque[float], limit: int) -> Optional[int]:
        if len(q) < limit:
            return None
        return max(1, int(WINDOW_SECONDS - (self._clock() - q[0])) + 1)

    def retry_after(self, address: str, username: Optional[str]) -> Optional[int]:
        """Seconds to wait when this attempt must be refused, else None.
        ``username=None`` counts the address alone (setup token guesses)."""
        addr = address_key(address)
        wait = self._wait(self._recent(self._by_address, addr), MAX_FAILURES_PER_ADDRESS)
        if wait is not None or username is None:
            return wait
        user = username.lower()
        wait = self._wait(self._recent(self._by_pair, f"{addr}|{user}"), MAX_FAILURES_PER_PAIR)
        if wait is not None:
            return wait
        if addr in self._known.get(user, ()):
            return None
        return self._wait(self._recent(self._by_username, user), MAX_FAILURES_PER_USERNAME)

    def failed(self, address: str, username: Optional[str]) -> None:
        now = self._clock()
        addr = address_key(address)
        self._record(self._by_address, addr, now)
        if username is not None:
            user = username.lower()
            self._record(self._by_pair, f"{addr}|{user}", now)
            self._record(self._by_username, user, now)

    def succeeded(self, address: str, username: str) -> None:
        addr, user = address_key(address), username.lower()
        self._by_pair.pop(f"{addr}|{user}", None)
        known = self._known.setdefault(user, set())
        if len(known) >= MAX_KNOWN_ADDRESSES:
            known.pop()
        known.add(addr)
        if len(self._known) > MAX_TRACKED_KEYS:
            self._known.pop(next(iter(self._known)))

    def busy(self) -> bool:
        return self._waiting >= HASH_QUEUE_LIMIT

    async def hashing(self, fn, *args):
        """Run a password hash/verify in a worker thread, a few at a time."""
        self._waiting += 1
        try:
            async with self._hashing:
                return await asyncio.to_thread(fn, *args)
        finally:
            self._waiting -= 1
