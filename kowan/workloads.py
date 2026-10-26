"""Workload ids shared by the server-side features that group containers the
way the UI does: ``dep~<deployment id>`` for a deployment's containers,
``stack~<host>~<project>`` for a compose project or swarm stack nobody
deployed through Kowan, ``ctr~<host>~<name>`` for a lone container."""

from __future__ import annotations

from typing import Any, Dict, Tuple

LABEL_DEPLOYMENT = "kowan.deployment.id"
LABEL_PROJECT = "com.docker.compose.project"
LABEL_SWARM_STACK = "com.docker.stack.namespace"
LABEL_SWARM_SERVICE = "com.docker.swarm.service.name"


def stack_of(labels: Dict[str, str]) -> str:
    """The compose project or swarm stack a container belongs to, or ""
    (the same rule authorization uses). A swarm service outside any stack is
    its own stack, so its tasks group into one workload."""
    labels = labels or {}
    return labels.get(LABEL_PROJECT) or labels.get(LABEL_SWARM_STACK) or labels.get(LABEL_SWARM_SERVICE) or ""


def compose_deployments(store: Any) -> Dict[Tuple[str, str], str]:
    """``(host, compose project) -> deployment id`` for compose deployments,
    whose containers carry the project label but not the deployment one."""
    return {
        (d.targets[0], d.resource_name()): d.id
        for d in store.list_deployments() if d.type == "compose" and d.targets
    }


def workload_of(labels: Dict[str, str], host_id: str, name: str, compose_deps: Dict[Tuple[str, str], str]) -> Tuple[str, str, str]:
    """``(workload, stack, deployment_id)`` of a container."""
    labels = labels or {}
    dep = labels.get(LABEL_DEPLOYMENT, "")
    project = stack_of(labels)
    if not dep and project:
        dep = compose_deps.get((host_id, project), "")
    if dep:
        return f"dep~{dep}", project, dep
    if project:
        return f"stack~{host_id}~{project}", project, ""
    return f"ctr~{host_id}~{name}", "", ""


def parse_workload(workload: str) -> Tuple[str, str, str]:
    """``(kind, host, rest)`` of a workload id; raises ValueError if malformed.
    ``rest`` is the deployment id, the project or the container name."""
    kind, _, tail = workload.partition("~")
    parts = tail.split("~")
    if kind == "dep" and len(parts) == 1 and parts[0]:
        return kind, "", parts[0]
    if kind in ("stack", "ctr") and len(parts) == 2 and all(parts):
        return kind, parts[0], parts[1]
    raise ValueError("workload must be dep~<id>, stack~<host>~<project> or ctr~<host>~<name>")
