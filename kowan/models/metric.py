"""MetricSample model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MetricSample(BaseModel):
    """A single point-in-time snapshot of a host's aggregate resource usage."""

    model_config = ConfigDict(populate_by_name=True)

    host_id: str = Field(default="", alias="hostId")
    time: str = Field(default="")  # RFC3339, stamped server-side
    running: int = Field(default=0)
    total: int = Field(default=0)
    images: int = Field(default=0)
    cpu_percent: float = Field(default=0.0, alias="cpuPercent")
    mem_used: int = Field(default=0, alias="memUsed")
    mem_total: int = Field(default=0, alias="memTotal")
