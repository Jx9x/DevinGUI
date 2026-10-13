"""ACP permissions, browser decisions, and expiry across session reconnects."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .acp_updates import permission_detail, select_permission_option

if TYPE_CHECKING:
    from .session_manager import ManagedSession


def _response_error(session_id: str | None, correlation_id: Any, message: str) -> dict:
    return {
        "type": "error",
        "sessionId": session_id,
        "requestId": correlation_id,
        "operation": "permission_response",
        "message": message,
    }


class PermissionCoordinator:
    """Handle permissions while ManagedSession owns pending requests and tasks.

    ACP callbacks and timeouts resolve the current session and attached socket.
    Browser responses use the receive loop's session snapshot and reply directly
    on its socket, preserving delivery errors and browser/ACP correlation IDs.
    """

    def __init__(
        self,
        *,
        current_session: Callable[[], ManagedSession],
        send: Callable[[dict], Awaitable[bool]],
        timeout_seconds: Callable[[], float],
        logger: logging.Logger,
    ) -> None:
        self.current_session = current_session
        self.send = send
        self.timeout_seconds = timeout_seconds
        self.logger = logger

    async def handle_request(self, msg: dict) -> None:
        ms = self.current_session()
        ms.last_acp_update_at = asyncio.get_running_loop().time()
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method != "session/request_permission":
            request_id = msg.get("id")
            if request_id is not None and ms.acp is not None:
                try:
                    await ms.acp.send_response(
                        request_id,
                        error={"code": -32601, "message": f"Method not supported: {method}"},
                    )
                except Exception as e:
                    self.logger.error("Failed to reject unsupported ACP request %s: %s", method, e)
            self.logger.warning("Ignoring unexpected ACP request: %s", method)
            return
        tool_call = (params.get("toolCall") or {}) if isinstance(params, dict) else {}
        tool_call_id = tool_call.get("toolCallId")
        request_id = msg.get("id")
        options = params.get("options", []) if isinstance(params, dict) else []
        if not tool_call_id or request_id is None:
            # Reject the malformed ACP request immediately if we have an id,
            # so the agent does not wait forever for a response.
            if request_id is not None and ms.acp is not None:
                try:
                    await ms.acp.send_response(
                        request_id,
                        error={
                            "code": -32602,
                            "message": "Permission request missing toolCallId or id",
                        },
                    )
                except Exception as e:
                    self.logger.error("Failed to reject malformed permission request: %s", e)
            # Do not show an unanswerable permission panel; surface an error instead.
            self.logger.warning("Permission request missing toolCallId or id: %s", msg)
            await self.send(
                {
                    "type": "error",
                    "sessionId": params.get("sessionId") if isinstance(params, dict) else None,
                    "operation": "permission",
                    "message": "Permission request missing toolCallId or id",
                }
            )
            return
        old = ms.permission_requests.get(tool_call_id)
        if old:
            timeout_task = old.get("timeout_task")
            if timeout_task and not timeout_task.done():
                timeout_task.cancel()
                try:
                    await timeout_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        timeout_task = asyncio.create_task(self._permission_timeout(tool_call_id, request_id))
        ms.permission_requests[tool_call_id] = {
            "request_id": request_id,
            "options": options,
            "timeout_task": timeout_task,
        }
        data = dict(params) if isinstance(params, dict) else params
        if isinstance(data, dict) and tool_call_id:
            data["toolCallId"] = tool_call_id
        if isinstance(data, dict):
            detail = permission_detail(data.get("toolCall"))
            if detail is None and isinstance(params, dict):
                meta = params.get("_meta")
                if isinstance(meta, dict):
                    candidate = meta.get("cognition.ai/command")
                    if isinstance(candidate, str) and candidate.strip():
                        detail = candidate
            if detail is None:
                detail = permission_detail(ms.tool_call_snapshots.get(tool_call_id))
            if detail:
                data["detail"] = detail
        frame = {
            "type": "request_permission",
            "sessionId": params.get("sessionId"),
            "requestId": request_id,
            "toolCallId": tool_call_id,
            "data": data,
        }
        # Retain an isolated replay frame before send can mutate nested payloads.
        ms.permission_requests[tool_call_id]["display_payload"] = deepcopy(frame)
        await self.send(frame)

    async def _permission_timeout(self, tool_call_id: str, request_id: Any) -> None:
        await asyncio.sleep(self.timeout_seconds())
        ms = self.current_session()
        pending = ms.permission_requests.get(tool_call_id)
        # A replacement for the same tool call must not expire on this deadline.
        if pending is None or pending["request_id"] != request_id:
            return
        ms.permission_requests.pop(tool_call_id, None)
        await ms.reject_permission(tool_call_id, request_id, "Permission request timed out")
        # Notify the frontend so the permission panel can be cleared and
        # the user sees an error instead of an actionable panel.
        msg: dict[str, Any] = {
            "type": "permission_expired",
            "toolCallId": tool_call_id,
            "requestId": request_id,
        }
        if ms.session_id is not None:
            msg["sessionId"] = ms.session_id
        await self.send(msg)

    async def handle_acp_unavailable(self, reason: str) -> None:
        """Reject pending permissions and report an unexpected ACP exit to the browser."""
        self.logger.error("ACP session became unavailable: %s", reason)
        ms = self.current_session()
        await ms.reject_all_pending_permissions(reason)
        await self.send(
            {
                "type": "error",
                "sessionId": ms.session_id,
                "operation": "acp_unavailable",
                "message": reason,
            }
        )

    async def handle_response(
        self,
        ms: ManagedSession,
        data: dict,
        reply: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Resolve a browser decision using the receive loop's session and socket."""
        session_id = data.get("sessionId")
        tool_call_id = data.get("toolCallId")
        # Browser requestId correlates errors and acknowledgments; pending["request_id"]
        # is the separate ACP JSON-RPC response ID.
        correlation_id = data.get("requestId")
        # Reject stale session IDs before consuming permissions. An omitted
        # sessionId targets the active session; without an active ID, skip the check.
        if (
            session_id is not None
            and ms.session_id is not None
            and session_id != ms.session_id
        ):
            await reply(
                _response_error(session_id, correlation_id, "Session id does not match the active backend session.")
            )
            return
        if session_id is None:
            session_id = ms.session_id
        decision = data.get("decision")
        if not ms.acp:
            await reply(
                _response_error(ms.session_id, correlation_id, "No active ACP session")
            )
            return
        if not decision:
            await reply(
                _response_error(session_id, correlation_id, "No decision provided")
            )
            return

        # Resolve only by toolCallId. Browser and ACP request IDs are separate
        # namespaces; cross-matching them could resolve an unrelated request.
        if not tool_call_id:
            await reply(
                _response_error(session_id, correlation_id, "Permission response missing toolCallId.")
            )
            return

        pending = None
        if tool_call_id in ms.permission_requests:
            pending = ms.permission_requests.pop(tool_call_id)

        if pending is None:
            # Late, duplicate, or expired response. Surface a
            # correlated error instead of silently dropping it so
            # the UI is not stuck in `submitting`.
            await reply(
                _response_error(session_id, correlation_id, "Permission request is no longer pending.")
            )
            return

        timeout_task = pending.get("timeout_task")
        if timeout_task and not timeout_task.done():
            timeout_task.cancel()
            try:
                await timeout_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        request_id = pending["request_id"]
        options = pending["options"]
        requested_option_id = data.get("optionId")
        option_ids = {option.get("optionId") for option in options if isinstance(option, dict)}
        option_id = requested_option_id if requested_option_id in option_ids else select_permission_option(decision, options)
        try:
            await ms.acp.send_response(
                request_id,
                {"outcome": {"outcome": "selected", "optionId": option_id or decision}},
            )
            # The UI clears the panel on this acknowledgment. requestId echoes the
            # browser correlation ID; acpRequestId identifies the ACP request.
            await reply(
                {
                    "type": "permission_received",
                    "sessionId": session_id,
                    "toolCallId": tool_call_id,
                    "requestId": correlation_id,
                    "acpRequestId": request_id,
                    "decision": decision,
                }
            )
        except Exception as e:
            self.logger.error("Permission response failed: %s", e)
            await reply(
                _response_error(session_id, correlation_id, str(e))
            )
