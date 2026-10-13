from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import WebSocketDisconnect

from server.main import websocket_endpoint
from server.test.main_test import (
    FakeACPClient, MockWebSocket, _cleanup_endpoint, _establish_active_session,
    _make_permission_request, patched_deps,
)
from server.permission_coordinator import PermissionCoordinator
from server.session_manager import ManagedSession, session_manager
from server.session_notifications import replay_pending_permissions


async def _frame(ws):
    return await asyncio.wait_for(ws.sent.get(), timeout=2)


def _decision(**overrides):
    return {
        "type": "permission_response", "sessionId": "active-session",
        "toolCallId": "tc-1", "requestId": "browser-correlation", "decision": "allow",
        **overrides,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id,send_failure", [("rpc-unsupported", False), ("rpc-unsupported", True), (None, False)])
async def test_unsupported_acp_request_rejects_only_identified_requests(patched_deps, caplog, request_id, send_failure):
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws)
        sender = AsyncMock(side_effect=OSError("ACP unavailable") if send_failure else None)
        with patch.object(acp, "send_response", sender):
            await acp.request_handler({"method": "session/unknown", "id": request_id})
        if request_id is None:
            sender.assert_not_awaited()
        else:
            sender.assert_awaited_once_with(
                request_id, error={"code": -32601, "message": "Method not supported: session/unknown"},
            )
        assert "Ignoring unexpected ACP request: session/unknown" in caplog.text
        if send_failure:
            assert "Failed to reject unsupported ACP request session/unknown: ACP unavailable" in caplog.text
        assert ws.sent.empty()
        assert session_manager.get("active-session").last_acp_update_at > 0
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params,request_id,session_id",
    [({"sessionId": "active-session", "toolCall": {}}, "rpc-malformed", "active-session"),
     (None, "rpc-malformed", None),
     ({"sessionId": "active-session", "toolCall": {"toolCallId": "tc-1"}}, None, "active-session"),
     ({"sessionId": "active-session", "toolCall": {"toolCallId": ""}}, 0, "active-session")],
)
async def test_malformed_permission_request_returns_exact_rpc_and_browser_errors(patched_deps, params, request_id, session_id):
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws)
        await acp.request_handler({"method": "session/request_permission", "id": request_id, "params": params})
        assert await _frame(ws) == {
            "type": "error", "sessionId": session_id, "operation": "permission",
            "message": "Permission request missing toolCallId or id",
        }
        expected = [] if request_id is None else [
            ((request_id,), {"error": {"code": -32602, "message": "Permission request missing toolCallId or id"}}),
        ]
        assert acp.send_response_calls == expected
        assert session_manager.get("active-session").permission_requests == {}
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,no_acp,message,error_session",
    [({"sessionId": "stale-session", "decision": None}, False, "Session id does not match the active backend session.", "stale-session"),
     ({"decision": None}, True, "No active ACP session", "active-session"),
     ({"decision": None, "toolCallId": None}, False, "No decision provided", "active-session"),
     ({"toolCallId": None}, False, "Permission response missing toolCallId.", "active-session"),
     ({"toolCallId": "unknown"}, False, "Permission request is no longer pending.", "active-session")],
)
async def test_permission_response_validation_preserves_pending_request(patched_deps, changes, no_acp, message, error_session):
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    ms = None
    acp = None
    try:
        acp = await _establish_active_session(ws)
        await acp.request_handler(_make_permission_request())
        await _frame(ws)
        ms = session_manager.get("active-session")
        pending = ms.permission_requests["tc-1"]
        if no_acp:
            ms.acp = None
        await ws.to_receive.put(_decision(**changes))
        assert await _frame(ws) == {
            "type": "error", "sessionId": error_session, "requestId": "browser-correlation",
            "operation": "permission_response", "message": message,
        }
        assert ms.permission_requests["tc-1"] is pending
        assert not pending["timeout_task"].done()
        assert acp.send_response_calls == []
    finally:
        if ms is not None:
            ms.acp = acp
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,requested_option,selected_option",
    [("deny", "explicit-option", "explicit-option"), ("deny", "unknown", "opt-deny"),
     ("allow", None, "opt-allow"), ("custom", None, "custom")],
)
async def test_permission_response_option_choice_and_duplicate_error(patched_deps, decision, requested_option, selected_option):
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws)
        request = _make_permission_request(request_id=0)
        request["params"]["options"].append({"optionId": "explicit-option", "kind": "allow_once"})
        await acp.request_handler(request)
        await _frame(ws)
        pending = session_manager.get("active-session").permission_requests["tc-1"]
        data = _decision(sessionId=None, decision=decision, optionId=requested_option)
        await ws.to_receive.put(data)
        assert await _frame(ws) == {
            "type": "permission_received", "sessionId": "active-session", "toolCallId": "tc-1",
            "requestId": "browser-correlation", "acpRequestId": 0, "decision": decision,
        }
        assert pending["timeout_task"].cancelled()
        assert acp.send_response_calls == [
            ((0, {"outcome": {"outcome": "selected", "optionId": selected_option}}), {}),
        ]
        await ws.to_receive.put(data)
        assert await _frame(ws) == {
            "type": "error", "sessionId": "active-session", "requestId": "browser-correlation",
            "operation": "permission_response", "message": "Permission request is no longer pending.",
        }
        assert len(acp.send_response_calls) == 1
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_replacing_permission_request_cancels_old_timeout_before_new_delivery(patched_deps):
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws)
        await acp.request_handler(_make_permission_request(request_id="first"))
        first_frame = await _frame(ws)
        ms = session_manager.get("active-session")
        old_timeout = ms.permission_requests["tc-1"]["timeout_task"]
        await asyncio.sleep(0)
        await acp.request_handler(_make_permission_request(request_id="replacement"))
        replacement_frame = await _frame(ws)
        assert replacement_frame["requestId"] == "replacement"
        assert old_timeout.cancelled()
        entry = ms.permission_requests["tc-1"]
        assert entry["request_id"] == "replacement"
        assert not entry["timeout_task"].done()
        assert entry["display_payload"] == replacement_frame
        assert entry["display_payload"] is not replacement_frame
        assert entry["display_payload"] != first_frame
        assert acp.send_response_calls == []
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_failed_permission_response_removes_pending_and_reports_error(patched_deps, caplog):
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws)
        await acp.request_handler(_make_permission_request())
        await _frame(ws)
        ms = session_manager.get("active-session")
        timeout = ms.permission_requests["tc-1"]["timeout_task"]
        sender = AsyncMock(side_effect=OSError("write failed"))
        with patch.object(acp, "send_response", sender):
            await ws.to_receive.put(_decision())
            assert await _frame(ws) == {
                "type": "error", "sessionId": "active-session", "requestId": "browser-correlation",
                "operation": "permission_response", "message": "write failed",
            }
        assert ms.permission_requests == {}
        assert timeout.cancelled()
        assert "Permission response failed: write failed" in caplog.text
        sender.assert_awaited_once()
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("command,metadata,expected", [("direct", "metadata", "direct"), (None, "  metadata  ", "  metadata  "), (None, "   ", "cached")])
async def test_permission_detail_priority_does_not_mutate_request(patched_deps, command, metadata, expected):
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws)
        ms = session_manager.get("active-session")
        ms.tool_call_snapshots["tc-1"] = {"rawInput": {"command": "cached"}}
        request = _make_permission_request()
        request["params"]["_meta"] = {"cognition.ai/command": metadata}
        if command is not None:
            request["params"]["toolCall"]["rawInput"] = {"command": command}
        original = deepcopy(request)
        await acp.request_handler(request)
        assert (await _frame(ws))["data"]["detail"] == expected
        assert request == original
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["response", "unavailable"])
async def test_pending_permissions_and_acp_callbacks_follow_reconnect(patched_deps, action):
    session_manager.grace_seconds = 30
    first = MockWebSocket()
    first_task = asyncio.create_task(websocket_endpoint(first))
    second = MockWebSocket()
    second_task = None
    try:
        acp = await _establish_active_session(first)
        await acp.request_handler(_make_permission_request())
        original = await _frame(first)
        ms = session_manager.get("active-session")
        timeout = ms.permission_requests["tc-1"]["timeout_task"]
        await first.to_receive.put(WebSocketDisconnect())
        await asyncio.wait_for(first_task, timeout=2)
        assert not timeout.done()
        second_task = asyncio.create_task(websocket_endpoint(second))
        await second.to_receive.put({"type": "resume_session", "sessionId": "active-session"})
        assert (await _frame(second))["type"] == "session_resumed"
        replay = await _frame(second)
        assert replay == original
        assert replay is not original
        assert session_manager.get("active-session") is ms
        assert len(FakeACPClient.instances) == 1
        if action == "response":
            await second.to_receive.put(_decision())
            assert (await _frame(second))["type"] == "permission_received"
            await acp.request_handler(_make_permission_request(request_id="after-reconnect", tool_call_id="tc-2"))
            assert (await _frame(second))["requestId"] == "after-reconnect"
        else:
            await acp.on_unavailable("ACP exited after reconnect")
            assert await _frame(second) == {
                "type": "error", "sessionId": "active-session", "operation": "acp_unavailable",
                "message": "ACP exited after reconnect",
            }
            assert ms.permission_requests == {}
        assert timeout.cancelled()
        assert first.sent.empty()
    finally:
        session_manager.grace_seconds = 0
        if second_task is not None:
            await _cleanup_endpoint(second, second_task)
        if not first_task.done():
            await _cleanup_endpoint(first, first_task)
        surviving = session_manager.get("active-session")
        if surviving is not None:
            await session_manager.cancel_orphan(surviving)


