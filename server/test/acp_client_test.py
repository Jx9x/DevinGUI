"""Backend tests for Task 4: ACP pending-request cleanup on cancellation.

Verifies that ``ACPClient._send_request`` removes its pending future from
``_pending`` even when the awaiting coroutine is cancelled (which raises
``asyncio.CancelledError``, not caught by ``except Exception`` in modern
Python), and that a late ACP response arriving after cancellation is
silently ignored instead of raising ``InvalidStateError`` on the future.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from server.acp_client import ACPClient, ModelNotAppliedError


class _FakeStreamWriter:
    """Minimal async stream writer that accepts writes without blocking."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None


def _make_client() -> ACPClient:
    """Build an ``ACPClient`` with a stubbed subprocess and no real I/O."""
    client = ACPClient(notification_handler=lambda msg: _async_none(), cmd=["synthetic-acp"])

    fake_stdin = _FakeStreamWriter()
    fake_proc = MagicMock()
    fake_proc.stdin = fake_stdin
    fake_proc.returncode = None

    async def _noop_wait() -> int:
        return 0

    fake_proc.wait = _noop_wait
    fake_proc.terminate = MagicMock()
    fake_proc.kill = MagicMock()

    client.process = fake_proc
    return client


async def _async_none() -> None:
    return None


@pytest.mark.asyncio
async def test_send_request_cleans_pending_on_cancel():
    """Cancelling a coroutine awaiting ``_send_request`` removes the
    request id from ``_pending``."""
    client = _make_client()

    async def _await_request() -> None:
        await client._send_request("session/prompt", {"sessionId": "s1"}, timeout=30.0)

    task = asyncio.create_task(_await_request())
    # Yield control so the task reaches ``asyncio.wait_for(fut, ...)``.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # The request should be pending now.
    assert client._pending, "Expected a pending request before cancel"
    req_id = next(iter(client._pending))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert req_id not in client._pending, (
        f"req_id {req_id} still in _pending after cancellation: {client._pending}"
    )
    assert client._pending == {}, f"_pending not cleared: {client._pending}"


@pytest.mark.asyncio
async def test_prompt_preserves_line_breaks_in_agent_payload():
    client = _make_client()
    text = "First line\nSecond line\n\nFourth line"

    task = asyncio.create_task(client.prompt("s1", text))
    for _ in range(100):
        if client._pending:
            break
        await asyncio.sleep(0)

    request = json.loads(client.process.stdin.buffer.decode().strip())
    assert request["method"] == "session/prompt"
    assert request["params"]["prompt"][0] == {"type": "text", "text": text}

    req_id = next(iter(client._pending))
    await client._handle_message({"jsonrpc": "2.0", "id": req_id, "result": {}})
    await task


