"""The ``Ops`` abstract base class: the uniform Docker abstraction.

``Ops`` is the single contract used by the rest
of the application. ``LocalClient`` implements it against the Docker Engine API;
``RemoteClient`` implements it by calling a Kowan agent. The concrete clients
live in local.py / remote.py; this module declares the interface. The
Docker-specific return/option structs it references are the canonical ones in
``kowan.models``, re-exported here so existing ``kowan.docker.base`` import sites
keep resolving.

Design notes:

- Methods take no explicit context (asyncio carries cancellation) and raise on
  error instead of returning it.
- Streams (logs, stats stream, pull, volume downloads) are async byte
  streams. We type them as ``AsyncByteStream`` — any object
  exposing ``async read(n)`` / ``async iter_chunked``-style iteration and
  ``close()`` (aiohttp's ``StreamReader``-backed response body qualifies). The
  caller is responsible for closing the underlying response.
- Exec attach returns an ``ExecStream``, a bidirectional
  duck-typed object (``read`` / ``write`` / ``close``).
- Broad shared model types (Container, Image, Volume, Network, ContainerSpec,
  Host) live in ``kowan.models``; here they are typed as ``Any`` so this module
  imports cleanly on its own. The Docker-specific return/option structs that the
  interface returns/accepts are the canonical ``kowan.models`` definitions,
  imported below and re-exported so ``kowan.docker.base.X`` keeps resolving.
"""

from __future__ import annotations

import abc
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol

