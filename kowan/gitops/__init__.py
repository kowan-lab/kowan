"""GitOps: configuration as code for Kowan, synced from a git repository.

A *Sync* points at a git repository/branch/path holding ``apiVersion: kowan/v1``
resource files. The engine fetches it, validates every resource, computes a
diff against Kowan's current state (create / update / delete, deletes only for
resources this sync manages), and applies it on approval or automatically.
Export renders current state back to the same format.

Modules: :mod:`.resources` (file format), :mod:`.kinds` (handler registry),
:mod:`.handlers` (built-in kinds), :mod:`.diff`, :mod:`.engine` (plan/apply),
:mod:`.export`, :mod:`.service` (fetch, locks, interval loop, webhook).
See docs/gitops.md.
"""

from . import handlers as _handlers  # noqa: F401 - registers the built-in kinds
from . import handlers_ext as _handlers_ext  # noqa: F401 - Schedule, AutoUpdatePolicy
from .kinds import Context, Current, KindHandler, SpecError, get_kind, kinds, register_kind
from .resources import API_VERSION, Resource, parse_documents, scan_tree

__all__ = [
    "API_VERSION",
    "Context",
    "Current",
    "KindHandler",
    "Resource",
    "SpecError",
    "get_kind",
    "kinds",
    "parse_documents",
    "register_kind",
    "scan_tree",
]
