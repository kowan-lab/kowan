"""The kind registry: how each ``kind:`` maps onto Kowan's own resources.

A :class:`KindHandler` turns between three shapes of the same resource:

* the **desired** spec from git (:meth:`normalize` validates it and fills
  defaults, resolving names such as a host or channel),
* the **current** state in Kowan (:meth:`current`, same canonical shape), and
* Kowan's store (:meth:`create` / :meth:`update` / :meth:`delete`).

Because desired and current share one canonical shape, the diff is a plain
structural comparison and ``export`` is just ``current`` with defaults pruned
(:meth:`export_spec`), which makes export -> parse -> plan a no-op round trip.

New kinds (schedules, backup jobs, ...) register a handler with
:func:`register_kind`; nothing else in the GitOps engine changes. ``order``
sets the apply order (dependencies first: channels before alert rules that
reference them, deployments before workflows that run them); deletes run in
reverse order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .resources import Resource


class SpecError(ValueError):
    """The desired spec is invalid (reported as a plan error)."""


@dataclass
class Current:
    id: str
    name: str
    spec: Dict[str, Any]


@dataclass
class Context:
    """What handlers may use. ``svc`` is the control plane's Services
    container (``store``, ``docker``, ``deployments``, ...)."""

    svc: Any
    checkout: str = ""
    commit: str = ""
    #: filled by the engine: (kind, resource id) -> sync id of the owner.
    managed: Dict[Tuple[str, str], str] = field(default_factory=dict)
    #: filled by the engine: (kind, name) of every resource in the plan, so a
    #: reference to something created by the same plan validates.
    planned: set = field(default_factory=set)

    def planned_name(self, kind: str, ref: str) -> bool:
        return (kind, ref) in self.planned

    @property
    def store(self) -> Any:
        return self.svc.store

    # -- name <-> id helpers shared by handlers ---------------------------
    def host_name(self, host_id: str) -> str:
        if not host_id:
            return ""
        try:
            return self.store.get_host(host_id).name or host_id
        except Exception:  # noqa: BLE001
            return host_id

    def resolve_host(self, ref: str) -> Optional[Any]:
        if not ref:
            return None
        try:
            return self.store.get_host(ref)
        except Exception:  # noqa: BLE001
            pass
        try:
            for h in self.store.list_hosts():
                if h.name == ref:
                    return h
        except Exception:  # noqa: BLE001
            pass
        return None

    def _creds(self, kind: str) -> List[Any]:
        try:
            return self.store.list_credentials(kind)
        except Exception:  # noqa: BLE001 - no cipher
            return []

    def credential_name(self, kind: str, cid: str) -> str:
        if not cid:
            return ""
        for c in self._creds(kind):
            if c.id == cid:
                return c.name
        return cid

    def resolve_credential(self, kind: str, ref: str) -> Optional[Any]:
        if not ref:
            return None
        creds = self._creds(kind)
        for c in creds:
            if c.id == ref:
                return c
        for c in creds:
            if c.name == ref:
                return c
        return None

    def deployment_name(self, ref: str) -> str:
        if not ref:
            return ""
        try:
            return self.store.get_deployment(ref).name
        except Exception:  # noqa: BLE001
            return ref


class KindHandler:
    kind: str = ""
    #: directory used by export (``deployments/web.yaml``).
    plural: str = ""
    order: int = 100
    description: str = ""
    #: top-level spec keys and their defaults (pruned on export).
    defaults: Dict[str, Any] = {}
    #: spec keys that are behaviour knobs for the sync, never diffed/stored.
    policy_keys: Tuple[str, ...] = ()

    async def normalize(self, res: Resource, ctx: Context) -> Dict[str, Any]:
        raise NotImplementedError

    def current(self, ctx: Context) -> Dict[str, Current]:
        raise NotImplementedError

    async def create(self, ctx: Context, name: str, spec: Dict[str, Any], policies: Dict[str, Any]) -> str:
        raise NotImplementedError

    async def update(self, ctx: Context, cur: Current, spec: Dict[str, Any], policies: Dict[str, Any]) -> None:
        raise NotImplementedError

    async def delete(self, ctx: Context, cur: Current) -> None:
        raise NotImplementedError

    def delete_blocker(self, ctx: Context, cur: Current) -> str:
        """Non-empty reason when ``cur`` must not be deleted (e.g. protected)."""
        return ""

    def export_spec(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in spec.items() if k not in self.defaults or self.defaults[k] != v}

    def split_policies(self, spec: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        spec = dict(spec)
        pol = {k: spec.pop(k) for k in list(spec) if k in self.policy_keys}
        return spec, pol

    def check_keys(self, spec: Dict[str, Any], allowed: set) -> None:
        extra = set(spec) - allowed - set(self.policy_keys)
        if extra:
            raise SpecError(f"unknown spec field(s): {', '.join(sorted(map(str, extra)))}")

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "plural": self.plural, "order": self.order,
            "description": self.description, "defaults": self.defaults,
            "policies": list(self.policy_keys),
        }


_REGISTRY: Dict[str, KindHandler] = {}


def register_kind(handler: KindHandler) -> KindHandler:
    if not handler.kind:
        raise ValueError("handler.kind is required")
    _REGISTRY[handler.kind] = handler
    return handler


def get_kind(kind: str) -> Optional[KindHandler]:
    return _REGISTRY.get(kind)


def kinds() -> List[KindHandler]:
    return sorted(_REGISTRY.values(), key=lambda h: (h.order, h.kind))
