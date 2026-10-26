"""AuditEntry model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AuditEntry(BaseModel):
    """A single audited event (mutating API action or auth event)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    time: str = Field(default="")  # RFC3339 UTC, stamped server-side
    user_id: str = Field(default="", alias="userId")
    username: str = Field(default="")
    action: str = Field(default="")  # HTTP method, or "login"/"login_failed"
    path: str = Field(default="")
    status: int = Field(default=0)
    ip: str = Field(default="", alias="ip")
    detail: str = Field(default="")  # omitted when empty
