"""Plan and apply: the reconcile core of a sync.

``plan`` compares the desired resources (parsed from git) with Kowan's current
state and produces a JSON-serialisable plan::

    {"commit": "...", "generatedAt": "...",
     "errors":  [{"file", "doc", "kind", "name", "message"}],
     "changes": [{"kind", "name", "action", "adopt", "resourceId", "file",
                  "fields": [{"path", "before", "after"}],
                  "diff":   [{"op": " "|"+"|"-", "text"}],
                  "blocked": "reason"?}],
     "summary": {"create": n, "update": n, "delete": n, "noop": n, "orphan": n}}

Actions: ``create`` / ``update`` / ``noop`` / ``delete`` (only resources whose
``managedBy`` marker names this sync) / ``orphan`` (a managed resource removed
from git while prune is off: kept, and released on apply). A resource with the
desired name that exists but is unmanaged is *adopted* (``adopt: true``); one
owned by another sync is a conflict error. A plan with errors is never applied.

``apply`` executes creates/updates in kind order (dependencies first), then
deletes in reverse kind order, writes the markers and returns per-resource
results. One failing resource does not stop the others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .diff import field_changes, line_diff
from .handlers import spec_hash
from .kinds import Context, Current, KindHandler, SpecError, get_kind, kinds
from .resources import ParseError, Resource

log = logging.getLogger("kowan.gitops")


def h_of(kind: str) -> KindHandler:
    return get_kind(kind)  # type: ignore[return-value]


def _visible(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Drop handler-internal keys (``_protected``) before diffing/exporting."""
    return {k: v for k, v in spec.items() if not str(k).startswith("_")}


@dataclass
class Desired:
    res: Resource
    spec: Dict[str, Any]
    policies: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanResult:
    plan: Dict[str, Any]
    desired: Dict[Tuple[str, str], Desired]

    @property
    def ok(self) -> bool:
        return not self.plan.get("errors")

    @property
    def has_changes(self) -> bool:
        return any(c["action"] in ("create", "update", "delete", "orphan") or c.get("adopt")
                   for c in self.plan.get("changes", []))


async def plan(
    ctx: Context,
    sync_id: str,
    resources: List[Resource],
    parse_errors: Optional[List[ParseError]] = None,
    *,
    prune: bool = True,
    generated_at: str = "",
) -> PlanResult:
    store = ctx.store
    errors: List[Dict[str, Any]] = [e.to_dict() for e in (parse_errors or [])]
    managed_rows = store.list_gitops_managed()
    ctx.managed = {(m["kind"], m["resource_id"]): m["sync_id"] for m in managed_rows}
    mine = [m for m in managed_rows if m["sync_id"] == sync_id]

    desired: Dict[Tuple[str, str], Desired] = {}
    ctx.planned = {r.key for r in resources}
    for r in resources:
        h = get_kind(r.kind)
        if h is None:
            errors.append({"file": r.file, "doc": r.doc, "kind": r.kind, "name": r.name,
                           "message": f"unknown kind {r.kind!r}"})
            continue
        try:
            _, pol = h.split_policies(r.spec)
            spec = await h.normalize(r, ctx)
        except SpecError as err:
            errors.append({"file": r.file, "doc": r.doc, "kind": r.kind, "name": r.name, "message": str(err)})
            continue
        except Exception as err:  # noqa: BLE001 - a handler bug must not kill the plan
            log.exception("gitops: normalize %s/%s", r.kind, r.name)
            errors.append({"file": r.file, "doc": r.doc, "kind": r.kind, "name": r.name,
                           "message": f"internal error: {err}"})
            continue
        desired[r.key] = Desired(r, spec, pol)

    needed = {k for k, _ in desired} | {m["kind"] for m in mine}
    currents: Dict[str, Dict[str, Current]] = {}
    for h in kinds():
        if h.kind in needed:
            try:
                currents[h.kind] = h.current(ctx)
            except Exception as err:  # noqa: BLE001
                errors.append({"file": "", "doc": 0, "kind": h.kind, "name": "",
                               "message": f"cannot read current {h.kind} state: {err}"})
                currents[h.kind] = {}

    changes: List[Dict[str, Any]] = []
    matched_ids: set = set()
    order = {h.kind: h.order for h in kinds()}
    for (kind, name), d in sorted(desired.items(), key=lambda kv: (order.get(kv[0][0], 999), kv[0][1])):
        cur = currents.get(kind, {}).get(name)
        entry: Dict[str, Any] = {"kind": kind, "name": name, "file": d.res.file, "adopt": False,
                                 "resourceId": "", "fields": [], "diff": []}
        if cur is None:
            entry["action"] = "create"
            entry["fields"] = field_changes({}, d.spec)
            entry["diff"] = line_diff(kind, name, None, h_of(kind).export_spec(d.spec))
        else:
            owner = ctx.managed.get((kind, cur.id))
            matched_ids.add((kind, cur.id))
            entry["resourceId"] = cur.id
            if owner is not None and owner != sync_id:
                errors.append({"file": d.res.file, "doc": d.res.doc, "kind": kind, "name": name,
                               "message": f"{kind} {name!r} is managed by another sync ({owner})"})
                continue
            before = _visible(cur.spec)
            entry["adopt"] = owner is None
            entry["fields"] = field_changes(before, d.spec)
            entry["action"] = "update" if entry["fields"] else "noop"
            entry["diff"] = (line_diff(kind, name, h_of(kind).export_spec(before), h_of(kind).export_spec(d.spec))
                             if entry["fields"] else [])
        changes.append(entry)

    for m in sorted(mine, key=lambda m: (-order.get(m["kind"], 0), m["name"])):
        key = (m["kind"], m["resource_id"])
        if key in matched_ids:
            continue
        h = get_kind(m["kind"])
        cur_by_id = {c.id: c for c in currents.get(m["kind"], {}).values()}
        cur = cur_by_id.get(m["resource_id"])
        if h is None or cur is None:
            changes.append({"kind": m["kind"], "name": m["name"], "action": "forget", "adopt": False,
                            "resourceId": m["resource_id"], "file": "", "fields": [], "diff": []})
            continue
        entry = {"kind": m["kind"], "name": cur.name, "action": "delete" if prune else "orphan",
                 "adopt": False, "resourceId": cur.id, "file": "",
                 "fields": [], "diff": line_diff(m["kind"], cur.name, h.export_spec(_visible(cur.spec)), None) if prune else []}
        if prune:
            reason = h.delete_blocker(ctx, cur)
            if reason:
                entry["blocked"] = reason
        changes.append(entry)

    summary = {a: 0 for a in ("create", "update", "delete", "noop", "orphan")}
    for c in changes:
        if c["action"] in summary:
            summary[c["action"]] += 1
    summary["adopt"] = sum(1 for c in changes if c.get("adopt"))
    return PlanResult(
        plan={"commit": ctx.commit, "generatedAt": generated_at, "errors": errors,
              "changes": changes, "summary": summary},
        desired=desired,
    )


