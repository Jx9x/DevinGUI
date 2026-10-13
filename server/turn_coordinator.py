"""Prompt completion and persistence delivery, independent of command dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import sqlite3
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from .acp_client import ACPClient
    from .db_reader import DBReader, DBWatcher
    from .session_manager import ManagedSession
    from .transcript_watcher import TranscriptWatcher


_TERMINAL_FINISH_REASONS = frozenset(
    {"stop", "end_turn", "max_tokens", "length", "refusal", "cancelled"}
)
_FINISH_REASON_TO_STOP_REASON = {
    "stop": "end_turn",
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "length": "max_tokens",
    "refusal": "refusal",
    "cancelled": "cancelled",
}


@dataclass(frozen=True)
class TurnSettlementTiming:
    """Monotonic polling/quiet/confirmation intervals for the DB fallback."""

    poll_seconds: float
    quiet_seconds: float
    confirm_seconds: float


class TurnCoordinator:
    """Run prompts and reconcile their live and final persistence snapshots.

    The endpoint owns scheduling and generation changes. Session state stays on
    ManagedSession; the current-session and send callbacks follow its socket
    during reconnect handoffs. Timing is read through a callback so changes to
    the endpoint's existing settings remain visible to running turns.
    """

    def __init__(
        self,
        *,
        current_session: Callable[[], ManagedSession],
        send: Callable[[dict], Awaitable[bool]],
        db_reader: DBReader,
        db_watcher: DBWatcher,
        transcript_watcher: TranscriptWatcher,
        reasoning_steps: Callable[[str], list[dict]],
        session_cost: Callable[[str], dict],
        timing: Callable[[], TurnSettlementTiming],
        logger: logging.Logger,
    ) -> None:
        self.current_session = current_session
        self.send = send
        self.db_reader = db_reader
        self.db_watcher = db_watcher
        self.transcript_watcher = transcript_watcher
        self.reasoning_steps = reasoning_steps
        self.session_cost = session_cost
        self.timing = timing
        self.logger = logger

    def start_watching(self, session_id: str) -> None:
        self.db_watcher.start_watching(session_id, self.handle_db_update)
        self.transcript_watcher.start_watching(session_id, self.handle_transcript_update)

    def resume_live_updates(self, session_id: str | None) -> None:
        """Reopen persistence polling when ACP continues a DB-settled turn."""
        ms = self.current_session()
        if ms.db_fallback_settled and session_id == ms.session_id:
            ms.db_fallback_settled = False
            self.start_watching(ms.session_id)
            if ms.db_fallback_resume_event is not None:
                ms.db_fallback_resume_event.set()

    async def send_cost_update(self, session_id: str) -> None:
        try:
            estimate = self.session_cost(session_id)
        except (OSError, sqlite3.DatabaseError) as error:
            self.logger.warning("Stored cost unavailable for %s: %s", session_id, error)
            return
        await self.send(
            {"type": "cost_update", "sessionId": session_id, "estimate": estimate}
        )

    async def send_final_persistence_snapshot(self, session_id: str) -> None:
        """Send a best-effort DB/reasoning refresh after a completion frame.

        Pick up persistence changes missed by the last watcher poll without
        restarting idle polling. Later writes are not awaited or retried.
        """
        try:
            await self.send(
                {
                    "type": "db_message_update",
                    "sessionId": session_id,
                    "messages": self.db_reader.get_messages(session_id),
                }
            )
            await self.send(
                {
                    "type": "reasoning_update",
                    "sessionId": session_id,
                    "steps": self.reasoning_steps(session_id),
                }
            )
        except Exception as e:
            # Persistence reconciliation is best-effort. A successful ACP
            # turn must not be converted into a prompt error because the local
            # DB/transcript happened to be briefly unreadable.
            self.logger.warning("Final persistence snapshot failed for %s: %s", session_id, e)

    async def handle_transcript_update(self, session_id: str, _steps: list[dict]) -> None:
        await self.send(
            {
                "type": "reasoning_update",
                "sessionId": session_id,
                "steps": self.reasoning_steps(session_id),
            }
        )
        await self.send_cost_update(session_id)

    async def handle_db_update(self, session_id: str, messages: list[dict]) -> None:
        await self.send(
            {
                "type": "db_message_update",
                "sessionId": session_id,
                "messages": messages,
            }
        )
        # Transcript usage takes precedence in cost estimates; DB usage is the fallback.
        if not self.transcript_watcher.get_usage_records(session_id):
            await self.send_cost_update(session_id)
        # Reasoning can also live in message_nodes.chat_message's thinking field,
        # so DB changes must refresh the merged reasoning list.
        await self.send(
            {
                "type": "reasoning_update",
                "sessionId": session_id,
                "steps": self.reasoning_steps(session_id),
            }
        )

    async def _wait_for_db_turn_end(
        self, session_id: str, baseline_signature: str
    ) -> tuple[dict, str]:
        """Return ``(provisional_result, signature)`` for a stable terminal DB tip.

        A missing ACP response can leave ``prompt()`` pending after persistence.
        Require a signature different from ``baseline_signature``, a confirmed
        terminal assistant tip, and quiet DB/ACP activity. Terminal rows can
        precede internal compaction, so this is not authoritative completion.
        """
        loop = asyncio.get_running_loop()
        last_signature = baseline_signature
        quiet_since = loop.time()
        terminal_signature: str | None = None
        terminal_since: float | None = None
        while True:
            try:
                signature = self.db_reader.get_message_signature(session_id)
                tip = self.db_reader.get_turn_tip_state(session_id)
            except Exception:
                signature = None
                tip = None
            now = loop.time()
            if signature is not None and signature != last_signature:
                last_signature = signature
                quiet_since = now
            terminal_tip = (
                signature is not None
                and signature != baseline_signature
                and tip is not None
                and tip.get("role") == "assistant"
                and tip.get("finish_reason") in _TERMINAL_FINISH_REASONS
            )
            if terminal_tip:
                if terminal_signature != signature:
                    terminal_signature = signature
                    terminal_since = now
            else:
                terminal_signature = None
                terminal_since = None
            if (
                terminal_tip
                and terminal_since is not None
                and now - terminal_since >= self.timing().confirm_seconds
                and now - quiet_since >= self.timing().quiet_seconds
                and now - self.current_session().last_acp_update_at
                >= self.timing().quiet_seconds
            ):
                finish = str(tip["finish_reason"])
                return (
                    {
                        "stopReason": _FINISH_REASON_TO_STOP_REASON.get(
                            finish, "end_turn"
                        ),
                        "settledFromDb": True,
                    },
                    signature,
                )
            await asyncio.sleep(self.timing().poll_seconds)

    async def run_prompt(
        self, acp_client: ACPClient, session_id: str, text: str,
        images: list[dict] | None, request_id: str | None,
        gen_id: int,
    ) -> None:
        """Run a prompt without cancelling ACP after provisional DB completion.

        The caller must register this task as ManagedSession.active_turn_task
        and pass its current generation_id so watcher/state cleanup can run.
        """
        ms = self.current_session()
        current_task = asyncio.current_task()
        prompt_task: asyncio.Task | None = None
        settle_task: asyncio.Task | None = None
        resume_task: asyncio.Task | None = None
        completed_from_db = False
        settled_signature: str | None = None

        async def send_completion(result: dict) -> None:
            await self.send(
                {
                    "type": "turn_complete",
                    "sessionId": session_id,
                    "stopReason": result.get("stopReason"),
                    "result": result,
                    # The frontend requires requestId to be a string; a
                    # missing one would get the whole completion frame
                    # dropped and leave the UI streaming forever.
                    "requestId": request_id or "",
                }
            )
            await self.send_final_persistence_snapshot(session_id)
            try:
                await self.send_cost_update(session_id)
            except Exception as e:
                self.logger.warning("Final cost update failed for %s: %s", session_id, e)

        try:
            ms.db_fallback_resume_event = asyncio.Event()
            # The previous turn may already end in a terminal assistant row.
            # Capture its signature before sending this prompt so the fallback
            # cannot complete the new turn from the previous turn's tip.
            try:
                baseline_signature = self.db_reader.get_message_signature(session_id)
            except Exception as e:
                self.logger.warning("DB turn-settlement unavailable for %s: %s", session_id, e)
                baseline_signature = None
            # Race the ACP response against the DB-settlement fallback. A
            # quiet terminal row permits a provisional completion, but the
            # ACP prompt must remain pending: a later internal continuation
            # can stream more output and needs the real response to clear it.
            prompt_task = asyncio.ensure_future(
                acp_client.prompt(session_id, text, images)
            )
            settle_task = (
                asyncio.ensure_future(self._wait_for_db_turn_end(session_id, baseline_signature))
                if baseline_signature is not None else None
            )
            while True:
                done, _pending = await asyncio.wait(
                    {task for task in (prompt_task, settle_task, resume_task) if task is not None},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if prompt_task in done:
                    result = prompt_task.result()
                    break
                if resume_task in done:
                    resume_task = None
                    assert settled_signature is not None
                    settle_task = asyncio.ensure_future(
                        self._wait_for_db_turn_end(session_id, settled_signature)
                    )
                    continue
                assert settle_task is not None
                provisional_result, settled_signature = settle_task.result()
                settle_task = None
                completed_from_db = True
                self.logger.warning(
                    "session/prompt response not yet received for %s; "
                    "provisionally settled turn from persisted state (stopReason=%s)",
                    session_id,
                    provisional_result.get("stopReason"),
                )
                if gen_id != self.current_session().generation_id:
                    return
                # Stop idle polling after a provisional completion. New ACP
                # activity restarts the watchers, while this task continues
                # waiting for the authoritative prompt response.
                ms.db_fallback_resume_event.clear()
                ms.db_fallback_settled = True
                self.db_watcher.stop_watching(session_id)
                self.transcript_watcher.stop_watching(session_id)
                ms.last_session_update = None
                await send_completion(provisional_result)
                resume_task = asyncio.ensure_future(
                    ms.db_fallback_resume_event.wait()
                )
            # A newer session switch or prompt has superseded this turn;
            # drop the result instead of overwriting current state.
            if gen_id != self.current_session().generation_id:
                return
            await send_completion(result)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            if completed_from_db and ms.db_fallback_settled:
                self.logger.warning("session/prompt timed out after DB settlement for %s", session_id)
                return
            self.logger.error("Prompt timed out for %s", session_id)
            if gen_id == self.current_session().generation_id:
                await self.send(
                    {
                        "type": "error",
                        "sessionId": session_id,
                        "requestId": request_id,
                        "operation": "prompt",
                        "message": "ACP prompt timed out",
                    }
                )
        except Exception as e:
            self.logger.error("Prompt failed: %s", e)
            if gen_id != self.current_session().generation_id:
                return
            await self.send(
                {
                    "type": "error",
                    "sessionId": session_id,
                    "requestId": request_id,
                    "operation": "prompt",
                    "message": str(e),
                }
            )
        finally:
            for pending_task in (prompt_task, settle_task, resume_task):
                if pending_task is not None and not pending_task.done():
                    pending_task.cancel()
            await asyncio.gather(
                *(task for task in (prompt_task, settle_task, resume_task) if task is not None),
                return_exceptions=True,
            )
            ms = self.current_session()
            if ms.active_turn_task is current_task and gen_id == ms.generation_id:
                ms.active_turn_task = None
                ms.db_fallback_settled = False
                ms.db_fallback_resume_event = None
                # Live persistence polling is only needed while a turn can be
                # changing. Stop both watchers as soon as the turn settles so
                # an idle GUI performs no recurring SQLite/filesystem work.
                self.db_watcher.stop_watching(session_id)
                self.transcript_watcher.stop_watching(session_id)
                # Reconnect replay is only useful while a turn is active. Do
                # not carry a stale tool/status update into an idle session.
                ms.last_session_update = None
