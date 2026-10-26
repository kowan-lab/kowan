"""One probe of one uptime check.

HTTP checks pass when the status code is in the expected set and, when a
keyword is set, the (first 1 MB of the) body contains it. TCP checks pass when a
connection opens. HTTPS checks also read the certificate's expiry, at most every
few hours, so the UI can warn before it lapses.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from kowan.httpcap import request_capped

# Body bytes read for the keyword check.
_BODY_LIMIT = 1 << 20

# Tests swap this for a factory returning an httpx client on a MockTransport.
HTTP_FACTORY: Callable[..., httpx.AsyncClient] = httpx.AsyncClient


@dataclass
class Result:
    ok: bool
    latency_ms: float
    code: int = 0
    error: str = ""


def parse_expect(spec: str) -> List[Tuple[int, int]]:
    """``"200-299,301"`` -> ``[(200, 299), (301, 301)]``. Raises ValueError."""
    out: List[Tuple[int, int]] = []
    for part in (spec or "200-399").split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        a, b = int(lo), int(hi or lo)
        if not (100 <= a <= b <= 599):
            raise ValueError(f"invalid status range {part!r}")
        out.append((a, b))
    if not out:
        raise ValueError("expected status is empty")
    return out


def parse_tcp_target(target: str) -> Tuple[str, int]:
    """``host:port`` (``[v6]:port`` too). Raises ValueError."""
    t = target.strip()
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = t.rpartition(":")
    if not host or not port.isdigit() or not (0 < int(port) < 65536):
        raise ValueError("target must be host:port")
    return host, int(port)


def validate_http_target(target: str) -> None:
    u = urlsplit(target.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("target must be an http:// or https:// URL")


async def http_probe(target: str, *, method: str, expect: str, keyword: str, verify_tls: bool, timeout: float) -> Result:
    ranges = parse_expect(expect)
    start = time.monotonic()
    try:
        async with HTTP_FACTORY(verify=verify_tls, follow_redirects=True, timeout=timeout) as client:
            resp = await request_capped(client, method or "GET", target, _BODY_LIMIT,
                                        headers={"User-Agent": "Kowan-Uptime/1"})
    except httpx.TimeoutException:
        return Result(False, (time.monotonic() - start) * 1000, error=f"timed out after {timeout:g}s")
    except httpx.HTTPError as err:
        return Result(False, (time.monotonic() - start) * 1000, error=_short(err))
    latency = (time.monotonic() - start) * 1000
    code = resp.status_code
    if not any(a <= code <= b for a, b in ranges):
        return Result(False, latency, code, f"HTTP {code}")
    if keyword and keyword not in resp.text:
        return Result(False, latency, code, f"keyword {keyword!r} not found")
    return Result(True, latency, code)


async def tcp_probe(target: str, *, timeout: float) -> Result:
    host, port = parse_tcp_target(target)
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        return Result(False, (time.monotonic() - start) * 1000, error=f"timed out after {timeout:g}s")
    except OSError as err:
        return Result(False, (time.monotonic() - start) * 1000, error=err.strerror or str(err))
    latency = (time.monotonic() - start) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return Result(True, latency)


async def cert_expiry(target: str, *, timeout: float) -> Optional[str]:
    """notAfter of the certificate an https:// target serves, as RFC3339, or
    None when it cannot be read (not https, handshake failed)."""
    u = urlsplit(target)
    if u.scheme != "https" or not u.hostname:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # read the date even of a broken chain
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname, u.port or 443, ssl=ctx, server_hostname=u.hostname), timeout
        )
    except (OSError, asyncio.TimeoutError, ssl.SSLError):
        return None
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    if not der:
        return None
    return _not_after(der)


def _not_after(der: bytes) -> Optional[str]:
    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        return cert.not_valid_after_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001 - unparsable certificate
        return None


def _short(err: Exception) -> str:
    text = str(err) or type(err).__name__
    return text if len(text) <= 200 else text[:197] + "..."


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
