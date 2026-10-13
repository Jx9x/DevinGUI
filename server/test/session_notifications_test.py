"""Live ACP delivery, tool snapshots, and reconnect buffering contracts."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server import main
from server.test.main_test import FakeACPClient, patched_deps
from server.session_manager import ManagedSession
from server.session_notifications import (
    SessionNotifications, replay_pending_permissions, replay_session_update,
)
from server.test.session_transitions_test import connection, create_session, receive_until


def notification(session_id, update):
    return {"method": "session/update", "params": {"sessionId": session_id, "update": update}}


def raw_update(kind):
    if kind in ("agent_message_chunk", "agent_thought_chunk"):
        return {"sessionUpdate": kind, "content": {"type": "text", "text": "delta"}}
    if kind == "usage_update":
        return {"sessionUpdate": kind, "used": 17, "size": 100}
    return {"sessionUpdate": kind, "toolCallId": "tool", "status": "in_progress"}


@pytest.fixture
async def live_session(connection):
    ws, ms = connection
    sid = await create_session(ws)
    return ws, ms, sid, ms.acp.notification_handler


@pytest.mark.parametrize("message", [{}, {"method": "other", "params": None}, {"method": None}])
async def test_other_notifications_leave_activity_and_buffers_untouched(live_session, message):
    ws, ms, sid, handle = live_session
    previous = {"sessionId": sid, "update": {"type": "usage_update", "used": 1}}
    ms.last_session_update = previous
    ms.db_fallback_settled = True
    ms.db_fallback_resume_event = asyncio.Event()
    await handle(message)
    assert ms.last_acp_update_at == 0 and ms.last_session_update is previous
    assert ms.db_fallback_settled and not ms.db_fallback_resume_event.is_set()
    assert ms.tool_call_snapshots == {} and ws.sent.empty()
    main.db_watcher.start_watching.assert_not_called()


@pytest.mark.parametrize("session", ["active", "other", "missing"])
@pytest.mark.parametrize("update", [
    {"sessionUpdate": "config_option_update"},
    {"sessionUpdate": "agent_message_chunk", "chunk": ""},
])
async def test_dropped_updates_still_record_activity_and_resume_matching_fallback(live_session, session, update):
    ws, ms, sid, handle = live_session
    requested_sid = sid if session == "active" else ("other" if session == "other" else None)
    ms.last_session_update = previous = {"update": {"type": "usage_update"}}
    ms.db_fallback_settled = True
    ms.db_fallback_resume_event = asyncio.Event()
    await handle(notification(requested_sid, update))
    assert ms.last_acp_update_at > 0 and ms.last_session_update is previous
    assert ms.db_fallback_settled is (session != "active")
    assert ms.db_fallback_resume_event.is_set() is (session == "active")
    assert ws.sent.empty()
    if session == "active":
        assert main.db_watcher.start_watching.call_args.args[0] == sid
        assert main.transcript_watcher.start_watching.call_args.args[0] == sid
    else:
        main.db_watcher.start_watching.assert_not_called()
        main.transcript_watcher.start_watching.assert_not_called()


@pytest.mark.parametrize("kind", ["agent_message_chunk", "agent_thought_chunk", "tool_call_update", "usage_update"])
@pytest.mark.parametrize("delivery", ["accepted", "failed", "orphaned"])
async def test_live_buffer_policy_before_and_after_delivery(live_session, monkeypatch, kind, delivery):
    ws, ms, sid, handle = live_session
    source = raw_update(kind)
    update = main.normalize_session_update(source)
    buffered = {"sessionId": sid, "update": update}
    previous = {"update": {"type": "usage_update", "used": 1}}
    ms.last_session_update = previous
    delta = kind in ("agent_message_chunk", "agent_thought_chunk")
    sent = []
    original_send = ws.send_json

    async def send(frame):
        if delta:
            assert ms.last_session_update is previous
        else:
            assert ms.last_session_update == buffered
        if kind == "tool_call_update":
            assert ms.tool_call_snapshots["tool"] is source
        sent.append(frame)
        if delivery == "failed":
            raise OSError("socket failed")
        await original_send(frame)

    monkeypatch.setattr(ws, "send_json", send)
    if delivery == "orphaned":
        ms.websocket = None
    await handle(notification(sid, source))
    assert sent == ([] if delivery == "orphaned" else [{"type": "session_update", **buffered}])
    assert ms.last_session_update == (None if delta and delivery == "accepted" else buffered)
    assert ws.sent.empty() is (delivery != "accepted")
    if delivery == "accepted":
        frame = ws.sent.get_nowait()
        assert frame == {"type": "session_update", **buffered}
        if not delta:
            assert frame["update"] is ms.last_session_update["update"]


async def test_tool_snapshots_merge_shallowly_and_evict_by_insertion_order(live_session):
    ws, ms, sid, handle = live_session
    ms.tool_call_snapshots = {
        f"tool-{index}": {
            "sessionUpdate": "tool_call", "toolCallId": f"tool-{index}",
            "title": "original", "rawInput": {"command": "synthetic"},
        }
        for index in range(256)
    }
    original = ms.tool_call_snapshots["tool-0"]
    original_copy = deepcopy(original)
    changed_input = {"command": "changed"}
    patch = {"sessionUpdate": "tool_call_update", "toolCallId": "tool-0", "rawInput": changed_input}
    await handle(notification(sid, patch))
    merged = ms.tool_call_snapshots["tool-0"]
    assert len(ms.tool_call_snapshots) == 256 and next(iter(ms.tool_call_snapshots)) == "tool-0"
    assert merged == {**original, **patch} and merged is not original
    assert merged["rawInput"] is changed_input and original == original_copy
    source = {"sessionUpdate": "tool_call", "toolCallId": "new-tool", "rawInput": {"command": "new"}}
    await handle(notification(sid, source))
    assert len(ms.tool_call_snapshots) == 256 and "tool-0" not in ms.tool_call_snapshots
    assert next(iter(ms.tool_call_snapshots)) == "tool-1"
    assert ms.tool_call_snapshots["new-tool"] is source
    assert "rawInput" not in ws.sent.get_nowait()["update"]
    assert "rawInput" not in ws.sent.get_nowait()["update"]


@pytest.mark.parametrize("source", [
    {"sessionUpdate": "tool_call", "toolCallId": None},
    {"sessionUpdate": "tool_call_update", "toolCallId": 5},
    {"sessionUpdate": "tool_call_update", "toolCallId": ""},
    {"type": "tool_call", "toolCallId": "flat-tool"},
])
async def test_snapshot_capture_requires_raw_tool_kind_and_nonempty_string_id(live_session, source):
    ws, ms, sid, handle = live_session
    await handle(notification(sid, source))
    assert ms.tool_call_snapshots == {}
    assert ws.sent.get_nowait() == {
        "type": "session_update", "sessionId": sid, "update": main.normalize_session_update(source),
    }


async def test_activity_and_raw_snapshot_precede_dynamic_normalization_failure(live_session, monkeypatch):
    ws, ms, sid, handle = live_session
    previous = {"update": {"type": "usage_update"}}
    ms.last_session_update = previous
    ms.db_fallback_settled = True
    ms.db_fallback_resume_event = asyncio.Event()
    source = {"sessionUpdate": "tool_call", "toolCallId": "tool", "rawInput": {"command": "synthetic"}}

    def normalize(update):
        assert update is source and ms.last_acp_update_at > 0
        assert not ms.db_fallback_settled and ms.db_fallback_resume_event.is_set()
        assert ms.tool_call_snapshots["tool"] is source
        assert main.db_watcher.start_watching.called and main.transcript_watcher.start_watching.called
        raise RuntimeError("normalization failed")

    monkeypatch.setattr(main, "normalize_session_update", normalize)
    with pytest.raises(RuntimeError, match="normalization failed"):
        await handle(notification(sid, source))
    assert ms.last_session_update is previous and ws.sent.empty()


@pytest.mark.parametrize("kind", ["agent_message_chunk", "tool_call_update"])
@pytest.mark.parametrize("delivered", [True, False])
async def test_live_delivery_resolves_buffer_owner_after_session_handoff(live_session, monkeypatch, kind, delivered):
    ws, original, sid, handle = live_session
    replacement = ManagedSession("survivor")
    replacement.acp = FakeACPClient(None)
    main.session_manager.register(replacement, "survivor")
    previous = {"update": {"type": "usage_update", "used": 1}}
    original.last_session_update = previous
    source = raw_update(kind)
    buffered = {"sessionId": sid, "update": main.normalize_session_update(source)}
    started, release = asyncio.Event(), asyncio.Event()
    original_send = ws.send_json

    async def send(frame):
        if frame["type"] == "session_update":
            started.set()
            await release.wait()
            if not delivered:
                raise OSError("socket failed")
        await original_send(frame)

    monkeypatch.setattr(ws, "send_json", send)
    update_task = asyncio.create_task(handle(notification(sid, source)))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await ws.to_receive.put({"type": "resume_session", "sessionId": "survivor"})
        assert await receive_until(ws, "session_resumed") == [{
            "type": "session_resumed", "sessionId": "survivor", "requestId": None,
        }]
        release.set()
        await asyncio.wait_for(update_task, timeout=2)
        assert original.last_acp_update_at > 0 and replacement.last_acp_update_at == 0
        if kind == "agent_message_chunk":
            assert original.last_session_update is previous
            assert replacement.last_session_update == (None if delivered else buffered)
        else:
            assert original.last_session_update == buffered and replacement.last_session_update is None
            assert original.tool_call_snapshots["tool"] is source and replacement.tool_call_snapshots == {}
        assert ws.sent.empty() is (not delivered)
    finally:
        release.set()
        update_task.cancel()
        await asyncio.gather(update_task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["agent_thought_chunk", "tool_call_update"])
async def test_socket_cancellation_propagates_with_existing_buffer_timing(live_session, monkeypatch, kind):
    ws, ms, sid, handle = live_session
    previous = {"update": {"type": "usage_update"}}
    ms.last_session_update = previous
    source = raw_update(kind)
    monkeypatch.setattr(ws, "send_json", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await handle(notification(sid, source))
    if kind == "agent_thought_chunk":
        assert ms.last_session_update is previous
    else:
        assert ms.last_session_update == {"sessionId": sid, "update": main.normalize_session_update(source)}


@pytest.mark.parametrize("delivered", [True, False])
async def test_replay_keeps_newer_buffer_arriving_during_send(live_session, monkeypatch, delivered):
    """Successful delta replay clears only the buffer it captured."""
    ws, original, sid, handle = live_session
    existing = ManagedSession("survivor")
    existing.acp = FakeACPClient(handle)
    missed = {"sessionId": "survivor", "update": {"type": "agent_message_chunk", "chunk": "missed"}}
    existing.last_session_update = missed
    main.session_manager.register(existing, "survivor")
    started, release = asyncio.Event(), asyncio.Event()
    original_send = ws.send_json

    async def send(frame):
        if frame["type"] == "session_update" and frame["update"] == missed["update"]:
            started.set()
            await release.wait()
            if not delivered:
                raise OSError("replay failed")
        await original_send(frame)

    monkeypatch.setattr(ws, "send_json", send)
    try:
        await ws.to_receive.put({"type": "resume_session", "sessionId": "survivor"})
        assert (await receive_until(ws, "session_resumed"))[0]["sessionId"] == "survivor"
        await asyncio.wait_for(started.wait(), timeout=2)
        source = raw_update("usage_update")
        await handle(notification("survivor", source))
        fresh = existing.last_session_update
        assert ws.sent.get_nowait() == {"type": "session_update", **fresh}
        assert fresh is not missed
        release.set()
        await ws.to_receive.put({"type": "resume_session", "sessionId": "", "requestId": "barrier"})
        frames = await receive_until(ws, "resume_failed")
        assert frames[:-1] == ([{"type": "session_update", **missed}] if delivered else [])
        assert existing.last_session_update is fresh
    finally:
        release.set()


async def test_notification_callbacks_can_rebind_between_activity_and_capture():
    original, replacement = ManagedSession("original"), ManagedSession("replacement")
    original.websocket = SimpleNamespace(send_json=AsyncMock())
    replacement.websocket = SimpleNamespace(send_json=AsyncMock())
    binding = [original]
    source = {"sessionUpdate": "tool_call", "toolCallId": "tool", "rawInput": {"command": "synthetic"}}
    update = {"type": "tool_call", "toolCallId": "tool"}
    events = []

    def resume(session_id):
        assert session_id == "original" and original.last_acp_update_at > 0
        binding[0] = replacement
        events.append("resume")

    def normalize(raw):
        assert raw is source and replacement.tool_call_snapshots["tool"] is source
        events.append("normalize")
        return update

    handler = SessionNotifications(
        current_session=lambda: binding[0], resume_live_updates=resume, normalize_update=normalize,
    )
    await handler.handle_notification(notification("original", source))
    assert events == ["resume", "normalize"]
    assert replacement.last_acp_update_at == 0 and original.tool_call_snapshots == {}
    assert original.last_session_update is None
    assert replacement.last_session_update == {"sessionId": "original", "update": update}
    replacement.websocket.send_json.assert_awaited_once_with({
        "type": "session_update", "sessionId": "original", "update": update,
    })
    original.websocket.send_json.assert_not_awaited()


@pytest.mark.parametrize("fails", [False, True])
async def test_safe_delivery_captures_one_socket_and_resolves_the_next_send(fails):
    original, replacement = ManagedSession("original"), ManagedSession("replacement")
    binding = [original]
    started, release = asyncio.Event(), asyncio.Event()
    frame = {"type": "cost_update", "sessionId": "original", "estimate": {"total": 1}}

    async def send(message):
        assert message is frame
        started.set()
        await release.wait()
        if fails:
            raise OSError("socket failed")

    original.websocket = SimpleNamespace(send_json=AsyncMock(side_effect=send))
    replacement.websocket = SimpleNamespace(send_json=AsyncMock())
    handler = SessionNotifications(current_session=lambda: binding[0], resume_live_updates=lambda sid: None)
    task = asyncio.create_task(handler.send(frame))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        binding[0] = replacement
        release.set()
        assert await asyncio.wait_for(task, timeout=2) is (not fails)
        replacement.websocket.send_json.assert_not_awaited()
        assert await handler.send(frame) is True
        replacement.websocket.send_json.assert_awaited_once_with(frame)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("update,delta", [
    ({"type": "agent_message_chunk", "chunk": "missed"}, True),
    ({"type": "agent_thought_chunk", "chunk": "missed"}, True),
    ({"type": "tool_call_update", "status": "completed"}, False),
    (None, False),
    ("opaque", False),
])
@pytest.mark.parametrize("delivered", [True, False])
async def test_replay_preserves_wire_shape_and_consumes_only_accepted_deltas(update, delta, delivered):
    session = ManagedSession("live")
    buffered = {"update": update}
    session.last_session_update = buffered
    send = AsyncMock(return_value=delivered)
    await replay_session_update(session, send)
    send.assert_awaited_once_with({"type": "session_update", "sessionId": None, "update": update})
    assert session.last_session_update is (None if delivered and delta else buffered)


@pytest.mark.parametrize("update", [
    {"type": "agent_message_chunk", "chunk": "missed"},
    {"type": "agent_thought_chunk", "chunk": "missed"},
])
@pytest.mark.parametrize("delivered", [True, False])
async def test_replay_clearing_requires_captured_buffer_identity(update, delivered):
    """An equal-but-distinct replacement buffered during the send is retained."""
    session = ManagedSession("live")
    session.last_session_update = {"sessionId": "live", "update": update}
    replacement = {"sessionId": "live", "update": dict(update)}

    async def send(frame):
        assert frame == {"type": "session_update", "sessionId": "live", "update": update}
        session.last_session_update = replacement
        return delivered

    await replay_session_update(session, send)
    assert session.last_session_update is replacement


async def test_replay_without_buffer_sends_nothing():
    send = AsyncMock()
    await replay_session_update(ManagedSession("live"), send)
    send.assert_not_awaited()


async def test_replay_keeps_the_caller_send_error_boundary():
    session = ManagedSession("live")
    session.last_session_update = buffered = {"update": {"type": "agent_message_chunk", "chunk": "missed"}}
    send = AsyncMock(side_effect=RuntimeError("callback failed"))
    with pytest.raises(RuntimeError, match="callback failed"):
        await replay_session_update(session, send)
    assert session.last_session_update is buffered


def _permission_frame(tool_call_id, request_id="acp-req"):
    return {
        "type": "request_permission", "sessionId": "live",
        "requestId": request_id, "toolCallId": tool_call_id,
        "data": {"options": [{"optionId": "opt"}], "detail": {"nested": ["v"]}},
    }


async def test_replay_pending_permissions_without_pending_sends_nothing():
    send = AsyncMock()
    await replay_pending_permissions(ManagedSession("live"), send)
    send.assert_not_awaited()


async def test_replay_pending_permissions_skips_entries_without_display_payload():
    session = ManagedSession("live")
    session.permission_requests["tc-1"] = {"request_id": "rpc-1", "options": [], "timeout_task": None}
    send = AsyncMock()
    await replay_pending_permissions(session, send)
    send.assert_not_awaited()
    assert "tc-1" in session.permission_requests


async def test_replay_pending_permissions_sends_deepcopies_in_insertion_order():
    session = ManagedSession("live")
    payloads = {}
    for key in ("tc-1", "tc-2", "tc-3"):
        payloads[key] = _permission_frame(key, request_id=f"rpc-{key}")
        session.permission_requests[key] = {
            "request_id": f"rpc-{key}", "options": [], "timeout_task": None,
            "display_payload": payloads[key],
        }
    sent = []

    async def capture(frame):
        sent.append((frame, deepcopy(frame)))
        frame["data"]["detail"]["nested"].append("mutated-by-send")
        return True

    await replay_pending_permissions(session, capture)

    frames = [frame for frame, _ in sent]
    snapshots = [snapshot for _, snapshot in sent]
    assert snapshots == [payloads["tc-1"], payloads["tc-2"], payloads["tc-3"]]
    for key, frame in zip(("tc-1", "tc-2", "tc-3"), frames):
        assert frame is not payloads[key]
        assert frame["data"] is not payloads[key]["data"]
        assert payloads[key]["data"]["detail"]["nested"] == ["v"]
        assert session.permission_requests[key]["display_payload"] is payloads[key]


@pytest.mark.parametrize("succeed", [True, False])
async def test_replay_pending_permissions_never_touches_entries_or_timers(succeed):
    session = ManagedSession("live")
    timer = asyncio.create_task(asyncio.sleep(60))
    pending = {
        "request_id": "rpc-1", "options": [], "timeout_task": timer,
        "display_payload": _permission_frame("tc-1"),
    }
    session.permission_requests["tc-1"] = pending
    send = AsyncMock(return_value=succeed)
    try:
        await replay_pending_permissions(session, send)
        send.assert_awaited_once_with(pending["display_payload"])
        assert session.permission_requests["tc-1"] is pending
        assert pending["timeout_task"] is timer
        assert not timer.done()
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)


async def test_replay_pending_permissions_stops_on_first_failed_delivery():
    session = ManagedSession("live")
    for key in ("tc-1", "tc-2", "tc-3"):
        session.permission_requests[key] = {
            "request_id": f"rpc-{key}", "options": [], "timeout_task": None,
            "display_payload": _permission_frame(key),
        }
    send = AsyncMock(side_effect=[True, False, True])
    await replay_pending_permissions(session, send)
    assert send.await_count == 2
    assert [call.args[0]["toolCallId"] for call in send.await_args_list] == ["tc-1", "tc-2"]
    assert set(session.permission_requests) == {"tc-1", "tc-2", "tc-3"}

    send.reset_mock()
    send.side_effect = None
    send.return_value = True
    await replay_pending_permissions(session, send)
    assert send.await_count == 3
    assert [call.args[0]["toolCallId"] for call in send.await_args_list] == ["tc-1", "tc-2", "tc-3"]
    assert set(session.permission_requests) == {"tc-1", "tc-2", "tc-3"}


async def test_replay_pending_permissions_skips_resolved_or_replaced_during_send():
    session = ManagedSession("live")
    stale = {
        "request_id": "rpc-1", "options": [], "timeout_task": None,
        "display_payload": _permission_frame("tc-1", "rpc-1"),
    }
    removed = {
        "request_id": "rpc-2", "options": [], "timeout_task": None,
        "display_payload": _permission_frame("tc-2", "rpc-2"),
    }
    replacement = {
        "request_id": "rpc-3b", "options": [], "timeout_task": None,
        "display_payload": _permission_frame("tc-3", "rpc-3b"),
    }
    session.permission_requests["tc-1"] = stale
    session.permission_requests["tc-2"] = removed
    session.permission_requests["tc-3"] = {
        "request_id": "rpc-3", "options": [], "timeout_task": None,
        "display_payload": _permission_frame("tc-3", "rpc-3"),
    }
    sent = []

    async def send(frame):
        sent.append(frame)
        if frame["toolCallId"] == "tc-1":
            session.permission_requests.pop("tc-2", None)
            session.permission_requests["tc-3"] = replacement
        return True

    await replay_pending_permissions(session, send)

    assert [frame["toolCallId"] for frame in sent] == ["tc-1"]
    assert session.permission_requests["tc-3"] is replacement


async def test_replay_pending_permissions_keeps_original_deadline_alive():
    session = ManagedSession("live")
    expire = asyncio.Event()
    fired = asyncio.Event()

    async def deadline():
        await expire.wait()
        session.permission_requests.pop("tc-1", None)
        fired.set()

    timer = asyncio.create_task(deadline())
    pending = {
        "request_id": "rpc-1", "options": [], "timeout_task": timer,
        "display_payload": _permission_frame("tc-1"),
    }
    session.permission_requests["tc-1"] = pending
    send = AsyncMock(return_value=True)
    await replay_pending_permissions(session, send)
    assert session.permission_requests["tc-1"]["timeout_task"] is timer

    expire.set()
    await asyncio.wait_for(timer, timeout=2)
    assert fired.is_set()
    assert session.permission_requests == {}

    send.reset_mock()
    await replay_pending_permissions(session, send)
    send.assert_not_awaited()
