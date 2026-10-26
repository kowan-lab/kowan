"""Secret-reference constants + the reference-preview model.

The secret record itself is stored as a :class:`~kowan.models.credential.Credential`
of kind ``secret`` (values encrypted at rest, only field NAMES exposed). This module
keeps the secret ``type``/``vault`` constants and the ``[[...]]`` interpolation
preview model that the credential store and the credentials API share.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Secret type constants.
SecretTypeGeneric = "generic"
SecretTypeRegistry = "registry"
SecretTypeGit = "git"
SecretTypeEnv = "env"
SecretTypeToken = "token"
SecretTypeVault = "vault"

# Vault reference field names.
VaultFieldAddress = "address"
VaultFieldToken = "token"
VaultFieldMount = "mount"
VaultFieldPath = "path"
VaultFieldKey = "key"

# VaultDefaultMount is the KV v2 mount used when a vault secret omits "mount".
VaultDefaultMount = "secret"

# Reference source prefixes for [[...]] interpolation tokens.
RefSourceSecret = "secret"
RefSourcePlugin = "plugin"

# Reference preview statuses.
RefStatusResolved = "resolved"
RefStatusUnresolved = "unresolved"
RefStatusError = "error"


class ReferencePreview(BaseModel):
    """Outcome of resolving one [[...]] interpolation token (admin tool).

    The resolved value is masked; several fields are omitted when empty.
    """

    model_config = ConfigDict(populate_by_name=True)

    token: str = Field(default="")  # the literal [[...]] as written
    source: str = Field(default="")  # "", "secret" or "plugin"
    name: str = Field(default="")  # referenced secret name
    field: str = Field(default="")  # optional .FIELD part (omitempty)
    status: str = Field(default="")  # resolved | unresolved | error
    secret_type: str = Field(default="", alias="secretType")  # omitempty
    external: bool = Field(default=False)  # resolved via an external plugin
    masked: str = Field(default="")  # masked preview (omitempty)
    length: int = Field(default=0)  # resolved value length (omitempty)
    message: str = Field(default="")  # why unresolved/error (omitempty)
