"""Clone an existing container's configuration onto a new image (pure).

The helper recreates the target from a raw ``docker inspect`` rather than from a
Kowan ``ContainerSpec`` so that every operator-chosen flag survives (env, binds,
published ports, restart policy, devices, caps, compose labels, ...). Two
subtleties the naive "feed inspect back into create" gets wrong, fixed here:

* **Image defaults leak.** ``Config`` in an inspect is the MERGE of the image's
  defaults and the user's overrides. Re-using it verbatim on a new image would
  pin the OLD image's ``Cmd``/``Entrypoint``/``Healthcheck``/``Env``/labels
  (including the version label) onto the new one. Values identical to the old
  image's defaults are therefore dropped so the new image's defaults apply.
* **Anonymous volumes are lost.** A ``VOLUME /data`` without ``-v`` is not in
  ``HostConfig.Binds``; a fresh container would get a NEW empty anonymous
  volume and the data would silently disappear. Every volume mount from
  ``Mounts`` that is not already declared is re-added as an explicit bind of the
  SAME volume name.

Network aliases (e.g. the compose service name other containers use to reach
the server) are carried over through ``NetworkingConfig`` / connect bodies.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

_BUILTIN_NETWORKS = ("bridge", "host", "none", "default", "")

# Config keys that come from the image and should follow the NEW image unless
# the user overrode them on the container.
_IMAGE_SCALARS = ("Cmd", "Entrypoint", "Healthcheck", "WorkingDir", "User",
                  "StopSignal", "Shell", "OnBuild", "ArgsEscaped")


def _bind_target(bind: str) -> str:
    parts = bind.split(":")
    # "src:dst[:opts]" ; Windows paths are out of scope.
    return parts[1] if len(parts) >= 2 else parts[0]


def _endpoint(ep: Dict[str, Any], drop_aliases: Tuple[str, ...]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    aliases = [a for a in (ep.get("Aliases") or []) if a and a not in drop_aliases]
    if aliases:
        out["Aliases"] = aliases
    ipam = ep.get("IPAMConfig")
    if ipam:
        out["IPAMConfig"] = ipam
    links = ep.get("Links")
    if links:
        out["Links"] = links
    opts = ep.get("DriverOpts")
    if opts:
        out["DriverOpts"] = opts
    return out


def clone_body(
    inspect: Dict[str, Any],
    old_image_config: Optional[Dict[str, Any]],
    new_image: str,
    labels: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, Any]]]]:
    """Return ``(create_body, extra_networks)`` cloning ``inspect`` onto ``new_image``.

    ``extra_networks`` is a list of ``(network_name, endpoint_config)`` to
    connect AFTER create and BEFORE start (the Engine accepts a single endpoint
    at create time on older API versions).
    """
    config: Dict[str, Any] = copy.deepcopy(inspect.get("Config") or {})
    host_config: Dict[str, Any] = copy.deepcopy(inspect.get("HostConfig") or {})
    img: Dict[str, Any] = old_image_config or {}
    cid = str(inspect.get("Id") or "")
    short_id = cid[:12]

    # -- strip values inherited from the OLD image -------------------------
    img_env = set(img.get("Env") or [])
    env = [e for e in (config.get("Env") or []) if e not in img_env]
    if env:
        config["Env"] = env
    else:
        config.pop("Env", None)

    img_labels = img.get("Labels") or {}
    lbls = {k: v for k, v in (config.get("Labels") or {}).items() if img_labels.get(k) != v}

    entrypoint_overridden = bool(config.get("Entrypoint")) and config.get("Entrypoint") != img.get("Entrypoint")
    for key in _IMAGE_SCALARS:
        if key == "Cmd" and entrypoint_overridden:
            continue  # Engine does not inherit the image Cmd when Entrypoint is set
        if key in config and config.get(key) == img.get(key):
            config.pop(key, None)

    for key in ("ExposedPorts", "Volumes"):
        cur = dict(config.get(key) or {})
        for k in (img.get(key) or {}):
            cur.pop(k, None)
        if cur:
            config[key] = cur
        else:
            config.pop(key, None)

    # Identity fields the daemon must regenerate. Hostname defaults to the short
    # container id; keep it only when the operator set a real hostname.
    hostname = str(config.get("Hostname") or "")
    if not hostname or (cid and cid.startswith(hostname)):
        config.pop("Hostname", None)
    if not config.get("Domainname"):
        config.pop("Domainname", None)
    config.pop("MacAddress", None)
    config.pop("Image", None)

    if labels:
        lbls.update(labels)
    config["Labels"] = lbls
    config["Image"] = new_image

    # -- keep every volume, including anonymous ones ----------------------
    binds: List[str] = list(host_config.get("Binds") or [])
    declared = {_bind_target(b) for b in binds}
    declared |= {str(m.get("Target") or "") for m in (host_config.get("Mounts") or [])}
    for m in inspect.get("Mounts") or []:
        if m.get("Type") != "volume":
            continue
        dest = str(m.get("Destination") or "")
        name = str(m.get("Name") or "")
        if not dest or not name or dest in declared:
            continue
        bind = f"{name}:{dest}"
        if m.get("RW") is False:
            bind += ":ro"
        binds.append(bind)
        declared.add(dest)
    if binds:
        host_config["Binds"] = binds

    body: Dict[str, Any] = dict(config)
    body["HostConfig"] = host_config

    # -- networks + aliases ---------------------------------------------
    networks: Dict[str, Any] = dict((inspect.get("NetworkSettings") or {}).get("Networks") or {})
    net_mode = str(host_config.get("NetworkMode") or "")
    drop = tuple(a for a in (short_id, cid, str((inspect.get("Config") or {}).get("Hostname") or "")) if a)
    extras: List[Tuple[str, Dict[str, Any]]] = []
    custom_primary = net_mode not in _BUILTIN_NETWORKS and not net_mode.startswith("container:")
    if custom_primary and net_mode in networks:
        ep = _endpoint(networks[net_mode] or {}, drop)
        if ep:
            body["NetworkingConfig"] = {"EndpointsConfig": {net_mode: ep}}
    if not net_mode.startswith("container:") and net_mode not in ("host", "none"):
        for name, ep in networks.items():
            if not name or name == net_mode:
                continue
            if net_mode in ("default", "") and name == "bridge":
                continue
            extras.append((name, {} if name == "bridge" else _endpoint(ep or {}, drop)))
    return body, extras


def restart_policy(inspect: Dict[str, Any]) -> Dict[str, Any]:
    """The container's restart policy as an ``/update`` body value."""
    pol = (inspect.get("HostConfig") or {}).get("RestartPolicy") or {}
    name = str(pol.get("Name") or "no")
    out: Dict[str, Any] = {"Name": name}
    if name == "on-failure":
        out["MaximumRetryCount"] = int(pol.get("MaximumRetryCount") or 0)
    return out
