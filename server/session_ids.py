"""Session ID validation shared by REST, WebSocket, and transcript paths."""

from __future__ import annotations

import re

from fastapi import HTTPException

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", re.ASCII)
# IDs become transcript filenames, so reject reserved Windows device names.
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def session_id_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SESSION_ID.fullmatch(value) is not None
        and value.upper() not in _WINDOWS_DEVICES
    )


def validate_session_id(value: object) -> str:
    if not session_id_is_valid(value):
        raise ValueError("Invalid session ID")
    return value


def valid_session_id(session_id: str) -> str:
    try:
        return validate_session_id(session_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid session ID") from None
