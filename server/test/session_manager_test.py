"""Tests for the server-side ACP session manager (Task 5).

Covers:
- ``attach_socket`` creates a new session.
- ``detach_socket`` starts the grace timer but does not stop ACP or
  cancel the turn.
- Re-``attach_socket`` within the grace window cancels the grace timer
  and rebinds the socket.
- Grace timer expiry calls ``cancel_orphan``, which stops ACP, rejects
  permissions, cancels the turn, and removes the session.
- ``grace_seconds=0`` triggers immediate teardown.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from server.session_manager import ManagedSession, SessionManager


class FakeWebSocket:
    """Minimal WebSocket stub."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class FakeACPClient:
    """Controllable ACP client stub for session-manager tests."""

    def __init__(self) -> None:
        self.stopped = False
        self.send_response_calls: list[tuple] = []

    async def stop(self) -> None:
        self.stopped = True

    async def send_response(self, *args, **kwargs) -> None:
        self.send_response_calls.append((args, kwargs))


@pytest.fixture
def mgr() -> SessionManager:
    """Fresh SessionManager with no grace and no sessions."""
    m = SessionManager(grace_seconds=0)
    return m


@pytest.mark.asyncio
async def test_attach_socket_creates_new_session(mgr: SessionManager):
    """``attach_socket`` with a new session id creates a managed session."""
    ws = FakeWebSocket()
    ms = mgr.attach_socket("sess-1", ws)
    assert ms.session_id == "sess-1"
    assert ms.websocket is ws
    assert mgr.get("sess-1") is ms


@pytest.mark.asyncio
async def test_attach_socket_anonymous_for_fresh_connect(mgr: SessionManager):
    """``attach_socket(None, ws)`` creates an anonymous session (no id)."""
    ws = FakeWebSocket()
    ms = mgr.attach_socket(None, ws)
    assert ms.session_id is None
    assert ms.websocket is ws
    # Anonymous sessions are not in the registry.
    assert mgr._sessions == {}


@pytest.mark.asyncio
async def test_detach_socket_grace_zero_tears_down_immediately(mgr: SessionManager):
    """``detach_socket`` with ``grace_seconds=0`` tears down immediately."""
    ws = FakeWebSocket()
    ms = mgr.attach_socket("sess-1", ws)
    acp = FakeACPClient()
    ms.acp = acp
    # Add a pending permission request.
    ms.permission_requests["tc-1"] = {
        "request_id": "req-1",
        "options": [],
        "timeout_task": None,
    }

    await mgr.detach_socket(ms, ws)

    assert ms.websocket is None
    assert acp.stopped, "ACP was not stopped on immediate teardown"
    assert ms.acp is None
    assert mgr.get("sess-1") is None, "Session was not removed from registry"
    # Pending permission should have been rejected.
    assert any(
        kwargs.get("error") and args[0] == "req-1"
        for args, kwargs in acp.send_response_calls
    ), "Pending permission was not rejected"


@pytest.mark.asyncio
async def test_detach_socket_starts_grace_timer_without_stopping_acp():
    """``detach_socket`` with a non-zero grace period starts a timer but
    does not stop ACP or cancel the turn."""
    m = SessionManager(grace_seconds=30)
    ws = FakeWebSocket()
    ms = m.attach_socket("sess-1", ws)
    acp = FakeACPClient()
    ms.acp = acp

    # Create a dummy turn task that just sleeps.
    async def _dummy_turn():
        await asyncio.sleep(100)

    turn_task = asyncio.create_task(_dummy_turn())
    ms.active_turn_task = turn_task

    await m.detach_socket(ms, ws)

    assert ms.websocket is None
    assert not acp.stopped, "ACP was stopped during grace period"
    assert ms.orphan_timer is not None, "Grace timer was not started"
    assert not turn_task.done(), "Turn task was cancelled during grace period"

    # Clean up.
    ms.orphan_timer.cancel()
    try:
        await ms.orphan_timer
    except asyncio.CancelledError:
        pass
    turn_task.cancel()
    try:
        await turn_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_re_attach_within_grace_cancels_timer_and_rebinds():
    """Re-``attach_socket`` within the grace window cancels the grace timer
    and rebinds the socket to the existing managed session."""
    m = SessionManager(grace_seconds=30)
    ws1 = FakeWebSocket()
    ms = m.attach_socket("sess-1", ws1)
    acp = FakeACPClient()
    ms.acp = acp

    # Disconnect — starts the grace timer.
    await m.detach_socket(ms, ws1)
    assert ms.orphan_timer is not None
    assert not ms.orphan_timer.done()
    assert ms.websocket is None

    # Reconnect within the grace window.
    ws2 = FakeWebSocket()
    ms2 = m.attach_socket("sess-1", ws2)
    assert ms2 is ms, "Re-attach should return the same managed session"
    assert ms.websocket is ws2, "Socket was not rebound"
    assert ms.orphan_timer is None or ms.orphan_timer.done(), (
        "Grace timer was not cancelled on reconnect"
    )
    assert not acp.stopped, "ACP was stopped despite reconnect within grace"
    assert m.get("sess-1") is ms


