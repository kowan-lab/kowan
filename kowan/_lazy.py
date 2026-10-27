"""Lazy package exports (PEP 562).

A package ``__init__`` that re-exports names from its submodules would import
every submodule as soon as any one of them is needed. The agent, for example,
imports ``kowan.models`` for a handful of container models and paid for all
the workflow, alert and deployment models too. With ``lazy_exports`` the
package keeps the same public names, but a submodule is imported the first
time one of its names is looked up.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any, Callable, Dict, List, Tuple


def lazy_exports(package: str, exports: Dict[str, Tuple[str, ...]]) -> Tuple[Callable[[str], Any], Callable[[], List[str]]]:
    """Return the ``__getattr__`` and ``__dir__`` for ``package``.

    ``exports`` maps a relative submodule (``".alert"``) to the names it
    provides. A resolved name is stored in the package namespace, so later
    lookups are plain attribute reads.
    """
    where = {name: mod for mod, names in exports.items() for name in names}

    def __getattr__(name: str) -> Any:
        mod = where.get(name)
        if mod is None:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(mod, package), name)
        setattr(sys.modules[package], name, value)
        return value

    def __dir__() -> List[str]:
        return sorted(set(vars(sys.modules[package])) | set(where))

    return __getattr__, __dir__