@pytest.mark.asyncio
async def test_stale_permission_timeout_leaves_replacement_pending(patched_deps):
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws)
        timeout_started = asyncio.Event()
        expire = asyncio.Event()
        original_sleep = asyncio.sleep

        async def wait_for_expiry(delay, *args):
            if delay == 123:
                timeout_started.set()
                await expire.wait()
            else:
                return await original_sleep(delay, *args)

        with patch("server.main._PERMISSION_TIMEOUT_SECONDS", 123), patch("server.main.asyncio.sleep", wait_for_expiry):
            await acp.request_handler(_make_permission_request(request_id="old"))
            await _frame(ws)
            await asyncio.wait_for(timeout_started.wait(), timeout=2)
            ms = session_manager.get("active-session")
            old_timeout = ms.permission_requests["tc-1"]["timeout_task"]
            replacement = {"request_id": "replacement", "options": [], "timeout_task": None}
            ms.permission_requests["tc-1"] = replacement
            expire.set()
            await asyncio.wait_for(old_timeout, timeout=2)
        assert ms.permission_requests["tc-1"] is replacement
        assert acp.send_response_calls == []
        assert ws.sent.empty()
    finally:
        await _cleanup_endpoint(ws, task)


def _coordinator(current_session, send):
    return PermissionCoordinator(
        current_session=current_session, send=send,
        timeout_seconds=lambda: 12.5, logger=logging.getLogger("devin_gui"),
    )


