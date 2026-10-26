"""Build models.

``registry_host``/``registry_user``/``registry_pass`` are filled by the API
layer and excluded from serialization.
"""

from __future__ import annotations

from typing import Dict

from pydantic import BaseModel, ConfigDict, Field


class BuildOptions(BaseModel):
    """A Docker image build request."""

    model_config = ConfigDict(populate_by_name=True)

    tag: str = Field(default="")
    git_url: str = Field(default="", alias="gitUrl")  # omitempty
    git_ref: str = Field(default="", alias="gitRef")  # omitempty
    dockerfile: str = Field(default="")  # omitempty
    context_subdir: str = Field(default="", alias="contextSubdir")  # omitempty
    build_args: Dict[str, str] = Field(default_factory=dict, alias="buildArgs")  # omitempty
    no_cache: bool = Field(default=False, alias="noCache")  # omitempty
    push: bool = Field(default=False)  # omitempty
    registry_id: str = Field(default="", alias="registryId")  # omitempty

    # Filled by the API layer from the registry; never serialized.
    registry_host: str = Field(default="", exclude=True)
    registry_user: str = Field(default="", exclude=True)
    registry_pass: str = Field(default="", exclude=True)


class BuildResult(BaseModel):
    """The outcome of a BuildImage call."""

    model_config = ConfigDict(populate_by_name=True)

    success: bool = Field(default=False)
    image_id: str = Field(default="", alias="imageId")
    output: str = Field(default="")