@pytest.mark.asyncio
async def test_grace_timer_expiry_tears_down_session():
    """Grace timer expiry calls ``cancel_orphan``, which stops ACP,
    rejects permissions, cancels the turn, and removes the session."""
    m = SessionManager(grace_seconds=0.1)
    cleanup_calls: list[str] = []
    m.on_session_cleanup = lambda sid: cleanup_calls.append(sid)

    ws = FakeWebSocket()
    ms = m.attach_socket("sess-1", ws)
    acp = FakeACPClient()
    ms.acp = acp
    ms.permission_requests["tc-1"] = {
        "request_id": "req-1",
        "options": [],
        "timeout_task": None,
    }

    # Create a dummy turn task.
    async def _dummy_turn():
        await asyncio.sleep(100)

    turn_task = asyncio.create_task(_dummy_turn())
    ms.active_turn_task = turn_task

    # Disconnect — starts the grace timer.
    await m.detach_socket(ms, ws)
    assert ms.orphan_timer is not None

    # Wait for the grace timer to fire.
    await asyncio.wait_for(ms.orphan_timer, timeout=5.0)

    assert acp.stopped, "ACP was not stopped after grace expiry"
    assert turn_task.done(), "Turn task was not cancelled after grace expiry"
    assert m.get("sess-1") is None, "Session was not removed after grace expiry"
    assert cleanup_calls == ["sess-1"], (
        f"Watcher cleanup was not called: {cleanup_calls}"
    )
    # Pending permission should have been rejected.
    assert any(
        kwargs.get("error") and args[0] == "req-1"
        for args, kwargs in acp.send_response_calls
    ), "Pending permission was not rejected on grace expiry"


@pytest.mark.asyncio
async def test_grace_zero_immediate_teardown():
    """``grace_seconds=0`` triggers immediate teardown (used by other tests)."""
    m = SessionManager(grace_seconds=0)
    ws = FakeWebSocket()
    ms = m.attach_socket("sess-1", ws)
    acp = FakeACPClient()
    ms.acp = acp

    await m.detach_socket(ms, ws)

    assert acp.stopped, "ACP was not stopped with grace=0"
    assert m.get("sess-1") is None, "Session was not removed with grace=0"
    assert ms.orphan_timer is None, "No orphan timer should be created with grace=0"


@pytest.mark.asyncio
async def test_shutdown_all_tears_down_every_session():
    """``shutdown_all`` tears down every managed session."""
    m = SessionManager(grace_seconds=0)
    cleanup_calls: list[str] = []
    m.on_session_cleanup = lambda sid: cleanup_calls.append(sid)

    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    ms1 = m.attach_socket("sess-1", ws1)
    ms2 = m.attach_socket("sess-2", ws2)
    acp1 = FakeACPClient()
    acp2 = FakeACPClient()
    ms1.acp = acp1
    ms2.acp = acp2

    await m.shutdown_all()

    assert acp1.stopped and acp2.stopped
    assert m.get("sess-1") is None
    assert m.get("sess-2") is None


@pytest.mark.asyncio
async def test_get_or_create_returns_existing(mgr: SessionManager):
    """``get_or_create`` returns an existing session or creates a new one."""
    ws = FakeWebSocket()
    ms1 = mgr.attach_socket("sess-1", ws)
    ms2 = mgr.get_or_create("sess-1")
    assert ms2 is ms1
    ms3 = mgr.get_or_create("sess-2")
    assert ms3 is not ms1
    assert ms3.session_id == "sess-2"


@pytest.mark.asyncio
async def test_register_rekeys_anonymous_session(mgr: SessionManager):
    """``register`` keys an anonymous managed session under a real id."""
    ws = FakeWebSocket()
    ms = mgr.attach_socket(None, ws)
    assert ms.session_id is None
    mgr.register(ms, "real-sess")
    assert ms.session_id == "real-sess"
    assert mgr.get("real-sess") is ms