@pytest.mark.asyncio
async def test_pending_entry_retains_enriched_display_frame_snapshot():
    """The exact wire frame is retained before send and isolated from mutation."""
    ms = ManagedSession("active-session")
    ms.tool_call_snapshots["tc-1"] = {"rawInput": {"command": "cached"}}
    send = AsyncMock(return_value=True)
    coordinator = _coordinator(lambda: ms, send)
    request = _make_permission_request()
    request["params"]["toolCall"]["rawInput"] = {"command": "rm -rf build", "nested": {"flags": ["-f"]}}
    original = deepcopy(request)

    await coordinator.handle_request(request)

    pending = ms.permission_requests.get("tc-1", {})
    try:
        frame = send.await_args.args[0]
        assert frame == {
            "type": "request_permission", "sessionId": "active-session",
            "requestId": "acp-req-1", "toolCallId": "tc-1",
            "data": {
                **original["params"], "toolCallId": "tc-1", "detail": "rm -rf build",
            },
        }
        assert pending["display_payload"] == frame
        assert pending["display_payload"] is not frame
        assert pending["display_payload"]["data"] is not frame["data"]
        assert request == original
        request["params"]["options"][0]["optionId"] = "mutated"
        request["params"]["toolCall"]["rawInput"]["nested"]["flags"].append("changed")
        frame["data"]["options"].clear()
        frame["data"]["detail"] = "changed"
        frame["toolCallId"] = "changed"
        assert pending["display_payload"]["toolCallId"] == "tc-1"
        assert pending["display_payload"]["data"]["detail"] == "rm -rf build"
        assert pending["display_payload"]["data"]["options"][0]["optionId"] == "opt-allow"
        assert pending["display_payload"]["data"]["toolCall"]["rawInput"]["nested"]["flags"] == ["-f"]
    finally:
        timer = pending.get("timeout_task")
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)