from ..models import (  # canonical Docker return/option structs, re-exported
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


# ---------------------------------------------------------------------------
# Stream protocols (io.ReadCloser / io.ReadWriteCloser analogues)
# ---------------------------------------------------------------------------


class AsyncByteStream(Protocol):
    """A readable, closable async byte stream (``io.ReadCloser`` analogue).

    Satisfied by aiohttp response bodies and by simple in-memory wrappers. The
    caller must close it when done.
    """

    async def read(self, n: int = -1) -> bytes: ...

    def __aiter__(self) -> AsyncIterator[bytes]: ...


class ExecStream(Protocol):
    """A bidirectional exec stream (``io.ReadWriteCloser`` analogue).

    Returned by :meth:`Ops.exec_attach`; the caller must close it.
    """

    async def read(self, n: int = -1) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


# The Docker-specific return/option structs (ImageHistoryLayer, VolumeFile,
# VolumeCreateOptions, NetworkCreateOptions, ContainerStats, DiskUsage,
# SystemInfo, BuildOptions, BuildResult, PluginInfo, PluginInstallOptions) are
# imported from ``kowan.models`` at the top of this module and re-exported here.


# ---------------------------------------------------------------------------
# The Ops contract
# ---------------------------------------------------------------------------


class Ops(abc.ABC):
    """The set of Docker operations exposed by a single host.

    Implementations map their underlying types to the shared ``kowan.models`` types so callers
    never depend on the Docker Engine API or the agent wire format directly.

    Broad model return types (Container, Image, Volume, Network, ContainerSpec)
    are typed ``Any`` here pending the ``kowan.models`` phase; see the module
    docstring.
    """

    # -- reachability --------------------------------------------------------

    @abc.abstractmethod
    async def ping(self) -> None:
        """Verify that the host is reachable. Raises on failure."""
        raise NotImplementedError

    # -- containers ----------------------------------------------------------

    @abc.abstractmethod
    async def list_containers(self, all: bool) -> List[Any]:
        """Return the containers (``list[models.Container]``); include stopped when ``all``."""
        raise NotImplementedError

    @abc.abstractmethod
    async def inspect_container(self, id: str) -> Dict[str, Any]:
        """Return the raw inspect document for a container."""
        raise NotImplementedError

    @abc.abstractmethod
    async def container_spec(self, id: str) -> Any:
        """Reconstruct a ``models.ContainerSpec`` from an existing container."""
        raise NotImplementedError

    @abc.abstractmethod
    async def start_container(self, id: str) -> None:
        """Start a stopped container."""
        raise NotImplementedError

    @abc.abstractmethod
    async def stop_container(self, id: str) -> None:
        """Gracefully stop a running container."""
        raise NotImplementedError

    @abc.abstractmethod
    async def restart_container(self, id: str) -> None:
        """Restart a container."""
        raise NotImplementedError

    @abc.abstractmethod
    async def remove_container(self, id: str, force: bool) -> None:
        """Remove a container; ``force`` removes it even if running."""
        raise NotImplementedError

    @abc.abstractmethod
    async def container_logs(
        self, id: str, follow: bool, tail: int
    ) -> AsyncByteStream:
        """Return a reader over a container's logs (``io.ReadCloser`` analogue).

        When ``follow`` the stream stays open and emits new lines. ``tail`` <= 0
        means all history. The caller must close the returned stream.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def container_stats(self, id: str) -> ContainerStats:
        """Return a single sampled set of resource metrics."""
        raise NotImplementedError

    @abc.abstractmethod
    async def container_stats_stream(self, id: str) -> AsyncByteStream:
        """Return a reader over a live stats stream (NDJSON of ContainerStats).

        Roughly one sample per second until the container stops or the stream is
        cancelled. The caller must close the returned stream.
        """
        raise NotImplementedError

    async def events(
        self,
        since: Optional[int] = None,
        until: Optional[int] = None,
        filters: Optional[Dict[str, List[str]]] = None,
    ) -> AsyncByteStream:
        """Return a reader over the Docker events stream (NDJSON, raw Engine shape).

        ``since``/``until`` are unix seconds. With ``until`` set the Engine
        replays the matching history and ends the stream; without it the stream
        stays open and emits new events live. ``filters`` is the Engine filter
        map (e.g. ``{"type": ["container"]}``). The caller must close the stream.

        Not abstract (unlike the other ops) so existing test doubles keep
        working; concrete clients override it.
        """
        raise NotImplementedError("events not supported by this client")

    @abc.abstractmethod
    async def create_container(
        self, name: str, spec: Any, start: bool
    ) -> str:
        """Create (and optionally start) a container from ``spec``; return its ID.

        Any pre-existing container with the same ``name`` is removed first.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def pause_container(self, id: str) -> None:
        """Suspend all processes in a running container."""
        raise NotImplementedError

    @abc.abstractmethod
    async def unpause_container(self, id: str) -> None:
        """Resume a paused container."""
        raise NotImplementedError

    @abc.abstractmethod
    async def kill_container(self, id: str, signal: str = "SIGKILL") -> None:
        """Send a signal to a running container (SIGKILL by default: stop it
        at once, without the grace period of ``stop_container``)."""
        raise NotImplementedError

    # -- exec ----------------------------------------------------------------

    @abc.abstractmethod
    async def exec(self, id: str, cmd: List[str]) -> str:
        """Run ``cmd`` inside the container and return the combined output."""
        raise NotImplementedError

    @abc.abstractmethod
    async def exec_attach(
        self, id: str, cmd: List[str], tty: bool
    ) -> ExecStream:
        """Start an interactive exec session; return a bidirectional stream.

        When ``tty`` the stream is raw; otherwise it carries Docker's multiplexed
        stdout/stderr framing. The caller must close the returned stream.
        """
        raise NotImplementedError

    # -- images --------------------------------------------------------------

    @abc.abstractmethod
    async def list_images(self) -> List[Any]:
        """Return the images (``list[models.Image]``) present on the host."""
        raise NotImplementedError

    @abc.abstractmethod
    async def build_image(self, opts: BuildOptions) -> BuildResult:
        """Build (and optionally push) a Docker image from ``opts``."""
        raise NotImplementedError

    @abc.abstractmethod
    async def pull_image(
        self, ref: str, registry_auth: str
    ) -> AsyncByteStream:
        """Pull an image by reference; return the progress stream.

        ``registry_auth`` is the base64 X-Registry-Auth token ("" for anonymous).
        The caller must close the returned stream.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def remove_image(self, id: str, force: bool) -> None:
        """Remove an image; ``force`` removes it even if in use/tagged."""
        raise NotImplementedError

    @abc.abstractmethod
    async def inspect_image(self, id: str) -> Dict[str, Any]:
        """Return the raw ``docker inspect`` JSON of an image as a generic map."""
        raise NotImplementedError

    @abc.abstractmethod
    async def image_history(self, id: str) -> List[ImageHistoryLayer]:
        """Return the build-history layers of an image."""
        raise NotImplementedError

    # -- volumes -------------------------------------------------------------

    @abc.abstractmethod
    async def list_volumes(self) -> List[Any]:
        """Return the volumes (``list[models.Volume]``) on the host."""
        raise NotImplementedError

    @abc.abstractmethod
    async def create_volume(self, opts: VolumeCreateOptions) -> Any:
        """Create a volume from ``opts``; return the created ``models.Volume``."""
        raise NotImplementedError

    @abc.abstractmethod
    async def remove_volume(self, name: str, force: bool) -> None:
        """Remove a volume; ``force`` removes it even if in use."""
        raise NotImplementedError

    @abc.abstractmethod
    async def inspect_volume(self, name: str) -> Dict[str, Any]:
        """Return the raw ``docker volume inspect`` JSON of a volume."""
        raise NotImplementedError

    @abc.abstractmethod
    async def browse_volume(self, name: str, sub_path: str) -> List[VolumeFile]:
        """List the immediate entries at ``sub_path`` within a volume.

        ``sub_path`` is relative to the volume root ("" or "/" is the root).
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def read_volume_file(self, name: str, sub_path: str) -> bytes:
        """Return the (size-bounded) contents of a single file within a volume."""
        raise NotImplementedError

    @abc.abstractmethod
    async def download_volume_file(
        self, name: str, sub_path: str
    ) -> AsyncByteStream:
        """Stream the raw, unbounded bytes of a single file within a volume.

        The caller must close the returned stream.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def download_volume_archive(
        self, name: str, sub_path: str
    ) -> AsyncByteStream:
        """Stream a zip archive of the file or directory at ``sub_path``.

        The caller must close the returned stream.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def upload_volume_file(
        self,
        name: str,
        dest_dir: str,
        filename: str,
        size: int,
        content: AsyncByteStream,
    ) -> None:
        """Write a single file (``size`` bytes read from ``content``) into a volume dir."""
        raise NotImplementedError

    # -- plugins -------------------------------------------------------------

    @abc.abstractmethod
    async def list_plugins(self) -> List[PluginInfo]:
        """Return the Docker managed plugins installed on the host."""
        raise NotImplementedError

    @abc.abstractmethod
    async def install_plugin(self, opts: PluginInstallOptions) -> None:
        """Install and enable a Docker managed plugin (synchronous)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def remove_plugin(self, name: str, force: bool) -> None:
        """Remove a Docker managed plugin from the host."""
        raise NotImplementedError

    # -- networks ------------------------------------------------------------

    @abc.abstractmethod
    async def list_networks(self) -> List[Any]:
        """Return the networks (``list[models.Network]``) on the host."""
        raise NotImplementedError

    @abc.abstractmethod
    async def create_network(self, opts: NetworkCreateOptions) -> Any:
        """Create a network from ``opts``; return the created ``models.Network``."""
        raise NotImplementedError

    @abc.abstractmethod
    async def inspect_network(self, id: str) -> Any:
        """Return a ``models.Network`` with its IPAM config and attached containers."""
        raise NotImplementedError

    @abc.abstractmethod
    async def connect_container(
        self, network_id: str, container_id: str
    ) -> None:
        """Attach a container to a network."""
        raise NotImplementedError

    @abc.abstractmethod
    async def disconnect_container(
        self, network_id: str, container_id: str, force: bool
    ) -> None:
        """Detach a container from a network; ``force`` detaches even if rejected."""
        raise NotImplementedError

    @abc.abstractmethod
    async def remove_network(self, id: str) -> None:
        """Remove a network by ID."""
        raise NotImplementedError

    # -- system / prune ------------------------------------------------------

    @abc.abstractmethod
    async def system_info(self) -> SystemInfo:
        """Return a summary of the Docker host."""
        raise NotImplementedError

    @abc.abstractmethod
    async def disk_usage(self) -> DiskUsage:
        """Return a breakdown of disk consumed by Docker objects."""
        raise NotImplementedError

    @abc.abstractmethod
    async def prune_containers(self) -> int:
        """Remove all stopped containers; return the bytes reclaimed."""
        raise NotImplementedError

    @abc.abstractmethod
    async def prune_images(self, all: bool) -> int:
        """Remove unused images; return the bytes reclaimed.

        When ``all`` every unused image is removed, otherwise only dangling ones.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def prune_volumes(self) -> int:
        """Remove unused volumes; return the bytes reclaimed."""
        raise NotImplementedError

    @abc.abstractmethod
    async def prune_networks(self) -> None:
        """Remove unused networks."""
        raise NotImplementedError

    @abc.abstractmethod
    async def prune_build_cache(self) -> int:
        """Remove build cache nothing uses; returns the bytes freed."""
        raise NotImplementedError

    # -- compose -------------------------------------------------------------

    @abc.abstractmethod
    async def compose_up(self, project: str, compose_yaml: str) -> str:
        """Bring up a compose project (``docker compose -p project up -d``).

        Return the combined command output.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def compose_down(self, project: str) -> str:
        """Tear down a compose project; return the combined command output."""
        raise NotImplementedError

    # -- swarm ---------------------------------------------------------------

    @abc.abstractmethod
    async def swarm_active(self) -> bool:
        """Report whether the host is an active swarm manager."""
        raise NotImplementedError

    @abc.abstractmethod
    async def list_swarm_nodes(self) -> List[Any]:
        """List the swarm's nodes (a ``List[SwarmNode]``).

        Returns an empty list on a host that is not a swarm manager (the Engine
        ``/nodes`` call answers 503 there), so callers can pair it with
        :meth:`swarm_active` without special-casing non-managers.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def swarm_deploy(self, stack: str, compose_yaml: str) -> str:
        """Deploy a swarm stack (``docker stack deploy``); return combined output."""
        raise NotImplementedError

    @abc.abstractmethod
    async def swarm_remove(self, stack: str) -> str:
        """Remove a swarm stack (``docker stack rm``); return combined output."""
        raise NotImplementedError

    # -- container filesystem (archive API) ----------------------------------
    # LocalClient / RemoteClient implement them. Paths are
    # absolute container paths already normalised by
    # ``kowan.docker.containerfs.normalize_path``. Used by the container file
    # browser and by exposure (reading a proxy's acme.json).

    @abc.abstractmethod
    async def container_path_stat(self, id: str, path: str) -> Dict[str, Any]:
        """Stat ``path`` in a container (``HEAD /containers/{id}/archive``).

        Returns a ``containerfs.entry_from_stat`` dict (name, path, size, mode,
        kind, isDir, isLink, linkTarget, modTime). Raises NotFound when missing.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def container_list_dir(self, id: str, path: str, limit: int) -> Dict[str, Any]:
        """List the immediate children of directory ``path`` (symlinks followed).

        Returns ``{"path", "entries", "truncated", "method"}`` where ``method``
        is ``"exec"`` (fast path on a running container with ``ls``) or
        ``"archive"`` (tar headers; works stopped / without a shell).
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def container_read_file(self, id: str, path: str, max_bytes: int) -> bytes:
        """Return the content of regular file ``path`` (at most ``max_bytes``)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def container_get_archive(self, id: str, path: str) -> AsyncByteStream:
        """Stream the raw tar of ``path`` (``GET /containers/{id}/archive``)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def container_put_archive(self, id: str, path: str, content: AsyncByteStream) -> None:
        """Extract the tar read from ``content`` into directory ``path``."""
        raise NotImplementedError

    @abc.abstractmethod
    async def container_exec_status(
        self, id: str, cmd: List[str], user: str = ""
    ) -> Dict[str, Any]:
        """Run ``cmd`` (no shell) and return ``{"exitCode": int, "output": str}``."""
        raise NotImplementedError

    @abc.abstractmethod
    async def container_relabel(
        self,
        id: str,
        set_labels: Dict[str, str],
        remove_labels: List[str],
        connect_networks: List[str],
        disconnect_networks: List[str],
    ) -> str:
        """Recreate a container with changed labels / networks, cloning the rest
        of its raw config. Rolls back to the original on failure. Returns the new id."""
        raise NotImplementedError
