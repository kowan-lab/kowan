"""Volume plugin catalog models."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field


class VolumePluginField(BaseModel):
    """One per-volume driver option for a volume plugin.

    Reused as the field schema by ``WorkflowActionSpec`` too.
    """

    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(default="")
    label: str = Field(default="")
    placeholder: str = Field(default="")
    sensitive: bool = Field(default=False)
    required: bool = Field(default=False)
    # When True the value is supplied by the selected connection (resolved on the
    # control plane), so the UI must NOT render an input for it — it is injected
    # server-side into the driver options, never typed or sent by the browser.
    from_connection: bool = Field(default=False, alias="fromConnection")


class VolumePluginSpec(BaseModel):
    """A curated Docker managed volume plugin Kowan can install and use."""

    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(default="")
    label: str = Field(default="")
    description: str = Field(default="")
    plugin_ref: str = Field(default="", alias="pluginRef")
    driver: str = Field(default="")
    options: List[VolumePluginField] = Field(default_factory=list)


class PluginInfo(BaseModel):
    """Summary of a Docker managed plugin installed on a host."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    enabled: bool = Field(default=False)
    reference: str = Field(default="")
    description: str = Field(default="")


class PluginInstallOptions(BaseModel):
    """Installing a Docker managed plugin on a host."""

    model_config = ConfigDict(populate_by_name=True)

    remote: str = Field(default="")
    alias: str = Field(default="")
    settings: Dict[str, str] = Field(default_factory=dict)


# VolumePlugins is the curated catalog of supported managed volume plugins.
VolumePlugins: List[VolumePluginSpec] = [
    VolumePluginSpec(
        type="s3fs",
        label="Amazon S3 bucket (s3fs)",
        description="Mount an S3 (or S3-compatible) bucket as a Docker volume via the s3fs FUSE plugin.",
        plugin_ref="mochoa/s3fs-volume-plugin:latest",
        driver="mochoa/s3fs-volume-plugin:latest",
        options=[
            VolumePluginField(key="bucket", label="Bucket", placeholder="my-bucket", required=True),
            # Access key / secret / region come from the selected `aws` connection
            # (injected server-side by volumeauth); never rendered or sent here.
            VolumePluginField(key="AWSACCESSKEYID", label="Access Key ID", from_connection=True),
            VolumePluginField(key="AWSSECRETACCESSKEY", label="Secret Access Key", sensitive=True, from_connection=True),
            VolumePluginField(key="AWSDEFAULTREGION", label="Region", from_connection=True),
            VolumePluginField(key="url", label="Endpoint URL", placeholder="https://s3.amazonaws.com (or S3-compatible)"),
        ],
    ),
    VolumePluginSpec(
        type="sshfs",
        label="SSHFS remote directory",
        description="Mount a directory on a remote host over SSH as a Docker volume (vieux/sshfs). Useful as a live test of the plugin install flow.",
        plugin_ref="vieux/sshfs:latest",
        driver="vieux/sshfs:latest",
        options=[
            # Host + remote path are typed here; the username and password come
            # from the selected `basic-auth` connection. volumeauth assembles the
            # final `user@host:/path` sshcmd server-side, so the login never
            # round-trips the browser.
            VolumePluginField(key="host", label="Host", placeholder="host.example.com", required=True),
            VolumePluginField(key="path", label="Remote path", placeholder="/remote/path"),
            VolumePluginField(key="sshcmd", label="SSH target", from_connection=True),
            VolumePluginField(key="password", label="Password", sensitive=True, from_connection=True),
            VolumePluginField(key="port", label="Port", placeholder="22"),
        ],
    ),
]
