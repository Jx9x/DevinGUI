"""Shared response format for REST operations with structured error handling."""

import sqlite3

from fastapi.responses import JSONResponse


def history_error_message(error: Exception) -> str:
    if isinstance(error, sqlite3.DatabaseError):
        return (
            f"Devin history is unavailable ({error}). Open Diagnostics and retry after starting "
            "the current official stable CLI. If the storage layout changed, update DevinGUI. "
            "The database has not been modified."
        )
    return str(error)


def error_response(status: int, message: str, operation: str) -> JSONResponse:
    """Build a structured error body for a failed REST operation."""
    return JSONResponse(
        status_code=status,
        content={"detail": message, "error": operation},
    )
