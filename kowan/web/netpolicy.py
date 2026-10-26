"""Who may reach the server, by network address.

* ``KOWAN_TRUSTED_PROXIES``: the reverse proxies allowed to state the client's
  address in ``X-Forwarded-For`` / ``X-Real-IP``. From anyone else those headers
  are ignored, so a client cannot claim to be someone else (audit log, rate
  limits, the allowlists below).
* ``KOWAN_UI_ALLOW_CIDRS``: who may use the web UI, the REST API and MCP.
* ``KOWAN_EDGE_ALLOW_CIDRS``: who may open an edge tunnel (``/edge/connect``).
* ``KOWAN_PROXY_HEADER``: ``X-Forwarded-For`` (default) or ``X-Real-IP``, the one
  header a trusted proxy uses; the other is ignored.

Each is a comma-separated list of addresses or CIDRs; empty means no limit.
Public webhooks (``/hooks/…``) and ``/health`` are not covered by the UI list:
CI systems and git hosts call them from addresses you do not control.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import List, Optional, Union

from aiohttp import web

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

NETPOLICY_KEY = web.AppKey("kowan_netpolicy", object)

EDGE_PATH = "/edge/connect"

# Settings keys of the lists managed in the UI (comma-separated CIDRs).
UI_SETTING = "netpolicy.ui_allow"
EDGE_SETTING = "netpolicy.edge_allow"
_OPEN_PREFIXES = ("/hooks/",)
_OPEN_PATHS = ("/health",)


def parse_cidrs(value: str, name: str = "CIDR list") -> List[Network]:
    """Parse ``"10.0.0.0/8, 192.168.1.10"``; raises ValueError on a bad entry."""
    out: List[Network] = []
    for raw in (value or "").split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError as err:
            raise ValueError(f"{name}: {item!r} is not an address or CIDR ({err})") from None
    return out


def _addr(value: str) -> Optional[Address]:
    """Parse an address as proxies write it: ``1.2.3.4``, ``1.2.3.4:5678``,
    ``2001:db8::1``, ``[2001:db8::1]:443``. None when it is not one."""
    value = (value or "").strip()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        value = value[1:end]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    if "%" in value:  # IPv6 zone id
        value = value.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    # An IPv4 client on a dual-stack socket shows up as ::ffff:a.b.c.d.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _within(addr: Optional[Address], nets: List[Network]) -> bool:
    return addr is not None and any(addr in n for n in nets if addr.version == n.version)


def address_within(value: str, nets: List[Network]) -> bool:
    """Whether the address string ``value`` falls in one of ``nets``."""
    return _within(_addr(value), nets)


def _proxy_header(value: str) -> str:
    value = (value or "x-forwarded-for").strip().lower()
    if value not in ("x-forwarded-for", "x-real-ip"):
        raise ValueError(f"KOWAN_PROXY_HEADER: {value!r} must be X-Forwarded-For or X-Real-IP")
    return value


@dataclass
class NetPolicy:
    trusted_proxies: List[Network] = field(default_factory=list)
    ui_allow: List[Network] = field(default_factory=list)
    agent_allow: List[Network] = field(default_factory=list)
    # Which header a trusted proxy states the client in: x-forwarded-for
    # (default) or x-real-ip. Only that one is read.
    proxy_header: str = "x-forwarded-for"
    # Lists an admin set in Settings > Network access. They can only narrow:
    # with both set, an address must be in the environment list AND in this one.
    ui_allow_stored: List[Network] = field(default_factory=list)
    agent_allow_stored: List[Network] = field(default_factory=list)

    @classmethod
    def from_config(cls, config) -> "NetPolicy":
        return cls(
            trusted_proxies=parse_cidrs(getattr(config, "trusted_proxies", ""), "KOWAN_TRUSTED_PROXIES"),
            ui_allow=parse_cidrs(getattr(config, "ui_allow_cidrs", ""), "KOWAN_UI_ALLOW_CIDRS"),
            agent_allow=parse_cidrs(getattr(config, "edge_allow_cidrs", ""), "KOWAN_EDGE_ALLOW_CIDRS"),
            proxy_header=_proxy_header(getattr(config, "proxy_header", "")),
        )

    def client_address(self, request: web.Request) -> Optional[Address]:
        """The client's address, or None when a trusted proxy sent one that
        cannot be read (denied by any allowlist rather than guessed)."""
        peer = _addr(request.remote or "")
        if not _within(peer, self.trusted_proxies):
            return peer
        if self.proxy_header == "x-real-ip":
            value = request.headers.get("X-Real-IP")
            return peer if value is None else _addr(value)
        value = request.headers.get("X-Forwarded-For")
        if value is None:
            return peer  # the proxy's own request (a health check)
        # Walk from the nearest hop back, skipping our own proxies: the first
        # address that is not one of them is the client. Anything before it
        # was written by the client and is not trusted.
        hops = [h for h in value.split(",") if h.strip()]
        addr = None
        for hop in reversed(hops):
            addr = _addr(hop)
            if addr is None or not _within(addr, self.trusted_proxies):
                return addr
        return addr

    def address_allowed(self, addr: Optional[Address], edge: bool = False) -> bool:
        layers = (self.agent_allow, self.agent_allow_stored) if edge else (self.ui_allow, self.ui_allow_stored)
        return all(not nets or _within(addr, nets) for nets in layers)

    def allowed(self, request: web.Request) -> bool:
        path = request.path
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            return True
        layers = (self.agent_allow, self.agent_allow_stored) if path == EDGE_PATH else (self.ui_allow, self.ui_allow_stored)
        if not any(layers):
            return True
        return self.address_allowed(self.client_address(request), edge=path == EDGE_PATH)

    def load_stored(self, store) -> None:
        """Read the Settings lists; a value that no longer parses is ignored
        rather than locking everyone out."""
        for attr, key in (("ui_allow_stored", UI_SETTING), ("agent_allow_stored", EDGE_SETTING)):
            try:
                setattr(self, attr, parse_cidrs(store.get_setting(key), key))
            except ValueError:
                setattr(self, attr, [])


def policy_of(request: web.Request) -> NetPolicy:
    policy = request.config_dict.get(NETPOLICY_KEY)
    return policy if isinstance(policy, NetPolicy) else NetPolicy()


def client_ip(request: web.Request) -> str:
    """The client's address as a string ("" when unknown)."""
    addr = policy_of(request).client_address(request)
    return str(addr) if addr is not None else ""


@web.middleware
async def netpolicy_middleware(request: web.Request, handler):
    if not policy_of(request).allowed(request):
        return web.json_response({"error": "access from this address is not allowed"}, status=403)
    return await handler(request)