async def apply(ctx: Context, sync_id: str, pr: PlanResult, *, now: str = "") -> List[Dict[str, Any]]:
    """Execute ``pr`` (which must have no errors). Returns per-change results."""
    if not pr.ok:
        raise SpecError("the plan has errors; fix them before applying")
    store = ctx.store
    order = {h.kind: h.order for h in kinds()}
    results: List[Dict[str, Any]] = []
    changes = pr.plan["changes"]
    ups = sorted((c for c in changes if c["action"] in ("create", "update", "noop")),
                 key=lambda c: order.get(c["kind"], 999))
    downs = sorted((c for c in changes if c["action"] in ("delete", "orphan", "forget")),
                   key=lambda c: -order.get(c["kind"], 0))

    for c in ups:
        h: KindHandler = get_kind(c["kind"])  # type: ignore[assignment]
        d = pr.desired[(c["kind"], c["name"])]
        r = {"kind": c["kind"], "name": c["name"], "action": c["action"], "status": "ok", "error": ""}
        try:
            if c["action"] == "create":
                rid = await h.create(ctx, c["name"], d.spec, d.policies)
            else:
                rid = c["resourceId"]
                if c["action"] == "update":
                    cur = h.current(ctx).get(c["name"])
                    if cur is None:
                        raise SpecError("resource disappeared since the plan was computed")
                    await h.update(ctx, cur, d.spec, d.policies)
            store.set_gitops_managed(sync_id, c["kind"], rid, c["name"], spec_hash(d.spec), ctx.commit, now)
            r["resourceId"] = rid
        except Exception as err:  # noqa: BLE001
            log.warning("gitops: %s %s/%s failed: %s", c["action"], c["kind"], c["name"], err)
            r["status"] = "failed"
            r["error"] = str(err)
        if c["action"] == "noop" and r["status"] == "ok" and not c.get("adopt"):
            r["status"] = "unchanged"
        results.append(r)

    for c in downs:
        r = {"kind": c["kind"], "name": c["name"], "action": c["action"], "status": "ok", "error": "",
             "resourceId": c["resourceId"]}
        try:
            if c["action"] == "delete":
                if c.get("blocked"):
                    r["status"] = "skipped"
                    r["error"] = c["blocked"]
                    results.append(r)
                    continue
                h = get_kind(c["kind"])  # type: ignore[assignment]
                cur = next((x for x in h.current(ctx).values() if x.id == c["resourceId"]), None)
                if cur is not None:
                    await h.delete(ctx, cur)
            store.delete_gitops_managed(c["kind"], c["resourceId"])
        except Exception as err:  # noqa: BLE001
            r["status"] = "failed"
            r["error"] = str(err)
        if c["action"] != "forget":
            results.append(r)
    return results


def run_status(results: List[Dict[str, Any]]) -> str:
    failed = sum(1 for r in results if r["status"] == "failed")
    if failed == 0:
        return "applied"
    return "failed" if failed == len(results) else "partial"
