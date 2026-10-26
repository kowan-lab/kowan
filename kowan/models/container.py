"""Docker object models exposed by the Kowan API.

Field aliases give the camelCase names, so ``model_dump(by_alias=True)``
yields the JSON the API returns and the React frontend expects.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# LabelSupport marks a container as an internal support/sidecar container. Ones
# carrying this label set to "true" are hidden from the default container list.
LabelSupport = "kowan.support"


class Port(BaseModel):
    """A published or exposed container port."""

    model_config = ConfigDict(populate_by_name=True)

    private_port: int = Field(default=0, alias="privatePort")
    public_port: int = Field(default=0, alias="publicPort")
    type: str = Field(default="")
    ip: str = Field(default="", alias="ip")


class Container(BaseModel):
    """A Docker container as exposed by the Kowan API."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    names: List[str] = Field(default_factory=list)
    image: str = Field(default="")
    state: str = Field(default="")
    status: str = Field(default="")
    created: int = Field(default=0)
    ports: List[Port] = Field(default_factory=list)
    labels: Dict[str, str] = Field(default_factory=dict)
    host_id: str = Field(default="", alias="hostId")


class Image(BaseModel):
    """A Docker image."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    repo_tags: List[str] = Field(default_factory=list, alias="repoTags")
    size: int = Field(default=0)
    created: int = Field(default=0)


class ImageHistoryLayer(BaseModel):
    """One entry of an image's build history (docker history)."""

    model_config = ConfigDict(populate_by_name=True)

    created: int = Field(default=0)
    created_by: str = Field(default="", alias="createdBy")
    size: int = Field(default=0)
    comment: str = Field(default="")
    tags: List[str] = Field(default_factory=list)


class VolumeFile(BaseModel):
    """One entry when browsing a volume's contents (file or directory)."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(default="")
    path: str = Field(default="")
    size: int = Field(default=0)
    mode: str = Field(default="")
    is_dir: bool = Field(default=False, alias="isDir")
    mod_time: int = Field(default=0, alias="modTime")


class Volume(BaseModel):
    """A Docker volume."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(default="")
    driver: str = Field(default="")
    mountpoint: str = Field(default="")
    created_at: str = Field(default="", alias="createdAt")
    labels: Optional[Dict[str, str]] = Field(default=None)
    options: Optional[Dict[str, str]] = Field(default=None)


class VolumeCreateOptions(BaseModel):
    """Parameters for creating a volume."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(default="")
    driver: str = Field(default="")
    options: Dict[str, str] = Field(default_factory=dict)
    labels: Dict[str, str] = Field(default_factory=dict)


class IPAMConfig(BaseModel):
    """A single IP Address Management configuration block of a network."""

    model_config = ConfigDict(populate_by_name=True)

    subnet: str = Field(default="")
    gateway: str = Field(default="")
    ip_range: str = Field(default="", alias="ipRange")


class NetworkContainer(BaseModel):
    """A container attached to a network, as reported by inspecting it."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    ipv4_address: str = Field(default="", alias="ipv4Address")


class Network(BaseModel):
    """A Docker network."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    driver: str = Field(default="")
    scope: str = Field(default="")
    internal: bool = Field(default=False)
    attachable: bool = Field(default=False)
    labels: Optional[Dict[str, str]] = Field(default=None)
    ipam: Optional[List[IPAMConfig]] = Field(default=None, alias="ipam")
    containers: Optional[List[NetworkContainer]] = Field(default=None)


class NetworkCreateOptions(BaseModel):
    """Parameters for creating a network."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(default="")
    driver: str = Field(default="")
    internal: bool = Field(default=False)
    attachable: bool = Field(default=False)
    labels: Dict[str, str] = Field(default_factory=dict)
    options: Dict[str, str] = Field(default_factory=dict)
    ipam: List[IPAMConfig] = Field(default_factory=list, alias="ipam")


class ContainerStats(BaseModel):
    """A single sampled set of container resource metrics."""

    model_config = ConfigDict(populate_by_name=True)

    cpu_percent: float = Field(default=0.0, alias="cpuPercent")
    mem_usage: int = Field(default=0, alias="memUsage")
    mem_limit: int = Field(default=0, alias="memLimit")
    mem_percent: float = Field(default=0.0, alias="memPercent")
    net_rx: int = Field(default=0, alias="netRx")
    net_tx: int = Field(default=0, alias="netTx")
    blk_read: int = Field(default=0, alias="blkRead")
    blk_write: int = Field(default=0, alias="blkWrite")
    pids: int = Field(default=0, alias="pids")


class DiskItem(BaseModel):
    """One object the guided cleanup can remove (kowan/cleanup.py)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    size: int = Field(default=0)
    image: str = Field(default="")
    state: str = Field(default="")
    labels: Dict[str, str] = Field(default_factory=dict)
    created: int = Field(default=0)  # unix seconds


class DiskUsage(BaseModel):
    """Disk consumed by Docker objects on a host (docker system df), with the
    objects nothing uses listed for the guided cleanup."""

    model_config = ConfigDict(populate_by_name=True)

    images_count: int = Field(default=0, alias="imagesCount")
    images_size: int = Field(default=0, alias="imagesSize")
    images_reclaimable: int = Field(default=0, alias="imagesReclaimable")
    containers_count: int = Field(default=0, alias="containersCount")
    containers_size: int = Field(default=0, alias="containersSize")
    volumes_count: int = Field(default=0, alias="volumesCount")
    volumes_size: int = Field(default=0, alias="volumesSize")
    volumes_reclaimable: int = Field(default=0, alias="volumesReclaimable")
    build_cache_size: int = Field(default=0, alias="buildCacheSize")
    layers_size: int = Field(default=0, alias="layersSize")
    build_cache_reclaimable: int = Field(default=0, alias="buildCacheReclaimable")
    stopped_containers: List[DiskItem] = Field(default_factory=list, alias="stoppedContainers")
    unused_images: List[DiskItem] = Field(default_factory=list, alias="unusedImages")
    unused_volumes: List[DiskItem] = Field(default_factory=list, alias="unusedVolumes")


class SystemInfo(BaseModel):
    """Summary of a Docker host."""

    model_config = ConfigDict(populate_by_name=True)

    containers: int = Field(default=0)
    containers_running: int = Field(default=0, alias="containersRunning")
    images: int = Field(default=0)
    server_version: str = Field(default="", alias="serverVersion")
    os: str = Field(default="", alias="os")
    arch: str = Field(default="")
    mem_total: int = Field(default=0, alias="memTotal")
    ncpu: int = Field(default=0, alias="ncpu")


class SwarmNode(BaseModel):
    """A single node of a Docker Swarm, as reported by the ``/nodes`` API.

    Flattens the nested Engine ``Node`` object (Description/Spec/Status/
    ManagerStatus) into the handful of fields the UI shows: who is a manager,
    who leads, and each node's availability/health/address. Every source field
    is optional in the Engine response (workers have no ``ManagerStatus``; a
    down node may lack ``Engine``), so all values default to empty/False.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    hostname: str = Field(default="")
    role: str = Field(default="")
    leader: bool = Field(default=False)
    availability: str = Field(default="")
    status: str = Field(default="")
    address: str = Field(default="")
    engine_version: str = Field(default="", alias="engineVersion")
