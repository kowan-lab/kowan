"""Deployment models.

Includes the ``ContainerSpec`` shared by the container/swarm paths and the
``Deployment.resource_name()`` helper (with its ``sanitize_resource_name``
free function).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .container import Port

# Deployment type and strategy constants.
DeploymentTypeContainer = "container"
DeploymentTypeCompose = "compose"
DeploymentStrategyStandalone = "standalone"
DeploymentStrategySwarm = "swarm"

# Management labels applied to every container/service Kowan deploys.
LabelManagedBy = "kowan.managed"
LabelDeploymentID = "kowan.deployment.id"
LabelDeploymentName = "kowan.deployment.name"


class KeyValue(BaseModel):
    """A single key/value pair, used for environment variables."""

    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(default="")
    value: str = Field(default="")


class PortMapping(BaseModel):
    """A published container port. Protocol is "tcp" or "udp"."""

    model_config = ConfigDict(populate_by_name=True)

    host_port: int = Field(default=0, alias="hostPort")
    container_port: int = Field(default=0, alias="containerPort")
    protocol: str = Field(default="")


class VolumeMapping(BaseModel):
    """A bind/volume mount. Source is a named volume or host path."""

    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(default="")
    target: str = Field(default="")
    read_only: bool = Field(default=False, alias="readOnly")


class ContainerSpec(BaseModel):
    """Declarative description of a single container.

    Fields below the mandatory block are optional; they are given Python
    defaults so a spec dumped ``by_alias`` always has the same shape.
    """

    model_config = ConfigDict(populate_by_name=True)

    image: str = Field(default="")
    env: List[KeyValue] = Field(default_factory=list)
    ports: List[PortMapping] = Field(default_factory=list)
    volumes: List[VolumeMapping] = Field(default_factory=list)
    networks: List[str] = Field(default_factory=list)
    restart_policy: str = Field(default="", alias="restartPolicy")
    command: List[str] = Field(default_factory=list)
    labels: Dict[str, str] = Field(default_factory=dict)

    # Advanced (optional) fields.
    entrypoint: List[str] = Field(default_factory=list)
    working_dir: str = Field(default="", alias="workingDir")
    user: str = Field(default="")
    cpu_limit: float = Field(default=0.0, alias="cpuLimit")  # cores; 0 = unlimited
    memory_limit_mb: int = Field(default=0, alias="memoryLimitMB")  # 0 = unlimited
    memory_reservation_mb: int = Field(default=0, alias="memoryReservationMB")
    privileged: bool = Field(default=False)
    gpus: str = Field(default="")  # "" | "all" | a number like "2"
    tty: bool = Field(default=False)
    open_stdin: bool = Field(default=False, alias="openStdin")
    pull_image: bool = Field(default=False, alias="pullImage")
    extra_args: List[str] = Field(default_factory=list, alias="extraArgs")


class PreviewSettings(BaseModel):
    """Preview environments of a git compose deployment (kowan/deployment/previews.py).

    ``branches`` is a comma-separated list of glob patterns: a push to a
    matching branch (other than the deployment's own ref) creates or updates a
    preview; empty means no branch previews. ``pull_requests`` makes opening a
    pull/merge request create one and closing it remove it. A preview nobody
    pushed to for ``ttl_hours`` is removed (0 = never); at most ``max`` exist.
    """

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = Field(default=False)
    branches: str = Field(default="")
    pull_requests: bool = Field(default=True, alias="pullRequests")
    ttl_hours: int = Field(default=72, alias="ttlHours")
    max: int = Field(default=5)


class PreviewInfo(BaseModel):
    """What makes a deployment a preview: its parent and the branch or pull
    request it runs."""

    model_config = ConfigDict(populate_by_name=True)

    parent_id: str = Field(default="", alias="parentId")
    ref: str = Field(default="")
    pull_request: int = Field(default=0, alias="pullRequest")
    title: str = Field(default="")
    url: str = Field(default="")
    created_by: str = Field(default="", alias="createdBy")
    last_activity_at: str = Field(default="", alias="lastActivityAt")


class GitSource(BaseModel):
    """Optional git source of a compose deployment (see kowan/deployment/gitsource.py).

    ``ref`` is a branch or tag (default ``main``); ``path`` is the compose file
    inside the repo (default ``docker-compose.yml``); ``credential_id`` points at a
    stored connection implementing ``gitFetch`` for private repos; ``auto_deploy``
    redeploys on a verified push webhook signed with ``webhook_secret``.
    """

    model_config = ConfigDict(populate_by_name=True)

    repo_url: str = Field(default="", alias="repoUrl")
    ref: str = Field(default="main")
    path: str = Field(default="docker-compose.yml")
    credential_id: str = Field(default="", alias="credentialId")
    auto_deploy: bool = Field(default=False, alias="autoDeploy")
    webhook_secret: str = Field(default="", alias="webhookSecret")
    previews: Optional[PreviewSettings] = Field(default=None)


class Deployment(BaseModel):
    """A deployment: a container or a compose stack Kowan runs on one host."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    type: str = Field(default="")
    strategy: str = Field(default="")
    targets: List[str] = Field(default_factory=list)
    replicas: int = Field(default=0)
    spec: Optional[ContainerSpec] = Field(default=None)
    compose: str = Field(default="")
    status: str = Field(default="")
    redeploy_token: str = Field(default="", alias="redeployToken")
    protected: bool = Field(default=False)
    # Desired-state switch: when True the reconcile loop keeps this deployment
    # live (redeploys a target that is missing/down). When False the system leaves
    # it alone (paused). Defaults True so existing deployments stay managed.
    enabled: bool = Field(default=True)
    # Git source (compose only). When set, deploy clones the repo and reads the
    # compose file from it; ``compose`` then mirrors the last fetched file.
    git: Optional[GitSource] = Field(default=None)
    deployed_commit: str = Field(default="", alias="deployedCommit")
    # Set on a preview environment (a copy of a git deployment on another branch).
    preview: Optional[PreviewInfo] = Field(default=None)
    # The deployment this one promotes to (staging -> production), "" for none.
    promote_to: str = Field(default="", alias="promoteTo")
    last_sync_at: str = Field(default="", alias="lastSyncAt")
    created_at: str = Field(default="", alias="createdAt")
    updated_at: str = Field(default="", alias="updatedAt")

    def resource_name(self) -> str:
        """Derive a Docker-safe resource name from the free-form display Name.

        Sanitize the Name, fall back
        to an ID-derived token, then to the literal "app".
        """
        name = sanitize_resource_name(self.name)
        if name:
            return name
        rid = sanitize_resource_name(self.id)
        if rid:
            return "app-" + rid
        return "app"


class DeploymentTarget(BaseModel):
    """Per-target runtime status of a deployment (computed, not persisted)."""

    model_config = ConfigDict(populate_by_name=True)

    host_id: str = Field(default="", alias="hostId")
    host_name: str = Field(default="", alias="hostName")
    state: str = Field(default="")
    container_id: str = Field(default="", alias="containerId")
    message: str = Field(default="")


class DeploymentService(BaseModel):
    """Runtime status of a single service of a deployment (computed)."""

    model_config = ConfigDict(populate_by_name=True)

    service: str = Field(default="")
    container_id: str = Field(default="", alias="containerId")
    # The container's Docker name (no leading slash), so the UI can show and link
    # to it rather than only a short id.
    name: str = Field(default="")
    state: str = Field(default="")
    image: str = Field(default="")
    ports: List[Port] = Field(default_factory=list)


def sanitize_resource_name(s: str) -> str:
    """Lowercase ``s`` and map it onto the Docker/Compose name charset.

    Keeps ``[a-z0-9_]`` as-is, collapses any run of unsupported characters
    (spaces, dots, slashes, unicode, dashes) into a single '-', and trims
    leading/trailing ``-``/``_``. Returns "" when nothing usable remains.
    Leading dashes are dropped (a '-' is only emitted when output is non-empty).
    """
    s = s.strip().lower()
    out: List[str] = []
    prev_dash = False
    for r in s:
        if ("a" <= r <= "z") or ("0" <= r <= "9") or r == "_":
            out.append(r)
            prev_dash = False
        else:
            # Spaces, dots, slashes, unicode and existing dashes all collapse to
            # a single separating dash; leading dashes are dropped (len == 0).
            if not prev_dash and len(out) > 0:
                out.append("-")
                prev_dash = True
    return "".join(out).strip("-_")
