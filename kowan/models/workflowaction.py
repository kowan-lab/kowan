"""Workflow action catalog.

The catalog powers the Plugins screen and the workflow step editor's per-type
form. Field shape is reused from ``VolumePluginField``.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field

from .volumeplugin import VolumePluginField
from .workflow import (
    WorkflowStepBackup,
    WorkflowStepContainer,
    WorkflowStepDeploy,
    WorkflowStepHTTP,
    WorkflowStepNotify,
    WorkflowStepPrune,
    WorkflowStepRunContainer,
    WorkflowStepScript,
)


class WorkflowActionSpec(BaseModel):
    """Describes one workflow action TYPE: its config field schema + metadata."""

    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(default="")
    label: str = Field(default="")
    description: str = Field(default="")
    fields: List[VolumePluginField] = Field(default_factory=list)


# WorkflowActions is the catalog of built-in workflow action types, in builder
# display order.
WorkflowActions: List[WorkflowActionSpec] = [
    WorkflowActionSpec(
        type=WorkflowStepScript,
        label="Run script",
        description="Execute a bash script. Receives the webhook body on stdin and KOWAN_WEBHOOK_* env vars.",
        fields=[
            VolumePluginField(key="script", label="Script", placeholder="#!/usr/bin/env bash\n...", required=True),
        ],
    ),
    WorkflowActionSpec(
        type=WorkflowStepHTTP,
        label="HTTP request",
        description="Call any HTTP endpoint with a configurable method. Template the webhook body with {{body}}, {{method}}, {{header.NAME}}, {{query.NAME}}.",
        fields=[
            VolumePluginField(key="method", label="Method", placeholder="POST"),
            VolumePluginField(key="url", label="URL", placeholder="https://api.example.com/hook", required=True),
            VolumePluginField(key="headers", label="Headers", placeholder="Content-Type: application/json"),
            VolumePluginField(key="body", label="Body", placeholder='{"text": "{{body}}"}'),
            VolumePluginField(key="connectionRef", label="Credential (httpBearer)", placeholder="(optional) connection id for auth"),
            VolumePluginField(key="expectStatus", label="Expected status", placeholder="2xx (default: any 2xx)"),
        ],
    ),
    WorkflowActionSpec(
        type=WorkflowStepDeploy,
        label="Deployment",
        description="Run a lifecycle action on a deployment by id or name.",
        fields=[
            VolumePluginField(key="deployment", label="Deployment", placeholder="id or name", required=True),
            VolumePluginField(key="action", label="Action", placeholder="redeploy | deploy | start | stop | destroy (default: redeploy)"),
        ],
    ),
    WorkflowActionSpec(
        type=WorkflowStepContainer,
        label="Container action",
        description="Start, stop or restart a container on a host.",
        fields=[
            VolumePluginField(key="host", label="Host", placeholder="host id", required=True),
            VolumePluginField(key="container", label="Container", placeholder="container id or name", required=True),
            VolumePluginField(key="action", label="Action", placeholder="start | stop | restart", required=True),
        ],
    ),
    WorkflowActionSpec(
        type=WorkflowStepRunContainer,
        label="Run container",
        description="Run a one-shot container to completion and capture its stdout.",
        fields=[
            VolumePluginField(key="host", label="Host", placeholder="host id", required=True),
            VolumePluginField(key="image", label="Image", placeholder="alpine:latest", required=True),
            VolumePluginField(key="command", label="Command", placeholder="(optional) echo hello"),
            VolumePluginField(key="env", label="Environment", placeholder="KEY=value (one per line)"),
            VolumePluginField(key="outputVar", label="Output variable", placeholder="(optional) exports stdout as $NAME and exit as $NAME_EXIT"),
            VolumePluginField(key="remove", label="Auto-remove", placeholder="true to remove the container after it exits"),
        ],
    ),
    WorkflowActionSpec(
        type=WorkflowStepPrune,
        label="Prune",
        description="Reclaim disk on a host by pruning unused Docker resources.",
        fields=[
            VolumePluginField(key="host", label="Host", placeholder="host id", required=True),
            VolumePluginField(key="target", label="Target", placeholder="containers | images | images-all | volumes | networks | all", required=True),
        ],
    ),
    WorkflowActionSpec(
        type=WorkflowStepNotify,
        label="Notify",
        description="Send a message through a notification channel.",
        fields=[
            VolumePluginField(key="channel", label="Channel", placeholder="channel id", required=True),
            VolumePluginField(key="title", label="Title"),
            VolumePluginField(key="body", label="Body"),
            VolumePluginField(key="level", label="Level", placeholder="info | warning | critical"),
        ],
    ),
    WorkflowActionSpec(
        type=WorkflowStepBackup,
        label="Backup",
        description="Write a tar.gz snapshot of the control-plane state.",
        fields=[
            VolumePluginField(key="path", label="Destination dir", placeholder="backups"),
        ],
    ),
]