@pytest.mark.asyncio
async def test_display_payload_lives_and_dies_with_the_pending_entry():
    """Resolution and replacement release the retained frame with the entry."""
    ms = ManagedSession("active-session")
    sender = AsyncMock()
    ms.acp = SimpleNamespace(send_response=sender)
    send = AsyncMock(return_value=True)
    coordinator = _coordinator(lambda: ms, send)
    reply = AsyncMock()

    await coordinator.handle_request(_make_permission_request())
    pending = ms.permission_requests["tc-1"]
    assert isinstance(pending["display_payload"], dict)
    await coordinator.handle_response(ms, _decision(), reply)

    assert ms.permission_requests == {}
    assert reply.await_args.args[0]["type"] == "permission_received"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "reconnected"])
@pytest.mark.parametrize("rejection_fails", [False, True])
async def test_deadline_resolves_current_session_after_sleep_and_preserves_expiry_frame(session_id, rejection_fails):
    original = ManagedSession("original")
    original.permission_requests["tc-1"] = {"request_id": "rpc", "options": [], "timeout_task": None}
    current = ManagedSession(session_id)
    sender = AsyncMock(side_effect=OSError("ACP gone") if rejection_fails else None)
    current.acp = SimpleNamespace(send_response=sender)
    current.permission_requests["tc-1"] = {"request_id": "rpc", "options": [], "timeout_task": None}
    holder = [original]
    send = AsyncMock(return_value=True)
    coordinator = _coordinator(lambda: holder[0], send)
    sleeping = asyncio.Event()
    expire = asyncio.Event()

    async def sleep_until_expiry(delay):
        sleeping.set()
        await expire.wait()

    with patch("server.permission_coordinator.asyncio.sleep", side_effect=sleep_until_expiry) as sleep:
        task = asyncio.create_task(coordinator._permission_timeout("tc-1", "rpc"))
        try:
            await asyncio.wait_for(sleeping.wait(), timeout=2)
            holder[0] = current
            expire.set()
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    expected = {"type": "permission_expired", "toolCallId": "tc-1", "requestId": "rpc"}
    if session_id is not None:
        expected["sessionId"] = session_id
    sleep.assert_awaited_once_with(12.5)
    send.assert_awaited_once_with(expected)
    sender.assert_awaited_once_with("rpc", error={"code": -32600, "message": "Permission request timed out"})
    assert current.permission_requests == {}
    assert "tc-1" in original.permission_requests


