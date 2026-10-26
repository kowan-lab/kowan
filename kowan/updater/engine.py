"""The Docker Engine surface the helper drives, and its real implementation.

The helper only needs a dozen raw calls. Expressing them as a tiny interface
keeps :mod:`kowan.updater.helper` independent of aiohttp/the socket, so the swap
and every rollback branch are unit-tested against an in-memory fake
(``tests/test_updates_helper.py``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol

from ..errors import NotFound


class Engine(Protocol):
    async def inspect_container(self, ref: str) -> Dict[str, Any]: ...
    async def inspect_image(self, ref: str) -> Dict[str, Any]: ...
    async def stop(self, ref: str, timeout: int) -> None: ...
    async def start(self, ref: str) -> None: ...
    async def rename(self, ref: str, new_name: str) -> None: ...
    async def set_restart_policy(self, ref: str, policy: Dict[str, Any]) -> None: ...
    async def create(self, name: str, body: Dict[str, Any]) -> str: ...
    async def connect(self, network: str, cid: str, endpoint: Dict[str, Any]) -> None: ...
    async def remove(self, ref: str, force: bool) -> None: ...
    async def logs_tail(self, ref: str, tail: int) -> List[str]: ...
    async def close(self) -> None: ...


def is_not_found(err: BaseException) -> bool:
    return isinstance(err, NotFound) or "no such" in str(err).lower()


class LocalEngine:
    """:class:`Engine` over :class:`kowan.docker.local.LocalClient` (unix socket)."""

    def __init__(self, socket_path: str = "/var/run/docker.sock") -> None:
        from ..docker.local import LocalClient

        self._c = LocalClient(socket_path=socket_path)

    async def inspect_container(self, ref: str) -> Dict[str, Any]:
        return await self._c.inspect_container(ref)

    async def inspect_image(self, ref: str) -> Dict[str, Any]:
        return await self._c.inspect_image(ref)

    async def stop(self, ref: str, timeout: int) -> None:
        await self._c.stop_container_timeout(ref, timeout)

    async def start(self, ref: str) -> None:
        await self._c.start_container(ref)

    async def rename(self, ref: str, new_name: str) -> None:
        await self._c.rename_container(ref, new_name)

    async def set_restart_policy(self, ref: str, policy: Dict[str, Any]) -> None:
        await self._c.update_container_config(ref, {"RestartPolicy": policy})

    async def create(self, name: str, body: Dict[str, Any]) -> str:
        return await self._c.create_container_raw(name, body)

    async def connect(self, network: str, cid: str, endpoint: Dict[str, Any]) -> None:
        await self._c.connect_container_endpoint(network, cid, endpoint)

    async def remove(self, ref: str, force: bool) -> None:
        await self._c.remove_container(ref, force)

    async def logs_tail(self, ref: str, tail: int) -> List[str]:
        return await self._c.container_logs_tail(ref, tail)

    async def close(self) -> None:
        await self._c.close()
