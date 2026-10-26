"""Shared provider-config field descriptor for the provider subsystems.

Both the registry and the external-secrets subsystems describe a provider's
configuration as an ordered list of typed fields, and both serialise those
fields to the same five-key lowercase JSON shape (key/label/sensitive/required/
placeholder). This module holds the single :class:`FieldSpec` both import, so the definition
lives in exactly one place. It deliberately imports nothing from either package
so it stays a dependency-free leaf that both can pull in without a cycle.


"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FieldSpec:
    """Describes a single provider-specific configuration field.

    Sensitive fields are stored encrypted and masked on read; Required fields
    must be present for the provider to build.
    """

    key: str
    label: str = ""
    sensitive: bool = False
    required: bool = False
    placeholder: str = ""

    def to_dict(self) -> dict:
        """Serialise to the five-key lowercase JSON shape."""
        return {
            "key": self.key,
            "label": self.label,
            "sensitive": self.sensitive,
            "required": self.required,
            "placeholder": self.placeholder,
        }
