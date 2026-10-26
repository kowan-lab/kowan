"""User model.

Note: ``password_hash`` is NEVER serialized. It is modelled as a field with
``exclude=True`` so ``model_dump(by_alias=True)`` omits it from API output.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# AuthSourceLocal is the AuthSource value for built-in username/password users.
AuthSourceLocal = "local"

# Role constants.
RoleAdmin = "admin"
RoleOperator = "operator"
RoleViewer = "viewer"


class User(BaseModel):
    """An authenticated principal of the control plane."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    username: str = Field(default="")
    role: str = Field(default="")  # "admin" | "operator" | "viewer"
    # Never serialized.
    password_hash: str = Field(default="", alias="passwordHash", exclude=True)
    email: str = Field(default="")
    auth_source: str = Field(default="", alias="authSource")
    created_at: str = Field(default="", alias="createdAt")
