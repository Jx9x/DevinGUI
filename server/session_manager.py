"""Keep registered ACP sessions independent of individual WebSocket connections.

After disconnect, preserve ACP, running turns, and pending permissions for
``DEVIN_GRACE_SECONDS`` (default 30 seconds). Reconnects reuse that state;
grace expiry tears it down. Anonymous sessions are torn down immediately.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Optional

from fastapi import WebSocket

from .acp_client import ACPClient

logger = logging.getLogger("devin_gui.session_manager")


def _grace_seconds_from_env() -> int:
    """Read ``DEVIN_GRACE_SECONDS`` and clamp to [0, 300]."""
    raw = os.environ.get("DEVIN_GRACE_SECONDS", "30")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 30
    return max(0, min(300, value))


async def cancel_task(
    task: asyncio.Task | None,
    description: str,
    *,
    error_logger: logging.Logger = logger,
) -> None:
    """Cancel a pending lifecycle task and wait for its cleanup.

    Cancellation is expected; other cleanup failures are logged using the
    caller's logger. The owning operation decides when to clear its task
    reference, since that depends on its generation and identity guards.
    """
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_logger.error(f"Error waiting for cancelled {description}: %s", e)


class ManagedSession:
    """Per-Devin-session state, independent of any individual WebSocket."""

    def __init__(self, session_id: Optional[str] = None) -> None:
        self.session_id: Optional[str] = session_id
        self.acp: Optional[ACPClient] = None
        self.active_turn_task: Optional[asyncio.Task] = None
        self.active_transition_task: Optional[asyncio.Task] = None
        self.permission_requests: dict[str, Any] = {}
        self.generation_id: int = 0
        self.transition_lock: asyncio.Lock = asyncio.Lock()
        self.websocket: Optional[WebSocket] = None
        self.orphan_timer: Optional[asyncio.Task] = None
        self.model: Optional[str] = None
        self.permission_mode: Optional[str] = None
        # Latest replayable state or undelivered text delta for reconnects.
        # None also follows successful text delivery or replay.
        self.last_session_update: Optional[dict] = None
        # Raw ACP tool payloads keyed by toolCallId supply approval details
        # (path, diff, command) when edit/write permission requests contain
        # only {"toolCallId": ...}.
        self.tool_call_snapshots: dict[str, dict] = {}
        # Monotonic timestamp (``loop.time()``) of the most recent ACP
        # traffic for this session.  The turn-settlement fallback uses it
        # to confirm the agent has gone quiet before treating a persisted
        # final message as the end of the turn.
        self.last_acp_update_at: float = 0.0
        # A quiet terminal DB row may have triggered a provisional completion
        # while the authoritative session/prompt response is still pending.
        # New ACP activity reopens live persistence watchers until that
        # response or another quiet terminal DB row settles the turn.
        self.db_fallback_settled: bool = False
        self.db_fallback_resume_event: asyncio.Event | None = None

    # -- permission helpers -------------------------------------------------

    async def reject_permission(
        self, tool_call_id: str, request_id: Any, reason: str, code: int = -32600
    ) -> None:
        """Reject a single pending permission request via ACP."""
        if self.acp is None:
            return
        try:
            await self.acp.send_response(request_id, error={"code": code, "message": reason})
        except Exception as e:
            logger.error("Failed to reject permission request %s: %s", tool_call_id, e)

    async def reject_all_pending_permissions(self, reason: str, code: int = -32600) -> None:
        """Reject every pending permission request and cancel their timeouts."""
        while self.permission_requests:
            tool_call_id, pending = self.permission_requests.popitem()
            timeout_task = pending.get("timeout_task")
            if timeout_task and not timeout_task.done():
                timeout_task.cancel()
                try:
                    await timeout_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            await self.reject_permission(tool_call_id, pending["request_id"], reason, code)


class SessionManager:
    """Registry of :class:`ManagedSession` instances with grace-period logic."""

    def __init__(
        self,
        grace_seconds: Optional[int] = None,
        on_session_cleanup: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.grace_seconds: int = grace_seconds if grace_seconds is not None else _grace_seconds_from_env()
        self._sessions: dict[str, ManagedSession] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        # Synchronous callback invoked with ``session_id`` when a session is
        # torn down (grace timer expiry or immediate teardown).  Used by
        # ``main.py`` to stop transcript / DB watchers.
        self.on_session_cleanup = on_session_cleanup

    # -- registry access ----------------------------------------------------

    def get(self, session_id: str) -> Optional[ManagedSession]:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str) -> ManagedSession:
        ms = self._sessions.get(session_id)
        if ms is None:
            ms = ManagedSession(session_id=session_id)
            self._sessions[session_id] = ms
        return ms

    def attach_socket(self, session_id: Optional[str], ws: WebSocket) -> ManagedSession:
        """Bind a socket, cancelling orphan cleanup for an existing session.

        ``session_id=None`` creates an unregistered anonymous session.
        An unknown ID creates and registers a session.
        """
        if session_id is not None:
            ms = self._sessions.get(session_id)
            if ms is not None:
                ms.websocket = ws
                if ms.orphan_timer and not ms.orphan_timer.done():
                    ms.orphan_timer.cancel()
                    try:
                        ms.orphan_timer = None
                    except Exception:
                        pass
                else:
                    ms.orphan_timer = None
                return ms
        ms = ManagedSession(session_id=session_id)
        ms.websocket = ws
        if session_id is not None:
            self._sessions[session_id] = ms
        return ms

    def register(self, ms: ManagedSession, session_id: str) -> None:
        """Assign the session ID and register this instance under it.

        Replacement does not tear down the previous owner. Old registry keys
        are not removed; callers must handle cleanup.
        """
        ms.session_id = session_id
        self._sessions[session_id] = ms

    def unregister(self, ms: ManagedSession, session_id: str) -> None:
        """Remove a registry key only if it still belongs to this instance."""
        if self._sessions.get(session_id) is ms:
            del self._sessions[session_id]

    # -- disconnect / grace period ------------------------------------------

    async def detach_socket(self, ms: ManagedSession, ws: WebSocket) -> None:
        """Ignore stale sockets; otherwise detach and apply the grace policy.

        Positive grace and a session ID: preserve ACP/turn and start or reset
        the timer. No session ID or nonpositive grace: tear down immediately.
        """
        if ms.websocket is not ws:
            return
        ms.websocket = None
        if ms.session_id is None or self.grace_seconds <= 0:
            await self.cancel_orphan(ms)
            return
        if ms.orphan_timer and not ms.orphan_timer.done():
            ms.orphan_timer.cancel()
        ms.orphan_timer = asyncio.create_task(self._orphan_timer(ms))

    async def _orphan_timer(self, ms: ManagedSession) -> None:
        try:
            await asyncio.sleep(self.grace_seconds)
            await self.cancel_orphan(ms)
        except asyncio.CancelledError:
            pass

    async def cancel_orphan(self, ms: ManagedSession) -> None:
        """Tear down a managed session in lifecycle order.

        Cancel orphan/transition/turn tasks, stop watchers, reject permissions,
        stop ACP, then unregister only if this instance still owns the key.
        """
        # Cancel any running orphan timer (idempotent — may be called
        # directly by ``detach_socket`` when grace is 0).
        if ms.orphan_timer and not ms.orphan_timer.done():
            ms.orphan_timer.cancel()
            try:
                await ms.orphan_timer
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        ms.orphan_timer = None

        await cancel_task(ms.active_transition_task, "transition", error_logger=logger)

        await cancel_task(ms.active_turn_task, "turn", error_logger=logger)

        if ms.session_id and self.on_session_cleanup:
            try:
                self.on_session_cleanup(ms.session_id)
            except Exception as e:
                logger.error("Error in session cleanup callback: %s", e)

        await ms.reject_all_pending_permissions("Session orphaned")

        if ms.acp:
            try:
                await ms.acp.stop()
            except Exception as e:
                logger.error("Error stopping ACP: %s", e)
            ms.acp = None

        if ms.session_id:
            self.unregister(ms, ms.session_id)

    async def shutdown_all(self) -> None:
        """Tear down every managed session (application shutdown / tests)."""
        async with self._lock:
            sessions = list(self._sessions.values())
        for ms in sessions:
            await self.cancel_orphan(ms)


session_manager = SessionManager()