@pytest.mark.asyncio
async def test_long_prompt_keeps_waiting_for_completion(monkeypatch):
    """Simulate a turn exceeding the old 30-minute deadline without waiting."""
    client = _make_client()
    original_wait_for = asyncio.wait_for

    async def accelerated_wait_for(awaitable, timeout):
        return await original_wait_for(
            awaitable, timeout=0.01 if timeout == 1800.0 else timeout
        )

    monkeypatch.setattr(asyncio, "wait_for", accelerated_wait_for)
    task = asyncio.create_task(client.prompt("s1", "A long coding task"))
    try:
        await asyncio.sleep(0.05)
        assert not task.done(), "Long turns must retain their completion listener"
        req_id = next(iter(client._pending))
        result = {"stopReason": "end_turn"}
        await client._handle_message({"jsonrpc": "2.0", "id": req_id, "result": result})
        assert await task == result
        assert not client._pending
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "process_exit"])
async def test_prompt_without_deadline_still_cleans_up_on_termination(ending):
    client = _make_client()
    task = asyncio.create_task(client.prompt("s1", "A long coding task"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert client._pending
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await client._mark_unavailable("ACP process exited unexpectedly")
        with pytest.raises(RuntimeError, match="ACP process exited unexpectedly"):
            await task
    assert not client._pending


@pytest.mark.asyncio
async def test_late_response_after_cancel_does_not_raise():
    """A late ACP response for a cancelled request is silently ignored
    (no ``InvalidStateError`` on the future)."""
    client = _make_client()

    async def _await_request() -> None:
        await client._send_request("session/prompt", {"sessionId": "s1"}, timeout=30.0)

    task = asyncio.create_task(_await_request())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert client._pending, "Expected a pending request before cancel"
    req_id = next(iter(client._pending))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The future is gone from _pending; simulate a late response arriving
    # via ``_handle_message``.  It must not raise InvalidStateError.
    late_response = {"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}}
    # Should complete without raising.
    await client._handle_message(late_response)

    # _pending remains empty.
    assert client._pending == {}


@pytest.mark.asyncio
async def test_send_request_timeout_still_cleans_pending():
    """Timeout behavior is unchanged: the pending entry is removed and a
    ``TimeoutError`` is raised to the caller."""
    client = _make_client()

    with pytest.raises(asyncio.TimeoutError):
        await client._send_request(
            "session/prompt", {"sessionId": "s1"}, timeout=0.05
        )

    assert client._pending == {}, f"_pending not cleared after timeout: {client._pending}"


async def _set_model_with_reply(client: ACPClient, model_id: str, reply: dict) -> str:
    """Call ``set_session_model`` and answer the request with *reply*.

    *reply* is merged into the JSON-RPC response envelope, so it must carry
    either a ``result`` or an ``error`` key.
    """
    task = asyncio.create_task(client.set_session_model("s1", model_id))
    for _ in range(100):
        if client._pending:
            break
        await asyncio.sleep(0)
    assert client._pending, "set_session_model did not send a request"
    req_id = next(iter(client._pending))
    await client._handle_message({"jsonrpc": "2.0", "id": req_id, **reply})
    return await task


def _config_options(current_value: str) -> dict:
    return {
        "result": {
            "configOptions": [
                {"id": "mode", "type": "select", "currentValue": "accept-edits"},
                {"id": "model", "type": "select", "currentValue": current_value},
            ]
        }
    }


@pytest.mark.asyncio
async def test_set_session_model_returns_confirmed_model():
    """The model the agent reports as active is returned to the caller."""
    client = _make_client()
    applied = await _set_model_with_reply(
        client, "claude-opus-5-high", _config_options("claude-opus-5-high")
    )
    assert applied == "claude-opus-5-high"


@pytest.mark.asyncio
async def test_set_session_model_raises_when_agent_reports_another_model():
    """A silent fallback to a different model (e.g. the CLI default) is
    reported as a failure rather than accepted."""
    client = _make_client()
    with pytest.raises(ModelNotAppliedError) as exc:
        await _set_model_with_reply(
            client, "claude-opus-5-high", _config_options("swe-1-7-medium")
        )
    assert "swe-1-7-medium" in str(exc.value)
    assert "claude-opus-5-high" in str(exc.value)


@pytest.mark.asyncio
async def test_set_session_model_raises_when_model_is_unverifiable():
    """A response without a ``model`` config option cannot confirm the
    change, so it is treated as a failure."""
    client = _make_client()
    with pytest.raises(ModelNotAppliedError):
        await _set_model_with_reply(
            client,
            "claude-opus-5-high",
            {"result": {"configOptions": [{"id": "mode", "currentValue": "ask"}]}},
        )


@pytest.mark.asyncio
async def test_set_session_model_reports_rejected_model_id():
    """An unknown model id is rejected by the agent with a JSON-RPC error;
    the detail is surfaced without the long list of accepted ids."""
    client = _make_client()
    with pytest.raises(ModelNotAppliedError) as exc:
        await _set_model_with_reply(
            client,
            "glm-5-2-high",
            {
                "error": {
                    "code": -32002,
                    "message": "Resource not found",
                    "data": {
                        "uri": (
                            "Model not found: glm-5-2-high. Available models: "
                            "glm-5-2, glm-5-2-max, swe-1-7-medium"
                        )
                    },
                }
            },
        )
    assert "Model not found: glm-5-2-high" in str(exc.value)
    assert "Available models" not in str(exc.value)


def _new_model_options(model: str, thought: str, speed: str | None = None) -> list[dict]:
    options = [
        {
            "id": "model", "currentValue": model,
            "options": [
                {"value": "swe-2-high"},
                {"value": "glm-5-2-1m"},
                {"value": "claude-opus-5-5-medium"},
                {"value": "fusion-claude-opus-5-5-high-sidekick-swe-2-medium"},
            ],
        },
        {
            "id": "thought_level", "currentValue": thought,
            "options": [{"value": value} for value in ("low", "medium", "high", "xhigh", "max")],
        },
    ]
    if speed is not None:
        options.append({
            "id": "speed", "currentValue": speed,
            "options": [{"value": "standard"}, {"value": "fast"}],
        })
    return options


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "base", "thought", "fast"),
    [
        ("swe-2-max", "swe-2-high", "max", False),
        ("glm-5-2-max-1m", "glm-5-2-1m", "max", False),
        ("claude-opus-5-5-xhigh-fast", "claude-opus-5-5-medium", "xhigh", True),
        (
            "fusion-claude-opus-5-5-xhigh-fast-sidekick-swe-2-medium",
            "fusion-claude-opus-5-5-high-sidekick-swe-2-medium",
            "xhigh", True,
        ),
    ],
)
async def test_set_session_model_uses_11_1_controls(
    monkeypatch, variant: str, base: str, thought: str, fast: bool,
):
    client = _make_client()
    client._session_config_options = _new_model_options("swe-2-high", "high")
    calls: list[tuple[str, str]] = []
    state = {"model": "swe-2-high", "thought_level": "high", "speed": "standard"}

    async def fake_request(method: str, params: dict, timeout: float = 30.0) -> dict:
        assert method == "session/set_config_option"
        config_id, value = params["configId"], params["value"]
        calls.append((config_id, value))
        state[config_id] = value
        if config_id == "model":
            # Devin resets effort and speed when the model picker changes.
            state["thought_level"] = "high" if value.startswith("fusion-") or value.startswith("swe-") else "medium"
            state["speed"] = "standard"
        return {"configOptions": _new_model_options(
            state["model"], state["thought_level"],
            state["speed"] if state["model"] not in ("swe-2-high", "glm-5-2-1m") else None,
        )}

    monkeypatch.setattr(client, "_send_request", fake_request)
    assert await client.set_session_model("s1", variant) == variant
    expected = [("model", base), ("thought_level", thought)]
    if base not in ("swe-2-high", "glm-5-2-1m"):
        expected.append(("speed", "fast" if fast else "standard"))
    assert calls == expected


@pytest.mark.asyncio
async def test_set_session_model_refuses_unconfirmed_11_1_effort(monkeypatch):
    client = _make_client()
    client._session_config_options = _new_model_options("swe-2-high", "high")

    async def fake_request(method: str, params: dict, timeout: float = 30.0) -> dict:
        return {"configOptions": _new_model_options("swe-2-high", "high")}

    monkeypatch.setattr(client, "_send_request", fake_request)
    with pytest.raises(ModelNotAppliedError, match="thinking level 'high'"):
        await client.set_session_model("s1", "swe-2-max")


@pytest.mark.asyncio
async def test_config_option_update_refreshes_model_choices():
    client = _make_client()
    options = _new_model_options("swe-2-high", "max")
    await client._handle_message({
        "method": "session/update",
        "params": {"update": {"sessionUpdate": "config_option_update", "configOptions": options}},
    })
    assert client._session_config_options == options


async def _set_mode_with_reply(client: ACPClient, mode: str, reply: dict) -> str:
    """Call ``set_session_mode`` and answer the request with *reply*."""
    task = asyncio.create_task(client.set_session_mode("s1", mode))
    for _ in range(100):
        if client._pending:
            break
        await asyncio.sleep(0)
    assert client._pending, "set_session_mode did not send a request"
    req_id = next(iter(client._pending))
    await client._handle_message({"jsonrpc": "2.0", "id": req_id, **reply})
    return await task


@pytest.mark.asyncio
async def test_set_session_mode_returns_confirmed_mode():
    """The mode the agent reports as active is returned to the caller."""
    client = _make_client()
    applied = await _set_mode_with_reply(
        client,
        "bypass",
        {
            "result": {
                "configOptions": [
                    {"id": "mode", "type": "select", "currentValue": "bypass"},
                    {"id": "model", "type": "select", "currentValue": "test-model"},
                ]
            }
        },
    )
    assert applied == "bypass"


@pytest.mark.asyncio
async def test_set_session_mode_raises_when_agent_reports_another_mode():
    """A silent fallback to a different mode (e.g. the persisted CLI default)
    is reported as a failure rather than accepted."""
    client = _make_client()
    with pytest.raises(RuntimeError) as exc:
        await _set_mode_with_reply(
            client,
            "bypass",
            {
                "result": {
                    "configOptions": [
                        {"id": "mode", "type": "select", "currentValue": "accept-edits"},
                    ]
                }
            },
        )
    assert "bypass" in str(exc.value)
    assert "accept-edits" in str(exc.value)
