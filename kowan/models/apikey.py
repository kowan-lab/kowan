"""APIKey model.

The full secret is shown only once at creation and is never stored/serialized
here — only the display ``Prefix`` is kept.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class APIKey(BaseModel):
    """An admin-managed credential that authenticates API requests."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    prefix: str = Field(default="")  # first 11 chars, e.g. "pk_1a2b3c4d"
    role: str = Field(default="")  # "admin" | "operator" | "viewer"
    created_at: str = Field(default="", alias="createdAt")
    last_used: str = Field(default="", alias="lastUsed")  # omitted when empty
    # Resource scoping (docs/permissions.md): a key bound to a team acts with the
    # team's grants; ``scopes`` are the key's own narrowed grants (filled by the
    # api layer from the grants table, never persisted on this row).
    team_id: str = Field(default="", alias="teamId")
    scopes: List[Dict[str, Any]] = Field(default_factory=list)
