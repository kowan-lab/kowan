"""Browser security headers on every response.

The SPA loads nothing from other origins and has no inline scripts, so the
Content-Security-Policy can be strict: scripts only from this server, no
framing (clickjacking), no plugins. Styles allow 'unsafe-inline' because React
style attributes and the code editor inject them. HSTS is sent only when the
page is served over HTTPS (directly, or via a trusted proxy / https external URL).
"""

from __future__ import annotations

from aiohttp import web

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

_STATIC = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


def security_headers_middleware(https_external: bool):
    @web.middleware
    async def middleware(request: web.Request, handler):
        resp = await handler(request)
        # WebSocket upgrades and already-sent streams cannot take headers.
        if getattr(resp, "prepared", False):
            return resp
        for k, v in _STATIC.items():
            resp.headers.setdefault(k, v)
        if https_external or request.secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    return middleware
