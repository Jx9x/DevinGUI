"""Live ACP notification delivery, tool snapshots, and reconnect buffering."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, Awaitable, Callable

from .acp_updates import TEXT_CHUNK_UPDATES, normalize_session_update

if TYPE_CHECKING:
    from .session_manager import ManagedSession


_MAX_TOOL_CALL_SNAPSHOTS = 256


class SessionNotifications:
    """Deliver ACP events using the session and socket currently attached.

    State remains on ManagedSession. Activity and raw tool snapshots precede
    normalization; delta buffering depends on whether the socket accepted a
    frame. Resolve the session again after sends to preserve handoff behavior.
    """

    def __init__(
        self,
        *,
        current_session: Callable[[], ManagedSession],
        resume_live_updates: Callable[[str | None], None],
        normalize_update: Callable[[dict], dict | None] = normalize_session_update,
    ) -> None:
        self.current_session = current_session
        self.resume_live_updates = resume_live_updates
        self.normalize_update = normalize_update

    async def send(self, msg: dict) -> bool:
        """Return whether the attached socket accepted the frame.

        Missing socket or send failure: False. Do not replay accepted text deltas.
        """
        sock = self.current_session().websocket
        if sock is None:
            return False
        try:
            await sock.send_json(msg)
            return True
        except Exception:
            return False

    async def handle_notification(self, msg: dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "session/update":
            ms = self.current_session()
            ms.last_acp_update_at = asyncio.get_running_loop().time()
            self.resume_live_updates(params.get("sessionId"))
            # Devin's ACP uses `sessionUpdate` with nested content objects,
            # while the frontend validates against a flat `type`/`chunk`
            # schema and discards any frame that fails validation. Normalize
            # to that schema and drop update kinds the UI cannot render.
            raw_update = params.get("update")
            if isinstance(raw_update, dict) and raw_update.get("sessionUpdate") in (
                "tool_call",
                "tool_call_update",
            ):
                tc_id = raw_update.get("toolCallId")
                if isinstance(tc_id, str) and tc_id:
                    snapshots = self.current_session().tool_call_snapshots
                    if tc_id in snapshots:
                        merged = dict(snapshots[tc_id])
                        merged.update(raw_update)
                        snapshots[tc_id] = merged
                    else:
                        if len(snapshots) >= _MAX_TOOL_CALL_SNAPSHOTS:
                            snapshots.pop(next(iter(snapshots)))
                        snapshots[tc_id] = raw_update
            update = self.normalize_update(params.get("update", {}))
            if update is None:
                return
            buffered_update = {
                "sessionId": params.get("sessionId"),
                "update": update,
            }
            # Tool/status/usage frames are idempotent and safe to keep as the
            # latest reconnect state. Text/thought frames are deltas: buffer
            # them only if they could not be delivered. Replaying a delta the
            # browser already accepted is a direct source of doubled output.
            if update.get("type") not in TEXT_CHUNK_UPDATES:
                self.current_session().last_session_update = buffered_update
            delivered = await self.send(
                {
                    "type": "session_update",
                    "sessionId": params.get("sessionId"),
                    "update": update,
                }
            )
            if update.get("type") in TEXT_CHUNK_UPDATES:
                self.current_session().last_session_update = None if delivered else buffered_update


async def replay_session_update(
    session: ManagedSession,
    send: Callable[[dict], Awaitable[bool]],
) -> None:
    """Replay the captured session's buffer and consume accepted text deltas."""
    if session.last_session_update is not None:
        buffered = session.last_session_update
        replayed = await send(
            {
                "type": "session_update",
                "sessionId": buffered.get("sessionId"),
                "update": buffered.get("update"),
            }
        )
        # A buffered text/thought frame is a one-shot missed
        # delta. Once replayed successfully, retaining it would
        # append the same delta again on another reconnect.
        # Clear only the captured buffer; a newer update may arrive during send.
        if (
            replayed
            and isinstance(buffered.get("update"), dict)
            and buffered["update"].get("type") in TEXT_CHUNK_UPDATES
            and session.last_session_update is buffered
        ):
            session.last_session_update = None


async def replay_pending_permissions(
    session: ManagedSession,
    send: Callable[[dict], Awaitable[bool]],
) -> None:
    """Replay retained ``request_permission`` frames for still-pending entries."""
    for tool_call_id, pending in list(session.permission_requests.items()):
        # Earlier sends may have resolved or replaced entries in this snapshot.
        if session.permission_requests.get(tool_call_id) is not pending:
            continue
        payload = pending.get("display_payload")
        if not isinstance(payload, dict):
            continue
        if not await send(deepcopy(payload)):
            return
