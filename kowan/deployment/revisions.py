"""Deployment revisions: what a deployment was configured to run, over time.

Every save that changes what runs (image or compose, spec, targets, git source
or the commit a git deployment follows) becomes a numbered revision. The
snapshot leaves out switches that don't change the workload (enabled,
protected) and the secrets of the git source. Revisions are compared as YAML
documents, so a diff reads like the configuration the user edited.

Pure functions only: the store records snapshots, the API diffs and rolls back.
"""

from __future__ import annotations

import difflib
import json
from typing import Any, Dict, List, Optional

import yaml

from kowan.models import ContainerSpec, Deployment
from kowan.models.deployment import KeyValue
from kowan.redact import MASK, redact_compose, strip_userinfo

# Keep this many revisions per deployment; older ones are dropped.
KEEP = 100

_SPEC_DEFAULTS = ContainerSpec().model_dump(by_alias=True)


def _container(spec: Optional[ContainerSpec]) -> Optional[Dict[str, Any]]:
    """The spec without the fields left at their defaults (short to read)."""
    if spec is None:
        return None
    full = spec.model_dump(by_alias=True)
    return {k: v for k, v in full.items() if v != _SPEC_DEFAULTS.get(k)} or {"image": ""}


def snapshot(dep: Deployment) -> Dict[str, Any]:
    """The runtime-relevant configuration of ``dep`` as a plain dict."""
    git = None
    if dep.git is not None and dep.git.repo_url:
        git = {
            "repoUrl": dep.git.repo_url,
            "ref": dep.git.ref or "main",
            "path": dep.git.path or "docker-compose.yml",
            "credentialId": dep.git.credential_id,
            "autoDeploy": bool(dep.git.auto_deploy),
        }
    out: Dict[str, Any] = {
        "name": dep.name,
        "type": dep.type,
        "strategy": dep.strategy,
        "targets": list(dep.targets or []),
        "replicas": int(dep.replicas or 0),
    }
    if dep.type == "container":
        out["container"] = _container(dep.spec)
    if git is not None:
        out["git"] = git
        # The commit a git deployment runs is part of what it runs: a new push
        # that got deployed is a new revision.
        out["commit"] = dep.deployed_commit or ""
    elif dep.type == "compose":
        out["compose"] = dep.compose or ""
    if dep.type == "compose" and dep.variables:
        out["variables"] = [{"key": v.key, "value": v.value} for v in dep.variables]
    return out


def encode(config: Dict[str, Any]) -> str:
    """Canonical JSON (sorted keys) so equal configs compare equal as text."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def redact(config: Dict[str, Any]) -> Dict[str, Any]:
    """A copy safe for readers without operate rights: no env values, extra
    args, inline compose secrets or credentials in git URLs."""
    out = json.loads(json.dumps(config))
    c = out.get("container")
    if isinstance(c, dict):
        for kv in c.get("env") or []:
            if isinstance(kv, dict):
                kv["value"] = MASK
        if c.get("extraArgs"):
            c["extraArgs"] = [MASK for _ in c["extraArgs"]]
    if out.get("compose"):
        out["compose"] = redact_compose(out["compose"])
    for kv in out.get("variables") or []:
        if isinstance(kv, dict):
            kv["value"] = MASK
    g = out.get("git")
    if isinstance(g, dict) and g.get("repoUrl"):
        g["repoUrl"] = strip_userinfo(g["repoUrl"])
    return out


class _Literal(yaml.SafeDumper):
    """Multi-line strings (compose files) as block literals."""


def _str(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Literal.add_representer(str, _str)


def to_yaml(config: Optional[Dict[str, Any]]) -> str:
    if not config:
        return ""
    return yaml.dump(config, Dumper=_Literal, sort_keys=False, allow_unicode=True, width=120)


def line_diff(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Full-context line diff of two snapshots rendered as YAML."""
    a = to_yaml(before).splitlines()
    b = to_yaml(after).splitlines()
    out: List[Dict[str, str]] = []
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend({"op": " ", "text": t} for t in a[i1:i2])
            continue
        if tag in ("replace", "delete"):
            out.extend({"op": "-", "text": t} for t in a[i1:i2])
        if tag in ("replace", "insert"):
            out.extend({"op": "+", "text": t} for t in b[j1:j2])
    return out


def changed_keys(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> List[str]:
    """Top-level fields that differ (``[]`` for the first revision)."""
    if before is None:
        return []
    keys = list(after.keys()) + [k for k in before.keys() if k not in after]
    return [k for k in keys if before.get(k) != after.get(k)]


def apply(dep: Deployment, config: Dict[str, Any]) -> bool:
    """Write a snapshot back onto ``dep`` (rollback). Returns True when a git
    deployment was pinned to the revision's commit.

    The git push secret and redeploy token stay as they are: a rollback never
    revives an old webhook secret.
    """
    from kowan.models import GitSource

    dep.name = config.get("name") or dep.name
    dep.type = config.get("type") or dep.type
    dep.strategy = config.get("strategy") or dep.strategy
    dep.targets = list(config.get("targets") or [])
    dep.replicas = int(config.get("replicas") or 0)
    c = config.get("container")
    dep.spec = ContainerSpec.model_validate(c) if dep.type == "container" and c is not None else None
    dep.variables = [KeyValue.model_validate(v) for v in config.get("variables") or []] if dep.type == "compose" else []
    pinned = False
    g = config.get("git")
    if g:
        commit = config.get("commit") or ""
        ref = g.get("ref") or "main"
        # A git revision ran a specific commit: follow it instead of the branch
        # head, or "rollback" would just redeploy the latest push.
        if commit and commit != dep.deployed_commit:
            ref = commit
            pinned = True
        dep.git = GitSource(
            repoUrl=g.get("repoUrl", ""), ref=ref, path=g.get("path") or "docker-compose.yml",
            credentialId=g.get("credentialId", ""), autoDeploy=bool(g.get("autoDeploy")) and not pinned,
            webhookSecret=dep.git.webhook_secret if dep.git is not None else "",
        )
    else:
        dep.git = None
        dep.compose = config.get("compose", "") if dep.type == "compose" else ""
        dep.deployed_commit = ""
        dep.last_sync_at = ""
    return pinned