@pytest.mark.asyncio
async def test_orphan_cleanup_does_not_remove_a_replacement_session(mgr: SessionManager):
    """A stale session may stop its own ACP without evicting the current owner."""
    stale = mgr.attach_socket("same-session", FakeWebSocket())
    stale_acp = FakeACPClient()
    stale.acp = stale_acp
    replacement = ManagedSession("same-session")
    replacement_acp = FakeACPClient()
    replacement.acp = replacement_acp
    mgr.register(replacement, "same-session")

    await mgr.cancel_orphan(stale)

    assert stale_acp.stopped is True
    assert mgr.get("same-session") is replacement
    assert replacement_acp.stopped is False


@pytest.mark.asyncio
async def test_orphan_cleanup_continues_after_task_cleanup_errors(mgr: SessionManager, caplog):
    """Task cleanup failures are logged in order and do not prevent ACP teardown."""
    ms = mgr.attach_socket("cleanup-session", FakeWebSocket())
    acp = FakeACPClient()
    ms.acp = acp
    events = []

    async def failing_task(name, started):
        started.set()
        try:
            await asyncio.Future()
        finally:
            events.append(name)
            raise RuntimeError(f"{name} cleanup failed")

    transition_started = asyncio.Event()
    turn_started = asyncio.Event()
    ms.active_transition_task = asyncio.create_task(failing_task("transition", transition_started))
    ms.active_turn_task = asyncio.create_task(failing_task("turn", turn_started))
    await transition_started.wait()
    await turn_started.wait()
    mgr.on_session_cleanup = lambda sid: events.append("watchers")

    await mgr.cancel_orphan(ms)

    assert events == ["transition", "turn", "watchers"]
    assert caplog.messages == [
        "Error waiting for cancelled transition: transition cleanup failed",
        "Error waiting for cancelled turn: turn cleanup failed",
    ]
    assert acp.stopped is True
    assert mgr.get("cleanup-session") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("grace", [0, 30])
async def test_detach_with_stale_socket_leaves_rebound_session_alone(grace: int):
    """``detach_socket`` must ignore a socket that is no longer attached.

    After a re-attach binds a newer socket, the old endpoint's ``finally``
    must not detach the session, start an orphan timer, or touch ACP, turn,
    transition, or permission state.
    """
    m = SessionManager(grace_seconds=grace)
    cleanup_calls: list[str] = []
    m.on_session_cleanup = lambda sid: cleanup_calls.append(sid)
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    ms = m.attach_socket("sess-1", ws1)
    acp = FakeACPClient()
    ms.acp = acp
    permission_timer = asyncio.create_task(asyncio.sleep(100))
    turn_task = asyncio.create_task(asyncio.sleep(100))
    transition_task = asyncio.create_task(asyncio.sleep(100))
    ms.active_turn_task = turn_task
    ms.active_transition_task = transition_task
    pending = {"request_id": "req-1", "options": [], "timeout_task": permission_timer}
    ms.permission_requests["tc-1"] = pending
    try:
        assert m.attach_socket("sess-1", ws2) is ms
        assert ms.websocket is ws2

        await m.detach_socket(ms, ws1)

        assert ms.websocket is ws2
        assert ms.orphan_timer is None
        assert not acp.stopped
        assert not turn_task.done() and not transition_task.done()
        assert not permission_timer.done()
        assert ms.permission_requests["tc-1"] is pending
        assert m.get("sess-1") is ms
        assert cleanup_calls == []

        await m.detach_socket(ms, ws2)

        if grace:
            assert ms.websocket is None
            assert ms.orphan_timer is not None and not ms.orphan_timer.done()
            assert not acp.stopped
            assert m.get("sess-1") is ms
            orphan = ms.orphan_timer
            await m.detach_socket(ms, ws1)
            assert ms.orphan_timer is orphan
            orphan.cancel()
            await asyncio.gather(orphan, return_exceptions=True)
            ms.orphan_timer = None
        else:
            assert acp.stopped
            assert m.get("sess-1") is None
    finally:
        for task in (permission_timer, turn_task, transition_task):
            task.cancel()
        await asyncio.gather(permission_timer, turn_task, transition_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("grace", [0, 30])
async def test_detach_wrong_socket_on_anonymous_session_is_noop(grace: int):
    """A wrong-socket detach on an anonymous session must not tear it down."""
    m = SessionManager(grace_seconds=grace)
    ws = FakeWebSocket()
    ms = m.attach_socket(None, ws)
    acp = FakeACPClient()
    ms.acp = acp

    await m.detach_socket(ms, FakeWebSocket())

    assert ms.websocket is ws
    assert not acp.stopped
    assert ms.orphan_timer is None
    assert m._sessions == {}

    await m.detach_socket(ms, ws)

    assert ms.websocket is None
    assert acp.stopped
