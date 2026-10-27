"""Docker host abstraction.

The ``Ops`` abstract base class is the single
contract used by the rest of the application; ``Provider`` resolves the right
concrete implementation (local vs remote/edge) for a given host.
"""

from .base import (
    Ops,
    ImageHistoryLayer,
    VolumeListing,
    ContainerStats,
    DiskUsage,
    SystemInfo,
    BuildResult,
    PluginInfo,
    PluginInstallOptions,
    VolumeCreateOptions,
    NetworkCreateOptions,
    BuildOptions,
)
from .provider import Provider, EdgeDialer
from .local import (
    LocalClient,
    build_registry_auth,
    decode_registry_auth,
    registry_host_from_ref,
)

__all__ = [
    "Ops",
    "Provider",
    "EdgeDialer",
    "LocalClient",
    "build_registry_auth",
    "decode_registry_auth",
    "registry_host_from_ref",
    "ImageHistoryLayer",
    "VolumeListing",
    "ContainerStats",
    "DiskUsage",
    "SystemInfo",
    "BuildResult",
    "PluginInfo",
    "PluginInstallOptions",
    "VolumeCreateOptions",
    "NetworkCreateOptions",
    "BuildOptions",
]
