"""Notification model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Notification kind constants.
NotificationKindAlert = "alert"
NotificationKindNotice = "notice"


class Notification(BaseModel):
    """One entry in the notification center (the top-bar bell)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    kind: str = Field(default="")  # "alert" | "notice"
    level: str = Field(default="")  # info | warning | critical
    title: str = Field(default="")
    body: str = Field(default="")
    host_id: str = Field(default="", alias="hostId")
    source: str = Field(default="")
    ref_id: str = Field(default="", alias="refId")
    read: bool = Field(default=False)
    created_at: str = Field(default="", alias="createdAt")
