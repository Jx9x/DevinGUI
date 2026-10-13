"""Project-folder browsing routes and best-effort filesystem listing."""

from __future__ import annotations

import ctypes
import logging
import os
import re
import stat
from pathlib import PureWindowsPath

from fastapi import APIRouter, Request

from .rest_errors import error_response

_BROWSE_HEADER = "x-devingui-browse"
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[/\\]")
# GetDriveTypeW results treated as local: removable (2), fixed (3),
# CD-ROM (5) and RAM disk (6); remote drives (4) are refused.
_LOCAL_DRIVE_TYPES = {2, 3, 5, 6}
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_WINDOWS_DEVICES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _has_browse_header(request: Request) -> bool:
    """Require one ``x-devingui-browse: 1`` header; a cross-site page cannot send it."""
    values = request.headers.getlist(_BROWSE_HEADER)
    return len(values) == 1 and values[0] == "1"


def _on_windows() -> bool:
    return os.name == "nt"


def _windows_drive_type(drive: str) -> int:
    return ctypes.windll.kernel32.GetDriveTypeW(drive)


def _reparse_or_link(st: os.stat_result) -> bool:
    if _on_windows():
        return bool(st.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    return stat.S_ISLNK(st.st_mode)


def _device_component(part: str) -> bool:
    if PureWindowsPath(part).is_reserved():
        return True
    base = part.strip(" .")
    # is_reserved() doesn't flag names that strip to nothing, such as " " or "...".
    if not base:
        return True
    upper = base.upper()
    return (
        PureWindowsPath(base).is_reserved()
        or upper in _WINDOWS_DEVICES
        or upper.split(".", 1)[0] in _WINDOWS_DEVICES
    )


def _walk_local(root: str, parts: list[str]) -> None:
    """Reject the path if any component below ``root`` is a link."""
    current = root
    for part in parts:
        current = os.path.join(current, part)
        if _reparse_or_link(os.lstat(current)):
            raise ValueError(f"path traverses a link: {current!r}")


def _assert_posix_path(path: str, normalized: str) -> None:
    if not normalized.startswith("/"):
        raise ValueError(f"not a local absolute path: {path!r}")
    _walk_local("/", [part for part in normalized.split("/") if part])


def _assert_local_directory(path: str, normalized: str) -> None:
    """Reject non-local or link-traversing paths before any listing."""
    if "\x00" in path:
        raise ValueError("path contains NUL")
    if len(path) >= 2 and path[0] in "/\\" and path[1] in "/\\":
        raise ValueError(f"network paths are not browsable: {path!r}")
    if not _on_windows():
        _assert_posix_path(path, normalized)
        return
    if _WINDOWS_DRIVE.match(path) is None or ":" in path[2:]:
        raise ValueError(f"not a fully qualified local drive path: {path!r}")
    parts = [part for part in normalized[3:].split(os.sep) if part]
    for part in parts:
        if _device_component(part):
            raise ValueError(f"device path components are not browsable: {part!r}")
    root = normalized[:3]
    if _windows_drive_type(root) not in _LOCAL_DRIVE_TYPES:
        raise ValueError(f"drive is not local: {root!r}")
    _walk_local(root, parts)


def _browse_roots() -> list[dict]:
    """Return available local drive roots on Windows, or ``/`` elsewhere."""
    if _on_windows():
        roots: list[dict] = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if _windows_drive_type(drive) not in _LOCAL_DRIVE_TYPES:
                continue
            if os.path.isdir(drive):
                roots.append({"name": f"{letter}:", "path": drive, "is_dir": True})
        return roots
    return [{"name": "/", "path": "/", "is_dir": True}]


def _browse_directory(path: str, *, logger: logging.Logger) -> dict:
    """List subdirectories and the parent path.

    Unreadable children and links: skip.
    Missing/unreadable requested directory: empty entries and an error (HTTP 200).
    """
    normalized = os.path.normpath(path)
    parent = os.path.dirname(normalized)
    # ``os.path.dirname`` returns the same path for a drive root (e.g.
    # ``C:\\``), which would make "go up" a no-op loop. Treat that as no
    # parent so the frontend can fall back to the roots listing.
    if parent == normalized:
        parent = ""

    entries: list[dict] = []
    try:
        _assert_local_directory(path, normalized)
        names = sorted(os.listdir(normalized), key=str.lower)
    except (PermissionError, OSError) as e:
        logger.warning("browse: cannot list %s: %s", normalized, e)
        return {"path": normalized, "parent": parent, "entries": [], "error": str(e)}

    for name in names:
        full = os.path.join(normalized, name)
        try:
            if _on_windows() and _device_component(name):
                continue
            if _reparse_or_link(os.lstat(full)):
                continue
            if os.path.isdir(full):
                entries.append({"name": name, "path": full, "is_dir": True})
        except (PermissionError, OSError):
            # Skip entries we can't stat — browsing should be best-effort.
            continue
    return {"path": normalized, "parent": parent, "entries": entries}


def create_browse_router(*, logger: logging.Logger) -> APIRouter:
    router = APIRouter()

    @router.get("/api/browse")
    async def browse(request: Request, path: str | None = None):
        """List directories under ``path`` so the UI can offer a folder picker.

        With no ``path`` query parameter the response describes the top-level
        browse targets (drive roots on Windows, ``/`` elsewhere) and an empty
        ``entries`` list; the frontend renders those targets as the starting
        point. With a ``path`` the response lists that directory's
        subdirectories plus a ``parent`` path for navigating up.
        """
        if not _has_browse_header(request):
            return error_response(
                403, "Folder browsing requires the application browse header.", "browse"
            )
        try:
            if not path:
                return {"path": "", "parent": "", "entries": [], "roots": _browse_roots()}
            return _browse_directory(path, logger=logger)
        except ValueError as e:
            return error_response(403, str(e), "browse")
        except Exception as e:
            logger.error("Failed to browse %s: %s", path, e)
            return error_response(500, str(e), "browse")

    return router
