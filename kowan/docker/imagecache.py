"""Short-TTL, per-host cache of the image list, with write-through invalidation.

``GET /images/json`` is one of the slowest Engine calls (~0.6 s with a few
hundred images: the daemon walks every image's layers to size them), and the UI
asks for it on every visit to the images page / dashboard. The list changes
rarely, and almost always because of something *Kowan* did (pull, build, remove,
prune, a compose/stack deploy), so the control plane caches it per host and
drops the entry whenever one of those operations goes through.

Two pieces:

* :class:`ImageListCache` — host id -> (fetched_at, generation, images). A
  per-host *generation* counter is bumped on every invalidation; a fetch that
  started before an invalidation never stores its (possibly stale) result, so a
  read racing a ``docker rmi`` can't re-poison the cache. Concurrent misses for
  the same host share one Engine call (single-flight).
* :class:`CachingOps` — a transparent proxy over an :class:`~kowan.docker.base.Ops`
  that serves ``list_images`` from the cache and invalidates it around every
  image-mutating op. :class:`~kowan.docker.provider.Provider` hands these out, so
  every caller (REST handlers, deployments, workflows, updates) is covered
  without each one having to remember to invalidate.

Changes made outside Kowan (``docker pull`` on the host) show up after at most
``ttl`` seconds, or immediately when a client is following the host's Docker
events (``kowan/api/events.py`` invalidates on every ``image`` event).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import pulls

DEFAULT_TTL = 30.0

# Ops methods after which the host's image list may have changed. compose/stack
# deploys can pull or build; removals can untag. create_container never pulls by
# itself (the API layer pulls explicitly via pull_image), so it's not listed.
IMAGE_MUTATING_OPS = frozenset(
    {
        "pull_image",
        "build_image",
        "remove_image",
        "prune_images",
        "tag_image",
        "compose_up",
        "compose_down",
        "swarm_deploy",
        "swarm_remove",
    }
)


class ImageListCache:
    """Per-host image list cache (see module docstring)."""

    def __init__(self, ttl: float = DEFAULT_TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = float(ttl)
        self._clock = clock
        self._entries: Dict[str, Tuple[float, int, List[Any]]] = {}
        self._gen: Dict[str, int] = {}
        self._inflight: Dict[str, Tuple[int, "asyncio.Future[List[Any]]"]] = {}

    def generation(self, host_id: str) -> int:
        return self._gen.get(host_id, 0)

    def invalidate(self, host_id: Optional[str] = None) -> None:
        """Drop the cached list for ``host_id`` (or every host when ``None``)."""
        hosts = list(set(self._entries) | set(self._gen)) if host_id is None else [host_id]
        for h in hosts:
            self._gen[h] = self._gen.get(h, 0) + 1
            self._entries.pop(h, None)
            # An in-flight fetch keeps running for its current waiters, but it was
            # started under the old generation so its result won't be stored, and
            # new callers won't join it.
            self._inflight.pop(h, None)

    def forget(self, host_id: str) -> None:
        """Drop everything kept for a deleted host."""
        for state in (self._entries, self._gen, self._inflight):
            state.pop(host_id, None)

    def peek(self, host_id: str) -> Optional[List[Any]]:
        """Return the fresh cached list for ``host_id`` without fetching, or None."""
        e = self._entries.get(host_id)
        if e is None:
            return None
        fetched_at, gen, images = e
        if gen != self.generation(host_id) or self._clock() - fetched_at >= self.ttl:
            # Stale: don't keep a whole image list around for an idle host.
            self._entries.pop(host_id, None)
            return None
        return list(images)

    async def get(self, host_id: str, fetch: Callable[[], Awaitable[List[Any]]]) -> List[Any]:
        """Return the cached list, calling ``fetch()`` on a miss."""
        hit = self.peek(host_id)
        if hit is not None:
            return hit
        gen = self.generation(host_id)
        inflight = self._inflight.get(host_id)
        if inflight is not None and inflight[0] == gen:
            try:
                return list(await asyncio.shield(inflight[1]))
            except asyncio.CancelledError:
                # The leader's request was cancelled (client went away), not
                # us: fetch on our own. If WE were cancelled, propagate.
                if not inflight[1].cancelled():
                    raise

        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[List[Any]]" = loop.create_future()
        self._inflight[host_id] = (gen, fut)
        try:
            images = list(await fetch() or [])
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
            raise
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
                # Mark retrieved so a fetch nobody else awaited doesn't log
                # "exception was never retrieved".
                fut.exception()
            raise
        finally:
            cur = self._inflight.get(host_id)
            if cur is not None and cur[1] is fut:
                del self._inflight[host_id]
        if not fut.done():
            fut.set_result(images)
        if self.generation(host_id) == gen:
            self._entries[host_id] = (self._clock(), gen, images)
        return list(images)


class _InvalidatingStream:
    """Wraps a pull progress stream; invalidates the cache when it ends/closes.

    The image only exists once the pull finishes, so invalidating at call time
    alone could let a list taken mid-pull be cached as "fresh".
    """

    def __init__(self, inner: Any, invalidate: Callable[[], None]) -> None:
        self._inner = inner
        self._invalidate = invalidate

    async def read(self, n: int = -1) -> bytes:
        data = await self._inner.read(n)
        if not data:
            self._invalidate()
        return data

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        try:
            async for chunk in self._inner:
                yield chunk
        finally:
            self._invalidate()

    async def close(self) -> None:
        try:
            close = getattr(self._inner, "close", None)
            if close is not None:
                res = close()
                if hasattr(res, "__await__"):
                    await res
        finally:
            self._invalidate()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class CachingOps:
    """Transparent :class:`Ops` proxy adding the image-list cache for one host.

    Every attribute not overridden here is delegated to the wrapped client, so
    callers can't tell the difference except that ``list_images`` is fast.
    """

    def __init__(self, inner: Any, host_id: str, cache: ImageListCache) -> None:
        self._inner = inner
        self._host_id = host_id
        self._cache = cache

    @property
    def inner(self) -> Any:
        return self._inner

    def invalidate_image_cache(self) -> None:
        self._cache.invalidate(self._host_id)

    async def list_images(self) -> List[Any]:
        return await self._cache.get(self._host_id, self._inner.list_images)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name not in IMAGE_MUTATING_OPS or not callable(attr):
            return attr
        invalidate = self.invalidate_image_cache

        async def _mutating(*args: Any, **kwargs: Any) -> Any:
            invalidate()
            try:
                result = await attr(*args, **kwargs)
            finally:
                invalidate()
            if name == "pull_image" and result is not None:
                ref = pulls.pull_ref(args, kwargs) or ""
                tracked = pulls.TRACKER.track(self._host_id, ref, result, pulls.current_user_id())
                return _InvalidatingStream(tracked, invalidate)
            return result

        _mutating.__name__ = name
        return _mutating

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CachingOps({self._inner!r}, host={self._host_id!r})"
