"""Newer version tags for a deployment's images.

The update check (``kowan/api/updates.py``) answers "has the image behind
this tag changed?" (``latest`` pushed again). This answers the other
question: an image pinned to a version (``api:2.0``, ``nginx:1.27-alpine``)
has newer versions in its registry (``2.1``, ``1.28-alpine``). Moving to one
edits the deployment (a new revision), so it is planned first: the changes
and the line diff are shown before anything is applied.

A tag counts as a version when it is numbers separated by dots, with an
optional ``v`` before and a suffix after (``v1.2.3``, ``1.27-alpine``). Only
tags with the same ``v``, the same number of parts and the same suffix are
compared, so ``1.27`` is offered ``1.28`` (not ``1.28.1``) and ``-alpine``
stays ``-alpine``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

from kowan.errors import Validation
from kowan.models import Deployment

from . import promotion
from . import revisions as revs

_VERSION = re.compile(r"^(?P<v>v?)(?P<nums>\d+(?:\.\d+){0,3})(?P<suffix>[-_+][A-Za-z0-9._+-]*)?$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
MAX_NEWER = 10
TAGS_TIMEOUT = 20.0

# The service key of a container deployment, which has a single image.
CONTAINER_SERVICE = ""


def version_of(tag: str) -> Optional[Tuple[str, Tuple[int, ...], str]]:
    """``(v, numbers, suffix)`` of a version tag, or None."""
    m = _VERSION.match(tag or "")
    if not m:
        return None
    return m.group("v"), tuple(int(n) for n in m.group("nums").split(".")), m.group("suffix") or ""


def newer_tags(current: str, tags: List[str]) -> List[str]:
    """Tags of the same shape as ``current`` and greater, newest first."""
    cur = version_of(current)
    if cur is None:
        return []
    v, nums, suffix = cur
    out = []
    for t in set(tags):
        other = version_of(t)
        if other is None or other[0] != v or other[2] != suffix or len(other[1]) != len(nums):
            continue
        if other[1] > nums:
            out.append((other[1], t))
    return [t for _, t in sorted(out, reverse=True)]


def images_of(dep: Deployment) -> Dict[str, str]:
    """``service -> image`` the deployment runs (one entry for a container)."""
    if dep.type == "compose":
        return promotion.compose_images(dep.compose or "")
    if dep.spec is not None and dep.spec.image:
        return {CONTAINER_SERVICE: dep.spec.image}
    return {}


def with_tag(image: str, tag: str) -> str:
    """``image`` with its tag replaced (the tag follows the last ``:`` after the last ``/``)."""
    base = image.rsplit("@", 1)[0]
    slash = base.rfind("/")
    colon = base.rfind(":")
    if colon > slash:
        base = base[:colon]
    return f"{base}:{tag}"


async def _list_tags(store: Any, ref: Any) -> List[str]:
    from kowan.agentupdate import _registry_creds
    from kowan.registry.browse import v2_tags

    username, password = await _registry_creds(store, ref.registry)
    return await v2_tags(username, password, f"https://{ref.api_host}", ref.repository, False)


async def check(store: Any, dep: Deployment, list_tags=None) -> List[Dict[str, Any]]:
    """Per service: its image, the current tag and the newer ones (or why
    there are none to look for)."""
    from kowan.api.updates import parse_image_ref

    rows: List[Dict[str, Any]] = []
    refs: Dict[str, Any] = {}
    for service, image in images_of(dep).items():
        row: Dict[str, Any] = {"service": service, "image": image, "current": "", "newer": []}
        rows.append(row)
        if "$" in image:
            row["note"] = "the image comes from a variable"
            continue
        try:
            ref = parse_image_ref(image)
        except ValueError as err:
            row["note"] = str(err)
            continue
        row["current"] = ref.tag
        if ref.digest:
            row["note"] = "pinned by digest"
        elif version_of(ref.tag) is None:
            row["note"] = f"{ref.tag} is not a version tag; the update check covers it"
        else:
            refs[service] = ref

    lister = list_tags or _list_tags

    async def fetch(ref: Any) -> Any:
        try:
            return await asyncio.wait_for(lister(store, ref), TAGS_TIMEOUT)
        except Exception as err:  # noqa: BLE001 - reported per service
            return err

    keys = {r.name: r for r in refs.values()}
    listed = dict(zip(keys, await asyncio.gather(*(fetch(r) for r in keys.values()))))
    for row in rows:
        ref = refs.get(row["service"])
        if ref is None:
            continue
        tags = listed[ref.name]
        if isinstance(tags, Exception):
            row["error"] = f"could not list the tags of {ref.name}: {tags}"
            continue
        newer = newer_tags(ref.tag, tags)
        row["newer"] = newer[:MAX_NEWER]
        if newer:
            row["latest"] = newer[0]
    return rows


def plan(dep: Deployment, tags: Dict[str, str]) -> Tuple[Deployment, List[Dict[str, str]], List[Dict[str, str]]]:
    """The deployment with ``service -> new tag`` applied, the changes and the
    line diff of its configuration."""
    if not isinstance(tags, dict) or not tags:
        raise Validation("pick at least one service and its new tag")
    images = images_of(dep)
    changes: List[Dict[str, str]] = []
    new_images: Dict[str, str] = {}
    for service, tag in tags.items():
        tag = str(tag or "").strip()
        if service not in images:
            raise Validation(f"{service or 'the container'} is not a service of {dep.name}")
        if not _TAG.match(tag):
            raise Validation(f"{tag!r} is not a valid tag")
        old = images[service]
        new = with_tag(old, tag)
        if new != old:
            new_images[service] = new
            changes.append({"service": service, "from": old, "to": new})
    if not changes:
        raise Validation("those are the tags it already runs")
    after = dep.model_copy(deep=True)
    if dep.type == "compose":
        after.compose = promotion.replace_images(dep.compose or "", new_images)
    else:
        assert after.spec is not None
        after.spec.image = new_images[CONTAINER_SERVICE]
    return after, changes, revs.line_diff(revs.snapshot(dep), revs.snapshot(after))


def note_of(changes: List[Dict[str, str]]) -> str:
    def short(c: Dict[str, str]) -> str:
        name = c["service"] or "image"
        return f"{name} {c['from'].rsplit(':', 1)[-1]} → {c['to'].rsplit(':', 1)[-1]}"

    return "new versions: " + ", ".join(short(c) for c in changes)
