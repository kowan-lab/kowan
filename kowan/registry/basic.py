"""Static basic-auth registry provider (generic + ghcr).

:class:`BasicProvider` implements the
generic and ghcr registry types: static HTTP basic auth using the registry's
username/password. :meth:`BasicProvider.validate` probes the Docker Registry V2
API (``GET /v2/``) and detects plain-HTTP / untrusted-TLS endpoints.

TLS failures are reclassified via a retry with verification disabled, using
:mod:`kowan.httpcap`: a first verified request,
then — over HTTPS only — a retry with ``verify=False`` to distinguish an
untrusted/self-signed certificate (insecure but reachable) from a genuine
connection failure.
"""

from __future__ import annotations

import base64
import json
from typing import Optional, Tuple

from kowan import httpcap
from kowan.models import Registry

from .provider import Provider

# Bounds a single probe request.
_PROBE_TIMEOUT = 15.0

# Body bytes read from a probe or token answer (only status/headers matter).
_PROBE_LIMIT = 64 << 10


def build_registry_auth(username: str, password: str, server_address: str) -> str:
    """Encode registry credentials into the base64 ``X-Registry-Auth`` token.

    A JSON object with the non-empty
    ``username``/``password``/``serveraddress`` fields, base64url-encoded. Returns
    ``""`` when all three are empty (anonymous access). The Python docker layer has
    no shared helper for this yet, so the registry providers build it here.
    """
    if username == "" and password == "" and server_address == "":
        return ""
    cfg: dict[str, str] = {}
    if username:
        cfg["username"] = username
    if password:
        cfg["password"] = password
    if server_address:
        cfg["serveraddress"] = server_address
    # Compact JSON (no spaces), then padded base64url.
    raw = json.dumps(cfg, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def scheme_and_host(url: str) -> Tuple[str, str]:
    """Split a registry URL into ``(scheme, host)``.

    A URL without a scheme is assumed HTTPS (the registry default); a trailing
    path/slash is dropped so only ``host[:port]`` remains.
    """
    scheme = "https"
    rest = url
    i = rest.find("://")
    if i >= 0:
        scheme = rest[:i].lower()
        rest = rest[i + 3:]
    slash = rest.find("/")
    if slash >= 0:
        rest = rest[:slash]
    return scheme, rest


def registry_reachable(status_code: int, www_authenticate: str) -> bool:
    """Report whether a response indicates a live Docker Registry V2 endpoint.

    A 2xx (anonymous/authenticated OK) or a 401 carrying the standard
    ``WWW-Authenticate`` challenge.
    """
    if 200 <= status_code < 300:
        return True
    if status_code == 401 and www_authenticate != "":
        return True
    return False


def is_tls_error(err: BaseException) -> bool:
    """Report whether ``err`` is a TLS verification/handshake failure.

    aiohttp wraps the underlying ``ssl.SSLError`` inside a connector error (and
    httpcap in an ``HTTPError``), so we match on the message text.
    """
    msg = str(err).lower()
    return (
        "certificate" in msg
        or "tls" in msg
        or "ssl" in msg
        or "x509" in msg
        or "self-signed" in msg
        or "self signed" in msg
    )


class BasicProvider(Provider):
    """Generic / ghcr static basic-auth provider."""

    def __init__(self, reg: Registry) -> None:
        self._url = reg.url
        self._username = reg.username
        self._password = reg.password
        # Honoured by the inherited v2 browse (Provider._browse_insecure): a
        # plain-HTTP or untrusted-TLS generic registry still lists repos/tags.
        self._insecure = reg.insecure

    async def docker_auth(self) -> str:
        """Return the base64 ``X-Registry-Auth`` token from the static credentials.

        Anonymous pulls (no credentials) keep returning ``""``.
        """
        return build_registry_auth(self._username, self._password, self._url)

    async def credentials(self) -> Tuple[str, str, str]:
        """Return the static stored credentials and the stored URL as server addr."""
        return self._username, self._password, self._url

    async def validate(self) -> Tuple[bool, Optional[str]]:
        """Probe ``{scheme}://{host}/v2/`` with basic auth.

        A registry is reachable when it answers 2xx or 401 + ``WWW-Authenticate``.
        ``insecure`` is reported when the scheme is http, or when the verified
        HTTPS request fails TLS verification but succeeds with verification
        disabled (self-signed / untrusted certificate). Returns
        ``(insecure, error_message_or_None)`` —
        the insecure flag stays meaningful even when an error is reported.
        """
        scheme, host = scheme_and_host(self._url)
        insecure = scheme == "http"
        endpoint = f"{scheme}://{host}/v2/"

        # First attempt: a normally-verified request.
        try:
            resp = await self._probe(endpoint, skip_verify=False)
        except Exception as err:  # noqa: BLE001
            # Over HTTPS, retry with TLS verification disabled to distinguish an
            # untrusted/self-signed certificate from a genuine connection failure.
            if scheme == "https" and is_tls_error(err):
                try:
                    resp2 = await self._probe(endpoint, skip_verify=True)
                except Exception as err2:  # noqa: BLE001
                    if _speaks_plain_http(err2):
                        return insecure, (f"registry {host} answers in plain HTTP, not HTTPS: "
                                          f"write the URL as http://{host}")
                    return insecure, f"connect to registry {host}: {err2}"
                if registry_reachable(
                    resp2.status_code, resp2.headers.get("WWW-Authenticate", "")
                ):
                    # Reachable only with verification disabled: untrusted TLS.
                    return True, await self._credentials_problem(resp2, host, skip_verify=True)
                return (
                    True,
                    f"registry {host} (untrusted TLS) returned unexpected status "
                    f"{resp2.status_code}",
                )
            return insecure, f"connect to registry {host}: {err}"

        if registry_reachable(resp.status_code, resp.headers.get("WWW-Authenticate", "")):
            return insecure, await self._credentials_problem(resp, host, skip_verify=False)
        return (
            insecure,
            f"registry {host} returned unexpected status {resp.status_code}",
        )

    async def _credentials_problem(self, resp: httpcap.Response, host: str, *, skip_verify: bool) -> Optional[str]:
        """Whether the stored credentials actually work, not just whether the
        registry answers: a 401 with a challenge proves only that it is alive.

        ``Basic``: the probe already sent the credentials, so a 401 rejects them.
        ``Bearer`` (Docker Hub, GHCR, Harbor…): ask the realm for a token with them.
        Without credentials, an anonymous registry is fine as it is.
        """
        if resp.status_code != 401 or not (self._username or self._password):
            return None
        challenge = resp.headers.get("WWW-Authenticate", "")
        scheme = challenge.split(" ", 1)[0].lower()
        if scheme == "basic":
            return f"registry {host} rejected the username or password"
        if scheme != "bearer":
            return None
        from .browse import _parse_challenge

        params = _parse_challenge(challenge)
        realm = params.get("realm", "")
        if not realm:
            return f"registry {host}: malformed auth challenge"
        query = {k: params[k] for k in ("service", "scope") if params.get(k)}
        try:
            token = await httpcap.request("GET", realm, limit=_PROBE_LIMIT, verify=not skip_verify,
                                          timeout=_PROBE_TIMEOUT, follow_redirects=False, params=query or None,
                                          auth=(self._username, self._password))
        except Exception as err:  # noqa: BLE001
            return f"registry {host}: token request failed: {err}"
        if token.status_code in (401, 403):
            return f"registry {host} rejected the username or password"
        if token.status_code != 200:
            return f"registry {host}: token endpoint returned {token.status_code}"
        return None

    async def _probe(self, endpoint: str, skip_verify: bool) -> httpcap.Response:
        """Perform ``GET /v2/`` with basic auth. ``skip_verify`` disables TLS
        certificate verification (used only to detect untrusted-TLS endpoints)."""
        auth = None
        if self._username != "" or self._password != "":
            auth = (self._username, self._password)
        # Do not follow redirects to other hosts during a reachability probe; the
        # status/challenge from the addressed endpoint is what we want.
        return await httpcap.request("GET", endpoint, limit=_PROBE_LIMIT, verify=not skip_verify,
                                     timeout=_PROBE_TIMEOUT, follow_redirects=False, auth=auth)


def _speaks_plain_http(err: BaseException) -> bool:
    """A TLS handshake answered by a plain-HTTP server fails with these."""
    text = str(err).upper()
    return "WRONG_VERSION_NUMBER" in text or "RECORD_LAYER_FAILURE" in text or "PACKET_LENGTH_TOO_LONG" in text
