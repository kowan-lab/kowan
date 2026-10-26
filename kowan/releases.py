"""Is a newer Kowan release out?

Released versions are the ``X.Y.Z`` tags of the server image
(``ghcr.io/kowan-lab/kowan`` by default, ``KOWAN_RELEASE_REPOSITORY``; empty
turns the check off), read from the registry anonymously or with a stored
registry credential for that host. It answers for every install, a container
or a checkout run from source, by comparing with ``kowan.__version__``.

Every 6 hours the server checks; the first time it sees a version newer than
its own it posts one notice to the bell, and records the version so the same
release is never announced twice.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from kowan import __version__
from kowan.events import Event

log = logging.getLogger("kowan.releases")

DEFAULT_REPOSITORY = "ghcr.io/kowan-lab/kowan"
CHECK_EVERY = 6 * 3600.0
FIRST_CHECK_AFTER = 120.0
CACHE_TTL = 3600.0
_NOTIFIED = "releases.notified"
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

Fetch = Callable[[str], Awaitable[List[str]]]


def parse(version: str) -> Optional[Tuple[int, int, int]]:
    """``(major, minor, patch)`` of a release version; None for anything else
    (``latest``, ``0.1``, a pre-release, ``dev``)."""
    m = _SEMVER.match((version or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def newest(tags: List[str]) -> str:
    """The highest release version among ``tags`` ("" when there is none)."""
    versions = [(parse(t), t.lstrip("v")) for t in tags]
    return max((v for v in versions if v[0] is not None), default=(None, ""))[1]


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse(candidate), parse(current)
    return a is not None and (b is None or a > b)


class Releases:
    def __init__(self, svc: Any, repository: str = DEFAULT_REPOSITORY, fetch: Optional[Fetch] = None) -> None:
        self._svc = svc
        self.repository = (repository or "").strip()
        self._fetch = fetch or self._registry_tags
        self._cache: Optional[Tuple[float, str, str]] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self.repository not in ("", "off")

    @property
    def current(self) -> str:
        return __version__

    async def latest(self, refresh: bool = False) -> Tuple[str, str]:
        """``(latest version, error)``; cached for an hour unless ``refresh``."""
        if not self.enabled:
            return "", ""
        now = time.monotonic()
        if not refresh and self._cache and self._cache[0] > now:
            return self._cache[1], self._cache[2]
        try:
            version, err = newest(await asyncio.wait_for(self._fetch(self.repository), 20)), ""
            if not version:
                err = f"no release versions found in {self.repository}"
        except Exception as e:  # noqa: BLE001 - offline, registry down, rate limited
            version, err = "", f"check {self.repository}: {e or type(e).__name__}"
        # A failure is retried sooner than a success.
        self._cache = (now + (CACHE_TTL if not err else 60.0), version, err)
        return version, err

    async def status(self, refresh: bool = False) -> dict:
        latest, err = await self.latest(refresh)
        return {"currentVersion": self.current, "latestVersion": latest,
                "newVersion": is_newer(latest, self.current), "releaseError": err}

    async def check(self) -> str:
        """Refresh, and announce a newer release once; returns it ("" if none)."""
        latest, _ = await self.latest(refresh=True)
        if not is_newer(latest, self.current):
            return ""
        store = self._svc.store
        if store.get_setting(_NOTIFIED) == latest:
            return latest
        bus = getattr(self._svc, "bus", None)
        if bus is not None:
            bus.publish(Event(type="notice", source="update", payload={
                "level": "info", "title": f"Kowan {latest} is available",
                "body": f"This server runs {self.current}. Update it from Settings → Updates.",
                "source": "update", "refId": latest}))
        store.set_setting(_NOTIFIED, latest)
        return latest

    def start(self) -> None:
        if self.enabled and self._task is None:
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
        await asyncio.sleep(FIRST_CHECK_AFTER)
        while True:
            try:
                await self.check()
            except Exception as err:  # noqa: BLE001 - keep checking
                log.warning("releases: %s", err)
            await asyncio.sleep(CHECK_EVERY)

    async def _registry_tags(self, repository: str) -> List[str]:
        from kowan.agentupdate import _registry_creds, _split_ref
        from kowan.registry.browse import v2_tags

        host, repo, _ = _split_ref(repository)
        if host in ("", "docker.io", "index.docker.io"):
            host, repo = "registry-1.docker.io", repo if "/" in repo else f"library/{repo}"
        username, password = await _registry_creds(self._svc.store, host)
        return await v2_tags(username, password, host, repo, False)
