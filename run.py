"""Entry point for the Devin GUI.

Starts the FastAPI backend on port 8000.  In production the built frontend
is served from ``frontend/dist``.  In development, run the Vite dev server
separately (``npm run dev`` in the frontend directory) and it will proxy
API/WebSocket requests to this backend.
"""

import argparse
import ipaddress
import logging
from pathlib import Path
import sys

import uvicorn

from server.diagnostics import print_startup_checks


def _loopback_host(value):
    if value.lower() == "localhost":
        return "127.0.0.1"
    if "%" in value:
        raise argparse.ArgumentTypeError(f"zone-scoped hosts are not allowed: {value!r}")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"host must be localhost or a loopback IP literal: {value!r}") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_loopback:
        raise argparse.ArgumentTypeError(f"host must be a loopback address: {value!r}")
    return str(address)


def main():
    parser = argparse.ArgumentParser(description="Devin GUI Server")
    parser.add_argument("--host", type=_loopback_host, default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    print(f"\n  Devin GUI starting on http://{args.host}:{args.port}\n")
    print("  If the frontend isn't built, run:")
    print("    cd frontend && npm ci && npm run build")
    print("  Or for dev mode, run `npm run dev` in another terminal.\n")
    print_startup_checks(Path(__file__).resolve().parent)

    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
