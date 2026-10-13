#!/usr/bin/env python3
"""Probe whether the DevinGUI backend port (8000) is free on loopback.

Called by ``start-devingui.bat`` before launching the backend.  The
probe performs a real ``bind()`` on ``127.0.0.1`` and ``::1`` (when the IPv6
family is available); on Windows it requests ``SO_EXCLUSIVEADDRUSE`` so a
sharable socket cannot mask an existing listener.  It never connects to or
terminates another process.  Exits ``0`` only when the IPv4 probe succeeded
and no probe reported occupied/unverifiable (fail closed).
"""

from __future__ import annotations

import errno
import socket
import sys

PORT = 8000
LOOPBACKS = ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1"))

_UNSUPPORTED_FAMILY_ERRNOS = {
    value
    for value in (
        getattr(errno, "EAFNOSUPPORT", None),
        getattr(errno, "EPROTONOSUPPORT", None),
        10047,
    )
    if value is not None
}


def _family_unsupported(exc: OSError) -> bool:
    """Whether *exc* means the address family itself is unavailable."""
    return exc.errno in _UNSUPPORTED_FAMILY_ERRNOS or getattr(exc, "winerror", None) == 10047


def _probe(family: int, host: str, port: int) -> bool | None:
    """Bind-probe one loopback address.

    Returns ``True`` when the bind succeeded, ``False`` when the port is
    occupied or the result cannot be trusted, and ``None`` only when a
    genuinely unsupported IPv6 loopback cannot exist on this host.
    """
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        if family == socket.AF_INET6 and _family_unsupported(exc):
            return None
        return False
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
        return True
    except OSError as exc:
        if family == socket.AF_INET6 and exc.errno == errno.EADDRNOTAVAIL:
            return None
        return False
    finally:
        sock.close()


def main() -> int:
    try:
        results = [_probe(family, host, PORT) for family, host in LOOPBACKS]
    except Exception:
        results = [False]
    if any(result is True for result in results) and not any(
        result is False for result in results
    ):
        return 0
    print(
        f"[port-check] port {PORT} is already listening on loopback or could not be verified.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
