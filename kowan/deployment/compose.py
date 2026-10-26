"""Synthesize a one-service compose file from a container spec.

Used by the swarm
container path: a single-container ``Deployment`` is turned into a minimal
one-service compose v3 document suitable for ``docker stack deploy``.

The service name is the deployment's Docker-safe ``resource_name()`` (the
display Name may contain spaces/capitals a stack would reject); replicas come
from the deployment; the management labels are stamped onto the service so it
is discoverable later.
"""

from __future__ import annotations

import yaml

from kowan.errors import Validation
from kowan.models import Deployment

from .service import managed_labels


def synthesize_swarm_compose(dep: Deployment) -> str:
    """Build a minimal one-service compose YAML from a deployment's spec.

    Keeps a fixed key set and omits the optional sections when they are empty.
    """
    if dep.spec is None:
        raise Validation(
            f"synthesize compose {dep.name!r}: container spec is required"
        )
    spec = dep.spec

    # Ordered dict so the encoded YAML is stable/canonical, in a fixed field
    # order (image, environment, ports, volumes, networks, command, labels,
    # deploy).
    svc: dict = {"image": spec.image}

    environment: list[str] = []
    for e in spec.env:
        environment.append(f"{e.key}={e.value}")
    if environment:
        svc["environment"] = environment

    ports: list[str] = []
    for p in spec.ports:
        proto = p.protocol
        mapping = f"{p.host_port}:{p.container_port}"
        if proto and proto != "tcp":
            mapping += "/" + proto
        ports.append(mapping)
    if ports:
        svc["ports"] = ports

    volumes: list[str] = []
    for v in spec.volumes:
        mapping = f"{v.source}:{v.target}"
        if v.read_only:
            mapping += ":ro"
        volumes.append(mapping)
    if volumes:
        svc["volumes"] = volumes

    if spec.networks:
        svc["networks"] = list(spec.networks)

    if spec.command:
        svc["command"] = list(spec.command)

    svc["labels"] = managed_labels(dep)

    if dep.replicas > 0:
        svc["deploy"] = {"replicas": dep.replicas}

    doc = {"services": {dep.resource_name(): svc}}

    # sort_keys=False keeps the field order we built above;
    # default_flow_style=False gives the canonical block style.
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, indent=2)
