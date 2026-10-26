"""WorkflowTrigger model."""

from __future__ import annotations

from typing import Dict

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from ._serde import drop_empty


class WorkflowTrigger(BaseModel):
    """One configured trigger attached to a workflow (OR semantics).

    ``state``/``lastFired``/``lastError`` are omitted when empty.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    type: str = Field(default="")
    enabled: bool = Field(default=False)
    config: Dict[str, str] = Field(default_factory=dict)
    state: str = Field(default="")
    last_fired: str = Field(default="", alias="lastFired")
    last_error: str = Field(default="", alias="lastError")

    @model_serializer(mode="wrap")
    def _ser(self, handler):
        return drop_empty(self, handler(self), "state", "last_fired", "last_error")
