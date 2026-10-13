"""Characterize session transitions through the WebSocket command boundary."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from server import main
from server.test.compaction_settings_test import write_rule
from server.test.main_test import (
    FakeACPClient,
    MockWebSocket,
    _cleanup_endpoint,
    _establish_active_session,
    _make_permission_request,
    patched_deps,
)
from server.session_manager import ManagedSession, SessionManager
from server.session_transitions import SessionTransitionCoordinator


@pytest.fixture
async def connection(patched_deps, monkeypatch):
    """Expose the anonymous session as well as its socket before any transition."""
    attached = []
    connected = asyncio.Event()
    original_attach = main.session_manager.attach_socket

    def attach(session_id, socket):
        ms = original_attach(session_id, socket)
        attached.append(ms)
        connected.set()
        return ms

    monkeypatch.setattr(main.session_manager, "attach_socket", attach)
    monkeypatch.setattr(main, "_session_cost", lambda sid: {"total": 1})
    ws = MockWebSocket()
    task = asyncio.create_task(main.websocket_endpoint(ws))
    try:
        await asyncio.wait_for(connected.wait(), timeout=2)
        yield ws, attached[0]
    finally:
        await _cleanup_endpoint(ws, task)


async def receive_until(ws, completion):
    frames = []
    while True:
        frame = await asyncio.wait_for(ws.sent.get(), timeout=2)
        frames.append(frame)
        if frame["type"] == completion:
            return frames


async def create_session(ws):
    await ws.to_receive.put({"type": "new_session", "model": "original", "cwd": "/tmp"})
    return (await receive_until(ws, "session_created"))[0]["sessionId"]


@pytest.mark.parametrize("transition,completion", [("view_history", "error"), ("load_history", "resume_failed"), ("resume_session", "resume_failed")])
async def test_unreadable_history_precheck_keeps_live_session_and_sends_actionable_failure(connection, transition, completion):
    ws, ms = connection
    active = await create_session(ws)
    original = ms.acp
    main.db_reader.get_session.side_effect = sqlite3.OperationalError("no such table: sessions")
    await ws.to_receive.put({"type": transition, "sessionId": "saved", "requestId": "schema-failure"})
    frames = await receive_until(ws, completion)
    assert "Open Diagnostics" in frames[-1]["message"]
    assert frames[-1]["requestId"] == "schema-failure"
    assert ms.acp is original and not original.stopped and ms.session_id == active


async def test_optional_rendered_history_failure_does_not_disable_successful_acp_resume(connection):
    ws, ms = connection
    main.db_reader.get_rendered_commits.side_effect = sqlite3.OperationalError("no such table: rendered_commits")
    await ws.to_receive.put({"type": "load_history", "sessionId": "saved", "requestId": "optional-rendered"})
    frames = await receive_until(ws, "session_resumed")
    history = next(frame for frame in frames if frame["type"] == "history_loaded")
    assert history["rendered"] == []
    assert ms.acp is not None and not ms.acp.stopped


@pytest.mark.parametrize("transition", ["new_session", "load_history", "resume_session"])
@pytest.mark.parametrize("stage", ["start", "initialize", "session"])
async def test_startup_failure_cleanup_and_frames(connection, monkeypatch, transition, stage):
    """History survives ACP failures; reconnect fallback does not resend history."""
    ws, ms = connection
    method = stage if stage != "session" else (
        "new_session" if transition == "new_session" else "load_session"
    )
    replay_flags = []

    async def fail(self, *args):
        replay_flags.append(self.suppress_updates)
        raise RuntimeError(f"{stage} failed")

    monkeypatch.setattr(FakeACPClient, method, fail)
    main.db_reader.get_messages.return_value = [{"text": "saved conversation"}]
    main.db_reader.get_rendered_commits.return_value = [{"text": "rendered conversation"}]
    monkeypatch.setattr(main, "_combined_reasoning_steps", lambda sid: [{"text": "saved thought"}])
    await ws.to_receive.put({
        "type": transition, "sessionId": "saved", "cwd": "/tmp",
        "model": "selected", "permissionMode": "bypass", "requestId": "startup",
    })
    completion = "error" if transition == "new_session" else "resume_failed"
    frames = await receive_until(ws, completion)

    failure = {
        "type": completion, "sessionId": None if transition == "new_session" else "saved",
        "requestId": "startup", "message": f"{stage} failed",
    }
    if transition == "new_session":
        failure["operation"] = "new_session"
    if transition == "load_history":
        assert frames == [
            {
                "type": "history_loaded", "sessionId": "saved", "requestId": "startup",
                "messages": [{"text": "saved conversation"}],
                "rendered": [{"text": "rendered conversation"}],
                "reasoning": [{"text": "saved thought"}],
            },
            {"type": "cost_update", "sessionId": "saved", "estimate": {"total": 1}},
            failure,
        ]
    else:
        assert frames == [failure]
        main.db_reader.get_messages.assert_not_called()
    assert replay_flags == [stage == "session" and transition != "new_session"]
    acp = FakeACPClient.instances[0]
    assert acp.stopped and not acp.suppress_updates
    assert acp.set_model_calls == acp.set_mode_calls == []
    assert ms.acp is None and ms.session_id is None
    assert ms.model == "selected" and ms.permission_mode == "bypass"
    assert ms.active_transition_task is None
    assert main.session_manager.get("saved") is None
    main.model_registry.set_last_used_model.assert_not_called()
    assert ws.sent.empty()


@pytest.mark.parametrize("transition", ["load_history", "resume_session"])
@pytest.mark.parametrize("requested_model", [None, "", "selected"])
async def test_history_model_fallback_and_preference_updates(
    connection, monkeypatch, transition, requested_model,
):
    ws, ms = connection
    main.db_reader.get_session.return_value = {"working_directory": "", "model": "saved-model"}
    monkeypatch.setattr(main.os, "getcwd", lambda: "/fallback-directory")
    await ws.to_receive.put({
        "type": transition, "sessionId": "saved", "model": requested_model,
        "requestId": "history", "permissionMode": "default",
    })
    frames = await receive_until(ws, "session_resumed")
    acp = FakeACPClient.instances[0]
    model = requested_model or "saved-model"
    assert acp.cwd == "/fallback-directory"
    assert acp.loaded_sid == "saved"
    assert acp.model == ms.model == model
    assert acp.set_model_calls == [("saved", model)]
    assert acp.set_mode_calls == [] and ms.permission_mode is None
    assert ms.acp is acp and main.session_manager.get("saved") is ms
    if requested_model:
        main.model_registry.set_last_used_model.assert_called_once_with(requested_model)
    else:
        main.model_registry.set_last_used_model.assert_not_called()
    if transition == "load_history":
        assert [frame["type"] for frame in frames] == [
            "history_loaded", "cost_update", "session_resumed",
        ]
    else:
        assert frames == [{"type": "session_resumed", "sessionId": "saved", "requestId": "history"}]
        main.db_reader.get_messages.assert_not_called()
    main.db_watcher.start_watching.assert_not_called()
    main.transcript_watcher.start_watching.assert_not_called()
    assert ms.active_transition_task is None


@pytest.mark.parametrize("last_model", [None, "last-model"])
async def test_new_session_uses_last_model_and_current_directory(connection, monkeypatch, last_model):
    ws, ms = connection
    main.model_registry.get_last_used_model.return_value = last_model
    monkeypatch.setattr(main.os, "getcwd", lambda: "/new-directory")
    await ws.to_receive.put({"type": "new_session", "requestId": "new"})
    frames = await receive_until(ws, "session_created")
    acp = FakeACPClient.instances[0]
    assert acp.new_session_cwd == "/new-directory"
    assert frames == [{
        "type": "session_created", "sessionId": ms.session_id,
        "model": last_model, "requestId": "new",
    }]
    assert acp.set_model_calls == ([(ms.session_id, last_model)] if last_model else [])
    if last_model:
        main.model_registry.set_last_used_model.assert_called_once_with(last_model)
    else:
        main.model_registry.set_last_used_model.assert_not_called()


async def test_unknown_view_keeps_live_session_and_legacy_operation(connection):
    ws, ms = connection
    old_sid = await create_session(ws)
    old_acp = ms.acp
    main.db_reader.get_session.return_value = None
    await ws.to_receive.put({"type": "view_history", "sessionId": "missing", "requestId": "view"})
    assert await receive_until(ws, "error") == [{
        "type": "error", "sessionId": "missing", "requestId": "view",
        "operation": "load_history", "message": "Session not found: missing",
    }]
    assert ms.acp is old_acp and not old_acp.stopped
    assert ms.session_id == old_sid and main.session_manager.get(old_sid) is ms
    assert ms.active_transition_task is None
    main.db_watcher.stop_watching.assert_not_called()
    main.transcript_watcher.stop_watching.assert_not_called()


@pytest.mark.parametrize("stage", ["history", "cost"])
async def test_view_failure_after_teardown_keeps_read_only_state(connection, monkeypatch, stage):
    ws, ms = connection
    old_sid = await create_session(ws)
    old_acp = ms.acp
    if stage == "history":
        main.db_reader.get_messages.side_effect = RuntimeError("history failed")
    else:
        def failed_cost(sid):
            raise RuntimeError("cost failed")

        monkeypatch.setattr(main, "_session_cost", failed_cost)
    await ws.to_receive.put({"type": "view_history", "sessionId": "saved", "requestId": "view"})
    frames = await receive_until(ws, "error")
    assert frames[-1] == {
        "type": "error", "sessionId": "saved", "requestId": "view",
        "operation": "load_history", "message": f"{stage} failed",
    }
    assert [frame["type"] for frame in frames] == (
        ["error"] if stage == "history" else ["history_loaded", "error"]
    )
    if stage == "cost":
        assert frames[0]["readOnly"] is True
    assert old_acp.stopped and ms.acp is None
    assert ms.session_id == "saved" and ms.model is ms.permission_mode is None
    assert main.session_manager.get(old_sid) is None
    assert main.session_manager.get("saved") is None
    assert ms.active_transition_task is None


@pytest.mark.parametrize("stage", ["history", "cost"])
async def test_history_delivery_failure_after_registration(connection, monkeypatch, stage):
    """Failed startups roll back the registry and never resend delivered history."""
    ws, ms = connection
    saved_messages = [{"text": "saved conversation"}]
    if stage == "history":
        main.db_reader.get_messages.side_effect = [RuntimeError("snapshot failed"), saved_messages]
        expected_reads = 2
    else:
        main.db_reader.get_messages.return_value = saved_messages
        expected_reads = 1
        costs = iter([RuntimeError("snapshot failed"), {"total": 1}])

        def read_cost(sid):
            value = next(costs)
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(main, "_session_cost", read_cost)
    await ws.to_receive.put({
        "type": "load_history", "sessionId": "saved", "model": "selected", "requestId": "load",
    })
    frames = await receive_until(ws, "resume_failed")
    history = {
        "type": "history_loaded", "sessionId": "saved", "requestId": "load",
        "messages": saved_messages, "rendered": [], "reasoning": [],
    }
    assert frames == [
        history,
        {"type": "cost_update", "sessionId": "saved", "estimate": {"total": 1}},
        {"type": "resume_failed", "sessionId": "saved", "requestId": "load", "message": "snapshot failed"},
    ]
    assert main.db_reader.get_messages.call_count == expected_reads
    assert FakeACPClient.instances[0].stopped
    assert ms.acp is None and ms.session_id is None
    assert main.session_manager.get("saved") is None
    assert ms.active_transition_task is None
    main.model_registry.set_last_used_model.assert_called_once_with("selected")


@pytest.mark.parametrize("stage", ["history", "cost"])
async def test_persistent_fallback_failure_still_reports_original_error(connection, monkeypatch, stage):
    """A persistent fallback read failure cannot bypass the correlated resume_failed."""
    ws, ms = connection
    if stage == "history":
        main.db_reader.get_messages.side_effect = RuntimeError("history failed")
    else:
        main.db_reader.get_messages.return_value = [{"text": "saved conversation"}]

        def failed_cost(sid):
            raise RuntimeError("cost failed")

        monkeypatch.setattr(main, "_session_cost", failed_cost)
    await ws.to_receive.put({
        "type": "load_history", "sessionId": "saved", "model": "selected", "requestId": "load",
    })
    frames = await receive_until(ws, "resume_failed")
    expected = []
    if stage == "cost":
        expected.append({
            "type": "history_loaded", "sessionId": "saved", "requestId": "load",
            "messages": [{"text": "saved conversation"}], "rendered": [], "reasoning": [],
        })
    else:
        expected.append({"type": "cost_update", "sessionId": "saved", "estimate": {"total": 1}})
    expected.append({
        "type": "resume_failed", "sessionId": "saved", "requestId": "load",
        "message": f"{stage} failed",
    })
    assert frames == expected
    assert FakeACPClient.instances[0].stopped
    assert ms.acp is None and ms.session_id is None
    assert main.session_manager.get("saved") is None
    assert ms.active_transition_task is None


@pytest.mark.parametrize("transition", ["new_session", "load_history", "resume_session"])
async def test_startup_failure_after_registration_rolls_back_registry(connection, monkeypatch, transition):
    """A failure after registration leaves no ready session and no registry entry."""
    ws, ms = connection
    saved_messages = [{"text": "saved conversation"}]
    main.db_reader.get_messages.return_value = saved_messages
    main.db_reader.get_rendered_commits.return_value = []
    monkeypatch.setattr(main, "_combined_reasoning_steps", lambda sid: [])
    main.model_registry.set_last_used_model.side_effect = RuntimeError("registry failed")
    await ws.to_receive.put({
        "type": transition, "sessionId": "saved", "cwd": "/tmp",
        "model": "selected", "requestId": "startup",
    })
    completion = "error" if transition == "new_session" else "resume_failed"
    frames = await receive_until(ws, completion)
    failure = {
        "type": completion, "sessionId": None if transition == "new_session" else "saved",
        "requestId": "startup", "message": "registry failed",
    }
    if transition == "new_session":
        failure["operation"] = "new_session"
        assert frames == [failure]
        main.db_reader.get_messages.assert_not_called()
    elif transition == "load_history":
        assert frames == [
            {
                "type": "history_loaded", "sessionId": "saved", "requestId": "startup",
                "messages": saved_messages, "rendered": [], "reasoning": [],
            },
            {"type": "cost_update", "sessionId": "saved", "estimate": {"total": 1}},
            failure,
        ]
    else:
        assert frames == [failure]
        main.db_reader.get_messages.assert_not_called()
    acp = FakeACPClient.instances[-1]
    assert acp.stopped and not acp.suppress_updates
    assert ms.acp is None and ms.session_id is None
    registered = "saved" if transition != "new_session" else f"fake-{id(acp)}"
    assert main.session_manager.get(registered) is None
    assert ms.active_transition_task is None
    main.model_registry.set_last_used_model.assert_called_once_with("selected")


async def test_silent_compaction_restart_failure_is_only_a_prompt_error(connection, monkeypatch, tmp_path):
    ws, ms = connection
    write_rule(tmp_path)
    main.db_reader.get_session.return_value = {"working_directory": str(tmp_path), "model": "original"}
    await ws.to_receive.put({"type": "load_history", "sessionId": "saved"})
    await receive_until(ws, "session_resumed")
    old_acp = ms.acp
    main.db_reader.get_messages.reset_mock()
    main.db_reader.get_rendered_commits.reset_mock()
    monkeypatch.setattr(FakeACPClient, "load_session", AsyncMock(side_effect=RuntimeError("reload failed")))
    await ws.to_receive.put({
        "type": "prompt", "sessionId": "saved", "model": "gpt-6-1-sol-max",
        "text": "hello", "requestId": "prompt",
    })
    assert await receive_until(ws, "error") == [{
        "type": "error", "sessionId": "saved", "requestId": "prompt",
        "operation": "prompt",
        "message": "Prompt not sent: could not switch the Devin CLI to 'gpt-6-1-sol-max' (reload failed).",
    }]
    assert old_acp.stopped and FakeACPClient.instances[-1].stopped
    assert not FakeACPClient.instances[-1].suppress_updates
    assert ms.acp is None and ms.session_id is None
    assert all(not acp.prompt_calls for acp in FakeACPClient.instances)
    main.db_reader.get_messages.assert_not_called()
    main.db_reader.get_rendered_commits.assert_not_called()
    assert ws.sent.empty()


@pytest.mark.parametrize("update_type", ["tool_call_update", "agent_message_chunk", "agent_thought_chunk"])
@pytest.mark.parametrize("delivered", [True, False])
async def test_live_handoff_keeps_generation_and_replays_buffer(connection, monkeypatch, update_type, delivered):
    ws, anonymous = connection
    existing = ManagedSession("live")
    existing.acp = FakeACPClient(None, model="live-model", permission_mode="default")
    existing.model = "live-model"
    existing.permission_mode = "default"
    existing.generation_id = 91
    buffered = {"sessionId": "live", "update": {"type": update_type, "chunk": "missed"}}
    existing.last_session_update = buffered
    main.session_manager.register(existing, "live")
    main.db_reader.get_session.side_effect = AssertionError("Live handoff must not read history")
    main.model_registry.set_last_used_model.side_effect = AssertionError("Live handoff must keep its model")
    orphan_started = asyncio.Event()

    async def orphan():
        orphan_started.set()
        await asyncio.Future()

    timer = asyncio.create_task(orphan())
    existing.orphan_timer = timer
    await orphan_started.wait()
    original_send = ws.send_json

    async def send(frame):
        assert existing.websocket is ws and existing.orphan_timer is None
        if frame["type"] == "session_update" and not delivered:
            raise OSError("socket unavailable")
        await original_send(frame)

    monkeypatch.setattr(ws, "send_json", send)
    try:
        await ws.to_receive.put({
            "type": "resume_session", "sessionId": "live", "model": "ignored-model",
            "permissionMode": "bypass", "requestId": "reconnect",
        })
        frames = await receive_until(ws, "session_update" if delivered else "session_resumed")
        assert frames[0] == {"type": "session_resumed", "sessionId": "live", "requestId": "reconnect"}
        if delivered:
            assert frames[1] == {"type": "session_update", **buffered}
        assert existing.last_session_update is (
            None if delivered and update_type != "tool_call_update" else buffered
        )
        assert existing.generation_id == 91 and anonymous.generation_id == 1
        assert existing.model == "live-model" and existing.permission_mode == "default"
        assert existing.acp.set_model_calls == existing.acp.set_mode_calls == []
        assert not existing.acp.stopped and len(FakeACPClient.instances) == 1
        assert existing.active_transition_task is None
        with pytest.raises(asyncio.CancelledError):
            await timer
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)


@pytest.mark.parametrize("transition", ["new_session", "load_history", "resume_session"])
@pytest.mark.parametrize("cleanup", ["same-owner", "replacement", "replacement-stop-error"])
async def test_cancelled_startup_resolves_current_owner_after_stop(
    patched_deps, monkeypatch, transition, cleanup,
):
    """Cancellation cannot clear a replacement's ACP or transition task."""
    ms = ManagedSession()
    ms.generation_id = 4
    binding = [ms]
    replacement = ManagedSession("replacement")
    replacement.generation_id = 4
    replacement_acp = MagicMock()
    replacement.acp = replacement_acp
    replacement_task = MagicMock(spec=asyncio.Task)
    replacement.active_transition_task = replacement_task
    db = MagicMock()
    db.get_session.return_value = {"working_directory": "/tmp", "model": "saved-model"}
    manager = MagicMock()
    manager.get.return_value = None
    send = AsyncMock(return_value=True)
    send_cost = AsyncMock()
    started = asyncio.Event()
    stopped = []

    def replace_session(session):
        binding[0] = session

    def create_acp(cwd, model, permission_mode):
        return FakeACPClient(AsyncMock(), cwd=cwd, model=model, permission_mode=permission_mode)

    async def blocked_startup(self, *args):
        started.set()
        await asyncio.Future()

    async def stop(self):
        assert self.suppress_updates is False
        stopped.append(self)
        if cleanup != "same-owner":
            replace_session(replacement)
        if cleanup == "replacement-stop-error":
            raise RuntimeError("stop failed")

    monkeypatch.setattr(FakeACPClient, "new_session", blocked_startup)
    monkeypatch.setattr(FakeACPClient, "load_session", blocked_startup)
    monkeypatch.setattr(FakeACPClient, "stop", stop)
    coordinator = SessionTransitionCoordinator(
        current_session=lambda: binding[0],
        replace_session=replace_session,
        websocket=MockWebSocket(),
        create_acp=create_acp,
        send=send,
        get_db_reader=lambda: db,
        get_db_watcher=MagicMock(),
        get_transcript_watcher=MagicMock(),
        get_session_manager=lambda: manager,
        get_model_registry=MagicMock(),
        reasoning_steps=MagicMock(),
        send_cost_update=send_cost,
        logger=logging.getLogger("test.transitions"),
    )
    if transition == "new_session":
        operation = coordinator.new_session("/tmp", "selected", None, "startup", 4)
    else:
        operation = getattr(coordinator, transition)("saved", "selected", None, "startup", 4)
    task = asyncio.create_task(operation)
    ms.active_transition_task = task
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        local_acp = ms.acp
        assert local_acp.suppress_updates is (transition != "new_session")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == [local_acp]
        assert local_acp.suppress_updates is False
        if cleanup == "same-owner":
            assert ms.acp is None and ms.active_transition_task is None
        else:
            assert binding[0] is replacement
            assert replacement.acp is replacement_acp
            assert replacement.active_transition_task is replacement_task
        manager.register.assert_not_called()
        send.assert_not_called()
        send_cost.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("transition", ["new_session", "load_history", "resume_session"])
