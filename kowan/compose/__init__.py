"""Compose validation: structural checks + Kowan's lint rules.

Public surface::

    from kowan.compose import validate_text, validate_for_target, apply_patch

See :mod:`kowan.compose.validate` (orchestration), :mod:`kowan.compose.lint`
(rules), :mod:`kowan.compose.structural` (``docker compose config`` + the
schema-lite fallback) and docs/compose-lint.md.
"""

from .diagnostics import Diagnostic, Fix, format_path, has_errors
from .lint import SecretInfo
from .patch import apply_patch
from .validate import ValidationResult, validate_for_target, validate_text

__all__ = [
    "Diagnostic",
    "Fix",
    "SecretInfo",
    "ValidationResult",
    "apply_patch",
    "format_path",
    "has_errors",
    "validate_for_target",
    "validate_text",
]
