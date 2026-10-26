"""Registry model.

``password`` and ``config`` are excluded from serialization.
Only ``configKeys`` and ``hasPassword`` are exposed.
"""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field

# Registry type constants.
RegistryTypeGeneric = "generic"
RegistryTypeGHCR = "ghcr"
RegistryTypeECR = "ecr"
RegistryTypeGCP = "gcp"
RegistryTypeACR = "acr"


def normalize_registry_type(t: str) -> str:
    """Return ``t`` when it is a recognized registry type, else generic."""
    if t in (
        RegistryTypeGeneric,
        RegistryTypeGHCR,
        RegistryTypeECR,
        RegistryTypeGCP,
        RegistryTypeACR,
    ):
        return t
    return RegistryTypeGeneric


class Registry(BaseModel):
    """A Docker image registry the control plane can authenticate against."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    url: str = Field(default="")
    type: str = Field(default="")  # generic|ghcr|ecr|gcp|acr
    username: str = Field(default="")
    # Never serialized.
    password: str = Field(default="", exclude=True)
    has_password: bool = Field(default=False, alias="hasPassword")
    insecure: bool = Field(default=False)
    verified: bool = Field(default=False)
    last_error: str = Field(default="", alias="lastError")  # omitempty
    # Never serialized in the clear.
    config: Dict[str, str] = Field(default_factory=dict, exclude=True)
    config_keys: List[str] = Field(default_factory=list, alias="configKeys")
    created_at: str = Field(default="", alias="createdAt")