async def test_cancelled_startup_after_registration_rolls_back_registry(
    patched_deps, monkeypatch, transition,
):
    """Cancelling after registration stops ACP and removes the registry entry."""
    ms = ManagedSession()
    ms.generation_id = 4
    db = MagicMock()
    db.get_session.return_value = {"working_directory": "/tmp", "model": "saved-model"}
    manager = SessionManager()
    send_started = asyncio.Event()
    send_blocked = asyncio.Event()
    success_frame = {
        "new_session": "session_created",
        "load_history": "history_loaded",
        "resume_session": "session_resumed",
    }[transition]

    async def send(frame):
        if frame["type"] == success_frame:
            send_started.set()
            await send_blocked.wait()
        return True

    def create_acp(cwd, model, permission_mode):
        return FakeACPClient(AsyncMock(), cwd=cwd, model=model, permission_mode=permission_mode)

    coordinator = SessionTransitionCoordinator(
        current_session=lambda: ms,
        replace_session=lambda session: None,
        websocket=MockWebSocket(),
        create_acp=create_acp,
        send=send,
        get_db_reader=lambda: db,
        get_db_watcher=MagicMock(),
        get_transcript_watcher=MagicMock(),
        get_session_manager=lambda: manager,
        get_model_registry=MagicMock(),
        reasoning_steps=MagicMock(return_value=[]),
        send_cost_update=AsyncMock(),
        logger=logging.getLogger("test.transitions"),
    )
    if transition == "new_session":
        operation = coordinator.new_session("/tmp", "selected", None, "startup", 4)
    else:
        operation = getattr(coordinator, transition)("saved", "selected", None, "startup", 4)
    task = asyncio.create_task(operation)
    ms.active_transition_task = task
    try:
        await asyncio.wait_for(send_started.wait(), timeout=2)
        local_acp = ms.acp
        registered = "saved" if transition != "new_session" else f"fake-{id(local_acp)}"
        assert manager.get(registered) is ms
        task.cancel()
        send_blocked.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert local_acp.stopped and not local_acp.suppress_updates
        assert ms.acp is None and ms.session_id is None
        assert manager.get(registered) is None
        assert ms.active_transition_task is None
    finally:
        send_blocked.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("transition", ["new_session", "load_history", "resume_session"])
