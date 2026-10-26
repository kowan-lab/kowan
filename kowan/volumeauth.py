"""Resolve a shared connection's secret material into a volume's DriverOpts.

A volume can reference a connection (``connectionRef``) so its provider auth — an
SMB/CIFS password, an S3 key pair, an SSHFS password — comes from the central
credentials store instead of being typed per volume. The mapping is applied on the
**control plane**, so the secret is injected into the DriverOpts *before* the
create RPC and never reaches the browser; it travels control-plane -> agent over
the token tunnel, like every other docker op.

Each provider names the connection types that can back it and the fields it lifts
onto the driver options Docker expects. Providers with no auth (nfs, tmpfs, plain
local) never reference a connection.
"""

from __future__ import annotations

from typing import Dict, Tuple

# provider -> connection types eligible to back it. Used to filter the UI picker
# (mirrored in the frontend) and to validate a reference server-side.
PROVIDER_CONNECTION_TYPES: Dict[str, Tuple[str, ...]] = {
    "s3fs": ("aws",),
    "cifs": ("basic-auth",),
    "sshfs": ("basic-auth",),
}


class VolumeAuthError(Exception):
    """Raised when a volume references a connection its provider cannot use."""


def _merge_o(existing: str, **pairs: str) -> str:
    """Append ``key=value`` pairs to a comma-joined DriverOpt ``o`` string without
    overriding keys already present (an explicit user option wins over the injected
    secret)."""
    parts = [p for p in existing.split(",") if p.strip()]
    present = {p.split("=", 1)[0] for p in parts}
    for key, value in pairs.items():
        if value and key not in present:
            parts.append(f"{key}={value}")
    return ",".join(parts)


def apply_connection(
    provider: str, conn_type: str, secret: Dict[str, str], options: Dict[str, str]
) -> Dict[str, str]:
    """Return ``options`` with the connection's secret fields injected for ``provider``.

    ``secret`` is the connection's merged non-secret identity (meta) + decrypted
    data — e.g. a ``basic-auth`` connection contributes ``username`` (meta) +
    ``password`` (data); an ``aws`` connection contributes ``accessKeyId`` /
    ``secretAccessKey`` / ``region``. Raises :class:`VolumeAuthError` if the
    connection's type cannot back the provider.
    """
    allowed = PROVIDER_CONNECTION_TYPES.get(provider)
    if allowed is None:
        raise VolumeAuthError(f"provider {provider!r} does not accept a connection")
    if conn_type not in allowed:
        raise VolumeAuthError(
            f"a {conn_type!r} connection cannot back a {provider!r} volume "
            f"(expected {', '.join(allowed)})"
        )

    opts = dict(options)
    if provider == "s3fs":
        opts["AWSACCESSKEYID"] = secret.get("accessKeyId", "")
        opts["AWSSECRETACCESSKEY"] = secret.get("secretAccessKey", "")
        region = secret.get("region", "")
        if region and not opts.get("AWSDEFAULTREGION"):
            opts["AWSDEFAULTREGION"] = region
    elif provider == "cifs":
        opts["o"] = _merge_o(
            opts.get("o", ""),
            username=secret.get("username", ""),
            password=secret.get("password", ""),
        )
    elif provider == "sshfs":
        # Host + remote path arrive as plain (non-secret) options; the username
        # and password come from the connection. Assemble the ``[user@]host:path``
        # target the vieux/sshfs driver expects, so the login never round-trips
        # the browser. An explicit ``sshcmd`` (if ever supplied) is left intact.
        host = str(opts.pop("host", "")).strip()
        path = str(opts.pop("path", "")).strip()
        username = secret.get("username", "")
        if host and not opts.get("sshcmd"):
            target = host + ":" + path
            if username:
                target = username + "@" + target
            opts["sshcmd"] = target
        password = secret.get("password", "")
        if password:
            opts["password"] = password
    return opts


__all__ = ["PROVIDER_CONNECTION_TYPES", "VolumeAuthError", "apply_connection"]
