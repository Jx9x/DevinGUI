"""Errors shared by ACP transport and verified session configuration."""

from __future__ import annotations


class ACPError(Exception):
    """Wraps a JSON-RPC error object returned by the agent."""

    def __init__(self, error_obj: dict):
        self.code = error_obj.get("code")
        self.message = error_obj.get("message", "Unknown ACP error")
        self.data = error_obj.get("data")
        super().__init__(f"[{self.code}] {self.message}")


class ModelNotAppliedError(RuntimeError):
    """Raised when the requested model variant cannot be selected or confirmed.

    Treat this as fatal for the operation. Configuration may be partially
    applied; continuing could use an unintended model variant or quota.
    """