@pytest.mark.parametrize("replacement", ["registry", "acp", "both"])
async def test_cancelled_cleanup_does_not_evict_replacement_or_newer_acp(
    patched_deps, monkeypatch, transition, replacement,
):
    """While a cancelled startup's ACP is still stopping, replacements made
    in the meantime must survive: a replacement registry entry, a newer ACP
    on the same managed session, or both with a newer session id."""
    ms = ManagedSession()
    ms.generation_id = 4
    db = MagicMock()
    db.get_session.return_value = {"working_directory": "/tmp", "model": "saved-model"}
    manager = SessionManager()
    send_started = asyncio.Event()
    send_blocked = asyncio.Event()
    stop_started = asyncio.Event()
    stop_blocked = asyncio.Event()
    replacement_ms = ManagedSession("saved")
    replacement_acp = MagicMock()

    async def send(frame):
        send_started.set()
        await send_blocked.wait()
        return True

    async def blocked_stop(self):
        self.stopped = True
        stop_started.set()
        await stop_blocked.wait()

    def create_acp(cwd, model, permission_mode):
        return FakeACPClient(AsyncMock(), cwd=cwd, model=model, permission_mode=permission_mode)

    monkeypatch.setattr(FakeACPClient, "stop", blocked_stop)
    coordinator = SessionTransitionCoordinator(
        current_session=lambda: ms,
        replace_session=lambda session: None,
        websocket=MockWebSocket(),
        create_acp=create_acp,
        send=send,
        get_db_reader=lambda: db,
        get_db_watcher=MagicMock(),
        get_transcript_watcher=MagicMock(),
        get_session_manager=lambda: manager,
        get_model_registry=MagicMock(),
        reasoning_steps=MagicMock(return_value=[]),
        send_cost_update=AsyncMock(),
        logger=logging.getLogger("test.transitions"),
    )
    if transition == "new_session":
        operation = coordinator.new_session("/tmp", "selected", None, "startup", 4)
    else:
        operation = getattr(coordinator, transition)("saved", "selected", None, "startup", 4)
    task = asyncio.create_task(operation)
    ms.active_transition_task = task
    try:
        await asyncio.wait_for(send_started.wait(), timeout=2)
        local_acp = ms.acp
        registered = "saved" if transition != "new_session" else f"fake-{id(local_acp)}"
        task.cancel()
        send_blocked.set()
        await asyncio.wait_for(stop_started.wait(), timeout=2)
        if replacement in ("registry", "both"):
            manager.register(replacement_ms, registered)
        if replacement in ("acp", "both"):
            ms.acp = replacement_acp
        if replacement == "both":
            ms.session_id = "newer"
        stop_blocked.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert local_acp.stopped
        if replacement == "registry":
            assert manager.get(registered) is replacement_ms
            assert ms.acp is None and ms.session_id is None
        elif replacement == "acp":
            assert manager.get(registered) is ms
            assert ms.acp is replacement_acp
            assert ms.session_id == registered
        else:
            assert manager.get(registered) is replacement_ms
            assert ms.acp is replacement_acp
            assert ms.session_id == "newer"
    finally:
        send_blocked.set()
        stop_blocked.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("grace", [0, 30])
