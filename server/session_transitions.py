"""Session startup, persisted history, and live reconnect handoffs.

The endpoint schedules transitions and advances generations. This coordinator
owns locked transitions, startup configuration, and generation/identity cleanup;
ManagedSession keeps process/task state so it survives reconnects.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Awaitable, Callable

from .rest_errors import history_error_message
from .session_manager import cancel_task
from .session_notifications import replay_pending_permissions, replay_session_update

if TYPE_CHECKING:
    from fastapi import WebSocket

    from .acp_client import ACPClient
    from .db_reader import DBReader, DBWatcher
    from .model_registry import ModelRegistry
    from .session_manager import ManagedSession, SessionManager
    from .transcript_watcher import TranscriptWatcher


class SessionTransitionCoordinator:
    """Run transitions while callbacks follow the endpoint's current session.

    Service getters retain request-time dependency resolution. The ACP factory
    belongs to the endpoint, which wires notification and permission callbacks.
    Cost and reasoning callbacks share the existing history assembly paths.
    """

    def __init__(
        self,
        *,
        current_session: Callable[[], ManagedSession],
        replace_session: Callable[[ManagedSession], None],
        websocket: WebSocket,
        create_acp: Callable[[str, str | None, str | None], ACPClient],
        send: Callable[[dict], Awaitable[bool]],
        get_db_reader: Callable[[], DBReader],
        get_db_watcher: Callable[[], DBWatcher],
        get_transcript_watcher: Callable[[], TranscriptWatcher],
        get_session_manager: Callable[[], SessionManager],
        get_model_registry: Callable[[], ModelRegistry],
        reasoning_steps: Callable[[str], list[dict]],
        send_cost_update: Callable[[str], Awaitable[None]],
        logger: logging.Logger,
    ) -> None:
        self.current_session = current_session
        self.replace_session = replace_session
        self.websocket = websocket
        self.create_acp = create_acp
        self.send = send
        self.get_db_reader = get_db_reader
        self.get_db_watcher = get_db_watcher
        self.get_transcript_watcher = get_transcript_watcher
        self.get_session_manager = get_session_manager
        self.get_model_registry = get_model_registry
        self.reasoning_steps = reasoning_steps
        self.send_cost_update = send_cost_update
        self.logger = logger

    async def _cleanup_startup_acp(
        self,
        ms: ManagedSession,
        acp_client: ACPClient | None,
        registered_sid: str | None,
    ) -> None:
        """Stop partial startup without clearing a newer session's state."""
        if acp_client is not None:
            acp_client.suppress_updates = False
            try:
                await acp_client.stop()
            except Exception:
                pass
        if ms.acp is acp_client:
            ms.acp = None
            if registered_sid is not None:
                self.get_session_manager().unregister(ms, registered_sid)
                if ms.session_id == registered_sid:
                    ms.session_id = None

    def _finish_transition(self, current_task: asyncio.Task | None, gen_id: int) -> None:
        """Release only the task reference still owned by this generation."""
        ms = self.current_session()
        if ms.active_transition_task is current_task and gen_id == ms.generation_id:
            ms.active_transition_task = None

    async def _release_session(self, ms: ManagedSession, reason: str) -> None:
        """Release the old session before a transition assigns its next state.

        The caller holds ``transition_lock`` and handles generation checks
        and the next session ID. Orphan teardown has a different cleanup
        order and remains owned by the session manager.
        """
        await cancel_task(ms.active_turn_task, "turn", error_logger=self.logger)
        old_session_id = ms.session_id
        if ms.acp:
            await ms.reject_all_pending_permissions(reason)
            await ms.acp.stop()
            ms.acp = None
        ms.permission_requests.clear()
        if old_session_id:
            self.get_transcript_watcher().stop_watching(old_session_id)
            self.get_db_watcher().stop_watching(old_session_id)
            # Remove the old managed session from the registry so
            # a reconnecting client does not find a stale entry.
            self.get_session_manager().unregister(ms, old_session_id)

    async def _configure_acp_session(
        self,
        acp_client: ACPClient, session_id: str,
        model: str | None, permission_mode: str | None,
    ) -> None:
        """Require the selected model and apply bypass mode on a best-effort basis.

        Configure before registering the session so a failed model selection
        leaves no ready managed session that could run on the wrong model.
        """
        # The CLI's --model flag alone cannot confirm the session's model,
        # thinking level, or speed. Verify the catalog variant through ACP
        # controls to avoid running on a different model or billing another quota.
        if model:
            await acp_client.set_session_model(session_id, model)
        # ACP ignores --permission-mode; set the per-session "mode" option.
        # Failure is non-fatal: the session remains usable but keeps prompting.
        if permission_mode == "bypass":
            try:
                await acp_client.set_session_mode(session_id, "bypass")
            except Exception as e:
                self.logger.warning(
                    "Could not enable bypass mode for session %s: %s",
                    session_id, e,
                )

    def _rendered_history(self, session_id: str) -> list[dict]:
        try:
            return self.get_db_reader().get_rendered_commits(session_id)
        except (OSError, sqlite3.DatabaseError) as error:
            self.logger.warning("Optional rendered history unavailable for %s: %s", session_id, error)
            return []

    async def _start_acp_from_history(self, acp_client: ACPClient, session_id: str, cwd: str) -> None:
        """Start ACP and load persisted history without replaying it to the browser."""
        await acp_client.start()
        await acp_client.initialize()
        # History comes from the DB or frontend, not ACP replay.
        # Reset suppression in finally so cancellation cannot leave it enabled.
        acp_client.suppress_updates = True
        try:
            await acp_client.load_session(session_id, cwd)
        finally:
            acp_client.suppress_updates = False

    async def new_session(
        self,
        cwd: str, model: str | None, permission_mode: str | None, request_id: str | None, gen_id: int,
    ) -> None:
        """Create a session outside the receive loop so it stays responsive.

        Live watchers start only when a prompt begins. A newer transition
        cancels this task; prompts are rejected while it runs. The ``gen_id``
        guard suppresses stale messages after supersession.
        """
        ms = self.current_session()
        current_task = asyncio.current_task()
        local_acp: ACPClient | None = None
        registered_sid: str | None = None
        try:
            async with ms.transition_lock:
                # Superseded before we even acquired the lock.
                if gen_id != ms.generation_id:
                    return
                await self._release_session(ms, "Session replaced")
                ms.session_id = None

                local_acp = self.create_acp(cwd, model, permission_mode)
                ms.acp = local_acp
                ms.model = model
                ms.permission_mode = permission_mode
                try:
                    await local_acp.start()
                    await local_acp.initialize()
                    session_id = await local_acp.new_session(cwd)
                    # A later transition superseded this creation; abandon
                    # the result so it cannot overwrite current state.
                    if gen_id != ms.generation_id:
                        try:
                            await local_acp.stop()
                        except Exception:
                            pass
                        if ms.acp is local_acp:
                            ms.acp = None
                        return
                    await self._configure_acp_session(
                        local_acp, session_id, model, permission_mode,
                    )
                    ms.session_id = session_id
                    self.get_session_manager().register(ms, session_id)
                    registered_sid = session_id
                    if model:
                        self.get_model_registry().set_last_used_model(model)
                    if gen_id != ms.generation_id:
                        return
                    await self.send(
                        {
                            "type": "session_created",
                            "sessionId": session_id,
                            "model": model,
                            "requestId": request_id,
                        }
                    )
                except Exception as e:
                    self.logger.error("Failed to create session: %s", e)
                    await self._cleanup_startup_acp(ms, local_acp, registered_sid)
                    if gen_id != ms.generation_id:
                        return
                    await self.send(
                        {
                            "type": "error",
                            "sessionId": ms.session_id,
                            "requestId": request_id,
                            "operation": "new_session",
                            "message": history_error_message(e),
                        }
                    )
        except asyncio.CancelledError:
            await self._cleanup_startup_acp(ms, local_acp, registered_sid)
            raise
        finally:
            self._finish_transition(current_task, gen_id)

    async def view_history(
        self,
        sid: str, request_id: str | None, gen_id: int,
    ) -> None:
        ms = self.current_session()
        current_task = asyncio.current_task()
        try:
            async with ms.transition_lock:
                if gen_id != ms.generation_id:
                    return
                # Check before the teardown below so a bad id cannot take
                # down the live session.
                if self.get_db_reader().get_session(sid) is None:
                    raise ValueError(f"Session not found: {sid}")
                await self._release_session(ms, "Session changed")
                ms.session_id = sid
                ms.model = None
                ms.permission_mode = None
                messages = self.get_db_reader().get_messages(sid)
                rendered = self._rendered_history(sid)
                reasoning = self.reasoning_steps(sid)
                if gen_id != ms.generation_id:
                    return
                await self.send(
                    {
                        "type": "history_loaded",
                        "sessionId": sid,
                        "messages": messages,
                        "rendered": rendered,
                        "reasoning": reasoning,
                        "readOnly": True,
                        "requestId": request_id,
                    }
                )
                await self.send_cost_update(sid)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error("Failed to view session %s: %s", sid, e)
            if gen_id == ms.generation_id:
                await self.send(
                    {
                        "type": "error",
                        "sessionId": sid,
                        "requestId": request_id,
                        "operation": "load_history",
                        "message": history_error_message(e),
                    }
                )
        finally:
            self._finish_transition(current_task, gen_id)

    async def load_history(
        self,
        sid: str, requested_model: str | None, permission_mode: str | None, request_id: str | None, gen_id: int,
        notify_client: bool = True,
    ) -> None:
        """Resume ACP before delivering DB history outside the receive loop.

        A nonempty ``requested_model`` overrides the stored model.
        On startup failure, try to deliver read-only history and ``resume_failed``.
        With ``notify_client=False``, send no frames and propagate startup
        exceptions to the caller, which uses this for compaction restarts.
        """
        ms = self.current_session()
        current_task = asyncio.current_task()
        local_acp: ACPClient | None = None
        registered_sid: str | None = None
        history_delivered = False
        try:
            async with ms.transition_lock:
                if gen_id != ms.generation_id:
                    return
                # ACP can only load persisted sessions. Check before teardown
                # so a missing ID does not destroy the live session.
                if self.get_db_reader().get_session(sid) is None:
                    self.logger.error("Failed to resume session %s: not in the session store", sid)
                    if notify_client and gen_id == ms.generation_id:
                        await self.send(
                            {
                                "type": "history_loaded",
                                "sessionId": sid,
                                "messages": [],
                                "rendered": [],
                                "reasoning": [],
                                "requestId": request_id,
                            }
                        )
                        await self.send(
                            {
                                "type": "resume_failed",
                                "sessionId": sid,
                                "message": f"Session not found: {sid}",
                                "requestId": request_id,
                            }
                        )
                    return
                await self._release_session(ms, "Session changed")
                ms.session_id = None

                session_info = self.get_db_reader().get_session(sid)
                cwd = (session_info or {}).get("working_directory") or os.getcwd()
                model = requested_model or (session_info or {}).get("model")

                local_acp = self.create_acp(cwd, model, permission_mode)
                ms.acp = local_acp
                ms.model = model
                ms.permission_mode = permission_mode
                try:
                    await self._start_acp_from_history(local_acp, sid, cwd)
                    # A newer transition superseded this resume; abandon
                    # the result so it cannot overwrite current state.
                    if gen_id != ms.generation_id:
                        try:
                            await local_acp.stop()
                        except Exception:
                            pass
                        if ms.acp is local_acp:
                            ms.acp = None
                        return
                    await self._configure_acp_session(
                        local_acp, sid, model, permission_mode,
                    )
                    ms.session_id = sid
                    self.get_session_manager().register(ms, sid)
                    registered_sid = sid
                    # Keep last_used_model in sync with the user's explicit
                    # selection so subsequent new sessions default to it.
                    if requested_model:
                        self.get_model_registry().set_last_used_model(requested_model)
                    # Live DB/transcript watchers are intentionally started
                    # only when a prompt begins, so an idle selected session
                    # performs no recurring filesystem/SQLite polling.
                    if gen_id != ms.generation_id:
                        return
                    if not notify_client:
                        return
                    messages = self.get_db_reader().get_messages(sid)
                    rendered = self._rendered_history(sid)
                    reasoning = self.reasoning_steps(sid)
                    history_delivered = bool(await self.send(
                        {
                            "type": "history_loaded",
                            "sessionId": sid,
                            "messages": messages,
                            "rendered": rendered,
                            "reasoning": reasoning,
                            "requestId": request_id,
                        }
                    ))
                    await self.send_cost_update(sid)
                    if gen_id != ms.generation_id:
                        return
                    await self.send(
                        {"type": "session_resumed", "sessionId": sid, "requestId": request_id}
                    )
                except Exception as e:
                    await self._cleanup_startup_acp(ms, local_acp, registered_sid)
                    self.logger.error("Failed to resume session %s: %s", sid, e)
                    if gen_id != ms.generation_id:
                        return
                    if not notify_client:
                        raise
                    if not history_delivered:
                        # Keep the conversation visible in read-only mode when
                        # ACP resume fails, rather than leaving an empty chat.
                        try:
                            messages = self.get_db_reader().get_messages(sid)
                            rendered = self._rendered_history(sid)
                            reasoning = self.reasoning_steps(sid)
                            await self.send(
                                {
                                    "type": "history_loaded",
                                    "sessionId": sid,
                                    "messages": messages,
                                    "rendered": rendered,
                                    "reasoning": reasoning,
                                    "requestId": request_id,
                                }
                            )
                        except Exception as fallback_error:
                            self.logger.warning(
                                "Could not send fallback history for %s: %s", sid, fallback_error
                            )
                    try:
                        await self.send_cost_update(sid)
                    except Exception as cost_error:
                        self.logger.warning(
                            "Could not send fallback cost for %s: %s", sid, cost_error
                        )
                    if gen_id != ms.generation_id:
                        return
                    await self.send(
                        {
                            "type": "resume_failed",
                            "sessionId": sid,
                            "message": history_error_message(e),
                            "requestId": request_id,
                        }
                    )
        except asyncio.CancelledError:
            await self._cleanup_startup_acp(ms, local_acp, registered_sid)
            raise
        except (OSError, sqlite3.DatabaseError) as error:
            if not notify_client:
                raise
            if gen_id == ms.generation_id:
                await self.send({"type": "resume_failed", "sessionId": sid,
                                 "requestId": request_id, "message": history_error_message(error)})
        finally:
            self._finish_transition(current_task, gen_id)

    async def resume_session(
        self,
        sid: str, requested_model: str | None, permission_mode: str | None, request_id: str | None, gen_id: int,
    ) -> None:
        """Reconnect outside the receive loop without resending frontend history.

        Live managed session: rebind the socket and preserve any running turn,
        model, and permission mode. Ignore the requested configuration.
        Otherwise, start ACP from persisted history. A nonempty
        ``requested_model`` overrides the stored model on this fallback path.
        """
        ms = self.current_session()
        current_task = asyncio.current_task()
        local_acp: ACPClient | None = None
        registered_sid: str | None = None
        try:
            async with ms.transition_lock:
                if gen_id != ms.generation_id:
                    return

                existing = self.get_session_manager().get(sid)
                if existing is not None and existing is not ms:
                    # Cancel the current session's turn, not the surviving session's.
                    await cancel_task(ms.active_turn_task, "turn", error_logger=self.logger)
                    if ms.acp:
                        await ms.reject_all_pending_permissions("Session resumed")
                        await ms.acp.stop()
                        ms.acp = None
                    ms.permission_requests.clear()

                    existing.websocket = self.websocket
                    if existing.orphan_timer and not existing.orphan_timer.done():
                        existing.orphan_timer.cancel()
                        existing.orphan_timer = None
                    # Switch the endpoint binding so all subsequent callbacks
                    # operate on the surviving managed session.
                    self.replace_session(existing)
                    # gen_id belongs to ms, not the surviving session's independent
                    # generation counter. Rebinding its socket is not a transition
                    # on that session, so do not compare gen_id against it.
                    await self.send(
                        {"type": "session_resumed", "sessionId": sid, "requestId": request_id}
                    )
                    await replay_session_update(existing, self.send)
                    await replay_pending_permissions(existing, self.send)
                    return

                # Without a handoff, ACP needs persisted history. Check before
                # teardown so a missing ID does not destroy the live session.
                if self.get_db_reader().get_session(sid) is None:
                    self.logger.error("Failed to resume session %s: not in the session store", sid)
                    if gen_id == ms.generation_id:
                        await self.send(
                            {
                                "type": "resume_failed",
                                "sessionId": sid,
                                "message": f"Session not found: {sid}",
                                "requestId": request_id,
                            }
                        )
                    return
                await self._release_session(ms, "Session resumed")
                ms.session_id = None

                session_info = self.get_db_reader().get_session(sid)
                cwd = (session_info or {}).get("working_directory") or os.getcwd()
                model = requested_model or (session_info or {}).get("model")

                local_acp = self.create_acp(cwd, model, permission_mode)
                ms.acp = local_acp
                ms.model = model
                ms.permission_mode = permission_mode
                try:
                    await self._start_acp_from_history(local_acp, sid, cwd)
                    if gen_id != ms.generation_id:
                        try:
                            await local_acp.stop()
                        except Exception:
                            pass
                        if ms.acp is local_acp:
                            ms.acp = None
                        return
                    await self._configure_acp_session(
                        local_acp, sid, model, permission_mode,
                    )
                    ms.session_id = sid
                    self.get_session_manager().register(ms, sid)
                    registered_sid = sid
                    if requested_model:
                        self.get_model_registry().set_last_used_model(requested_model)
                    if gen_id != ms.generation_id:
                        return
                    await self.send(
                        {"type": "session_resumed", "sessionId": sid, "requestId": request_id}
                    )
                except Exception as e:
                    await self._cleanup_startup_acp(ms, local_acp, registered_sid)
                    self.logger.error("Failed to resume session %s: %s", sid, e)
                    if gen_id != ms.generation_id:
                        return
                    await self.send(
                        {
                            "type": "resume_failed",
                            "sessionId": sid,
                            "message": history_error_message(e),
                            "requestId": request_id,
                        }
                    )
        except asyncio.CancelledError:
            await self._cleanup_startup_acp(ms, local_acp, registered_sid)
            raise
        except (OSError, sqlite3.DatabaseError) as error:
            if gen_id == ms.generation_id:
                await self.send({"type": "resume_failed", "sessionId": sid,
                                 "requestId": request_id, "message": history_error_message(error)})
        finally:
            self._finish_transition(current_task, gen_id)
