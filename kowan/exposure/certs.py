"""Certificate status read from a proxy's own storage.

Traefik keeps ACME certificates in ``acme.json`` (one object per resolver),
read through the container archive API, so no Traefik API, shell or published
port is needed. Private keys are never returned: only subject names, issuer
and validity.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cryptography import x509


def cert_info(pem: bytes, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Summarise the FIRST certificate of a PEM bundle (the leaf)."""
    try:
        cert = x509.load_pem_x509_certificate(pem)
    except Exception:  # noqa: BLE001
        return None
    now = now or datetime.now(timezone.utc)
    not_after = cert.not_valid_after_utc
    not_before = cert.not_valid_before_utc
    names: List[str] = []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names = [str(n) for n in san.value.get_values_for_type(x509.DNSName)]
    except Exception:  # noqa: BLE001
        pass
    try:
        issuer = cert.issuer.rfc4514_string()
    except Exception:  # noqa: BLE001
        issuer = ""
    days = int((not_after - now).total_seconds() // 86400)
    return {
        "names": names,
        "issuer": issuer,
        "notBefore": not_before.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notAfter": not_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "daysLeft": days,
        "status": "expired" if days < 0 else ("expiring" if days < 14 else "valid"),
        "staging": "staging" in issuer.lower() or "fake" in issuer.lower(),
    }


def parse_acme_json(raw: bytes, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Certificates in a Traefik ``acme.json`` as ``[{domain, sans, resolver, ...}]``."""
    try:
        doc = json.loads(raw or b"{}")
    except ValueError:
        return []
    out: List[Dict[str, Any]] = []
    if not isinstance(doc, dict):
        return out
    for resolver in sorted(doc):
        body = doc[resolver]
        if not isinstance(body, dict):
            continue
        for c in body.get("Certificates") or []:
            if not isinstance(c, dict):
                continue
            dom = c.get("domain") or c.get("Domain") or {}
            main = str(dom.get("main") or dom.get("Main") or "")
            sans = [str(s) for s in (dom.get("sans") or dom.get("SANs") or [])]
            entry: Dict[str, Any] = {"domain": main, "sans": sans, "resolver": resolver, "source": "acme.json"}
            pem_b64 = c.get("certificate") or c.get("Certificate") or ""
            try:
                info = cert_info(base64.b64decode(pem_b64), now=now) if pem_b64 else None
            except Exception:  # noqa: BLE001
                info = None
            if info:
                entry.update(info)
            else:
                entry.update({"status": "unknown"})
            out.append(entry)
    return out


def match(domain: str, certs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The certificate covering ``domain`` (exact or one-level wildcard)."""
    d = (domain or "").lower()
    best = None
    for c in certs:
        names = {n.lower() for n in ([c.get("domain", "")] + list(c.get("sans") or []) + list(c.get("names") or [])) if n}
        if d in names:
            return c
        parent = "*." + d.split(".", 1)[1] if "." in d else ""
        if parent and parent in names:
            best = c
    return best