async def test_stale_endpoint_disconnect_cannot_detach_rebound_session(patched_deps, grace):
    """The old endpoint's ``finally`` must not detach a rebound session.

    Both endpoint tasks overlap: the second endpoint resumes the surviving
    session while the first socket is still attached. When the first
    endpoint later disconnects, its detach must leave the rebound socket,
    registry entry, ACP process, and pending permission timer alone.
    """
    main.session_manager.grace_seconds = grace
    first = MockWebSocket()
    first_task = asyncio.create_task(main.websocket_endpoint(first))
    second = MockWebSocket()
    second_task = None
    try:
        acp = await _establish_active_session(first)
        ms = main.session_manager.get("active-session")
        await acp.request_handler(_make_permission_request())
        original = await asyncio.wait_for(first.sent.get(), timeout=2)
        pending = ms.permission_requests["tc-1"]
        timer = pending["timeout_task"]

        second_task = asyncio.create_task(main.websocket_endpoint(second))
        await second.to_receive.put(
            {"type": "resume_session", "sessionId": "active-session", "requestId": "r2"}
        )
        assert (await asyncio.wait_for(second.sent.get(), timeout=2)) == {
            "type": "session_resumed", "sessionId": "active-session", "requestId": "r2",
        }
        replay = await asyncio.wait_for(second.sent.get(), timeout=2)
        assert replay == original

        await first.to_receive.put(WebSocketDisconnect())
        await asyncio.wait_for(first_task, timeout=5)

        assert ms.websocket is second
        assert ms.orphan_timer is None
        assert main.session_manager.get("active-session") is ms
        assert not acp.stopped
        assert not timer.done()
        assert ms.permission_requests["tc-1"] is pending

        await acp.notification_handler({
            "method": "session/update",
            "params": {
                "sessionId": "active-session",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "after handoff"},
                },
            },
        })
        update = await asyncio.wait_for(second.sent.get(), timeout=2)
        assert update["type"] == "session_update" and update["sessionId"] == "active-session"
        assert first.sent.empty()

        await second.to_receive.put(WebSocketDisconnect())
        await asyncio.wait_for(second_task, timeout=5)
        second_task = None
        if grace:
            assert ms.websocket is None
            assert ms.orphan_timer is not None and not ms.orphan_timer.done()
            assert not acp.stopped
            assert main.session_manager.get("active-session") is ms
        else:
            assert acp.stopped
            assert main.session_manager.get("active-session") is None
    finally:
        main.session_manager.grace_seconds = 0
        for ws, task in ((first, first_task), (second, second_task)):
            if task is not None and not task.done():
                await _cleanup_endpoint(ws, task)
        surviving = main.session_manager.get("active-session")
        if surviving is not None:
            await main.session_manager.cancel_orphan(surviving)


