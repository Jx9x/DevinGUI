"""Bounded static hosting for the built frontend SPA."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles


def create_frontend_router(directory: str | Path) -> APIRouter:
    """Serve the built SPA from ``directory`` without ever leaving it."""
    root = Path(directory).resolve()
    static = StaticFiles(directory=root, follow_symlink=False)
    router = APIRouter()

    def _safe(full_path: str) -> bool:
        if "\x00" in full_path or "\\" in full_path or ":" in full_path:
            return False
        windows = PureWindowsPath(full_path)
        if full_path.startswith("/") or windows.drive or windows.root:
            return False
        if ".." in full_path.split("/"):
            return False
        try:
            return Path(root, full_path).resolve().is_relative_to(root)
        except (OSError, ValueError, RuntimeError):
            return False

    @router.get("/{full_path:path}")
    async def serve_spa(full_path: str, request: Request):
        # Don't intercept API or WebSocket paths
        if full_path.startswith("api") or full_path.startswith("ws"):
            raise HTTPException(status_code=404, detail="Not found")
        if not _safe(full_path):
            raise HTTPException(status_code=404, detail="Not found")
        try:
            return await static.get_response(full_path, request.scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        return await static.get_response("index.html", request.scope)

    return router
