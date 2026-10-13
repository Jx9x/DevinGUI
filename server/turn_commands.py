"""Browser prompt preparation and cancellation, separate from task dispatch."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from .session_manager import cancel_task

if TYPE_CHECKING:
    from .db_reader import DBReader
    from .model_registry import ModelRegistry
    from .session_manager import ManagedSession
    from .session_transitions import SessionTransitionCoordinator


@dataclass(frozen=True)
class PreparedPrompt:
    """Resolved session and original prompt fields, without new payload validation.

    Prompt content and correlation IDs retain their permissive wire values.
    ACP content validation belongs to the existing client path.
    """

    session_id: str
    text: Any
    images: Any
    request_id: Any


def _command_error(
    operation: Literal["prompt", "cancel"], session_id: str | None,
    request_id: Any, message: str,
) -> dict:
    return {
        "type": "error",
        "sessionId": session_id,
        "requestId": request_id,
        "operation": operation,
        "message": message,
    }


class TurnCommands:
    """Prepare and cancel using the receive loop's session snapshot and socket.

    Preparation returns before the endpoint advances generations or schedules a
    turn. Model/history services are resolved through getters. Replies go directly
    to the receiving socket; cancellation retains its best-effort send boundaries.
    """

    def __init__(
        self,
        *,
        reply: Callable[[dict], Awaitable[None]],
        get_db_reader: Callable[[], DBReader],
        get_model_registry: Callable[[], ModelRegistry],
        compaction_thresholds: Callable[[str | None, str | None], tuple[int, int, int] | None],
        transitions: SessionTransitionCoordinator,
        logger: logging.Logger,
    ) -> None:
        self.reply = reply
        self.get_db_reader = get_db_reader
        self.get_model_registry = get_model_registry
        self.compaction_thresholds = compaction_thresholds
        self.transitions = transitions
        self.logger = logger

    async def prepare_prompt(self, ms: ManagedSession, data: dict) -> PreparedPrompt | None:
        """Reject unsafe starts, confirm the model, and return fields for scheduling."""
        request_id = data.get("requestId")
        session_id = data.get("sessionId")
        # Reject early prompts without cancelling the transition: cancellation
        # can tear down ACP and cause a misleading "No active ACP session" error.
        # The client can retry once the transition finishes.
        if ms.active_transition_task and not ms.active_transition_task.done():
            await self.reply(
                _command_error(
                    "prompt", session_id if session_id is not None else ms.session_id,
                    request_id, "Session is still resuming.",
                )
            )
            return None
        # Reject stale session IDs before changing state. An omitted sessionId
        # targets the active session.
        if (
            session_id is not None
            and ms.session_id is not None
            and session_id != ms.session_id
        ):
            await self.reply(
                _command_error(
                    "prompt", session_id, request_id,
                    "Session id does not match the active backend session.",
                )
            )
            return None
        if session_id is None:
            session_id = ms.session_id
        if not ms.acp:
            await self.reply(
                _command_error("prompt", ms.session_id, request_id, "No active ACP session")
            )
            return None
        text = data.get("text", "")
        images = data.get("images") or []
        if not session_id:
            await self.reply(
                _command_error("prompt", session_id, request_id, "No session ID")
            )
            return None
        # The model is ACP session state, not prompt content. Reassert this
        # prompt's selection so a stale session model cannot bill the wrong quota.
        # set_session_model confirms the model, thinking level, and speed before
        # a turn starts, catching silent fallbacks.
        requested_model = data.get("model") or ms.model
        if requested_model:
            model_changed = requested_model != ms.model
            try:
                history_error = None
                try:
                    session_info = self.get_db_reader().get_session(session_id)
                except (OSError, sqlite3.DatabaseError) as error:
                    session_info = None
                    history_error = error
                    self.logger.warning("History unavailable for live session %s: %s", session_id, error)
                cwd = getattr(ms.acp, "cwd", None) or (
                    (session_info or {}).get("working_directory") or os.getcwd()
                )
                needs_compaction_restart = (
                    model_changed
                    and self.compaction_thresholds(cwd, requested_model)
                    != getattr(ms.acp, "compaction_thresholds", None)
                )
                if needs_compaction_restart and history_error is not None:
                    raise RuntimeError("Cannot restart for changed compaction settings while history is unavailable. Retry Diagnostics or start a new session.")
                # session/load needs a persisted row; session/new has none until
                # the first turn. Restarting would lose the only live session.
                # Apply the model in place, keeping spawn-time compaction flags
                # until a later resume.
                if needs_compaction_restart and session_info is None:
                    self.logger.info(
                        "Session %s is not persisted yet; applying model %s without a compaction restart",
                        session_id, requested_model,
                    )
                elif needs_compaction_restart:
                    await self.transitions.load_history(
                        session_id, requested_model, ms.permission_mode, None, ms.generation_id,
                        notify_client=False,
                    )
                    if not ms.acp or ms.session_id != session_id:
                        raise RuntimeError("Could not resume the session with the selected model's compaction settings")
                ms.model = await ms.acp.set_session_model(session_id, requested_model)
            except Exception as e:
                self.logger.error(
                    "Refusing prompt for session %s: could not apply model %s: %s",
                    session_id, requested_model, e,
                )
                await self.reply(
                    _command_error(
                        "prompt", session_id, request_id,
                        (
                            f"Prompt not sent: could not switch the Devin CLI to "
                            f"'{requested_model}' ({e})."
                        ),
                    )
                )
                return None
            if model_changed:
                # A mid-session switch also becomes the default for
                # the next new session, matching the transition paths.
                self.get_model_registry().set_last_used_model(ms.model)
        return PreparedPrompt(session_id, text, images, request_id)

    async def cancel(self, ms: ManagedSession, data: dict) -> None:
        """Cancel the captured session's operation and preserve acknowledgment policy."""
        request_id = data.get("requestId")
        session_id = data.get("sessionId")
        # Reject stale session IDs before cancelling. An omitted sessionId
        # targets the active session. Without an active ID, allow cancellation
        # so an in-flight transition can still be interrupted.
        if (
            session_id is not None
            and ms.session_id is not None
            and session_id != ms.session_id
        ):
            await self.reply(
                _command_error(
                    "cancel", session_id, request_id,
                    "Session id does not match the active backend session.",
                )
            )
            return
        if session_id is None:
            session_id = ms.session_id
        # Capture the operation before cleanup. turn_cancelled makes the frontend
        # ready for prompts; transition_cancelled leaves it read-only because
        # ACP may no longer be loaded.
        transition_was_active = bool(
            ms.active_transition_task
            and not ms.active_transition_task.done()
        )
        turn_was_active = bool(
            ms.active_turn_task and not ms.active_turn_task.done()
        )
        # Cancel any in-flight transition as well as the turn.
        # Transitions and turns are mutually exclusive; a cancel
        # stops whichever is active.
        await cancel_task(ms.active_transition_task, "transition", error_logger=self.logger)
        await cancel_task(ms.active_turn_task, "turn", error_logger=self.logger)
        if ms.acp:
            await ms.reject_all_pending_permissions("Turn cancelled")
            if session_id:
                try:
                    await ms.acp.cancel(session_id)
                except Exception as e:
                    self.logger.error("Cancel failed: %s", e)
                    # ACP cancellation failed: surface a correlated
                    # error so the frontend can leave the
                    # `cancelling` state instead of hanging.
                    try:
                        await self.reply(
                            _command_error("cancel", session_id, request_id, str(e))
                        )
                    except Exception as send_err:
                        self.logger.error(
                            "Failed to send cancel error: %s", send_err
                        )
                    return
        # With a session ID, acknowledge even an idle cancellation so the UI
        # leaves `cancelling`. Without an ID, log and send no acknowledgment.
        # A transition-only cancellation must not imply ACP is ready.
        if transition_was_active and not turn_was_active:
            if session_id:
                try:
                    await self.reply(
                        {
                            "type": "transition_cancelled",
                            "sessionId": session_id,
                            "requestId": request_id or "",
                        }
                    )
                except Exception as send_err:
                    self.logger.error(
                        "Failed to send transition_cancelled: %s",
                        send_err,
                    )
            else:
                self.logger.warning(
                    "cancel handler skipped transition_cancelled: "
                    "no sessionId"
                )
        elif session_id:
            try:
                await self.reply(
                    {
                        "type": "turn_cancelled",
                        "sessionId": session_id,
                        "requestId": request_id or "",
                    }
                )
            except Exception as send_err:
                self.logger.error(
                    "Failed to send turn_cancelled: %s", send_err
                )
        else:
            self.logger.warning(
                "cancel handler skipped turn_cancelled: no sessionId"
            )