@pytest.mark.asyncio
async def test_reconnect_replays_pending_permission_frames_on_new_socket(patched_deps):
    """Resume replays each still-pending enriched permission frame once."""
    main.session_manager.grace_seconds = 30
    first = MockWebSocket()
    first_task = asyncio.create_task(main.websocket_endpoint(first))
    second = MockWebSocket()
    second_task = None
    third = MockWebSocket()
    third_task = None
    try:
        acp = await _establish_active_session(first)
        ms = main.session_manager.get("active-session")
        ms.tool_call_snapshots["tc-2"] = {"rawInput": {"command": "cached-detail"}}
        await acp.request_handler(_make_permission_request(request_id="rpc-1"))
        await asyncio.wait_for(first.sent.get(), timeout=2)

        await first.to_receive.put(WebSocketDisconnect())
        await asyncio.wait_for(first_task, timeout=5)
        assert ms.websocket is None and ms.orphan_timer is not None

        await acp.request_handler(
            _make_permission_request(request_id="rpc-2", tool_call_id="tc-2")
        )
        await acp.request_handler(_make_permission_request(request_id="rpc-1b"))
        pend1 = ms.permission_requests["tc-1"]
        pend2 = ms.permission_requests["tc-2"]
        timer1, timer2 = pend1["timeout_task"], pend2["timeout_task"]
        payload1, payload2 = pend1["display_payload"], pend2["display_payload"]
        assert payload1["requestId"] == "rpc-1b"
        assert payload2["data"]["detail"] == "cached-detail"
        assert first.sent.empty()

        main.db_reader.get_messages.reset_mock()
        second_task = asyncio.create_task(main.websocket_endpoint(second))
        await second.to_receive.put(
            {"type": "resume_session", "sessionId": "active-session", "requestId": "r2"}
        )
        assert (await asyncio.wait_for(second.sent.get(), timeout=2)) == {
            "type": "session_resumed", "sessionId": "active-session", "requestId": "r2",
        }
        replay1 = await asyncio.wait_for(second.sent.get(), timeout=2)
        replay2 = await asyncio.wait_for(second.sent.get(), timeout=2)
        assert replay1 == payload1 and replay1 is not payload1
        assert replay2 == payload2 and replay2 is not payload2
        assert replay1["toolCallId"] == "tc-1" and replay2["toolCallId"] == "tc-2"
        assert len(FakeACPClient.instances) == 1
        main.db_reader.get_messages.assert_not_called()
        assert ms.permission_requests["tc-1"] is pend1
        assert ms.permission_requests["tc-2"] is pend2
        assert pend1["timeout_task"] is timer1 and not timer1.done()
        assert pend2["timeout_task"] is timer2 and not timer2.done()

        await second.to_receive.put({
            "type": "permission_response", "sessionId": "active-session",
            "toolCallId": "tc-1", "requestId": "corr-1", "decision": "allow",
        })
        assert (await asyncio.wait_for(second.sent.get(), timeout=2)) == {
            "type": "permission_received", "sessionId": "active-session",
            "toolCallId": "tc-1", "requestId": "corr-1",
            "acpRequestId": "rpc-1b", "decision": "allow",
        }
        assert acp.send_response_calls == [
            (("rpc-1b", {"outcome": {"outcome": "selected", "optionId": "opt-allow"}}), {})
        ]
        assert "tc-1" not in ms.permission_requests

        await second.to_receive.put(WebSocketDisconnect())
        await asyncio.wait_for(second_task, timeout=5)
        second_task = None
        third_task = asyncio.create_task(main.websocket_endpoint(third))
        await third.to_receive.put(
            {"type": "resume_session", "sessionId": "active-session", "requestId": "r3"}
        )
        assert (await asyncio.wait_for(third.sent.get(), timeout=2)) == {
            "type": "session_resumed", "sessionId": "active-session", "requestId": "r3",
        }
        replay3 = await asyncio.wait_for(third.sent.get(), timeout=2)
        assert replay3 == payload2 and replay3["toolCallId"] == "tc-2"
        assert third.sent.empty()
        assert ms.permission_requests["tc-2"] is pend2
        assert pend2["timeout_task"] is timer2 and not timer2.done()
    finally:
        main.session_manager.grace_seconds = 0
        for ws, task in ((first, first_task), (second, second_task), (third, third_task)):
            if task is not None and not task.done():
                await _cleanup_endpoint(ws, task)
        surviving = main.session_manager.get("active-session")
        if surviving is not None:
            await main.session_manager.cancel_orphan(surviving)


