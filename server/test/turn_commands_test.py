"""Prompt preparation and cancellation at the WebSocket command boundary."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from server import main
from server.test.compaction_settings_test import write_rule
from server.test.main_test import patched_deps
from server.session_manager import ManagedSession
from server.test.session_transitions_test import connection, create_session, receive_until
from server.turn_commands import TurnCommands


async def pending_operation(events=None, label="operation", cleanup=None):
    started = asyncio.Event()

    async def run():
        started.set()
        try:
            await asyncio.Future()
        finally:
            if events is not None:
                events.append(label)
            if cleanup is not None:
                await cleanup()

    task = asyncio.create_task(run())
    await started.wait()
    return task


async def command_and_barrier(ws, command):
    """An invalid resume confirms the preceding command finished receiving."""
    await ws.to_receive.put(command)
    await ws.to_receive.put({"type": "resume_session", "sessionId": "", "requestId": "barrier"})
    frames = await receive_until(ws, "resume_failed")
    assert frames[-1] == {
        "type": "resume_failed", "sessionId": "", "requestId": "barrier", "message": "No session ID",
    }
    return frames[:-1]


@pytest.mark.parametrize(
    "transition,has_acp,active_sid,requested_sid,message,error_sid",
    [
        ("active", False, "active", "wrong", "Session is still resuming.", "wrong"),
        ("active", False, "active", None, "Session is still resuming.", "active"),
        ("done", False, "active", "wrong", "Session id does not match the active backend session.", "wrong"),
        (None, False, "active", None, "No active ACP session", "active"),
        (None, False, None, "requested", "No active ACP session", None),
        (None, True, None, None, "No session ID", None),
        (None, True, None, "", "No session ID", ""),
        (None, True, "active", "", "Session id does not match the active backend session.", ""),
    ],
)
async def test_prompt_validation_precedence_preserves_tasks_and_generation(
    connection, transition, has_acp, active_sid, requested_sid, message, error_sid,
):
    ws, ms = connection
    ms.session_id = active_sid
    ms.acp = AsyncMock() if has_acp else None
    ms.generation_id = 7
    turn = await pending_operation()
    ms.active_turn_task = turn
    if transition == "active":
        ms.active_transition_task = await pending_operation()
    elif transition == "done":
        completed = asyncio.create_task(asyncio.sleep(0))
        await completed
        ms.active_transition_task = completed
    transition_task = ms.active_transition_task
    await ws.to_receive.put({
        "type": "prompt", "sessionId": requested_sid, "requestId": "validation",
    })
    assert await receive_until(ws, "error") == [{
        "type": "error", "sessionId": error_sid, "requestId": "validation",
        "operation": "prompt", "message": message,
    }]
    assert ms.generation_id == 7 and ms.active_turn_task is turn and not turn.done()
    assert ms.active_transition_task is transition_task
    if transition == "active":
        assert not transition_task.done()
    main.db_reader.get_session.assert_not_called()
    main.db_watcher.start_watching.assert_not_called()
    main.transcript_watcher.start_watching.assert_not_called()
    main.model_registry.set_last_used_model.assert_not_called()
    main.pricing_history.record_prompt.assert_not_called()


async def test_prompt_preparation_and_scheduling_order(connection, monkeypatch):
    ws, ms = connection
    sid = await create_session(ws)
    events = []
    ms.generation_id = 7

    async def finish_old_turn():
        assert ms.generation_id == 8 and ms.transition_lock.locked()

    old_turn = await pending_operation(events, "old-turn", finish_old_turn)
    ms.active_turn_task = old_turn

    def before_turn(event):
        assert ms.generation_id == 7 and not old_turn.done()
        assert not ms.transition_lock.locked()
        events.append(event)

    def read_session(requested_sid):
        assert requested_sid == sid
        before_turn("session-read")
        return {"working_directory": "/tmp", "model": "original"}

    async def set_model(requested_sid, model):
        assert (requested_sid, model) == (sid, "selected")
        before_turn("model-confirmed")
        return "confirmed-model"

    def remember_model(model):
        assert model == ms.model == "confirmed-model"
        before_turn("preference")

    def during_setup(event):
        assert old_turn.done() and ms.generation_id == 8
        assert ms.transition_lock.locked()
        events.append(event)

    main.db_reader.get_session.side_effect = read_session
    monkeypatch.setattr(ms.acp, "set_session_model", set_model)
    main.model_registry.set_last_used_model.side_effect = remember_model
    main.db_watcher.start_watching.side_effect = lambda *args: during_setup("db-watcher")
    main.transcript_watcher.start_watching.side_effect = lambda *args: during_setup("transcript-watcher")
    main.model_registry.refresh.side_effect = lambda: during_setup("catalogue")
    model_info = {"id": "confirmed-model"}

    def lookup_model(model):
        assert model == "confirmed-model"
        during_setup("model-lookup")
        return model_info

    def record_price(session_id, model):
        assert session_id == sid and model is model_info
        during_setup("pricing")

    main.model_registry.get_model.side_effect = lookup_model
    main.pricing_history.record_prompt.side_effect = record_price
    started = asyncio.Event()
    images = [{"type": "image", "data": "synthetic"}]

    async def prompt(session_id, text, supplied_images):
        assert (session_id, text) == (sid, "hello") and supplied_images is images
        assert ms.active_turn_task is not old_turn and not ms.active_turn_task.done()
        assert not ms.transition_lock.locked()
        events.append("prompt")
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(ms.acp, "prompt", prompt)
    await ws.to_receive.put({
        "type": "prompt", "model": "selected", "text": "hello", "images": images, "requestId": "prompt",
    })
    await asyncio.wait_for(started.wait(), timeout=2)
    assert events == [
        "session-read", "model-confirmed", "preference", "old-turn", "db-watcher",
        "transcript-watcher", "catalogue", "model-lookup", "pricing", "prompt",
    ]


@pytest.mark.parametrize(
    "payload,expected_text,expected_images",
    [({}, "", []), ({"text": None, "images": None}, None, []),
     ({"text": "hello", "images": 0}, "hello", []),
     ({"text": "hello", "images": {"opaque": "image"}}, "hello", {"opaque": "image"})],
)
async def test_prompt_preserves_wire_values(connection, monkeypatch, payload, expected_text, expected_images):
    ws, ms = connection
    sid = await create_session(ws)
    started = asyncio.Event()
    received = []

    async def prompt(session_id, text, images):
        received.append((session_id, text, images))
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(ms.acp, "prompt", prompt)
    main.model_registry.set_last_used_model.reset_mock()
    await ws.to_receive.put({"type": "prompt", "requestId": "raw", **payload})
    await asyncio.wait_for(started.wait(), timeout=2)
    assert received == [(sid, expected_text, expected_images)]
    main.model_registry.set_last_used_model.assert_not_called()


@pytest.mark.parametrize("failure", ["session-read", "compaction"])
async def test_model_preparation_read_failures_keep_current_turn(connection, monkeypatch, failure):
    ws, ms = connection
    sid = await create_session(ws)
    ms.generation_id = 7
    old_turn = await pending_operation()
    ms.active_turn_task = old_turn
    main.model_registry.set_last_used_model.reset_mock()
    if failure == "session-read":
        main.db_reader.get_session.side_effect = RuntimeError("setup failed")
    else:
        def compaction(*args):
            raise RuntimeError("setup failed")

        monkeypatch.setattr(main, "compaction_thresholds", compaction)
    await ws.to_receive.put({"type": "prompt", "model": "selected", "requestId": "model"})
    assert await receive_until(ws, "error") == [{
        "type": "error", "sessionId": sid, "requestId": "model", "operation": "prompt",
        "message": "Prompt not sent: could not switch the Devin CLI to 'selected' (setup failed).",
    }]
    assert ms.generation_id == 7 and ms.model == "original"
    assert ms.active_turn_task is old_turn and not old_turn.done()
    main.model_registry.set_last_used_model.assert_not_called()
    main.pricing_history.record_prompt.assert_not_called()


@pytest.mark.parametrize(
    "has_transition,has_turn,has_acp,ack",
    [(True, False, True, "transition_cancelled"),
     (True, True, True, "turn_cancelled"),
     (False, True, True, "turn_cancelled"),
     (False, False, True, "turn_cancelled"),
     (False, False, False, "turn_cancelled")],
)
async def test_cancel_cleanup_order_and_acknowledgment(connection, has_transition, has_turn, has_acp, ack):
    ws, ms = connection
    ms.session_id = "active"
    ms.generation_id = 7
    events = []
    if has_transition:
        ms.active_transition_task = await pending_operation(events, "transition")
    if has_turn:
        ms.active_turn_task = await pending_operation(events, "turn")
    if has_acp:
        ms.acp = AsyncMock()

        async def reject(reason):
            assert reason == "Turn cancelled"
            events.append("permissions")

        async def cancel(sid):
            assert sid == "active"
            events.append("acp-cancel")

        ms.reject_all_pending_permissions = reject
        ms.acp.cancel.side_effect = cancel
    assert await command_and_barrier(ws, {"type": "cancel"}) == [{
        "type": ack, "sessionId": "active", "requestId": "",
    }]
    assert events == (["transition"] if has_transition else []) + (
        ["turn"] if has_turn else []
    ) + (["permissions", "acp-cancel"] if has_acp else [])
    assert ms.generation_id == 7


@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_cancel_rpc_error_is_correlated_and_delivery_failure_is_tolerated(connection, monkeypatch, caplog, delivery_fails):
    ws, ms = connection
    sid = await create_session(ws)
    ms.acp.cancel = AsyncMock(side_effect=RuntimeError("cancel failed"))
    original_send = ws.send_json
    attempted = []

    async def send(frame):
        if frame["type"] == "error":
            attempted.append(frame)
            if delivery_fails:
                raise OSError("socket failed")
        await original_send(frame)

    monkeypatch.setattr(ws, "send_json", send)
    frames = await command_and_barrier(ws, {"type": "cancel"})
    failure = {"type": "error", "sessionId": sid, "requestId": None, "operation": "cancel", "message": "cancel failed"}
    assert attempted == [failure]
    assert frames == ([] if delivery_fails else [failure])
    assert "Cancel failed: cancel failed" in caplog.messages
    if delivery_fails:
        assert "Failed to send cancel error: socket failed" in caplog.messages
    assert not any(message.startswith("WebSocket error:") for message in caplog.messages)


@pytest.mark.parametrize("has_transition", [False, True])
async def test_cancel_without_session_id_keeps_warning_and_sends_no_ack(connection, caplog, has_transition):
    ws, ms = connection
    if has_transition:
        ms.active_transition_task = await pending_operation()
    assert await command_and_barrier(ws, {"type": "cancel"}) == []
    ack = "transition_cancelled" if has_transition else "turn_cancelled"
    assert f"cancel handler skipped {ack}: no sessionId" in caplog.messages


@pytest.mark.parametrize("has_transition", [False, True])
async def test_cancel_ack_delivery_failure_keeps_receive_loop(connection, monkeypatch, caplog, has_transition):
    ws, ms = connection
    ms.session_id = "active"
    if has_transition:
        ms.active_transition_task = await pending_operation()
    ack = "transition_cancelled" if has_transition else "turn_cancelled"
    original_send = ws.send_json

    async def send(frame):
        if frame["type"] == ack:
            raise OSError("socket failed")
        await original_send(frame)

    monkeypatch.setattr(ws, "send_json", send)
    assert await command_and_barrier(ws, {"type": "cancel", "requestId": "ack"}) == []
    assert f"Failed to send {ack}: socket failed" in caplog.messages


async def test_prompt_rejection_delivery_failure_reaches_endpoint_cleanup(connection, monkeypatch, caplog):
    ws, ms = connection
    sid = await create_session(ws)
    acp = ms.acp
    detached = asyncio.Event()
    original_detach = main.session_manager.detach_socket

    async def detach(session, socket):
        await original_detach(session, socket)
        detached.set()

    async def send(frame):
        assert frame == {
            "type": "error", "sessionId": "wrong", "requestId": "prompt", "operation": "prompt",
            "message": "Session id does not match the active backend session.",
        }
        raise OSError("socket failed")

    monkeypatch.setattr(main.session_manager, "detach_socket", detach)
    monkeypatch.setattr(ws, "send_json", send)
    await ws.to_receive.put({"type": "prompt", "sessionId": "wrong", "requestId": "prompt"})
    await asyncio.wait_for(detached.wait(), timeout=2)
    assert acp.stopped and main.session_manager.get(sid) is None
    assert "WebSocket error: socket failed" in caplog.messages


async def test_cancel_resolves_direct_reply_after_awaited_cleanup(connection, monkeypatch):
    ws, ms = connection
    ms.session_id = "active"
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()

    async def cleanup():
        cleanup_started.set()
        await finish_cleanup.wait()

    ms.active_turn_task = await pending_operation(cleanup=cleanup)
    await ws.to_receive.put({"type": "cancel", "requestId": "cancel"})
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        send = AsyncMock(wraps=ws.send_json)
        monkeypatch.setattr(ws, "send_json", send)
        finish_cleanup.set()
        assert await receive_until(ws, "turn_cancelled") == [{
            "type": "turn_cancelled", "sessionId": "active", "requestId": "cancel",
        }]
        send.assert_awaited_once()
    finally:
        finish_cleanup.set()


@pytest.mark.parametrize("result", ["no-acp", "wrong-session"])
async def test_restart_unready_error_reports_intended_explanation(
    connection, monkeypatch, tmp_path, result,
):
    """The failed restart invariant reports its own reason, not a protocol error."""
    ws, ms = connection
    write_rule(tmp_path)
    await ws.to_receive.put({"type": "new_session", "cwd": str(tmp_path), "model": "original"})
    sid = (await receive_until(ws, "session_created"))[0]["sessionId"]
    main.db_reader.get_session.return_value = {"working_directory": str(tmp_path), "model": "original"}
    generation = ms.generation_id

    async def reload(self, *args, **kwargs):
        if result == "no-acp":
            ms.acp = None
        else:
            ms.session_id = "other"

    monkeypatch.setattr(main.SessionTransitionCoordinator, "load_history", reload)
    await ws.to_receive.put({"type": "prompt", "model": "gpt-6-1-sol-max", "requestId": "restart"})
    assert await receive_until(ws, "error") == [{
        "type": "error", "sessionId": sid, "requestId": "restart", "operation": "prompt",
        "message": (
            "Prompt not sent: could not switch the Devin CLI to 'gpt-6-1-sol-max' "
            "(Could not resume the session with the selected model's compaction settings)."
        ),
    }]
    assert ms.generation_id == generation and ms.active_turn_task is None


@pytest.fixture
def isolated_commands():
    """Construct command handlers without an endpoint or application services."""
    ms = ManagedSession("active")
    ms.generation_id = 7
    ms.model = "old-model"
    ms.permission_mode = "bypass"
    ms.acp = AsyncMock()
    ms.acp.cwd = "/project"
    ms.acp.compaction_thresholds = None
    ms.acp.set_session_model.return_value = "confirmed-model"
    services = SimpleNamespace(
        db=MagicMock(), registry=MagicMock(), thresholds=MagicMock(return_value=None),
        transitions=SimpleNamespace(load_history=AsyncMock()), reply=AsyncMock(),
    )
    services.db.get_session.return_value = {"working_directory": "/saved-project"}
    commands = TurnCommands(
        reply=services.reply,
        get_db_reader=lambda: services.db,
        get_model_registry=lambda: services.registry,
        compaction_thresholds=services.thresholds,
        transitions=services.transitions,
        logger=logging.getLogger("test.commands"),
    )
    return commands, ms, services


@pytest.mark.parametrize("changed_thresholds", [False, True])
async def test_missing_history_does_not_block_chat_or_silently_change_compaction(isolated_commands, changed_thresholds):
    commands, ms, services = isolated_commands
    services.db.get_session.side_effect = FileNotFoundError("No database")
    services.thresholds.return_value = (10, 20, 30) if changed_thresholds else None
    prepared = await commands.prepare_prompt(ms, {"model": "new-model", "text": "hello", "requestId": "prompt"})
    services.transitions.load_history.assert_not_awaited()
    if changed_thresholds:
        assert prepared is None
        assert "compaction" in services.reply.call_args.args[0]["message"]
        ms.acp.set_session_model.assert_not_awaited()
    else:
        assert prepared is not None and prepared.text == "hello"
        ms.acp.set_session_model.assert_awaited_once()
        services.reply.assert_not_awaited()


@pytest.mark.parametrize("result", ["ready", "no-acp", "wrong-session"])
async def test_silent_restart_requires_the_captured_session_to_be_ready(isolated_commands, result):
    commands, ms, services = isolated_commands
    services.thresholds.return_value = (10, 20, 30)
    original_acp = ms.acp
    replacement_acp = AsyncMock()
    replacement_acp.set_session_model.return_value = "confirmed-model"

    async def reload(*args, **kwargs):
        ms.acp = None if result == "no-acp" else replacement_acp
        if result == "wrong-session":
            ms.session_id = "other"

    services.transitions.load_history.side_effect = reload
    prepared = await commands.prepare_prompt(ms, {"model": "new-model", "text": "hello", "requestId": "prompt"})
    services.transitions.load_history.assert_awaited_once_with(
        "active", "new-model", "bypass", None, 7, notify_client=False,
    )
    assert ms.generation_id == 7 and ms.active_turn_task is None
    original_acp.set_session_model.assert_not_awaited()
    if result == "ready":
        assert prepared.session_id == "active" and prepared.text == "hello"
        replacement_acp.set_session_model.assert_awaited_once_with("active", "new-model")
        services.registry.set_last_used_model.assert_called_once_with("confirmed-model")
        services.reply.assert_not_awaited()
    else:
        assert prepared is None
        replacement_acp.set_session_model.assert_not_awaited()
        services.registry.set_last_used_model.assert_not_called()
        services.reply.assert_awaited_once_with({
            "type": "error", "sessionId": "active", "requestId": "prompt", "operation": "prompt",
            "message": (
                "Prompt not sent: could not switch the Devin CLI to 'new-model' "
                "(Could not resume the session with the selected model's compaction settings)."
            ),
        })


async def test_command_services_resolve_after_construction(isolated_commands):
    commands, ms, services = isolated_commands
    original_db, original_registry = services.db, services.registry
    services.db = MagicMock()
    services.registry = MagicMock()
    images = [{"opaque": "image"}]
    prepared = await commands.prepare_prompt(ms, {"model": "new-model", "images": images})
    assert prepared.session_id == "active" and prepared.images is images and prepared.request_id is None
    services.db.get_session.assert_called_once_with("active")
    original_db.get_session.assert_not_called()
    services.thresholds.assert_called_once_with("/project", "new-model")
    services.registry.set_last_used_model.assert_called_once_with("confirmed-model")
    original_registry.set_last_used_model.assert_not_called()


async def test_cancel_keeps_receive_snapshot_when_cleanup_rebinds_session(isolated_commands):
    commands, ms, services = isolated_commands
    replacement = ManagedSession("replacement")
    replacement.acp = AsyncMock()
    replacement.active_turn_task = await pending_operation()
    binding = [ms]

    async def rebind():
        binding[0] = replacement
        ms.acp = None

    ms.active_transition_task = await pending_operation(cleanup=rebind)
    try:
        await commands.cancel(binding[0], {"requestId": "cancel"})
        assert binding[0] is replacement
        assert not replacement.active_turn_task.done()
        replacement.acp.cancel.assert_not_awaited()
        services.reply.assert_awaited_once_with({
            "type": "transition_cancelled", "sessionId": "active", "requestId": "cancel",
        })
    finally:
        replacement.active_turn_task.cancel()
        await asyncio.gather(replacement.active_turn_task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["prompt-mismatch", "cancel-mismatch", "model-failure"])
async def test_command_rejection_reply_errors_propagate(isolated_commands, operation):
    commands, ms, services = isolated_commands
    services.reply.side_effect = OSError("socket failed")
    old_turn = await pending_operation()
    ms.active_turn_task = old_turn
    try:
        if operation == "cancel-mismatch":
            action = commands.cancel(ms, {"sessionId": "wrong"})
        else:
            if operation == "model-failure":
                ms.acp.set_session_model.side_effect = RuntimeError("model failed")
                payload = {}
            else:
                payload = {"sessionId": "wrong"}
            action = commands.prepare_prompt(ms, payload)
        with pytest.raises(OSError, match="socket failed"):
            await action
        assert ms.generation_id == 7 and ms.active_turn_task is old_turn and not old_turn.done()
        services.reply.assert_awaited_once()
    finally:
        old_turn.cancel()
        await asyncio.gather(old_turn, return_exceptions=True)
