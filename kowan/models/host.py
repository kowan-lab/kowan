"""Host model.

``token`` is excluded from serialization.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Host type constants.
HostTypeLocal = "local"
HostTypeAgent = "agent"

# Host mode constants. The fixed local host uses HostModeAgent.
HostModeAgent = "agent"
HostModeEdge = "edge"


class Host(BaseModel):
    """A Docker host managed by Kowan: the local daemon or a remote agent."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    type: str = Field(default="")  # "local" | "agent"
    mode: str = Field(default="")  # "agent" | "edge"
    address: str = Field(default="")
    # Never serialized.
    token: str = Field(default="", exclude=True)
    online: bool = Field(default=False)
    last_seen: str = Field(default="", alias="lastSeen")
    created_at: str = Field(default="", alias="createdAt")
