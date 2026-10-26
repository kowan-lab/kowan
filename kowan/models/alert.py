"""Alert models.

Includes the ``RuleTypeSpec`` catalog (``rule_types()``) and the
``valid_operator``/``valid_level``/``rule_type_by_name`` helpers.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

# Alert level constants.
AlertLevelInfo = "info"
AlertLevelWarning = "warning"
AlertLevelCritical = "critical"

# Alert rule type constants.
AlertTypeContainerDown = "container_down"
AlertTypeCPUHigh = "cpu_high"
AlertTypeMemHigh = "mem_high"
AlertTypeDiskHigh = "disk_high"
AlertTypeContainerUnhealthy = "container_unhealthy"
AlertTypeContainerRestarts = "container_restart_loop"
AlertTypeContainerExited = "container_exited"
AlertTypeHostDown = "host_down"

# Comparison operator constants for numeric rules.
OpGreater = ">"
OpLess = "<"
OpGreaterEqual = ">="
OpLessEqual = "<="
OpEqual = "=="


class Alert(BaseModel):
    """A fired notification about a host/container condition."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    rule_id: str = Field(default="", alias="ruleId")
    host_id: str = Field(default="", alias="hostId")
    level: str = Field(default="")  # "info" | "warning" | "critical"
    title: str = Field(default="")
    message: str = Field(default="")
    resource: str = Field(default="")  # container/host display label
    channel_id: str = Field(default="", alias="channelId")
    resolved: bool = Field(default=False)
    created_at: str = Field(default="", alias="createdAt")


class AlertRule(BaseModel):
    """A condition that produces Alerts."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    type: str = Field(default="")
    operator: str = Field(default="")  # numeric rules
    threshold: float = Field(default=0.0)
    severity: str = Field(default="")  # info | warning | critical
    for_duration: int = Field(default=0, alias="forDuration")
    host_id: str = Field(default="", alias="hostId")  # "" = all hosts
    match: str = Field(default="")
    label_key: str = Field(default="", alias="labelKey")
    label_value: str = Field(default="", alias="labelValue")
    channel_id: str = Field(default="", alias="channelId")
    auto_resolve: bool = Field(default=False, alias="autoResolve")
    enabled: bool = Field(default=False)
    created_at: str = Field(default="", alias="createdAt")


class RuleTypeSpec(BaseModel):
    """Metadata describing one alert rule type (drives validation + UI)."""

    model_config = ConfigDict(populate_by_name=True)

    type: str = Field(default="")
    label: str = Field(default="")
    description: str = Field(default="")
    category: str = Field(default="")  # "resource" | "lifecycle" | "host"
    numeric: bool = Field(default=False)  # shows operator + threshold
    unit: str = Field(default="")  # "%" | "GB" | "restarts"
    container_scoped: bool = Field(default=False, alias="containerScoped")
    default_operator: str = Field(default="", alias="defaultOperator")
    default_threshold: float = Field(default=0.0, alias="defaultThreshold")
    default_severity: str = Field(default="", alias="defaultSeverity")


def rule_types() -> List[RuleTypeSpec]:
    """Metadata for every supported alert rule type, in display order."""
    return [
        RuleTypeSpec(
            type=AlertTypeCPUHigh,
            label="CPU usage",
            description="A container's CPU usage crosses the threshold.",
            category="resource", numeric=True, unit="%", container_scoped=True,
            default_operator=OpGreater, default_threshold=80, default_severity=AlertLevelWarning,
        ),
        RuleTypeSpec(
            type=AlertTypeMemHigh,
            label="Memory usage",
            description="A container's memory usage crosses the threshold.",
            category="resource", numeric=True, unit="%", container_scoped=True,
            default_operator=OpGreater, default_threshold=80, default_severity=AlertLevelWarning,
        ),
        RuleTypeSpec(
            type=AlertTypeDiskHigh,
            label="Docker disk usage",
            description="The disk consumed by Docker on a host (images, containers, volumes, build cache) crosses the threshold.",
            category="resource", numeric=True, unit="GB", container_scoped=False,
            default_operator=OpGreater, default_threshold=20, default_severity=AlertLevelWarning,
        ),
        RuleTypeSpec(
            type=AlertTypeContainerRestarts,
            label="Restart loop",
            description="A container's restart count crosses the threshold (a crash loop).",
            category="lifecycle", numeric=True, unit="restarts", container_scoped=True,
            default_operator=OpGreaterEqual, default_threshold=5, default_severity=AlertLevelWarning,
        ),
        RuleTypeSpec(
            type=AlertTypeContainerDown,
            label="Container down",
            description="A container that was previously running is no longer running.",
            category="lifecycle", numeric=False, container_scoped=True,
            default_severity=AlertLevelCritical,
        ),
        RuleTypeSpec(
            type=AlertTypeContainerUnhealthy,
            label="Container unhealthy",
            description="A container's Docker healthcheck reports unhealthy.",
            category="lifecycle", numeric=False, container_scoped=True,
            default_severity=AlertLevelCritical,
        ),
        RuleTypeSpec(
            type=AlertTypeContainerExited,
            label="Abnormal exit",
            description="A container exited with a non-zero exit code or was killed for running out of memory (OOM).",
            category="lifecycle", numeric=False, container_scoped=True,
            default_severity=AlertLevelCritical,
        ),
        RuleTypeSpec(
            type=AlertTypeHostDown,
            label="Host unreachable",
            description="A managed host's Docker endpoint is offline.",
            category="host", numeric=False, container_scoped=False,
            default_severity=AlertLevelCritical,
        ),
    ]


def rule_type_by_name(t: str) -> Tuple[Optional[RuleTypeSpec], bool]:
    """Return the spec for a rule type and whether it is known."""
    for s in rule_types():
        if s.type == t:
            return s, True
    return None, False


def valid_operator(op: str) -> bool:
    """Whether ``op`` is a recognized comparison operator."""
    return op in (OpGreater, OpLess, OpGreaterEqual, OpLessEqual, OpEqual)


def valid_level(level: str) -> bool:
    """Whether ``level`` is a recognized alert level/severity."""
    return level in (AlertLevelInfo, AlertLevelWarning, AlertLevelCritical)