INVALID_SESSION_IDS = [
    "../evil",
    "..\\..\\transcripts\\secret",
    "\\\\server\\share\\s",
    "C:\\Windows\\win.ini",
    "not a session",
    "-dash",
    "_under",
    "x" * 129,
    "x.json",
    "NUL",
    "con",
    "id\x00x",
    "ünïcödé",
    123,
    {"nested": 1},
    ["a"],
    True,
]


@pytest.mark.parametrize("transition", ["view_history", "load_history"])
@pytest.mark.parametrize("sid", INVALID_SESSION_IDS)
async def test_invalid_history_ids_rejected_without_side_effects(connection, transition, sid):
    ws, ms = connection
    old_sid = await create_session(ws)
    old_acp = ms.acp
    generation = ms.generation_id
    await ws.to_receive.put({"type": transition, "sessionId": sid, "requestId": "bad"})
    assert await receive_until(ws, "error") == [
        {"type": "error", "message": "Invalid session ID", "requestId": "bad"}
    ]
    assert ms.generation_id == generation
    assert ms.session_id == old_sid and ms.acp is old_acp and not old_acp.stopped
    assert ms.active_transition_task is None and ms.active_turn_task is None
    assert len(FakeACPClient.instances) == 1
    main.db_reader.get_session.assert_not_called()
    main.db_reader.get_messages.assert_not_called()
    main.db_reader.get_rendered_commits.assert_not_called()
    main.db_watcher.stop_watching.assert_not_called()
    main.transcript_watcher.stop_watching.assert_not_called()

    main.db_reader.get_session.return_value = {"working_directory": "/tmp"}
    main.db_reader.get_messages.return_value = []
    main.db_reader.get_rendered_commits.return_value = []
    main.transcript_watcher.get_reasoning_steps.return_value = []
    main.db_reader.get_reasoning_steps.return_value = []
    await ws.to_receive.put({"type": "view_history", "sessionId": "saved", "requestId": "ok"})
    frames = await receive_until(ws, "history_loaded")
    assert frames[-1]["sessionId"] == "saved"
    assert frames[-1]["requestId"] == "ok"
    assert frames[-1]["readOnly"] is True


