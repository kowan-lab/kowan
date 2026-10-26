"""Promotion: run in one deployment (production) what another (staging) runs.

What moves is the artifact, never the environment: a container deployment
gets the source's image, an inline compose deployment the image of every
service it shares with the source, a git deployment the exact commit the
source last deployed (pinned, with auto-deploy off so the next push to its
branch does not undo it). The target keeps its own name, host, environment,
ports, volumes and everything else.

:func:`plan` works on copies and says what would change; the API shows that
as a diff before applying it as a new revision of the target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

from kowan.errors import Validation
from kowan.models import Deployment

_IMAGE_LINE = re.compile(r"^(\s*image:\s*)(['\"]?)([^'\"#\s]*)(['\"]?)(\s*(?:#.*)?)$")


@dataclass
class Change:
    what: str
    before: str
    after: str

    def as_dict(self) -> Dict[str, str]:
        return {"what": self.what, "from": self.before, "to": self.after}


@dataclass
class Plan:
    target: Deployment  # the target as it would be after promoting
    changes: List[Change] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    pinned: bool = False


def _same_repo(a: str, b: str) -> bool:
    norm = lambda u: re.sub(r"(\.git)?/?$", "", u.strip().lower())  # noqa: E731
    return norm(a) == norm(b)


def compose_images(text: str) -> Dict[str, str]:
    """``service -> image`` of a compose file (services without one left out)."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as err:
        raise Validation(f"compose file is not valid YAML: {err}") from None
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict):
        return {}
    return {name: str(s["image"]) for name, s in services.items() if isinstance(s, dict) and s.get("image")}


def replace_images(text: str, images: Dict[str, str]) -> str:
    """Rewrite the ``image:`` of the given services in place, keeping every
    other line (comments, order, quoting) as it is. Only a service's own
    ``image:`` key counts, not a nested one."""
    lines = text.splitlines(keepends=True)
    in_services = False
    service_indent = -1  # indent of the service names under services:
    child_indent = -1  # indent of a service's own keys
    current: Optional[str] = None
    todo = dict(images)
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_services = stripped == "services:"
            service_indent, child_indent, current = -1, -1, None
            continue
        if not in_services:
            continue
        if service_indent < 0 or indent <= service_indent:
            service_indent = indent
            current = stripped[:-1].strip("'\"") if stripped.endswith(":") else None
            child_indent = -1
            continue
        if child_indent < 0:
            child_indent = indent
        if indent != child_indent or current not in todo:
            continue
        m = _IMAGE_LINE.match(line)
        if m:
            lines[i] = f"{m.group(1)}{m.group(2)}{todo.pop(current)}{m.group(4)}{m.group(5)}{raw[len(line):]}"
    return "".join(lines)


def plan(source: Deployment, target: Deployment) -> Plan:
    """What promoting ``source`` into ``target`` changes. Raises Validation
    when the two cannot be promoted into one another."""
    if source.id == target.id:
        raise Validation("a deployment cannot be promoted into itself")
    if source.preview is not None or target.preview is not None:
        raise Validation("previews are not promoted; promote the deployment they preview")
    if source.type != target.type:
        raise Validation(f"{source.name} is a {source.type} deployment and {target.name} a {target.type} one")
    after = target.model_copy(deep=True)
    out = Plan(target=after)

    if source.type == "container":
        if source.spec is None or after.spec is None:
            raise Validation("both deployments need a container spec")
        if source.spec.image != after.spec.image:
            out.changes.append(Change("image", after.spec.image, source.spec.image))
            after.spec.image = source.spec.image
        return out

    src_git = source.git is not None and bool(source.git.repo_url)
    dst_git = after.git is not None and bool(after.git.repo_url)
    if src_git != dst_git:
        raise Validation("one deployment comes from git and the other from an inline file; promote between two of the same kind")
    if src_git:
        if not _same_repo(source.git.repo_url, after.git.repo_url):
            raise Validation("the deployments use different repositories")
        if source.git.path != after.git.path:
            out.notes.append(f"{target.name} reads {after.git.path}, {source.name} reads {source.git.path}")
        commit = source.deployed_commit
        if not commit:
            raise Validation(f"{source.name} has not been deployed from git yet: nothing to promote")
        if after.git.ref != commit or after.deployed_commit != commit:
            out.changes.append(Change("commit", after.deployed_commit or after.git.ref, commit))
            after.git.ref = commit
            after.git.auto_deploy = False
            out.pinned = True
        return out

    src = compose_images(source.compose)
    dst = compose_images(after.compose)
    moves = {name: img for name, img in src.items() if name in dst and dst[name] != img}
    for name in sorted(moves):
        out.changes.append(Change(f"{name} image", dst[name], moves[name]))
    missing = sorted(set(src) - set(dst))
    if missing:
        out.notes.append(f"not in {target.name}, left out: {', '.join(missing)}")
    if moves:
        after.compose = replace_images(after.compose, moves)
        got = compose_images(after.compose)
        if any(got.get(k) != v for k, v in moves.items()):
            # A layout the line editor does not follow (flow style): rewrite
            # the file through YAML, losing its comments.
            doc = yaml.safe_load(after.compose)
            for name, img in moves.items():
                doc["services"][name]["image"] = img
            after.compose = yaml.safe_dump(doc, sort_keys=False)
            out.notes.append("the compose file was rewritten (its comments are gone)")
    return out
