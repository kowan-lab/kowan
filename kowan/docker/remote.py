"""``RemoteClient`` — an :class:`Ops` that drives a remote/edge host over a mux
stream using the agent protocol.

The transport is the in-house stream mux: every call opens ONE fresh :class:`kowan.mux.Stream` via the injected
``dial`` callable and runs :func:`kowan.agentproto.call` over it.

Because ``dial`` re-fetches a live mux stream each time (wired to
``hub.dial(host_id)`` by :class:`kowan.docker.provider.Provider`), the same
``RemoteClient`` keeps working across tunnel reconnects.

The result JSON returned by the agent is validated back into the shared
``kowan.models`` types (Container/Image/Volume/Network/...) so callers see the
same objects a ``LocalClient`` would return. Streaming ops
(logs/stats/pull/download/exec) return the raw mux-backed byte stream from
:func:`agentproto.call` untouched.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List

from .. import agentproto
from ..models import (
    BuildOptions,
    BuildResult,
    ContainerStats,
    DiskUsage,
    ImageHistoryLayer,
    NetworkCreateOptions,
    PluginInfo,
    PluginInstallOptions,
    SystemInfo,
    VolumeCreateOptions,
    VolumeFile,
)
from .base import AsyncByteStream, ExecStream, Ops

# Dial callback: an async callable returning a fresh mux Stream to the agent.
Dial = Callable[[], Awaitable[Any]]


def _dump(model: Any) -> Any:
    """Serialise a pydantic model / options object to a camelCase JSON dict."""
    md = getattr(model, "model_dump", None)
    if callable(md):
        return md(by_alias=True)
    return model


class RemoteClient(Ops):
    """Drives a single remote host by opening a mux stream per operation.

    ``dial`` is an async callable that returns a fresh :class:`kowan.mux.Stream`
    connected to the target agent (control-plane -> agent direction). The
    provider wires it to ``hub.dial(host_id)``; tests can inject a fake.
    """

    def __init__(self, dial: Dial) -> None:
        self._dial = dial

    # -- helpers -------------------------------------------------------------

    async def _call(self, op: str, args: Dict[str, Any] | None = None) -> Any:
        """Open a stream, run a unary op, return the decoded meta, close the stream."""
        stream = await self._dial()
        try:
            meta, body = await agentproto.call(stream, op, args or {})
            # Unary ops never carry a body; if the agent unexpectedly sent one,
            # close it so the stream isn't left half-open.
            if body is not None:
                await body.close()
            return meta
        finally:
            await _reset(stream)

    async def _stream(self, op: str, args: Dict[str, Any] | None = None) -> AsyncByteStream:
        """Open a stream, run a streaming op, hand back the response body.

        The returned body owns the stream: closing it resets the stream. We do
        NOT reset here, otherwise we'd tear down the very body we return.
        """
        stream = await self._dial()
        try:
            _meta, body = await agentproto.call(stream, op, args or {})
        except BaseException:
            # The agent refused the op (or the call was cancelled) before a body
            # took ownership of the stream: reset it, or the mux stream (and its
            # buffers) would stay open on the long-lived tunnel session.
            await _reset(stream)
            raise
        if body is None:
            # Op wasn't actually streaming; nothing to read.
            await _reset(stream)

            class _Empty:
                async def read(self, n: int = -1) -> bytes:
                    return b""

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise StopAsyncIteration

                async def close(self) -> None:
                    return None

            return _Empty()
        return body

    # -- reachability --------------------------------------------------------

    async def ping(self) -> None:
        await self._call("ping")

    # -- containers ----------------------------------------------------------

    async def list_containers(self, all: bool) -> List[Any]:
        from ..models import Container

        meta = await self._call("list_containers", {"all": all})
        return [Container.model_validate(m) for m in (meta or [])]

    async def inspect_container(self, id: str) -> Dict[str, Any]:
        return await self._call("inspect_container", {"id": id}) or {}

    async def container_spec(self, id: str) -> Any:
        from ..models import ContainerSpec

        meta = await self._call("container_spec", {"id": id})
        return ContainerSpec.model_validate(meta or {})

    async def start_container(self, id: str) -> None:
        await self._call("start_container", {"id": id})

    async def stop_container(self, id: str) -> None:
        await self._call("stop_container", {"id": id})

    async def restart_container(self, id: str) -> None:
        await self._call("restart_container", {"id": id})

    async def remove_container(self, id: str, force: bool) -> None:
        await self._call("remove_container", {"id": id, "force": force})

    async def container_logs(
        self, id: str, follow: bool, tail: int
    ) -> AsyncByteStream:
        return await self._stream(
            "container_logs", {"id": id, "follow": follow, "tail": tail}
        )

    async def container_stats(self, id: str) -> ContainerStats:
        meta = await self._call("container_stats", {"id": id})
        return ContainerStats.model_validate(meta or {})

    async def container_stats_stream(self, id: str) -> AsyncByteStream:
        return await self._stream("container_stats_stream", {"id": id})

    async def events(
        self,
        since: Any = None,
        until: Any = None,
        filters: Dict[str, List[str]] | None = None,
    ) -> AsyncByteStream:
        return await self._stream(
            "events",
            {"since": since or 0, "until": until or 0, "filters": filters or {}},
        )

    async def create_container(self, name: str, spec: Any, start: bool) -> str:
        meta = await self._call(
            "create_container",
            {"name": name, "spec": _dump(spec), "start": start},
        )
        # Agent returns the created id as a bare string (LocalClient.create returns str).
        if isinstance(meta, dict):
            return meta.get("id", "")
        return meta or ""

    async def pause_container(self, id: str) -> None:
        await self._call("pause_container", {"id": id})

    async def unpause_container(self, id: str) -> None:
        await self._call("unpause_container", {"id": id})

    async def kill_container(self, id: str, signal: str = "SIGKILL") -> None:
        await self._call("kill_container", {"id": id, "signal": signal})

    # -- exec ----------------------------------------------------------------

    async def exec(self, id: str, cmd: List[str]) -> str:
        meta = await self._call("exec", {"id": id, "cmd": cmd})
        return meta or ""

    async def exec_attach(self, id: str, cmd: List[str], tty: bool) -> ExecStream:
        """Open a bidirectional exec stream over a dedicated mux stream.

        The returned object's ``write`` sends stdin frames and ``read`` yields
        stdout/stderr bytes; ``close`` terminates the request-body framing and
        resets the stream. It owns the mux stream for the session's lifetime.
        """
        stream = await self._dial()
        try:
            _meta, body = await agentproto.call(
                stream, "exec_attach", {"id": id, "cmd": cmd, "tty": tty}
            )
        except BaseException:
            await _reset(stream)
            raise
        return _ExecAttachStream(stream, body)

    # -- images --------------------------------------------------------------

    async def list_images(self) -> List[Any]:
        from ..models import Image

        meta = await self._call("list_images")
        return [Image.model_validate(m) for m in (meta or [])]

    async def build_image(self, opts: BuildOptions) -> BuildResult:
        meta = await self._call("build_image", {"opts": _dump(opts)})
        return BuildResult.model_validate(meta or {})

    async def pull_image(self, ref: str, registry_auth: str) -> AsyncByteStream:
        return await self._stream("pull_image", {"ref": ref, "auth": registry_auth})

    async def remove_image(self, id: str, force: bool) -> None:
        await self._call("remove_image", {"id": id, "force": force})

    async def inspect_image(self, id: str) -> Dict[str, Any]:
        return await self._call("inspect_image", {"id": id}) or {}

    async def image_history(self, id: str) -> List[ImageHistoryLayer]:
        meta = await self._call("image_history", {"id": id})
        return [ImageHistoryLayer.model_validate(m) for m in (meta or [])]

    # -- volumes -------------------------------------------------------------

    async def list_volumes(self) -> List[Any]:
        from ..models import Volume

        meta = await self._call("list_volumes")
        return [Volume.model_validate(m) for m in (meta or [])]

    async def create_volume(self, opts: VolumeCreateOptions) -> Any:
        from ..models import Volume

        meta = await self._call("create_volume", {"opts": _dump(opts)})
        return Volume.model_validate(meta or {})

    async def remove_volume(self, name: str, force: bool) -> None:
        await self._call("remove_volume", {"name": name, "force": force})

    async def inspect_volume(self, name: str) -> Dict[str, Any]:
        return await self._call("inspect_volume", {"name": name}) or {}

    async def browse_volume(self, name: str, sub_path: str) -> List[VolumeFile]:
        meta = await self._call(
            "browse_volume", {"name": name, "subPath": sub_path}
        )
        return [VolumeFile.model_validate(m) for m in (meta or [])]

    async def read_volume_file(self, name: str, sub_path: str) -> bytes:
        body = await self._stream(
            "read_volume_file", {"name": name, "subPath": sub_path}
        )
        try:
            return await body.read(-1)
        finally:
            await _close_body(body)

    async def download_volume_file(
        self, name: str, sub_path: str
    ) -> AsyncByteStream:
        return await self._stream(
            "download_volume_file", {"name": name, "subPath": sub_path}
        )

    async def download_volume_archive(
        self, name: str, sub_path: str
    ) -> AsyncByteStream:
        return await self._stream(
            "download_volume_archive", {"name": name, "subPath": sub_path}
        )

    async def upload_volume_file(
        self,
        name: str,
        dest_dir: str,
        filename: str,
        size: int,
        content: AsyncByteStream,
    ) -> None:
        stream = await self._dial()
        try:
            _meta, body = await agentproto.call(
                stream,
                "upload_volume_file",
                {
                    "name": name,
                    "destDir": dest_dir,
                    "filename": filename,
                    "size": size,
                },
                body=content,
            )
            if body is not None:
                await body.close()
        finally:
            await _reset(stream)

    # -- plugins -------------------------------------------------------------

    async def list_plugins(self) -> List[PluginInfo]:
        meta = await self._call("list_plugins")
        return [PluginInfo.model_validate(m) for m in (meta or [])]

    async def install_plugin(self, opts: PluginInstallOptions) -> None:
        await self._call("install_plugin", {"opts": _dump(opts)})

    async def remove_plugin(self, name: str, force: bool) -> None:
        await self._call("remove_plugin", {"name": name, "force": force})

    # -- networks ------------------------------------------------------------

    async def list_networks(self) -> List[Any]:
        from ..models import Network

        meta = await self._call("list_networks")
        return [Network.model_validate(m) for m in (meta or [])]

    async def create_network(self, opts: NetworkCreateOptions) -> Any:
        from ..models import Network

        meta = await self._call("create_network", {"opts": _dump(opts)})
        return Network.model_validate(meta or {})

    async def inspect_network(self, id: str) -> Any:
        from ..models import Network

        meta = await self._call("inspect_network", {"id": id})
        return Network.model_validate(meta or {})

    async def connect_container(self, network_id: str, container_id: str) -> None:
        await self._call(
            "connect_container",
            {"networkId": network_id, "containerId": container_id},
        )

    async def disconnect_container(
        self, network_id: str, container_id: str, force: bool
    ) -> None:
        await self._call(
            "disconnect_container",
            {"networkId": network_id, "containerId": container_id, "force": force},
        )

    async def remove_network(self, id: str) -> None:
        await self._call("remove_network", {"id": id})

    # -- system / prune ------------------------------------------------------

    async def system_info(self) -> SystemInfo:
        meta = await self._call("system_info")
        return SystemInfo.model_validate(meta or {})

    async def disk_usage(self) -> DiskUsage:
        meta = await self._call("disk_usage")
        return DiskUsage.model_validate(meta or {})

    async def prune_containers(self) -> int:
        return int(await self._call("prune_containers") or 0)

    async def prune_images(self, all: bool) -> int:
        return int(await self._call("prune_images", {"all": all}) or 0)

    async def prune_volumes(self) -> int:
        return int(await self._call("prune_volumes") or 0)

    async def prune_networks(self) -> None:
        await self._call("prune_networks")

    async def prune_build_cache(self) -> int:
        return int(await self._call("prune_build_cache") or 0)

    # -- compose -------------------------------------------------------------

    async def compose_up(self, project: str, compose_yaml: str) -> str:
        return await self._call(
            "compose_up", {"project": project, "compose": compose_yaml}
        ) or ""

    async def compose_down(self, project: str) -> str:
        return await self._call("compose_down", {"project": project}) or ""

    # -- swarm ---------------------------------------------------------------

    async def swarm_active(self) -> bool:
        return bool(await self._call("swarm_active"))

    async def list_swarm_nodes(self) -> List[Any]:
        from ..models import SwarmNode

        meta = await self._call("list_swarm_nodes")
        return [SwarmNode.model_validate(m) for m in (meta or [])]

    async def swarm_deploy(self, stack: str, compose_yaml: str) -> str:
        return await self._call(
            "swarm_deploy", {"stack": stack, "compose": compose_yaml}
        ) or ""

    async def swarm_remove(self, stack: str) -> str:
        return await self._call("swarm_remove", {"stack": stack}) or ""

    # -- container filesystem (archive API) ----------------------------------

    async def container_path_stat(self, id: str, path: str) -> Dict[str, Any]:
        return await self._call("container_path_stat", {"id": id, "path": path}) or {}

    async def container_list_dir(self, id: str, path: str, limit: int) -> Dict[str, Any]:
        return await self._call(
            "container_list_dir", {"id": id, "path": path, "limit": limit}
        ) or {}

    async def container_read_file(self, id: str, path: str, max_bytes: int) -> bytes:
        body = await self._stream(
            "container_read_file", {"id": id, "path": path, "maxBytes": max_bytes}
        )
        try:
            return await body.read(-1)
        finally:
            await _close_body(body)

    async def container_get_archive(self, id: str, path: str) -> AsyncByteStream:
        return await self._stream("container_get_archive", {"id": id, "path": path})

    async def container_put_archive(self, id: str, path: str, content: AsyncByteStream) -> None:
        stream = await self._dial()
        try:
            _meta, body = await agentproto.call(
                stream, "container_put_archive", {"id": id, "path": path}, body=content
            )
            if body is not None:
                await body.close()
        finally:
            await _reset(stream)

    async def container_exec_status(
        self, id: str, cmd: List[str], user: str = ""
    ) -> Dict[str, Any]:
        return await self._call(
            "container_exec_status", {"id": id, "cmd": list(cmd), "user": user}
        ) or {}

    async def container_relabel(
        self,
        id: str,
        set_labels: Dict[str, str],
        remove_labels: List[str],
        connect_networks: List[str],
        disconnect_networks: List[str],
    ) -> str:
        meta = await self._call(
            "container_relabel",
            {
                "id": id,
                "setLabels": dict(set_labels or {}),
                "removeLabels": list(remove_labels or []),
                "connectNetworks": list(connect_networks or []),
                "disconnectNetworks": list(disconnect_networks or []),
            },
        )
        return str(meta or "")


# ---------------------------------------------------------------------------
# Exec-attach bidirectional wrapper
# ---------------------------------------------------------------------------


class _ExecAttachStream:
    """Bidirectional :class:`ExecStream` over a mux stream running exec_attach.

    ``write`` sends a length-prefixed stdin frame; ``read`` returns raw
    stdout/stderr bytes; ``close`` terminates the request framing and resets the
    stream so the session tears down cleanly.
    """

    def __init__(self, stream: Any, body: Any) -> None:
        self._stream = stream
        self._body = body  # agentproto _ResponseBody
        self._closed = False

    async def read(self, n: int = -1) -> bytes:
        return await self._body.read(n if n and n > 0 else 65536)

    async def write(self, data: bytes) -> None:
        await self._body.write(data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Send the body terminator so the agent's stdin pump sees EOF, then reset.
        try:
            await agentproto._write_body_terminator(self._stream)
        except Exception:
            pass
        await _reset(self._stream)


# ---------------------------------------------------------------------------
# small stream lifecycle helpers
# ---------------------------------------------------------------------------


async def _reset(stream: Any) -> None:
    """Best-effort reset of a mux stream (releases both directions)."""
    try:
        await stream.reset()
    except Exception:
        try:
            await stream.close()
        except Exception:
            pass


async def _close_body(body: Any) -> None:
    close = getattr(body, "close", None)
    if callable(close):
        res = close()
        if hasattr(res, "__await__"):
            await res


def make_remote_client(dial: Dial) -> RemoteClient:
    """Factory: build a :class:`RemoteClient` from a mux-stream ``dial`` callable.

    ``dial`` is an async callable returning a fresh :class:`kowan.mux.Stream`.
    The provider passes ``lambda: hub.dial(host_id)``.
    """
    return RemoteClient(dial)