@pytest.mark.parametrize("sid", INVALID_SESSION_IDS)
async def test_invalid_resume_ids_send_resume_failed_without_side_effects(connection, sid):
    ws, ms = connection
    old_sid = await create_session(ws)
    old_acp = ms.acp
    generation = ms.generation_id
    await ws.to_receive.put({"type": "resume_session", "sessionId": sid, "requestId": "bad"})
    assert await receive_until(ws, "resume_failed") == [{
        "type": "resume_failed",
        "sessionId": sid if isinstance(sid, str) else "",
        "message": "Invalid session ID",
        "requestId": "bad",
    }]
    assert ms.generation_id == generation
    assert ms.session_id == old_sid and ms.acp is old_acp and not old_acp.stopped
    assert ms.active_transition_task is None and ms.active_turn_task is None
    assert len(FakeACPClient.instances) == 1
    main.db_reader.get_session.assert_not_called()
    main.db_reader.get_messages.assert_not_called()
    main.db_watcher.stop_watching.assert_not_called()
    main.transcript_watcher.stop_watching.assert_not_called()

    main.db_reader.get_session.return_value = {"working_directory": "/tmp"}
    main.db_reader.get_messages.return_value = []
    main.db_reader.get_rendered_commits.return_value = []
    main.transcript_watcher.get_reasoning_steps.return_value = []
    main.db_reader.get_reasoning_steps.return_value = []
    await ws.to_receive.put({"type": "view_history", "sessionId": "saved", "requestId": "ok"})
    frames = await receive_until(ws, "history_loaded")
    assert frames[-1]["sessionId"] == "saved"
    assert frames[-1]["requestId"] == "ok"


