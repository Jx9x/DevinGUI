"""Enforce the local boundary for every HTTP and WebSocket route.

Runs before routing and CORS. Rejected requests never read a body, accept
a socket, or reach services. Require a loopback peer and Host, and reject
cross-site fetches. Origin may be absent, same-origin, or HTTP loopback on
Vite ports 5173/5174.
"""
from __future__ import annotations

import ipaddress
import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

_AUTHORITY = re.compile(
    r"(localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?", re.IGNORECASE | re.ASCII
)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_SCHEME_EQUIVALENT = {"ws": "http", "wss": "https"}
_VITE_PORTS = {5173, 5174}
_SINGLE_HEADERS = (b"host", b"origin", b"sec-fetch-site")


def _loopback_authority(authority: str) -> tuple[str, int | None] | None:
    """Parse a strict ``host[:port]`` authority; only loopback names are valid."""
    match = _AUTHORITY.fullmatch(authority)
    if match is None:
        return None
    host = match.group(1).lower()
    if host.startswith("["):
        host = host[1:-1]
    port_text = match.group(2)
    if port_text is None:
        return host, None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    return host, port


def _loopback_origin(origin: str) -> tuple[str, str, int | None] | None:
    """Parse a strictly serialized ``scheme://host[:port]`` loopback origin."""
    scheme, separator, authority = origin.partition("://")
    if not separator or scheme not in ("http", "https"):
        return None
    parsed = _loopback_authority(authority)
    if parsed is None:
        return None
    return scheme, parsed[0], parsed[1]


def _loopback_peer(scope) -> bool:
    # Use the ASGI peer, not spoofable forwarded headers.
    client = scope.get("client")
    if not isinstance(client, (list, tuple)) or len(client) != 2:
        return False
    host, port = client
    if not isinstance(host, str) or not isinstance(port, int) or "%" in host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _allowed(scope) -> bool:
    if not _loopback_peer(scope):
        return False
    counts: dict[bytes, int] = {}
    for name, _ in scope.get("headers", []):
        lowered = name.lower()
        if lowered in _SINGLE_HEADERS:
            counts[lowered] = counts.get(lowered, 0) + 1
    if counts.get(b"host") != 1 or counts.get(b"origin", 0) > 1 or counts.get(b"sec-fetch-site", 0) > 1:
        return False
    headers = Headers(scope=scope)
    host = _loopback_authority(headers["host"])
    if host is None:
        return False
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() == "cross-site":
        return False
    origin = headers.get("origin")
    if origin is None:
        return True
    parsed = _loopback_origin(origin)
    if parsed is None:
        return False
    scheme, origin_host, origin_port = parsed
    request_scheme = _SCHEME_EQUIVALENT.get(scope.get("scheme", "http"), scope.get("scheme"))
    if scheme == request_scheme and scheme in _DEFAULT_PORTS and origin_host == host[0]:
        request_port = host[1] if host[1] is not None else _DEFAULT_PORTS[request_scheme]
        effective_origin_port = origin_port if origin_port is not None else _DEFAULT_PORTS[scheme]
        if effective_origin_port == request_port:
            return True
    return scheme == "http" and origin_port in _VITE_PORTS


class LocalBoundaryMiddleware:
    """Reject non-loopback or cross-site requests before routing or accept."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or _allowed(scope):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "local origin required"})
            return
        response = JSONResponse(
            {"detail": "Requests require a loopback Host and a same-origin or local-dev Origin."},
            status_code=403,
        )
        await response(scope, receive, send)
