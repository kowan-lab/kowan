"""Workflow models."""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from ._serde import drop_empty
from .workflowtrigger import WorkflowTrigger

# Workflow step types.
WorkflowStepScript = "script"
WorkflowStepDeploy = "deploy"
WorkflowStepContainer = "container"
WorkflowStepNotify = "notify"
WorkflowStepBackup = "backup"
WorkflowStepHTTP = "http"
WorkflowStepPrune = "prune"
WorkflowStepRunContainer = "run-container"

# Container step actions.
ContainerActionStart = "start"
ContainerActionStop = "stop"
ContainerActionRestart = "restart"

# Step result statuses.
StepStatusSuccess = "success"
StepStatusFailed = "failed"
StepStatusSkipped = "skipped"

# Workflow run statuses.
WorkflowRunRunning = "running"
WorkflowRunSuccess = "success"
WorkflowRunFailed = "failed"

# Workflow run triggers.
WorkflowTriggerManual = "manual"
WorkflowTriggerWebhook = "webhook"
WorkflowTriggerCron = "cron"


class WorkflowPayload(BaseModel):
    """The inbound request that triggered a workflow run via its webhook token.

    Nil for manual and cron runs.
    """

    model_config = ConfigDict(populate_by_name=True)

    method: str = Field(default="")
    body: str = Field(default="")
    headers: Dict[str, str] = Field(default_factory=dict)
    query: Dict[str, str] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    """A single typed, ordered step within a workflow."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(default="")
    type: str = Field(default="")
    config: Dict[str, str] = Field(default_factory=dict)
    continue_on_error: bool = Field(default=False, alias="continueOnError")


class Workflow(BaseModel):
    """A named, ordered list of steps executed sequentially."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    description: str = Field(default="")
    steps: List[WorkflowStep] = Field(default_factory=list)
    triggers: List[WorkflowTrigger] = Field(default_factory=list)
    # Env-style run variables seeding the run environment (CI-style, exported
    # between steps). Values may hold [[secret]] references resolved at run start.
    variables: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = Field(default=False)
    run_token: str = Field(default="", alias="runToken")  # omitted when empty
    protected: bool = Field(default=False)
    created_at: str = Field(default="", alias="createdAt")
    updated_at: str = Field(default="", alias="updatedAt")

    @model_serializer(mode="wrap")
    def _ser(self, handler):
        return drop_empty(self, handler(self), "run_token", "variables")


class StepResult(BaseModel):
    """The outcome of executing a single workflow step."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(default="")
    type: str = Field(default="")
    status: str = Field(default="")
    output: str = Field(default="")
    error: str = Field(default="")  # omitted when empty
    duration_ms: int = Field(default=0, alias="durationMs")

    @model_serializer(mode="wrap")
    def _ser(self, handler):
        return drop_empty(self, handler(self), "error")


class WorkflowRun(BaseModel):
    """A single execution of a workflow, with per-step results."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    workflow_id: str = Field(default="", alias="workflowId")
    trigger: str = Field(default="")
    status: str = Field(default="")
    steps: List[StepResult] = Field(default_factory=list)
    started_at: str = Field(default="", alias="startedAt")
    finished_at: str = Field(default="", alias="finishedAt")