async def test_empty_and_missing_resume_ids_keep_existing_behavior(connection):
    ws, ms = connection
    await ws.to_receive.put({"type": "resume_session", "requestId": "r1"})
    assert await receive_until(ws, "resume_failed") == [{
        "type": "resume_failed", "sessionId": "",
        "message": "No session ID", "requestId": "r1",
    }]
    await ws.to_receive.put({"type": "resume_session", "sessionId": "", "requestId": "r2"})
    assert await receive_until(ws, "resume_failed") == [{
        "type": "resume_failed", "sessionId": "",
        "message": "No session ID", "requestId": "r2",
    }]
    assert ms.generation_id == 0
    main.db_reader.get_session.assert_not_called()


async def test_empty_view_and_load_ids_are_ignored(connection):
    ws, ms = connection
    await ws.to_receive.put({"type": "view_history", "sessionId": "", "requestId": "v"})
    await ws.to_receive.put({"type": "load_history", "sessionId": "", "requestId": "l"})
    await ws.to_receive.put({"type": "view_history", "requestId": "v2"})
    await ws.to_receive.put({"type": "load_history", "requestId": "l2"})
    await asyncio.sleep(0.05)
    assert ws.sent.empty()
    assert ms.generation_id == 0
    main.db_reader.get_session.assert_not_called()