@pytest.mark.asyncio
async def test_validation_error_uses_direct_browser_reply_and_propagates_delivery_failure():
    ms = ManagedSession("active-session")
    send = AsyncMock(return_value=False)
    coordinator = _coordinator(lambda: ms, send)
    reply = AsyncMock(side_effect=OSError("browser closed"))

    with pytest.raises(OSError, match="browser closed"):
        await coordinator.handle_response(ms, _decision(), reply)

    reply.assert_awaited_once_with({
        "type": "error", "sessionId": "active-session", "requestId": "browser-correlation",
        "operation": "permission_response", "message": "No active ACP session",
    })
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_reply_fails", [False, True])
async def test_ack_delivery_failure_is_reported_on_the_same_browser_socket(error_reply_fails, caplog):
    ms = ManagedSession("active-session")
    sender = AsyncMock()
    ms.acp = SimpleNamespace(send_response=sender)
    ms.permission_requests["tc-1"] = {"request_id": "rpc", "options": [], "timeout_task": None}
    send = AsyncMock(return_value=True)
    coordinator = _coordinator(lambda: ms, send)
    reply = AsyncMock(side_effect=[OSError("ack failed"), OSError("error failed") if error_reply_fails else None])

    if error_reply_fails:
        with pytest.raises(OSError, match="error failed"):
            await coordinator.handle_response(ms, _decision(), reply)
    else:
        await coordinator.handle_response(ms, _decision(), reply)

    assert [call.args[0] for call in reply.await_args_list] == [
        {"type": "permission_received", "sessionId": "active-session", "toolCallId": "tc-1",
         "requestId": "browser-correlation", "acpRequestId": "rpc", "decision": "allow"},
        {"type": "error", "sessionId": "active-session", "requestId": "browser-correlation",
         "operation": "permission_response", "message": "ack failed"},
    ]
    sender.assert_awaited_once_with("rpc", {"outcome": {"outcome": "selected", "optionId": "allow"}})
    assert "Permission response failed: ack failed" in caplog.text
    assert ms.permission_requests == {}
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_response_keeps_receive_loop_session_snapshot():
    ms = ManagedSession("active-session")
    sender = AsyncMock()
    ms.acp = SimpleNamespace(send_response=sender)
    ms.permission_requests["tc-1"] = {"request_id": "rpc", "options": [], "timeout_task": None}
    other = ManagedSession("other-session")
    other.permission_requests["tc-1"] = {"request_id": "other-rpc", "options": [], "timeout_task": None}
    send = AsyncMock(return_value=True)
    coordinator = _coordinator(lambda: other, send)
    reply = AsyncMock()

    await coordinator.handle_response(ms, _decision(), reply)

    sender.assert_awaited_once_with("rpc", {"outcome": {"outcome": "selected", "optionId": "allow"}})
    assert reply.await_args.args[0]["sessionId"] == "active-session"
    assert ms.permission_requests == {}
    assert other.permission_requests["tc-1"]["request_id"] == "other-rpc"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_deadline_survives_reconnect_and_expires_on_new_socket(patched_deps, monkeypatch):
    """The original timeout task — not a restarted one — expires after reconnect."""
    session_manager.grace_seconds = 30
    monkeypatch.setattr("server.main._PERMISSION_TIMEOUT_SECONDS", 123)
    real_sleep = asyncio.sleep
    timeout_started = asyncio.Event()
    expiry = asyncio.Event()
    sleep_calls = []

    async def fake_sleep(delay, *args, **kwargs):
        sleep_calls.append(delay)
        if delay == 123:
            timeout_started.set()
            await expiry.wait()
            return
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr("server.permission_coordinator.asyncio.sleep", fake_sleep)
    first = MockWebSocket()
    first_task = asyncio.create_task(websocket_endpoint(first))
    second = MockWebSocket()
    second_task = None
    try:
        acp = await _establish_active_session(first)
        await acp.request_handler(_make_permission_request())
        original = await _frame(first)
        await asyncio.wait_for(timeout_started.wait(), timeout=2)
        ms = session_manager.get("active-session")
        pending = ms.permission_requests["tc-1"]
        timer = pending["timeout_task"]

        await first.to_receive.put(WebSocketDisconnect())
        await asyncio.wait_for(first_task, timeout=5)

        second_task = asyncio.create_task(websocket_endpoint(second))
        await second.to_receive.put(
            {"type": "resume_session", "sessionId": "active-session", "requestId": "r2"}
        )
        assert (await _frame(second))["type"] == "session_resumed"
        replay = await _frame(second)
        assert replay == original
        assert ms.permission_requests["tc-1"] is pending
        assert pending["timeout_task"] is timer and not timer.done()
        assert sleep_calls.count(123) == 1

        expiry.set()
        await asyncio.wait_for(timer, timeout=2)
        assert (await _frame(second)) == {
            "type": "permission_expired", "sessionId": "active-session",
            "toolCallId": "tc-1", "requestId": "acp-req-1",
        }
        assert acp.send_response_calls == [
            (("acp-req-1",), {"error": {"code": -32600, "message": "Permission request timed out"}})
        ]
        assert "tc-1" not in ms.permission_requests
        send = AsyncMock()
        await replay_pending_permissions(ms, send)
        send.assert_not_awaited()
    finally:
        session_manager.grace_seconds = 0
        expiry.set()
        for ws, task in ((first, first_task), (second, second_task)):
            if task is not None and not task.done():
                await _cleanup_endpoint(ws, task)
        surviving = session_manager.get("active-session")
        if surviving is not None:
            await session_manager.cancel_orphan(surviving)
