"""Backend tests for Task 3: transitions out of the receive loop.

Verifies that session transitions (``load_history``, ``resume_session``,
``new_session``) run as cancellable background tasks so the WebSocket
receive loop stays responsive, and that a newer transition cancels the
older one without emitting stale messages.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

from server.main import websocket_endpoint
from server.compaction_settings import compaction_thresholds


class FakeACPClient:
    """Controllable ACP client stub.

    ``load_session`` blocks on a class-level ``asyncio.Event`` so tests
    can simulate a long-running ACP startup.  The ``load_started_event``
    is set when ``load_session`` is entered, giving the test a
    deterministic synchronization point.
    """

    load_event: asyncio.Event | None = None
    load_started_event: asyncio.Event | None = None
    # Optional blocking gates for ``prompt`` and ``new_session``.  When set,
    # the corresponding method blocks on the event until the test releases it.
    # ``*_started_event`` is set when the method is entered, giving the test a
    # deterministic synchronization point.
    prompt_event: asyncio.Event | None = None
    prompt_started_event: asyncio.Event | None = None
    new_session_event: asyncio.Event | None = None
    new_session_started_event: asyncio.Event | None = None
    # When set, ``set_session_model`` raises it instead of applying the
    # model, simulating a CLI that rejects or fails to confirm the model.
    set_model_error: Exception | None = None
    instances: list["FakeACPClient"] = []

    @classmethod
    def reset(cls) -> None:
        cls.load_event = None
        cls.load_started_event = None
        cls.prompt_event = None
        cls.prompt_started_event = None
        cls.new_session_event = None
        cls.new_session_started_event = None
        cls.set_model_error = None
        cls.instances = []

    def __init__(self, notification_handler, cmd=None, model=None,
                 permission_mode=None, request_handler=None, on_unavailable=None, cwd=None):
        self.notification_handler = notification_handler
        self.request_handler = request_handler or notification_handler
        self.on_unavailable = on_unavailable
        self.model = model
        self.cwd = cwd
        self.compaction_thresholds = compaction_thresholds(cwd, model)
        self.permission_mode = permission_mode
        self.suppress_updates = False
        self.unavailable = False
        self.stopped = False
        self.loaded_sid: str | None = None
        self.new_session_cwd: str | None = None
        self.prompt_calls: list[tuple] = []
        self.cancel_calls: list[str] = []
        self.send_response_calls: list[tuple] = []
        self.set_model_calls: list[tuple[str, str]] = []
        self.set_mode_calls: list[tuple[str, str]] = []
        type(self).instances.append(self)

    async def start(self) -> None:
        pass

    async def initialize(self) -> dict:
        return {}

    async def new_session(self, cwd: str) -> str:
        self.new_session_cwd = cwd
        if type(self).new_session_started_event is not None:
            type(self).new_session_started_event.set()
        if type(self).new_session_event is not None:
            await type(self).new_session_event.wait()
        return f"fake-{id(self)}"

    async def set_session_model(self, session_id: str, model_id: str) -> str:
        # Record the call so tests can assert the selected model was actually
        # applied to the ACP session. The real client sends a
        # session/set_config_option JSON-RPC request and returns the model the
        # agent confirms as active; the fake just echoes the request back.
        self.set_model_calls.append((session_id, model_id))
        if type(self).set_model_error is not None:
            raise type(self).set_model_error
        return model_id

    async def load_session(self, sid: str, cwd: str) -> dict:
        self.loaded_sid = sid
        if type(self).load_started_event is not None:
            type(self).load_started_event.set()
        if type(self).load_event is not None:
            await type(self).load_event.wait()
        return {}

    async def set_session_mode(self, session_id: str, mode: str) -> str:
        # Record the call so tests can assert bypass mode was actually
        # applied to the ACP session. The real client sends a
        # session/set_config_option JSON-RPC request (configId "mode") and
        # returns the mode the agent confirms as active; the fake just
        # echoes the request back.
        self.set_mode_calls.append((session_id, mode))
        return mode

    async def prompt(self, session_id: str, text: str, images: list | None = None) -> dict:
        self.prompt_calls.append((session_id, text))
        if type(self).prompt_started_event is not None:
            type(self).prompt_started_event.set()
        if type(self).prompt_event is not None:
            await type(self).prompt_event.wait()
        return {"stopReason": "stop"}

    async def stop(self) -> None:
        self.stopped = True

    async def cancel(self, session_id: str) -> None:
        self.cancel_calls.append(session_id)

    async def send_response(self, *args, **kwargs) -> None:
        self.send_response_calls.append((args, kwargs))


class MockWebSocket:
    """Minimal async WebSocket stub for driving ``websocket_endpoint``."""

    def __init__(self):
        self.sent: asyncio.Queue = asyncio.Queue()
        self.to_receive: asyncio.Queue = asyncio.Queue()

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        await self.sent.put(data)

    async def receive_json(self) -> dict:
        item = await self.to_receive.get()
        if isinstance(item, Exception):
            raise item
        return item


def _patch_dependencies() -> list:
    """Return patchers for all module-level dependencies used by the WS endpoint."""
    mock_db = MagicMock()
    mock_db.get_session.return_value = {
        "working_directory": "/tmp",
        "model": "test-model",
    }
    mock_db.get_messages.return_value = []
    mock_db.get_rendered_commits.return_value = []
    mock_db.get_reasoning_steps.return_value = []

    mock_tw = MagicMock()
    mock_tw.get_reasoning_steps.return_value = []
    mock_tw.get_usage_records.return_value = []

    mock_dw = MagicMock()
    mock_mr = MagicMock()
    mock_mr.get_last_used_model.return_value = "test-model"
    # ``get_model`` must return a JSON-serializable dict because
    # ``pricing_history.record_prompt`` and ``CostEstimator`` consume its
    # fields and may persist/serialize them.
    mock_mr.get_model.return_value = {
        "id": "test-model",
        "display_name": "Test Model",
        "tags": [],
        "input_price": None,
        "cached_input_price": None,
        "output_price": None,
        "context_window": 0,
    }

    # ``pricing_history`` is a module-level instance that writes to a real
    # JSON file in production; replace it with a MagicMock so tests never
    # touch the user's real pricing history data.
    mock_ph = MagicMock()

    return [
        patch("server.main.ACPClient", FakeACPClient),
        patch("server.main.db_reader", mock_db),
        patch("server.main.transcript_watcher", mock_tw),
        patch("server.main.db_watcher", mock_dw),
        patch("server.main.model_registry", mock_mr),
        patch("server.main.pricing_history", mock_ph),
    ]


@pytest.fixture
def patched_deps():
    FakeACPClient.reset()
    # Clear the session manager registry between tests and use
    # immediate teardown (grace=0) so existing tests reproduce the
    # old disconnect behaviour.
    from server.session_manager import session_manager as sm
    sm._sessions.clear()
    sm.grace_seconds = 0
    with ExitStack() as stack:
        for p in _patch_dependencies():
            stack.enter_context(p)
        yield
    sm._sessions.clear()


async def _drain_messages(ws: MockWebSocket, count: int, timeout: float = 2.0) -> list[dict]:
    """Collect up to *count* messages from the WebSocket, stopping after *timeout*."""
    messages = []
    for _ in range(count):
        try:
            msg = await asyncio.wait_for(ws.sent.get(), timeout=timeout)
            messages.append(msg)
        except asyncio.TimeoutError:
            break
    return messages


async def _cleanup_endpoint(ws: MockWebSocket, task: asyncio.Task) -> None:
    """Gracefully shut down the endpoint task after a test."""
    await ws.to_receive.put(WebSocketDisconnect())
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError, WebSocketDisconnect, Exception):
        pass
    if not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transition,completion,reason",
    [
        ("new_session", "session_created", "Session replaced"),
        ("view_history", "history_loaded", "Session changed"),
        ("load_history", "session_resumed", "Session changed"),
        ("resume_session", "session_resumed", "Session resumed"),
    ],
)
async def test_transition_releases_previous_session_in_order(
    patched_deps, monkeypatch, transition, completion, reason,
):
    """Replacement finishes turn/permission cleanup before starting another ACP."""
    import server.main as main

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    events = []
    try:
        await ws.to_receive.put({"type": "new_session", "cwd": "/tmp"})
        created = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        old_sid = created["sessionId"]
        ms = main.session_manager.get(old_sid)
        old_acp = ms.acp

        async def pending_task(name, started):
            started.set()
            try:
                await asyncio.Future()
            finally:
                events.append(name)

        turn_started = asyncio.Event()
        timeout_started = asyncio.Event()
        ms.active_turn_task = asyncio.create_task(pending_task("turn", turn_started))
        timeout_task = asyncio.create_task(pending_task("permission_timeout", timeout_started))
        ms.permission_requests["tool-before-transition"] = {
            "request_id": "permission-before-transition",
            "options": [],
            "timeout_task": timeout_task,
        }
        await turn_started.wait()
        await timeout_started.wait()

        async def reject_permission(request_id, *, error):
            assert request_id == "permission-before-transition"
            assert error == {"code": -32600, "message": reason}
            events.append("permission_rejected")

        async def stop_old_acp():
            assert ms.acp is old_acp
            events.append("acp_stopped")

        original_start = FakeACPClient.start

        async def start_next_acp(self):
            assert main.session_manager.get(old_sid) is None
            events.append("next_acp_started")
            await original_start(self)

        monkeypatch.setattr(old_acp, "send_response", reject_permission)
        monkeypatch.setattr(old_acp, "stop", stop_old_acp)
        monkeypatch.setattr(FakeACPClient, "start", start_next_acp)
        main.transcript_watcher.stop_watching.side_effect = lambda sid: events.append("transcript_stopped")
        main.db_watcher.stop_watching.side_effect = lambda sid: events.append("db_stopped")

        await ws.to_receive.put({
            "type": transition,
            "sessionId": "next-session",
            "cwd": "/tmp",
            "requestId": "replace-session",
        })
        while True:
            message = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            if message["type"] == completion:
                break
            assert message["type"] not in ("error", "resume_failed")

        expected = [
            "turn", "permission_timeout", "permission_rejected",
            "acp_stopped", "transcript_stopped", "db_stopped",
        ]
        if transition != "view_history":
            expected.append("next_acp_started")
        assert events == expected
        assert ms.permission_requests == {}
        assert main.session_manager.get(old_sid) is None
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["new_session", "load_history", "resume_session"])
@pytest.mark.parametrize("failure", [None, "model", "bypass"])
async def test_transition_configures_model_before_best_effort_bypass(
    patched_deps, monkeypatch, transition, failure,
):
    """Model failure aborts setup; bypass failure still offers a configured session."""
    import server.main as main

    calls = []
    original_load = FakeACPClient.load_session

    async def load_session(self, sid, cwd):
        assert self.suppress_updates is True
        calls.append("load")
        return await original_load(self, sid, cwd)

    async def set_model(self, sid, model):
        assert self.suppress_updates is False
        assert main.session_manager.get(sid) is None
        assert model == "selected-model"
        calls.append("model")
        if failure == "model":
            raise RuntimeError("model rejected")
        return model

    async def set_mode(self, sid, mode):
        assert main.session_manager.get(sid) is None
        assert mode == "bypass"
        calls.append("bypass")
        if failure == "bypass":
            raise RuntimeError("bypass unavailable")
        return mode

    monkeypatch.setattr(FakeACPClient, "load_session", load_session)
    monkeypatch.setattr(FakeACPClient, "set_session_model", set_model)
    monkeypatch.setattr(FakeACPClient, "set_session_mode", set_mode)
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await ws.to_receive.put({
            "type": transition,
            "sessionId": "configured-session",
            "cwd": "/tmp",
            "model": "selected-model",
            "permissionMode": "bypass",
            "requestId": "configure-session",
        })
        if failure == "model":
            completion = "error" if transition == "new_session" else "resume_failed"
        else:
            completion = "session_created" if transition == "new_session" else "session_resumed"
        while True:
            message = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            if message["type"] == completion:
                break

        expected = [] if transition == "new_session" else ["load"]
        expected.append("model")
        if failure != "model":
            expected.append("bypass")
        assert calls == expected
        acp = FakeACPClient.instances[-1]
        assert acp.suppress_updates is False
        if failure == "model":
            assert acp.stopped is True
            assert main.session_manager.get(message["sessionId"]) is None
        else:
            assert main.session_manager.get(message["sessionId"]).acp is acp
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_view_history_is_read_only_and_does_not_start_acp(patched_deps):
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await ws.to_receive.put(
            {"type": "view_history", "sessionId": "session-A", "requestId": "view-1"}
        )

        messages = await _drain_messages(ws, 3, timeout=2.0)
        history = [message for message in messages if message.get("type") == "history_loaded"]

        assert len(history) == 1
        assert history[0]["sessionId"] == "session-A"
        assert history[0]["requestId"] == "view-1"
        assert history[0]["readOnly"] is True
        assert FakeACPClient.instances == []
        assert not any(message.get("type") == "session_resumed" for message in messages)
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_two_rapid_load_history_only_second_succeeds(patched_deps):
    """Two rapid ``load_history`` messages: only the second session becomes active."""
    load_event = asyncio.Event()
    load_started = asyncio.Event()
    FakeACPClient.load_event = load_event
    FakeACPClient.load_started_event = load_started

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        # Send first load_history.
        await ws.to_receive.put(
            {"type": "load_history", "sessionId": "session-A", "requestId": "r1"}
        )
        # Wait for the first transition to reach load_session.
        await asyncio.wait_for(load_started.wait(), timeout=5.0)
        load_started.clear()

        # Send second load_history — should cancel the first transition.
        await ws.to_receive.put(
            {"type": "load_history", "sessionId": "session-B", "requestId": "r2"}
        )
        # Wait for the second transition to reach load_session.
        await asyncio.wait_for(load_started.wait(), timeout=5.0)

        # Release the load event so the second transition completes.
        load_event.set()

        messages = await _drain_messages(ws, 10, timeout=3.0)

        # Only session-B should have session_resumed / history_loaded.
        resumed = [m for m in messages if m.get("type") == "session_resumed"]
        assert len(resumed) == 1, f"Expected exactly one session_resumed, got {resumed}"
        assert resumed[0]["sessionId"] == "session-B"

        history = [m for m in messages if m.get("type") == "history_loaded"]
        assert len(history) == 1, f"Expected exactly one history_loaded, got {history}"
        assert history[0]["sessionId"] == "session-B"

        # No session_resumed or history_loaded for session-A.
        stale = [
            m for m in messages
            if m.get("type") in ("session_resumed", "history_loaded")
            and m.get("sessionId") == "session-A"
        ]
        assert stale == [], f"First transition emitted stale messages: {stale}"

        # The first ACP instance should have been stopped (cancelled).
        assert len(FakeACPClient.instances) >= 2
        first_acp = FakeACPClient.instances[0]
        assert first_acp.stopped, "First transition's ACP client was not stopped"
        assert first_acp.loaded_sid == "session-A"
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_cancel_during_transition_does_not_wait(patched_deps):
    """``cancel`` during a transition is processed without waiting for the transition to finish."""
    load_event = asyncio.Event()
    load_started = asyncio.Event()
    FakeACPClient.load_event = load_event
    FakeACPClient.load_started_event = load_started

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        # Start a load_history transition that will block indefinitely.
        await ws.to_receive.put(
            {"type": "load_history", "sessionId": "session-X", "requestId": "r1"}
        )
        await asyncio.wait_for(load_started.wait(), timeout=5.0)

        # Send cancel — must be processed without releasing load_event.
        await ws.to_receive.put(
            {"type": "cancel", "sessionId": "session-X", "requestId": "r2"}
        )

        # transition_cancelled should arrive promptly (within 5s).  If the
        # cancel handler waited for the transition to complete, this
        # would hang because load_event is never set.  A transition
        # cancellation is reported as ``transition_cancelled`` (not
        # ``turn_cancelled``) so the frontend does not assume ACP is
        # loaded and ready for prompts.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "transition_cancelled", (
            f"Expected transition_cancelled, got {msg}"
        )
        assert msg["sessionId"] == "session-X"
        assert msg["requestId"] == "r2"

        # The transition was cancelled — no session_resumed should follow.
        try:
            extra = await asyncio.wait_for(ws.sent.get(), timeout=1.0)
            assert extra.get("type") != "session_resumed", (
                f"Unexpected session_resumed after cancel: {extra}"
            )
        except asyncio.TimeoutError:
            pass  # Good — no more messages.

        # The cancelled transition's ACP client should have been stopped.
        assert FakeACPClient.instances, "No FakeACPClient was created"
        assert FakeACPClient.instances[0].stopped, (
            "Transition's ACP client was not stopped on cancel"
        )
        # suppress_updates must never be left True (assessment issue #17).
        assert FakeACPClient.instances[0].suppress_updates is False, (
            "suppress_updates was left True after cancelled transition"
        )
    finally:
        # Unblock any leaked executor threads before cleanup.
        load_event.set()
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_cancel_during_load_history_emits_transition_cancelled(patched_deps):
    """Cancelling an in-flight ``load_history`` transition sends
    ``transition_cancelled`` (not ``turn_cancelled``) and leaves
    ``ms.acp`` set to ``None`` so a subsequent prompt is rejected."""
    load_event = asyncio.Event()
    load_started = asyncio.Event()
    FakeACPClient.load_event = load_event
    FakeACPClient.load_started_event = load_started

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await ws.to_receive.put(
            {"type": "load_history", "sessionId": "session-T", "requestId": "lh1"}
        )
        await asyncio.wait_for(load_started.wait(), timeout=5.0)

        await ws.to_receive.put(
            {"type": "cancel", "sessionId": "session-T", "requestId": "c1"}
        )

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "transition_cancelled", (
            f"Expected transition_cancelled, got {msg}"
        )
        assert msg["sessionId"] == "session-T"
        assert msg["requestId"] == "c1"

        # No turn_cancelled should follow — the operation was a
        # transition, not a turn.
        try:
            extra = await asyncio.wait_for(ws.sent.get(), timeout=1.0)
            assert extra.get("type") != "turn_cancelled", (
                f"Unexpected turn_cancelled after transition cancel: {extra}"
            )
        except asyncio.TimeoutError:
            pass

        # The managed session's ACP reference must be cleared so a
        # subsequent prompt is rejected rather than sent to a
        # half-loaded session.
        from server.session_manager import session_manager as sm
        ms = sm.get("session-T")
        if ms is not None:
            assert ms.acp is None, "ms.acp was not cleared after transition cancel"
    finally:
        load_event.set()
        await _cleanup_endpoint(ws, task)


async def _establish_active_session(
    ws: MockWebSocket, sid: str = "active-session", request_id: str = "init"
) -> FakeACPClient:
    """Drive a ``load_history`` transition to completion and return the ACP client.

    The returned client is the one whose ``active_session_id`` matches *sid*.
    """
    load_event = asyncio.Event()
    load_started = asyncio.Event()
    FakeACPClient.load_event = load_event
    FakeACPClient.load_started_event = load_started

    await ws.to_receive.put(
        {"type": "load_history", "sessionId": sid, "requestId": request_id}
    )
    await asyncio.wait_for(load_started.wait(), timeout=5.0)
    load_event.set()

    # Drain until session_resumed arrives.
    while True:
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        if msg.get("type") == "session_resumed":
            assert msg["sessionId"] == sid
            break

    # The last FakeACPClient instance is the active one.
    assert FakeACPClient.instances, "No FakeACPClient was created"
    return FakeACPClient.instances[-1]


@pytest.mark.asyncio
async def test_prompt_with_wrong_session_id_is_rejected(patched_deps):
    """A ``prompt`` with a ``sessionId`` that does not match the active
    backend session is rejected with a correlated error and no prompt is
    started on the ACP process."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # Send a prompt with a wrong sessionId.
        await ws.to_receive.put(
            {
                "type": "prompt",
                "sessionId": "stale-session",
                "text": "hello",
                "requestId": "prompt-1",
            }
        )

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "error", f"Expected error, got {msg}"
        assert msg["sessionId"] == "stale-session"
        assert msg["requestId"] == "prompt-1"
        assert msg["operation"] == "prompt"
        assert "match the active backend session" in msg["message"]

        # No prompt should have been started on the ACP client.
        assert acp.prompt_calls == [], (
            f"Prompt was started on ACP: {acp.prompt_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_prompt_applies_selected_model_before_starting_turn(patched_deps):
    """The model sent with a ``prompt`` is applied to the live ACP session
    before the turn starts.

    The ACP session's model is only ever changed by
    ``session/set_config_option``, so a dropdown change made after the
    session was created must be re-applied at prompt time — otherwise the
    turn silently runs on the model the session was created with.
    """
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        # The session is established on "test-model" (from the DB mock).
        acp = await _establish_active_session(ws, sid="active-session")
        assert acp.set_model_calls == [("active-session", "test-model")]

        # The user picks a different model in the dropdown, then prompts.
        await ws.to_receive.put(
            {
                "type": "prompt",
                "sessionId": "active-session",
                "text": "hello",
                "model": "claude-opus-5-high",
                "requestId": "prompt-1",
            }
        )

        for _ in range(50):
            if acp.prompt_calls:
                break
            await asyncio.sleep(0.01)

        assert acp.set_model_calls[-1] == ("active-session", "claude-opus-5-high"), (
            f"Selected model was not applied before the turn: {acp.set_model_calls}"
        )
        assert acp.prompt_calls == [("active-session", "hello")]
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_model_switch_restarts_to_add_and_remove_project_compaction(patched_deps, tmp_path):
    from server import main
    from server.test.compaction_settings_test import write_rule

    write_rule(tmp_path)
    main.db_reader.get_session.return_value = {
        "working_directory": str(tmp_path), "model": "test-model",
    }
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        original = await _establish_active_session(ws, sid="active-session")
        previous = original
        for model, expected in (("gpt-6-1-sol-max", (300000, 300000, 450000)),
                                ("claude-opus-5-high", None)):
            await ws.to_receive.put({
                "type": "prompt", "sessionId": "active-session", "text": "hello",
                "model": model, "requestId": model,
            })
            for _ in range(100):
                current = FakeACPClient.instances[-1]
                if current is not previous and current.prompt_calls:
                    break
                await asyncio.sleep(0.01)
            assert previous.stopped
            assert current is not previous
            assert current.loaded_sid == "active-session"
            assert current.cwd == str(tmp_path)
            assert current.compaction_thresholds == expected
            assert current.prompt_calls == [("active-session", "hello")]
            for _ in range(100):
                from server.session_manager import session_manager
                managed = session_manager.get("active-session")
                if managed.active_turn_task is None:
                    break
                await asyncio.sleep(0.01)
            while not ws.sent.empty():
                message = ws.sent.get_nowait()
                assert message["type"] not in ("history_loaded", "session_resumed")
            previous = current
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_model_switch_on_unpersisted_session_skips_compaction_restart(
    patched_deps, tmp_path
):
    """A ``session/new`` session has no row in the CLI's session store
    until its first turn, so ``session/load`` cannot resume it — the
    compaction restart would tear down the only process holding it and
    then fail with "Session not found". The model switch must be applied
    on the live process instead."""
    from server import main
    from server.test.compaction_settings_test import write_rule

    write_rule(tmp_path)
    main.db_reader.get_session.return_value = None
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await ws.to_receive.put(
            {
                "type": "new_session",
                "cwd": str(tmp_path),
                "model": "swe-2-max",
                "requestId": "init",
            }
        )
        session_id = None
        while True:
            msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            if msg.get("type") == "session_created":
                session_id = msg["sessionId"]
                break
        acp = FakeACPClient.instances[-1]
        assert acp.compaction_thresholds is None

        await ws.to_receive.put(
            {
                "type": "prompt",
                "sessionId": session_id,
                "text": "hello",
                "model": "gpt-6-1-sol-max",
                "requestId": "prompt-1",
            }
        )
        for _ in range(100):
            if acp.prompt_calls:
                break
            await asyncio.sleep(0.01)
        assert len(FakeACPClient.instances) == 1
        assert not acp.stopped
        assert acp.set_model_calls[-1] == (session_id, "gpt-6-1-sol-max")
        assert acp.prompt_calls == [(session_id, "hello")]
        while not ws.sent.empty():
            message = ws.sent.get_nowait()
            assert message["type"] != "error"
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_load_history_unknown_session_keeps_live_session(patched_deps):
    """``load_history`` for a session id that is not in the CLI's store can
    never succeed; it must fail without tearing down the live session."""
    from server import main

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        def session_or_none(sid):
            if sid == "ghost-session":
                return None
            return {"working_directory": "/tmp", "model": "test-model"}

        main.db_reader.get_session.side_effect = session_or_none
        await ws.to_receive.put(
            {"type": "load_history", "sessionId": "ghost-session", "requestId": "lh-ghost"}
        )
        messages = await _drain_messages(ws, 4, timeout=2.0)
        assert any(
            m.get("type") == "resume_failed" and m.get("sessionId") == "ghost-session"
            for m in messages
        ), messages
        assert not acp.stopped
        await ws.to_receive.put(
            {
                "type": "prompt",
                "sessionId": "active-session",
                "text": "still alive",
                "requestId": "prompt-alive",
            }
        )
        for _ in range(100):
            if acp.prompt_calls:
                break
            await asyncio.sleep(0.01)
        assert acp.prompt_calls == [("active-session", "still alive")]
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_resume_session_unknown_session_keeps_live_session(patched_deps):
    """A ``resume_session`` for a session id the CLI never persisted must
    fail without destroying the live session."""
    from server import main

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        def session_or_none(sid):
            if sid == "ghost-session":
                return None
            return {"working_directory": "/tmp", "model": "test-model"}

        main.db_reader.get_session.side_effect = session_or_none
        await ws.to_receive.put(
            {"type": "resume_session", "sessionId": "ghost-session", "requestId": "rs-ghost"}
        )
        messages = await _drain_messages(ws, 4, timeout=2.0)
        assert any(
            m.get("type") == "resume_failed" and m.get("sessionId") == "ghost-session"
            for m in messages
        ), messages
        assert not acp.stopped
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_prompt_rejected_when_model_cannot_be_applied(patched_deps):
    """When the CLI will not confirm the selected model, the prompt is
    refused with an error instead of running on the previous model."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        from server.acp_client import ModelNotAppliedError

        FakeACPClient.set_model_error = ModelNotAppliedError(
            "agent is running 'glm-5-2' after being asked for 'claude-opus-5-high'"
        )
        await ws.to_receive.put(
            {
                "type": "prompt",
                "sessionId": "active-session",
                "text": "expensive prompt",
                "model": "claude-opus-5-high",
                "requestId": "prompt-1",
            }
        )

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "error", f"Expected error, got {msg}"
        assert msg["sessionId"] == "active-session"
        assert msg["requestId"] == "prompt-1"
        assert msg["operation"] == "prompt"
        assert "claude-opus-5-high" in msg["message"]
        assert "Prompt not sent" in msg["message"]

        # The turn must not have started on the wrong model.
        assert acp.prompt_calls == [], (
            f"Prompt was sent despite the model not being applied: {acp.prompt_calls}"
        )
    finally:
        FakeACPClient.set_model_error = None
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_cancel_with_wrong_session_id_is_rejected(patched_deps):
    """A ``cancel`` with a ``sessionId`` that does not match the active
    backend session is rejected with a correlated error and no
    cancellation is performed on the ACP process."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # Send a cancel with a wrong sessionId.
        await ws.to_receive.put(
            {
                "type": "cancel",
                "sessionId": "stale-session",
                "requestId": "cancel-1",
            }
        )

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "error", f"Expected error, got {msg}"
        assert msg["sessionId"] == "stale-session"
        assert msg["requestId"] == "cancel-1"
        assert msg["operation"] == "cancel"
        assert "match the active backend session" in msg["message"]

        # No cancellation should have been performed on the ACP client.
        assert acp.cancel_calls == [], (
            f"Cancel was called on ACP: {acp.cancel_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_permission_response_with_wrong_session_id_is_rejected(patched_deps):
    """A ``permission_response`` with a ``sessionId`` that does not match
    the active backend session is rejected with a correlated error and no
    ACP response is sent."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # Send a permission_response with a wrong sessionId.
        await ws.to_receive.put(
            {
                "type": "permission_response",
                "sessionId": "stale-session",
                "toolCallId": "tc-1",
                "requestId": "perm-1",
                "decision": "allow",
            }
        )

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "error", f"Expected error, got {msg}"
        assert msg["sessionId"] == "stale-session"
        assert msg["requestId"] == "perm-1"
        assert msg["operation"] == "permission_response"
        assert "match the active backend session" in msg["message"]

        # No ACP response should have been sent.
        assert acp.send_response_calls == [], (
            f"send_response was called on ACP: {acp.send_response_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_omitting_session_id_defaults_to_active(patched_deps):
    """Omitting ``sessionId`` on a ``prompt`` continues to default to the
    active backend session (existing behavior is preserved)."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # Send a prompt with no sessionId — should default to active.
        await ws.to_receive.put(
            {"type": "prompt", "text": "hello", "requestId": "prompt-2"}
        )

        # Wait for the turn to complete (turn_complete) or error.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "turn_complete", f"Expected turn_complete, got {msg}"
        assert msg["sessionId"] == "active-session"
        assert msg["requestId"] == "prompt-2"

        # The prompt should have been started on the active session.
        assert acp.prompt_calls == [("active-session", "hello")], (
            f"Unexpected prompt calls: {acp.prompt_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


# ---------------------------------------------------------------------------
# Task 8: Backend protocol tests for Tasks 1, 2, 5, and reconnect/unavailable
# behavior.  Tests 5 (two rapid session selections) and 7 (ACP _pending
# cleanup) are already covered above and in ``acp_client_test.py``.
# ---------------------------------------------------------------------------


def _make_permission_request(
    request_id: str = "acp-req-1",
    tool_call_id: str = "tc-1",
    session_id: str = "active-session",
) -> dict:
    """Build an ACP ``session/request_permission`` inbound request message."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": {"toolCallId": tool_call_id},
            "options": [
                {"optionId": "opt-allow", "kind": "allow"},
                {"optionId": "opt-deny", "kind": "deny"},
            ],
        },
    }


@pytest.mark.asyncio
async def test_cancel_request_emits_turn_cancelled_and_cancels_acp(patched_deps):
    """1. Cancel request → ACP cancel → ``turn_cancelled`` (asserts Task 1)."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # Block the prompt so it stays in-flight until we cancel.
        prompt_event = asyncio.Event()
        prompt_started = asyncio.Event()
        FakeACPClient.prompt_event = prompt_event
        FakeACPClient.prompt_started_event = prompt_started

        await ws.to_receive.put(
            {"type": "prompt", "sessionId": "active-session", "text": "hello", "requestId": "p1"}
        )
        await asyncio.wait_for(prompt_started.wait(), timeout=5.0)

        await ws.to_receive.put(
            {"type": "cancel", "sessionId": "active-session", "requestId": "c1"}
        )

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "turn_cancelled", f"Expected turn_cancelled, got {msg}"
        assert msg["sessionId"] == "active-session"
        assert msg["requestId"] == "c1"

        assert "active-session" in acp.cancel_calls, (
            f"ACP cancel was not called: {acp.cancel_calls}"
        )
    finally:
        prompt_event.set()
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_permission_request_browser_decision_sends_acp_response(patched_deps):
    """2. Permission request → browser decision → exact ACP JSON-RPC response
    with the correct option id (asserts Task 5)."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # Simulate the ACP subprocess sending a permission request.
        await acp.request_handler(_make_permission_request())

        # Frontend should receive ``request_permission``.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "request_permission", f"Expected request_permission, got {msg}"
        assert msg["toolCallId"] == "tc-1"
        assert msg["requestId"] == "acp-req-1"

        # Browser allows the request.
        await ws.to_receive.put(
            {
                "type": "permission_response",
                "sessionId": "active-session",
                "toolCallId": "tc-1",
                "requestId": "acp-req-1",
                "decision": "allow",
            }
        )

        # Frontend should receive ``permission_received``.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "permission_received", f"Expected permission_received, got {msg}"
        assert msg["toolCallId"] == "tc-1"
        assert msg["decision"] == "allow"

        # ACP should have received the exact JSON-RPC response with the
        # correct option id.
        assert acp.send_response_calls, "No ACP send_response was called"
        args, kwargs = acp.send_response_calls[0]
        assert args[0] == "acp-req-1", f"Unexpected ACP request id: {args[0]}"
        assert args[1] == {"outcome": {"outcome": "selected", "optionId": "opt-allow"}}, (
            f"Unexpected ACP response body: {args[1]}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_permission_request_forwards_command_detail(patched_deps):
    """A permission request whose toolCall carries rawInput.command (exec)
    forwards a flat ``data.detail`` string so the frontend can show what is
    being approved even when the tool call is absent from chat history."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        req = _make_permission_request()
        req["params"]["toolCall"] = {
            "toolCallId": "tc-1",
            "title": "Ran rm",
            "kind": "execute",
            "rawInput": {"command": "rm -rf build/"},
            "_meta": {"cognition.ai/editableCommand": "rm -rf build/"},
        }
        await acp.request_handler(req)

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "request_permission", f"Expected request_permission, got {msg}"
        assert msg["data"]["detail"] == "rm -rf build/"
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_permission_request_forwards_edit_diff_detail(patched_deps):
    """An edit permission request forwards the target path plus a compact
    unified diff in ``data.detail``."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        req = _make_permission_request()
        req["params"]["toolCall"] = {
            "toolCallId": "tc-1",
            "title": "Edit file",
            "kind": "edit",
            "content": [
                {
                    "type": "diff",
                    "path": "C:/proj/app.py",
                    "oldText": "x = 1\ny = 2\n",
                    "newText": "x = 1\ny = 3\n",
                }
            ],
        }
        await acp.request_handler(req)

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "request_permission", f"Expected request_permission, got {msg}"
        detail = msg["data"]["detail"]
        assert "C:/proj/app.py" in detail
        assert "-y = 2" in detail
        assert "+y = 3" in detail
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_permission_request_detail_from_cached_tool_call(patched_deps):
    """Real Devin sends a bare ``{"toolCallId": ...}`` toolCall in the
    permission request for edit/write calls (verified against `devin acp`).
    The detail must be recovered from the ``tool_call`` session update that
    preceded the request."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        await acp.notification_handler(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "active-session",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "Edit file",
                        "kind": "edit",
                        "content": [
                            {
                                "type": "diff",
                                "path": "C:/proj/app.py",
                                "oldText": "x = 1\ny = 2\n",
                                "newText": "x = 1\ny = 3\n",
                            }
                        ],
                        "rawInput": {"file_path": "C:/proj/app.py"},
                    },
                },
            }
        )
        # Drain the forwarded session_update frame.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "session_update"

        await acp.request_handler(_make_permission_request())

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "request_permission", f"Expected request_permission, got {msg}"
        detail = msg["data"]["detail"]
        assert "C:/proj/app.py" in detail
        assert "-y = 2" in detail
        assert "+y = 3" in detail
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_permission_response_without_tool_call_id_is_rejected(patched_deps):
    """A ``permission_response`` carrying only a ``requestId`` (correlation
    id) and no ``toolCallId`` is rejected with an error and does not match
    any pending permission request (asserts Task 6)."""
    from server.session_manager import session_manager as sm

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # Simulate the ACP subprocess sending a permission request.
        await acp.request_handler(_make_permission_request())

        # Frontend should receive ``request_permission``.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "request_permission", f"Expected request_permission, got {msg}"
        assert msg["toolCallId"] == "tc-1"

        # Browser responds with only a correlation id (requestId) that
        # happens to equal the ACP JSON-RPC id, but no toolCallId. This
        # must NOT resolve the pending request.
        await ws.to_receive.put(
            {
                "type": "permission_response",
                "sessionId": "active-session",
                "requestId": "acp-req-1",
                "decision": "allow",
            }
        )

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "error", f"Expected error, got {msg}"
        assert msg["requestId"] == "acp-req-1"
        assert msg["operation"] == "permission_response"
        assert "missing toolCallId" in msg["message"], (
            f"Unexpected error message: {msg['message']}"
        )

        # No ACP response should have been sent.
        assert acp.send_response_calls == [], (
            f"send_response was called on ACP: {acp.send_response_calls}"
        )

        # The pending permission request should still be present.
        ms = sm.get("active-session")
        assert ms is not None, "Active managed session not found"
        assert "tc-1" in ms.permission_requests, (
            "Pending permission request was removed despite missing toolCallId"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_permission_response_with_correct_tool_call_id_resolves(patched_deps):
    """A ``permission_response`` with the correct ``toolCallId`` resolves
    the pending permission and forwards the decision to ACP (asserts
    Task 6)."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # Simulate the ACP subprocess sending a permission request.
        await acp.request_handler(_make_permission_request())

        # Frontend should receive ``request_permission``.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "request_permission", f"Expected request_permission, got {msg}"
        assert msg["toolCallId"] == "tc-1"

        # Browser allows the request using the correct toolCallId.
        await ws.to_receive.put(
            {
                "type": "permission_response",
                "sessionId": "active-session",
                "toolCallId": "tc-1",
                "requestId": "browser-corr-1",
                "decision": "allow",
            }
        )

        # Frontend should receive ``permission_received``.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "permission_received", f"Expected permission_received, got {msg}"
        assert msg["toolCallId"] == "tc-1"
        assert msg["decision"] == "allow"

        # ACP should have received the exact JSON-RPC response with the
        # correct option id.
        assert acp.send_response_calls, "No ACP send_response was called"
        args, kwargs = acp.send_response_calls[0]
        assert args[0] == "acp-req-1", f"Unexpected ACP request id: {args[0]}"
        assert args[1] == {"outcome": {"outcome": "selected", "optionId": "opt-allow"}}, (
            f"Unexpected ACP response body: {args[1]}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_permission_timeout_produces_permission_expired_and_rejects_acp(patched_deps):
    """3. Permission timeout produces ``permission_expired`` and rejects the
    ACP request (asserts Task 5)."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        with patch("server.main._PERMISSION_TIMEOUT_SECONDS", 0.1):
            # Simulate the ACP subprocess sending a permission request.
            await acp.request_handler(
                _make_permission_request(
                    request_id="acp-req-2", tool_call_id="tc-2"
                )
            )

            # Frontend should receive ``request_permission``.
            msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert msg["type"] == "request_permission"
            assert msg["toolCallId"] == "tc-2"

            # Wait for the timeout to fire.
            msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert msg["type"] == "permission_expired", f"Expected permission_expired, got {msg}"
            assert msg["toolCallId"] == "tc-2"
            assert msg["requestId"] == "acp-req-2"

        # ACP should have received a rejection for the timed-out request.
        rejection_found = False
        for args, kwargs in acp.send_response_calls:
            if kwargs.get("error") and args[0] == "acp-req-2":
                rejection_found = True
                break
        assert rejection_found, (
            f"Permission rejection not found in send_response_calls: {acp.send_response_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_new_session_forwards_selected_project_working_directory(patched_deps):
    """The project selected by the GUI is sent to ACP as session/new.cwd."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        selected_project = r"C:\Code\ProjectA"
        await ws.to_receive.put(
            {
                "type": "new_session",
                "cwd": selected_project,
                "requestId": "new-project-session",
            }
        )
        message = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert message["type"] == "session_created"
        assert FakeACPClient.instances[-1].new_session_cwd == selected_project
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_new_session_with_bypass_sets_session_mode(patched_deps):
    """``new_session`` with ``permissionMode: "bypass"`` must apply bypass via
    the per-session ``mode`` config option — the ``--permission-mode`` CLI
    flag is not honored by ACP sessions."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await ws.to_receive.put(
            {
                "type": "new_session",
                "cwd": "/tmp",
                "permissionMode": "bypass",
                "requestId": "ns-bypass",
            }
        )
        message = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert message["type"] == "session_created"
        acp = FakeACPClient.instances[-1]
        sid = message["sessionId"]
        assert acp.set_mode_calls == [(sid, "bypass")], (
            f"bypass mode was not applied to the ACP session: {acp.set_mode_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_new_session_without_bypass_does_not_set_session_mode(patched_deps):
    """Without ``permissionMode``, the session mode is left at the CLI default."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await ws.to_receive.put(
            {"type": "new_session", "cwd": "/tmp", "requestId": "ns-plain"}
        )
        message = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert message["type"] == "session_created"
        acp = FakeACPClient.instances[-1]
        assert acp.set_mode_calls == [], (
            f"session mode must not be touched without bypass: {acp.set_mode_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_new_session_with_bypass_survives_mode_failure(patched_deps):
    """A failing ``set_session_mode`` (e.g. an older CLI without the ``mode``
    config option) must not fail session creation — bypass is best-effort."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        original = FakeACPClient.set_session_mode

        async def failing_set_mode(self, session_id, mode):
            self.set_mode_calls.append((session_id, mode))
            raise RuntimeError("mode option not supported")

        FakeACPClient.set_session_mode = failing_set_mode
        try:
            await ws.to_receive.put(
                {
                    "type": "new_session",
                    "cwd": "/tmp",
                    "permissionMode": "bypass",
                    "requestId": "ns-bypass-fail",
                }
            )
            message = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert message["type"] == "session_created"
        finally:
            FakeACPClient.set_session_mode = original
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_load_history_with_bypass_sets_session_mode(patched_deps):
    """``load_history`` with ``permissionMode: "bypass"`` applies bypass to the
    resumed ACP session too."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await ws.to_receive.put(
            {
                "type": "load_history",
                "sessionId": "existing-session",
                "permissionMode": "bypass",
                "requestId": "lh-bypass",
            }
        )
        messages = await _drain_messages(ws, 10, timeout=5.0)
        resumed = [m for m in messages if m.get("type") == "session_resumed"]
        assert len(resumed) == 1, f"Expected one session_resumed, got {resumed}"
        acp = FakeACPClient.instances[-1]
        assert acp.set_mode_calls == [("existing-session", "bypass")], (
            f"bypass mode was not applied to the resumed session: {acp.set_mode_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_new_session_after_selection_does_not_overwrite(patched_deps):
    """4. New-session response arriving after session selection does not
    overwrite the selected session (asserts Task 2)."""
    new_session_event = asyncio.Event()
    new_session_started = asyncio.Event()
    FakeACPClient.new_session_event = new_session_event
    FakeACPClient.new_session_started_event = new_session_started

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        # Start a new_session that blocks inside ACP.
        await ws.to_receive.put(
            {"type": "new_session", "cwd": "/tmp", "requestId": "ns1"}
        )
        await asyncio.wait_for(new_session_started.wait(), timeout=5.0)

        # Select an existing session — cancels the in-flight new_session.
        await ws.to_receive.put(
            {"type": "load_history", "sessionId": "existing-session", "requestId": "lh1"}
        )

        messages = await _drain_messages(ws, 10, timeout=5.0)

        # No session_created should be emitted for the abandoned new session.
        session_created = [m for m in messages if m.get("type") == "session_created"]
        assert session_created == [], f"Unexpected session_created: {session_created}"

        # The selected session should be the active one.
        resumed = [m for m in messages if m.get("type") == "session_resumed"]
        assert len(resumed) == 1, f"Expected one session_resumed, got {resumed}"
        assert resumed[0]["sessionId"] == "existing-session"

        # The abandoned new_session's ACP client should have been stopped.
        assert len(FakeACPClient.instances) >= 2
        first_acp = FakeACPClient.instances[0]
        assert first_acp.stopped, "Abandoned new_session ACP client was not stopped"
    finally:
        new_session_event.set()
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_disconnect_during_running_prompt_cancels_turn(patched_deps):
    """6. Disconnect during a running prompt: turn is cancelled and the
    frontend connection is cleaned up (asserts reconnect behavior)."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        prompt_event = asyncio.Event()
        prompt_started = asyncio.Event()
        FakeACPClient.prompt_event = prompt_event
        FakeACPClient.prompt_started_event = prompt_started

        await ws.to_receive.put(
            {"type": "prompt", "sessionId": "active-session", "text": "hello", "requestId": "p1"}
        )
        await asyncio.wait_for(prompt_started.wait(), timeout=5.0)

        # Simulate the browser disconnecting.
        await ws.to_receive.put(WebSocketDisconnect())

        # The endpoint should exit cleanly.
        await asyncio.wait_for(task, timeout=10.0)

        # The ACP client must have been stopped (turn cancelled + cleanup).
        assert acp.stopped, "ACP client was not stopped on disconnect"
    finally:
        prompt_event.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_acp_exit_rejects_pending_permissions_and_notifies_frontend(patched_deps):
    """8. ACP exit while a permission request is active: pending requests are
    rejected and the frontend receives an error."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # Simulate the ACP subprocess sending a permission request.
        await acp.request_handler(_make_permission_request())

        # Frontend should receive ``request_permission``.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "request_permission"
        assert msg["toolCallId"] == "tc-1"

        # Simulate the ACP process exiting unexpectedly.
        await acp.on_unavailable("ACP process exited")

        # Frontend should receive a correlated error.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "error", f"Expected error, got {msg}"
        assert msg["operation"] == "acp_unavailable"
        assert "ACP process exited" in msg["message"]

        # The pending permission should have been rejected via send_response.
        rejection_found = False
        for args, kwargs in acp.send_response_calls:
            err = kwargs.get("error")
            if err and args[0] == "acp-req-1" and "ACP process exited" in err.get("message", ""):
                rejection_found = True
                break
        assert rejection_found, (
            f"Permission rejection not found in send_response_calls: {acp.send_response_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


# ---------------------------------------------------------------------------
# Task 5: Server-side ACP session manager with reconnect grace period.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_watchers_only_run_for_active_turn(patched_deps):
    """Persistence polling starts with a prompt and stops when that turn settles."""
    import server.main as main_module

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await _establish_active_session(ws, sid="active-session")
        main_module.db_watcher.reset_mock()
        main_module.transcript_watcher.reset_mock()

        await ws.to_receive.put(
            {"type": "prompt", "sessionId": "active-session", "text": "hello", "requestId": "p-watch"}
        )
        completed = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert completed["type"] == "turn_complete"
        # Completion is sent before the final snapshots and task cleanup.
        async def watchers_stopped() -> None:
            while not main_module.db_watcher.stop_watching.called:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(watchers_stopped(), timeout=2.0)

        assert main_module.db_watcher.start_watching.call_args.args[0] == "active-session"
        assert main_module.transcript_watcher.start_watching.call_args.args[0] == "active-session"
        main_module.db_watcher.stop_watching.assert_called_with("active-session")
        main_module.transcript_watcher.stop_watching.assert_called_with("active-session")
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_turn_complete_sends_one_final_persistence_snapshot(patched_deps):
    """A settled turn gets one DB/reasoning reconciliation without idle polling."""
    import server.main as main_module

    main_module.db_reader.get_messages.return_value = [
        {
            "node_id": 7,
            "role": "assistant",
            "content": "final persisted answer",
            "created_at": "2026-08-13T02:00:00Z",
        }
    ]
    main_module.db_reader.get_reasoning_steps.return_value = [
        {
            "step_id": 3,
            "reasoning_content": "final persisted reasoning",
            "timestamp": "2026-08-13T01:59:59Z",
            "tool_calls": [],
            "message": "final persisted answer",
            "metrics": {},
        }
    ]

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await _establish_active_session(ws, sid="active-session")
        await ws.to_receive.put(
            {
                "type": "prompt",
                "sessionId": "active-session",
                "text": "hello",
                "requestId": "p-final-snapshot",
            }
        )

        completed = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        db_snapshot = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        reasoning_snapshot = await asyncio.wait_for(ws.sent.get(), timeout=5.0)

        assert completed["type"] == "turn_complete"
        assert db_snapshot["type"] == "db_message_update"
        assert db_snapshot["messages"][0]["content"] == "final persisted answer"
        assert reasoning_snapshot["type"] == "reasoning_update"
        assert reasoning_snapshot["steps"][0]["reasoning_content"] == "final persisted reasoning"
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_source,frame_types",
    [
        ("messages", ["turn_complete", "cost_update"]),
        ("reasoning", ["turn_complete", "db_message_update", "cost_update"]),
    ],
)
async def test_final_snapshot_read_failure_remains_best_effort(
    patched_deps, caplog, failure_source, frame_types
):
    import server.main as main_module

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await _establish_active_session(ws, sid="active-session")
        with ExitStack() as stack:
            stack.enter_context(patch.object(main_module, "_session_cost", return_value={"cost": 1}))
            if failure_source == "messages":
                main_module.db_reader.get_messages.side_effect = RuntimeError("snapshot unreadable")
            else:
                stack.enter_context(
                    patch.object(
                        main_module, "_combined_reasoning_steps",
                        side_effect=RuntimeError("snapshot unreadable"),
                    )
                )
            await ws.to_receive.put({"type": "prompt", "text": "hello", "requestId": "p-snapshot"})
            frames = [await asyncio.wait_for(ws.sent.get(), timeout=2.0) for _ in frame_types]
            assert [frame["type"] for frame in frames] == frame_types
            assert frames[0]["requestId"] == "p-snapshot"
            assert frames[-1]["estimate"] == {"cost": 1}
            assert "Final persistence snapshot failed for active-session: snapshot unreadable" in caplog.text
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_unreadable_db_baseline_keeps_authoritative_prompt_completion(patched_deps, caplog):
    import server.main as main_module

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        await _establish_active_session(ws, sid="active-session")
        main_module.db_reader.get_message_signature.side_effect = RuntimeError("baseline unreadable")
        await ws.to_receive.put({"type": "prompt", "text": "hello"})
        frames = [await asyncio.wait_for(ws.sent.get(), timeout=2.0) for _ in range(4)]
        assert [frame["type"] for frame in frames] == [
            "turn_complete", "db_message_update", "reasoning_update", "cost_update"
        ]
        assert frames[0]["requestId"] == ""
        assert frames[0]["result"] == {"stopReason": "stop"}
        main_module.db_reader.get_turn_tip_state.assert_not_called()
        assert "DB turn-settlement unavailable for active-session: baseline unreadable" in caplog.text
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,message",
    [(asyncio.TimeoutError(), "ACP prompt timed out"), (RuntimeError("prompt rejected"), "prompt rejected")],
)
async def test_prompt_failure_preserves_correlated_error_and_cleanup(patched_deps, failure, message):
    import server.main as main_module
    from server.session_manager import session_manager as sm

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")
        acp.prompt = AsyncMock(side_effect=failure)
        main_module.db_watcher.reset_mock()
        main_module.transcript_watcher.reset_mock()
        await ws.to_receive.put({"type": "prompt", "text": "hello", "requestId": "p-failure"})
        assert await asyncio.wait_for(ws.sent.get(), timeout=2.0) == {
            "type": "error", "sessionId": "active-session", "requestId": "p-failure",
            "operation": "prompt", "message": message,
        }
        ms = sm.get("active-session")
        if ms.active_turn_task is not None:
            await asyncio.wait_for(ms.active_turn_task, timeout=2.0)
        assert ms.active_turn_task is None
        assert ms.db_fallback_resume_event is None
        assert not ms.db_fallback_settled
        main_module.db_watcher.stop_watching.assert_called_once_with("active-session")
        main_module.transcript_watcher.stop_watching.assert_called_once_with("active-session")
        assert ws.sent.empty()
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript_usage", [[], [{"prompt_tokens": 1}]])
async def test_live_persistence_callback_frame_order(patched_deps, transcript_usage):
    import server.main as main_module

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    gate = asyncio.Event()
    started = asyncio.Event()
    try:
        await _establish_active_session(ws, sid="active-session")
        FakeACPClient.prompt_event = gate
        FakeACPClient.prompt_started_event = started
        main_module.transcript_watcher.get_usage_records.return_value = transcript_usage
        with patch.object(main_module, "_session_cost", return_value={"cost": 1}), patch.object(
            main_module, "_combined_reasoning_steps", return_value=[{"reasoning_content": "merged"}]
        ):
            await ws.to_receive.put({"type": "prompt", "text": "hello", "requestId": "p-callbacks"})
            await asyncio.wait_for(started.wait(), timeout=2.0)
            db_callback = main_module.db_watcher.start_watching.call_args.args[1]
            await db_callback("active-session", [{"content": "live message"}])
            db_frame = await asyncio.wait_for(ws.sent.get(), timeout=2.0)
            assert db_frame["type"] == "db_message_update"
            assert db_frame["messages"] == [{"content": "live message"}]
            if not transcript_usage:
                assert (await asyncio.wait_for(ws.sent.get(), timeout=2.0))["type"] == "cost_update"
            reasoning_frame = await asyncio.wait_for(ws.sent.get(), timeout=2.0)
            assert reasoning_frame["type"] == "reasoning_update"
            assert reasoning_frame["steps"] == [{"reasoning_content": "merged"}]
            assert ws.sent.empty()

            transcript_callback = main_module.transcript_watcher.start_watching.call_args.args[1]
            await transcript_callback("active-session", [{"reasoning_content": "unmerged"}])
            reasoning_frame = await asyncio.wait_for(ws.sent.get(), timeout=2.0)
            assert reasoning_frame["type"] == "reasoning_update"
            assert reasoning_frame["steps"] == [{"reasoning_content": "merged"}]
            assert (await asyncio.wait_for(ws.sent.get(), timeout=2.0))["type"] == "cost_update"
            assert ws.sent.empty()
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_superseded_prompt_keeps_newer_generation_state(patched_deps):
    import server.main as main_module
    from server.session_manager import session_manager as sm

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    gate = asyncio.Event()
    started = asyncio.Event()
    try:
        await _establish_active_session(ws, sid="active-session")
        FakeACPClient.prompt_event = gate
        FakeACPClient.prompt_started_event = started
        main_module.db_watcher.reset_mock()
        main_module.transcript_watcher.reset_mock()
        await ws.to_receive.put({"type": "prompt", "text": "hello", "requestId": "p-stale"})
        await asyncio.wait_for(started.wait(), timeout=2.0)
        ms = sm.get("active-session")
        turn = ms.active_turn_task
        resume_event = ms.db_fallback_resume_event
        latest_update = {"update": {"type": "tool_call", "toolCallId": "newer"}}
        ms.last_session_update = latest_update
        ms.generation_id += 1
        gate.set()
        await asyncio.wait_for(turn, timeout=2.0)
        assert ws.sent.empty()
        assert ms.active_turn_task is turn
        assert ms.db_fallback_resume_event is resume_event
        assert ms.last_session_update is latest_update
        main_module.db_watcher.stop_watching.assert_not_called()
        main_module.transcript_watcher.stop_watching.assert_not_called()
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("activity_resumes", [False, True])
async def test_prompt_timeout_after_db_settlement_depends_on_resumed_activity(
    patched_deps, caplog, activity_resumes
):
    import server.main as main_module
    from server.session_manager import session_manager as sm

    current = {"signature": "previous"}
    main_module.db_reader.get_message_signature.side_effect = lambda _sid: current["signature"]
    main_module.db_reader.get_turn_tip_state.return_value = {"role": "assistant", "finish_reason": "stop"}
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    gate = asyncio.Event()
    started = asyncio.Event()

    async def prompt_that_times_out(*_args):
        started.set()
        await gate.wait()
        raise asyncio.TimeoutError()

    try:
        acp = await _establish_active_session(ws, sid="active-session")
        acp.prompt = prompt_that_times_out
        with patch.object(main_module, "_TURN_SETTLE_QUIET_SECONDS", 0), patch.object(
            main_module, "_TURN_SETTLE_POLL_SECONDS", 0.01
        ), patch.object(main_module, "_TURN_SETTLE_CONFIRM_SECONDS", 0):
            await ws.to_receive.put({"type": "prompt", "text": "hello", "requestId": "p-timeout"})
            await asyncio.wait_for(started.wait(), timeout=2.0)
            ms = sm.get("active-session")
            turn = ms.active_turn_task
            current["signature"] = "final"
            frames = [await asyncio.wait_for(ws.sent.get(), timeout=2.0) for _ in range(4)]
            assert [frame["type"] for frame in frames] == [
                "turn_complete", "db_message_update", "reasoning_update", "cost_update"
            ]
            assert frames[0]["result"]["settledFromDb"] is True
            assert ms.db_fallback_settled
            assert not turn.done()
            if activity_resumes:
                await acp.notification_handler({
                    "method": "session/update",
                    "params": {
                        "sessionId": "active-session",
                        "update": {"type": "agent_message_chunk", "chunk": "continued"},
                    },
                })
                assert (await asyncio.wait_for(ws.sent.get(), timeout=2.0))["type"] == "session_update"
                assert not ms.db_fallback_settled
            gate.set()
            await asyncio.wait_for(turn, timeout=2.0)
            if activity_resumes:
                error = await asyncio.wait_for(ws.sent.get(), timeout=2.0)
                assert error["type"] == "error"
                assert error["requestId"] == "p-timeout"
                assert error["message"] == "ACP prompt timed out"
            else:
                assert ws.sent.empty()
                assert "session/prompt timed out after DB settlement for active-session" in caplog.text
            assert ms.active_turn_task is None
            assert ms.db_fallback_resume_event is None
            assert not ms.db_fallback_settled
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_prompt_settles_from_db_when_response_never_arrives(patched_deps):
    """When the ``session/prompt`` response never arrives but SQLite shows
    a terminal assistant message and the session has gone quiet, the
    backend synthesizes ``turn_complete`` instead of streaming forever."""
    import server.main as main_module

    main_module.db_reader.get_turn_tip_state.return_value = {
        "role": "assistant",
        "finish_reason": "stop",
    }

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")
        # Never resolve the prompt — simulates a response lost on the wire.
        FakeACPClient.prompt_event = asyncio.Event()
        FakeACPClient.prompt_started_event = asyncio.Event()
        signatures = iter(("sig-previous", "sig-final"))
        main_module.db_reader.get_message_signature.side_effect = (
            lambda _sid: next(signatures, "sig-final")
        )

        with patch.object(main_module, "_TURN_SETTLE_QUIET_SECONDS", 0.2), patch.object(
            main_module, "_TURN_SETTLE_POLL_SECONDS", 0.05
        ), patch.object(
            main_module, "_TURN_SETTLE_CONFIRM_SECONDS", 0.3
        ):
            await ws.to_receive.put(
                {
                    "type": "prompt",
                    "sessionId": "active-session",
                    "text": "hello",
                    "requestId": "p-settle",
                }
            )
            await asyncio.wait_for(
                FakeACPClient.prompt_started_event.wait(), timeout=5.0
            )

            completed = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert completed["type"] == "turn_complete", (
                f"Expected turn_complete, got {completed}"
            )
            assert completed["requestId"] == "p-settle"
            assert completed["stopReason"] == "end_turn"
            assert completed["result"]["settledFromDb"] is True
            from server.session_manager import session_manager as sm
            assert not sm.get("active-session").active_turn_task.done()

            # The final persistence snapshot still follows completion.
            snapshot = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert snapshot["type"] == "db_message_update", (
                f"Expected db_message_update, got {snapshot}"
            )
            reasoning = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert reasoning["type"] == "reasoning_update", (
                f"Expected reasoning_update, got {reasoning}"
            )
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "cost_update"
            reads_after_completion = main_module.db_reader.get_message_signature.call_count
            await asyncio.sleep(0.1)
            assert main_module.db_reader.get_message_signature.call_count == reads_after_completion
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_db_fallback_keeps_acp_prompt_until_real_completion(patched_deps):
    """If a DB completion is premature, the later ACP response must send
    another completion so renewed streaming cannot leave the GUI spinning."""
    import server.main as main_module

    current = {"signature": "sig-previous"}
    main_module.db_reader.get_message_signature.side_effect = (
        lambda _sid: current["signature"]
    )
    main_module.db_reader.get_turn_tip_state.return_value = {
        "role": "assistant",
        "finish_reason": "stop",
    }
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    prompt_event = asyncio.Event()
    prompt_started = asyncio.Event()
    try:
        acp = await _establish_active_session(ws, sid="active-session")
        FakeACPClient.prompt_event = prompt_event
        FakeACPClient.prompt_started_event = prompt_started

        with patch.object(main_module, "_TURN_SETTLE_QUIET_SECONDS", 0.05), patch.object(
            main_module, "_TURN_SETTLE_POLL_SECONDS", 0.01
        ), patch.object(
            main_module, "_TURN_SETTLE_CONFIRM_SECONDS", 0.2
        ):
            await ws.to_receive.put(
                {
                    "type": "prompt",
                    "sessionId": "active-session",
                    "text": "hello",
                    "requestId": "p-late-reply",
                }
            )
            await asyncio.wait_for(prompt_started.wait(), timeout=5.0)
            current["signature"] = "sig-intermediate"

            provisional = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert provisional["type"] == "turn_complete"
            assert provisional["result"]["settledFromDb"] is True
            assert not prompt_event.is_set()
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "db_message_update"
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "reasoning_update"
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "cost_update"

            # Compaction resumes after the provisional signal. The backend
            # must keep the ACP request and live DB watchers available.
            current["signature"] = "sig-compaction"
            await acp.notification_handler(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "active-session",
                        "update": {"type": "agent_message_chunk", "chunk": "continued"},
                    },
                }
            )
            update = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert update["type"] == "session_update"
            assert main_module.db_watcher.start_watching.call_count == 2

            current["signature"] = "sig-final"
            settled_again = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert settled_again["type"] == "turn_complete"
            assert settled_again["result"]["settledFromDb"] is True
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "db_message_update"
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "reasoning_update"
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "cost_update"

            # Even after the backup settlement, the real ACP response must
            # still be able to clear any last streaming state.
            prompt_event.set()
            completed = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert completed["type"] == "turn_complete"
            assert completed["requestId"] == "p-late-reply"
            assert "settledFromDb" not in completed["result"]
    finally:
        prompt_event.set()
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_final_cost_failure_keeps_turn_completion_and_cleanup(patched_deps, caplog):
    """A local cost-read failure after a settled turn logs a warning instead
    of converting the completed turn into a prompt error."""
    import server.main as main_module
    from server.session_manager import session_manager as sm

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))

    def failed_cost(sid):
        raise RuntimeError("cost failed")

    try:
        await _establish_active_session(ws, sid="active-session")
        main_module.db_watcher.reset_mock()
        main_module.transcript_watcher.reset_mock()
        with patch.object(main_module, "_session_cost", side_effect=failed_cost):
            await ws.to_receive.put({"type": "prompt", "text": "hello", "requestId": "p-cost"})
            frames = [await asyncio.wait_for(ws.sent.get(), timeout=2.0) for _ in range(3)]
            assert [frame["type"] for frame in frames] == [
                "turn_complete", "db_message_update", "reasoning_update"
            ]
            assert frames[0]["requestId"] == "p-cost"
            assert frames[0]["result"] == {"stopReason": "stop"}
            ms = sm.get("active-session")
            if ms.active_turn_task is not None:
                await asyncio.wait_for(ms.active_turn_task, timeout=2.0)
            assert ms.active_turn_task is None
            main_module.db_watcher.stop_watching.assert_called_once_with("active-session")
            main_module.transcript_watcher.stop_watching.assert_called_once_with("active-session")
            assert ws.sent.empty()
            assert "Final cost update failed for active-session: cost failed" in caplog.text
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_provisional_cost_failure_keeps_pending_acp_prompt(patched_deps, caplog):
    """A cost-read failure on a provisional DB completion must not end the
    still-pending authoritative ACP prompt wait."""
    import server.main as main_module
    from server.session_manager import session_manager as sm

    current = {"signature": "sig-previous"}
    main_module.db_reader.get_message_signature.side_effect = (
        lambda _sid: current["signature"]
    )
    main_module.db_reader.get_turn_tip_state.return_value = {
        "role": "assistant", "finish_reason": "stop",
    }
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    prompt_event = asyncio.Event()
    prompt_started = asyncio.Event()

    def failed_cost(sid):
        raise RuntimeError("cost failed")

    try:
        acp = await _establish_active_session(ws, sid="active-session")
        FakeACPClient.prompt_event = prompt_event
        FakeACPClient.prompt_started_event = prompt_started
        with patch.object(main_module, "_TURN_SETTLE_QUIET_SECONDS", 0.05), patch.object(
            main_module, "_TURN_SETTLE_POLL_SECONDS", 0.01
        ), patch.object(
            main_module, "_TURN_SETTLE_CONFIRM_SECONDS", 0.2
        ), patch.object(main_module, "_session_cost", side_effect=failed_cost):
            await ws.to_receive.put(
                {
                    "type": "prompt",
                    "sessionId": "active-session",
                    "text": "hello",
                    "requestId": "p-late-reply",
                }
            )
            await asyncio.wait_for(prompt_started.wait(), timeout=5.0)
            current["signature"] = "sig-final"

            provisional = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert provisional["type"] == "turn_complete"
            assert provisional["result"]["settledFromDb"] is True
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "db_message_update"
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "reasoning_update"
            ms = sm.get("active-session")
            turn = ms.active_turn_task
            assert turn is not None and not turn.done()
            assert ms.db_fallback_settled

            prompt_event.set()
            completed = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert completed["type"] == "turn_complete"
            assert completed["requestId"] == "p-late-reply"
            assert "settledFromDb" not in completed["result"]
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "db_message_update"
            assert (await asyncio.wait_for(ws.sent.get(), timeout=5.0))["type"] == "reasoning_update"
            await asyncio.wait_for(turn, timeout=5.0)
            assert ms.active_turn_task is None
            assert not ms.db_fallback_settled
            assert ws.sent.empty()
            assert "Final cost update failed for active-session: cost failed" in caplog.text
    finally:
        prompt_event.set()
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_prompt_does_not_settle_on_intermediate_stop_before_compaction(patched_deps):
    """An intermediate assistant ``stop`` followed by a quiet compaction
    pause must not cancel the still-running ACP prompt."""
    import server.main as main_module

    current = {
        "signature": "sig-previous",
        "tip": {"role": "assistant", "finish_reason": "stop"},
    }
    main_module.db_reader.get_message_signature.side_effect = (
        lambda _sid: current["signature"]
    )
    main_module.db_reader.get_turn_tip_state.side_effect = (
        lambda _sid: current["tip"]
    )

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    prompt_event = asyncio.Event()
    prompt_started = asyncio.Event()
    try:
        await _establish_active_session(ws, sid="active-session")
        FakeACPClient.prompt_event = prompt_event
        FakeACPClient.prompt_started_event = prompt_started

        with patch.object(main_module, "_TURN_SETTLE_QUIET_SECONDS", 0.05), patch.object(
            main_module, "_TURN_SETTLE_POLL_SECONDS", 0.01
        ), patch.object(
            main_module, "_TURN_SETTLE_CONFIRM_SECONDS", 0.5
        ):
            await ws.to_receive.put(
                {
                    "type": "prompt",
                    "sessionId": "active-session",
                    "text": "hello",
                    "requestId": "p-compaction",
                }
            )
            await asyncio.wait_for(prompt_started.wait(), timeout=5.0)

            # The previous turn's terminal row cannot settle this prompt.
            await asyncio.sleep(0.08)
            assert ws.sent.empty()

            # This row is terminal for one generation, but the agent is about
            # to write internal compaction prompts and continue the same turn.
            current["signature"] = "sig-intermediate"
            await asyncio.sleep(0.15)
            assert ws.sent.empty()

            current["signature"] = "sig-compaction-prompt"
            current["tip"] = {"role": "user", "finish_reason": None}
            await asyncio.sleep(0.02)
            assert ws.sent.empty()

            # The authoritative ACP reply eventually ends the actual turn.
            prompt_event.set()
            completed = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert completed["type"] == "turn_complete"
            assert completed["requestId"] == "p-compaction"
            assert "settledFromDb" not in completed["result"]
    finally:
        prompt_event.set()
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_prompt_does_not_settle_while_tip_is_mid_turn(patched_deps):
    """A ``tool_calls`` finish_reason on the newest row means the turn is
    still in progress — the settlement fallback must not fire."""
    import server.main as main_module

    main_module.db_reader.get_message_signature.return_value = "sig-mid"
    main_module.db_reader.get_turn_tip_state.return_value = {
        "role": "assistant",
        "finish_reason": "tool_calls",
    }

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    prompt_event = asyncio.Event()
    prompt_started = asyncio.Event()
    try:
        await _establish_active_session(ws, sid="active-session")
        FakeACPClient.prompt_event = prompt_event
        FakeACPClient.prompt_started_event = prompt_started

        with patch.object(main_module, "_TURN_SETTLE_QUIET_SECONDS", 0.2), patch.object(
            main_module, "_TURN_SETTLE_POLL_SECONDS", 0.05
        ):
            await ws.to_receive.put(
                {
                    "type": "prompt",
                    "sessionId": "active-session",
                    "text": "hello",
                    "requestId": "p-mid",
                }
            )
            await asyncio.wait_for(prompt_started.wait(), timeout=5.0)

            # Nothing should be sent while the tip shows a mid-turn state.
            try:
                stray = await asyncio.wait_for(ws.sent.get(), timeout=0.5)
                assert stray["type"] != "turn_complete", (
                    f"turn_complete fired mid-turn: {stray}"
                )
            except asyncio.TimeoutError:
                pass

            # Releasing the real prompt still completes the turn normally.
            prompt_event.set()
            completed = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
            assert completed["type"] == "turn_complete"
            assert completed["requestId"] == "p-mid"
            assert "settledFromDb" not in completed["result"]
    finally:
        prompt_event.set()
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_provisional_completion_can_resume_on_reconnected_socket(patched_deps):
    import server.main as main_module
    from server.session_manager import session_manager as sm

    current = {"signature": "previous", "tip": {"role": "assistant", "finish_reason": "stop"}}
    main_module.db_reader.get_message_signature.side_effect = lambda _sid: current["signature"]
    main_module.db_reader.get_turn_tip_state.side_effect = lambda _sid: current["tip"]
    sm.grace_seconds = 30
    ws1 = MockWebSocket()
    task1 = asyncio.create_task(websocket_endpoint(ws1))
    ws2 = MockWebSocket()
    task2 = None
    gate = asyncio.Event()
    started = asyncio.Event()
    try:
        acp = await _establish_active_session(ws1, sid="active-session")
        FakeACPClient.prompt_event = gate
        FakeACPClient.prompt_started_event = started
        with patch.object(main_module, "_TURN_SETTLE_QUIET_SECONDS", 0), patch.object(
            main_module, "_TURN_SETTLE_POLL_SECONDS", 0.01
        ), patch.object(main_module, "_TURN_SETTLE_CONFIRM_SECONDS", 0):
            await ws1.to_receive.put({"type": "prompt", "text": "hello", "requestId": "p-reconnect"})
            await asyncio.wait_for(started.wait(), timeout=2.0)
            ms = sm.get("active-session")
            turn = ms.active_turn_task
            current["signature"] = "intermediate"
            frames = [await asyncio.wait_for(ws1.sent.get(), timeout=2.0) for _ in range(4)]
            assert frames[0]["result"]["settledFromDb"] is True
            await ws1.to_receive.put(WebSocketDisconnect())
            await asyncio.wait_for(task1, timeout=2.0)
            assert not turn.done()
            assert not acp.stopped

            task2 = asyncio.create_task(websocket_endpoint(ws2))
            await ws2.to_receive.put({
                "type": "resume_session", "sessionId": "active-session", "requestId": "r-reconnect"
            })
            assert (await asyncio.wait_for(ws2.sent.get(), timeout=2.0))["type"] == "session_resumed"
            assert sm.get("active-session") is ms
            current.update(signature="continuing", tip={"role": "user", "finish_reason": None})
            await acp.notification_handler({
                "method": "session/update",
                "params": {
                    "sessionId": "active-session",
                    "update": {"type": "agent_message_chunk", "chunk": "continued"},
                },
            })
            assert (await asyncio.wait_for(ws2.sent.get(), timeout=2.0))["type"] == "session_update"
            assert main_module.db_watcher.start_watching.call_count == 2
            gate.set()
            frames = [await asyncio.wait_for(ws2.sent.get(), timeout=2.0) for _ in range(4)]
            assert [frame["type"] for frame in frames] == [
                "turn_complete", "db_message_update", "reasoning_update", "cost_update"
            ]
            assert frames[0]["requestId"] == "p-reconnect"
            assert "settledFromDb" not in frames[0]["result"]
            await asyncio.wait_for(turn, timeout=2.0)
            assert ms.active_turn_task is None
            assert ws1.sent.empty()
    finally:
        sm.grace_seconds = 0
        if task2 is not None:
            await _cleanup_endpoint(ws2, task2)
        if not task1.done():
            await _cleanup_endpoint(ws1, task1)
        if sm.get("active-session") is not None:
            await sm.cancel_orphan(sm.get("active-session"))


@pytest.mark.asyncio
async def test_brief_disconnect_does_not_cancel_turn(patched_deps):
    """A brief WebSocket disconnect (shorter than the grace window) does
    not cancel the active turn or stop ACP, and reconnecting within the
    window rebinds to the same in-flight turn."""
    from server.session_manager import session_manager as sm

    # Use a non-zero grace period for this test.
    sm.grace_seconds = 30

    ws1 = MockWebSocket()
    task1 = asyncio.create_task(websocket_endpoint(ws1))
    try:
        acp = await _establish_active_session(ws1, sid="active-session")

        # Start a prompt that blocks (stays in-flight).
        prompt_event = asyncio.Event()
        prompt_started = asyncio.Event()
        FakeACPClient.prompt_event = prompt_event
        FakeACPClient.prompt_started_event = prompt_started

        await ws1.to_receive.put(
            {"type": "prompt", "sessionId": "active-session", "text": "hello", "requestId": "p1"}
        )
        await asyncio.wait_for(prompt_started.wait(), timeout=5.0)

        # Simulate the browser disconnecting.
        await ws1.to_receive.put(WebSocketDisconnect())
        # Wait for the endpoint task to finish (the finally block runs
        # detach_socket which starts the grace timer but does not stop ACP).
        await asyncio.wait_for(task1, timeout=10.0)

        # The ACP client must NOT have been stopped — the grace timer is
        # still running.
        assert not acp.stopped, (
            "ACP was stopped on disconnect despite non-zero grace period"
        )

        # The session should still be in the manager.
        assert sm.get("active-session") is not None, (
            "Session was removed from manager during grace period"
        )

        # Reconnect with resume_session within the grace window.
        ws2 = MockWebSocket()
        task2 = asyncio.create_task(websocket_endpoint(ws2))
        try:
            await ws2.to_receive.put(
                {"type": "resume_session", "sessionId": "active-session", "requestId": "r1"}
            )

            # The frontend should receive session_resumed promptly.
            msg = await asyncio.wait_for(ws2.sent.get(), timeout=5.0)
            assert msg["type"] == "session_resumed", (
                f"Expected session_resumed, got {msg}"
            )
            assert msg["sessionId"] == "active-session"

            # The ACP client should still not be stopped — the turn is
            # still running.
            assert not acp.stopped, (
                "ACP was stopped after reconnect within grace window"
            )

            # Release the prompt so the turn completes.
            prompt_event.set()

            # The reconnected client should receive turn_complete.
            msg = await asyncio.wait_for(ws2.sent.get(), timeout=10.0)
            assert msg["type"] == "turn_complete", (
                f"Expected turn_complete on reconnect, got {msg}"
            )
            assert msg["sessionId"] == "active-session"
        finally:
            await _cleanup_endpoint(ws2, task2)
    finally:
        prompt_event.set()
        sm.grace_seconds = 0
        sm._sessions.clear()
        if not task1.done():
            task1.cancel()
            try:
                await task1
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_reconnect_does_not_replay_text_delta_already_delivered(patched_deps):
    """A text delta accepted by the browser must not be replayed on reconnect."""
    from server.session_manager import session_manager as sm

    sm.grace_seconds = 30
    ws1 = MockWebSocket()
    task1 = asyncio.create_task(websocket_endpoint(ws1))
    try:
        acp = await _establish_active_session(ws1, sid="active-session")
        await acp.notification_handler(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "active-session",
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}},
                },
            }
        )
        delivered = await asyncio.wait_for(ws1.sent.get(), timeout=5.0)
        assert delivered["type"] == "session_update"
        assert delivered["update"]["chunk"] == "hello"

        await ws1.to_receive.put(WebSocketDisconnect())
        await asyncio.wait_for(task1, timeout=10.0)

        ws2 = MockWebSocket()
        task2 = asyncio.create_task(websocket_endpoint(ws2))
        try:
            await ws2.to_receive.put(
                {"type": "resume_session", "sessionId": "active-session", "requestId": "r-text"}
            )
            resumed = await asyncio.wait_for(ws2.sent.get(), timeout=5.0)
            assert resumed["type"] == "session_resumed"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws2.sent.get(), timeout=0.05)
        finally:
            await _cleanup_endpoint(ws2, task2)
    finally:
        sm.grace_seconds = 0
        sm._sessions.clear()
        if not task1.done():
            task1.cancel()
            try:
                await task1
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_reconnect_replays_text_delta_missed_while_disconnected(patched_deps):
    """Keep one missed text delta when ACP emits while the socket is orphaned."""
    from server.session_manager import session_manager as sm

    sm.grace_seconds = 30
    ws1 = MockWebSocket()
    task1 = asyncio.create_task(websocket_endpoint(ws1))
    try:
        acp = await _establish_active_session(ws1, sid="active-session")
        await ws1.to_receive.put(WebSocketDisconnect())
        await asyncio.wait_for(task1, timeout=10.0)

        await acp.notification_handler(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "active-session",
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "missed"}},
                },
            }
        )

        ws2 = MockWebSocket()
        task2 = asyncio.create_task(websocket_endpoint(ws2))
        try:
            await ws2.to_receive.put(
                {"type": "resume_session", "sessionId": "active-session", "requestId": "r-missed"}
            )
            resumed = await asyncio.wait_for(ws2.sent.get(), timeout=5.0)
            replay = await asyncio.wait_for(ws2.sent.get(), timeout=5.0)
            assert resumed["type"] == "session_resumed"
            assert replay["type"] == "session_update"
            assert replay["update"]["type"] == "agent_message_chunk"
            assert replay["update"]["chunk"] == "missed"
        finally:
            await _cleanup_endpoint(ws2, task2)
    finally:
        sm.grace_seconds = 0
        sm._sessions.clear()
        if not task1.done():
            task1.cancel()
            try:
                await task1
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_disconnect_longer_than_grace_tears_down(patched_deps):
    """A disconnect longer than the grace window tears down ACP, rejects
    pending permissions, and removes the session.  A subsequent reconnect
    resumes from persisted history."""
    from server.session_manager import session_manager as sm

    # Use a very short grace period.
    sm.grace_seconds = 0.1

    ws1 = MockWebSocket()
    task1 = asyncio.create_task(websocket_endpoint(ws1))
    try:
        acp = await _establish_active_session(ws1, sid="active-session")

        # Start a prompt that blocks (stays in-flight).
        prompt_event = asyncio.Event()
        prompt_started = asyncio.Event()
        FakeACPClient.prompt_event = prompt_event
        FakeACPClient.prompt_started_event = prompt_started

        await ws1.to_receive.put(
            {"type": "prompt", "sessionId": "active-session", "text": "hello", "requestId": "p1"}
        )
        await asyncio.wait_for(prompt_started.wait(), timeout=5.0)

        # Simulate the browser disconnecting.
        await ws1.to_receive.put(WebSocketDisconnect())
        await asyncio.wait_for(task1, timeout=10.0)

        # Wait for the grace timer to fire and tear down the session.
        # The grace period is 0.1s; give it a bit of headroom.
        await asyncio.sleep(1.0)

        # The ACP client must have been stopped.
        assert acp.stopped, (
            "ACP was not stopped after grace period expired"
        )

        # The session should have been removed from the manager.
        assert sm.get("active-session") is None, (
            "Session was not removed from manager after grace expiry"
        )

        # Reconnect — should fall back to resume-from-persisted-history.
        ws2 = MockWebSocket()
        task2 = asyncio.create_task(websocket_endpoint(ws2))
        try:
            await ws2.to_receive.put(
                {"type": "resume_session", "sessionId": "active-session", "requestId": "r2"}
            )

            # The frontend should receive session_resumed (from the
            # fallback path that creates a new ACP and loads the session).
            msg = await asyncio.wait_for(ws2.sent.get(), timeout=10.0)
            assert msg["type"] == "session_resumed", (
                f"Expected session_resumed from fallback, got {msg}"
            )
            assert msg["sessionId"] == "active-session"

            # A new FakeACPClient should have been created for the
            # fallback resume.
            assert len(FakeACPClient.instances) >= 2, (
                "Expected a new ACP client for fallback resume"
            )
        finally:
            await _cleanup_endpoint(ws2, task2)
    finally:
        prompt_event.set()
        sm.grace_seconds = 0
        sm._sessions.clear()
        if not task1.done():
            task1.cancel()
            try:
                await task1
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Task 9: Reject prompts during active session transitions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_during_load_history_is_rejected_and_transition_not_cancelled(patched_deps):
    """A ``prompt`` that arrives while a ``load_history`` transition is in
    flight is rejected with ``"Session is still resuming."``, the transition
    is **not** cancelled, and no ``"No active ACP session"`` error is
    produced."""
    load_event = asyncio.Event()
    load_started = asyncio.Event()
    FakeACPClient.load_event = load_event
    FakeACPClient.load_started_event = load_started

    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        # Start a load_history transition that blocks inside load_session.
        await ws.to_receive.put(
            {"type": "load_history", "sessionId": "session-T", "requestId": "lh1"}
        )
        await asyncio.wait_for(load_started.wait(), timeout=5.0)

        # Send a prompt while the transition is in flight.
        await ws.to_receive.put(
            {
                "type": "prompt",
                "sessionId": "session-T",
                "text": "hello",
                "requestId": "p1",
            }
        )

        # The backend should reject the prompt promptly.
        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "error", f"Expected error, got {msg}"
        assert msg["requestId"] == "p1"
        assert msg["operation"] == "prompt"
        assert msg["message"] == "Session is still resuming.", (
            f"Unexpected error message: {msg['message']}"
        )
        # The sessionId in the error should echo the client's request.
        assert msg["sessionId"] == "session-T"

        # The transition must still be running — it was not cancelled.
        assert FakeACPClient.instances, "No FakeACPClient was created"
        first_acp = FakeACPClient.instances[0]
        assert first_acp.loaded_sid == "session-T", (
            "Transition's load_session was not reached / was cancelled"
        )
        assert not first_acp.stopped, (
            "Transition's ACP client was stopped — transition was cancelled"
        )

        # Release the load event so the transition can complete.
        load_event.set()

        # The transition should complete normally and emit session_resumed.
        messages = await _drain_messages(ws, 10, timeout=5.0)
        resumed = [m for m in messages if m.get("type") == "session_resumed"]
        assert len(resumed) == 1, (
            f"Transition did not complete after prompt rejection: {messages}"
        )
        assert resumed[0]["sessionId"] == "session-T"

        # No "No active ACP session" error should have been produced.
        no_acp = [
            m for m in messages
            if m.get("type") == "error" and m.get("message") == "No active ACP session"
        ]
        assert no_acp == [], (
            f"Unexpected 'No active ACP session' error: {no_acp}"
        )
    finally:
        load_event.set()
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_prompt_with_no_transition_and_acp_loaded_starts_turn(patched_deps):
    """A ``prompt`` sent when no transition is running and ACP is loaded
    starts a turn normally (asserts the happy path is preserved)."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        acp = await _establish_active_session(ws, sid="active-session")

        # No transition is in flight; send a prompt.
        await ws.to_receive.put(
            {"type": "prompt", "sessionId": "active-session", "text": "hello", "requestId": "p1"}
        )

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "turn_complete", f"Expected turn_complete, got {msg}"
        assert msg["sessionId"] == "active-session"
        assert msg["requestId"] == "p1"

        assert acp.prompt_calls == [("active-session", "hello")], (
            f"Unexpected prompt calls: {acp.prompt_calls}"
        )
    finally:
        await _cleanup_endpoint(ws, task)


@pytest.mark.asyncio
async def test_prompt_with_no_transition_and_no_acp_gets_no_acp_error(patched_deps):
    """A ``prompt`` sent when ACP is genuinely absent (no transition
    running) still gets the ``"No active ACP session"`` error."""
    ws = MockWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws))
    try:
        # Do NOT establish a session — there is no ACP and no transition.
        await ws.to_receive.put(
            {"type": "prompt", "text": "hello", "requestId": "p1"}
        )

        msg = await asyncio.wait_for(ws.sent.get(), timeout=5.0)
        assert msg["type"] == "error", f"Expected error, got {msg}"
        assert msg["requestId"] == "p1"
        assert msg["operation"] == "prompt"
        assert msg["message"] == "No active ACP session", (
            f"Unexpected error message: {msg['message']}"
        )

        # No FakeACPClient should have been created.
        assert FakeACPClient.instances == [], (
            f"Unexpected ACP client creation: {FakeACPClient.instances}"
        )
    finally:
        await _cleanup_endpoint(ws, task)
