"""Exercise DB settlement timing with an explicit clock and no app startup."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from server.session_manager import ManagedSession
from server.turn_coordinator import TurnCoordinator, TurnSettlementTiming


@pytest.fixture
def coordinator():
    return TurnCoordinator(
        current_session=lambda: SimpleNamespace(last_acp_update_at=0),
        send=AsyncMock(return_value=True),
        db_reader=MagicMock(),
        db_watcher=MagicMock(),
        transcript_watcher=MagicMock(),
        reasoning_steps=MagicMock(return_value=[]),
        session_cost=MagicMock(return_value={}),
        timing=lambda: TurnSettlementTiming(poll_seconds=0.5, quiet_seconds=0, confirm_seconds=0),
        logger=MagicMock(),
    )


@pytest.mark.parametrize(
    "finish_reason,stop_reason",
    [
        ("stop", "end_turn"),
        ("end_turn", "end_turn"),
        ("max_tokens", "max_tokens"),
        ("length", "max_tokens"),
        ("refusal", "refusal"),
        ("cancelled", "cancelled"),
    ],
)
async def test_db_terminal_finish_reasons_keep_wire_mapping(coordinator, finish_reason, stop_reason):
    coordinator.db_reader.get_message_signature.return_value = "final"
    coordinator.db_reader.get_turn_tip_state.return_value = {
        "role": "assistant", "finish_reason": finish_reason,
    }
    assert await coordinator._wait_for_db_turn_end("session", "previous") == (
        {"stopReason": stop_reason, "settledFromDb": True}, "final"
    )


async def test_db_settlement_waits_for_new_tip_confirmation_and_acp_quiet(coordinator):
    coordinator.db_reader.get_message_signature.side_effect = ["previous", "final", "final", "final"]
    coordinator.db_reader.get_turn_tip_state.return_value = {"role": "assistant", "finish_reason": "stop"}
    coordinator.current_session = lambda: SimpleNamespace(last_acp_update_at=4)
    coordinator.timing = lambda: TurnSettlementTiming(poll_seconds=0.5, quiet_seconds=2, confirm_seconds=2)
    clock = SimpleNamespace(time=MagicMock(side_effect=[0, 0, 1, 4, 6]))
    with patch("server.turn_coordinator.asyncio.get_running_loop", return_value=clock), patch(
        "server.turn_coordinator.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        result = await coordinator._wait_for_db_turn_end("session", "previous")
    assert result == ({"stopReason": "end_turn", "settledFromDb": True}, "final")
    assert coordinator.db_reader.get_message_signature.call_count == 4
    assert sleep.await_args_list == [call(0.5)] * 3


@pytest.mark.parametrize("failure_source", ["signature", "tip"])
async def test_db_poll_read_failure_restarts_terminal_confirmation(coordinator, failure_source):
    terminal_tip = {"role": "assistant", "finish_reason": "stop"}
    coordinator.db_reader.get_message_signature.return_value = "final"
    coordinator.db_reader.get_turn_tip_state.return_value = terminal_tip
    failing_read = (
        coordinator.db_reader.get_message_signature
        if failure_source == "signature" else coordinator.db_reader.get_turn_tip_state
    )
    value = "final" if failure_source == "signature" else terminal_tip
    failing_read.side_effect = [value, OSError("DB briefly unreadable"), value, value]
    coordinator.timing = lambda: TurnSettlementTiming(poll_seconds=0.5, quiet_seconds=2, confirm_seconds=2)
    clock = SimpleNamespace(time=MagicMock(side_effect=[0, 0, 1, 2, 4]))
    with patch("server.turn_coordinator.asyncio.get_running_loop", return_value=clock), patch(
        "server.turn_coordinator.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        result = await coordinator._wait_for_db_turn_end("session", "previous")
    assert result == ({"stopReason": "end_turn", "settledFromDb": True}, "final")
    assert failing_read.call_count == 4
    assert sleep.await_args_list == [call(0.5)] * 3


@pytest.mark.parametrize("failure", [FileNotFoundError("No database"), sqlite3.OperationalError("Missing history table")])
async def test_unavailable_stored_cost_does_not_block_live_reasoning(coordinator, failure):
    coordinator.session_cost.side_effect = failure
    await coordinator.handle_transcript_update("session", [])
    coordinator.send.assert_awaited_once_with({"type": "reasoning_update", "sessionId": "session", "steps": []})
    coordinator.logger.warning.assert_called_once()


async def test_successful_prompt_survives_history_and_cost_failures(coordinator):
    ms = ManagedSession("session")
    ms.generation_id = 1
    coordinator.current_session = lambda: ms
    coordinator.db_reader.get_message_signature.side_effect = sqlite3.OperationalError("Missing history table")
    coordinator.db_reader.get_messages.side_effect = sqlite3.OperationalError("Missing history table")
    coordinator.session_cost.side_effect = sqlite3.OperationalError("Missing history table")
    acp = AsyncMock()
    acp.prompt.return_value = {"stopReason": "stop"}
    task = asyncio.create_task(coordinator.run_prompt(acp, "session", "hello", None, "req", 1))
    ms.active_turn_task = task
    await task
    coordinator.send.assert_awaited_once_with({
        "type": "turn_complete", "sessionId": "session", "stopReason": "stop",
        "result": {"stopReason": "stop"}, "requestId": "req",
    })
    assert ms.active_turn_task is None


async def test_final_cost_cancellation_propagates(coordinator):
    """Cancellation during the final cost update is not contained."""
    ms = ManagedSession("session")
    ms.generation_id = 1
    coordinator.current_session = lambda: ms
    coordinator.db_reader.get_message_signature.return_value = None
    coordinator.session_cost = MagicMock(side_effect=asyncio.CancelledError())
    acp = AsyncMock()
    acp.prompt = AsyncMock(return_value={"stopReason": "stop"})
    task = asyncio.create_task(
        coordinator.run_prompt(acp, "session", "hello", None, "req", 1)
    )
    ms.active_turn_task = task
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.send.await_count == 3
    assert ms.active_turn_task is None
