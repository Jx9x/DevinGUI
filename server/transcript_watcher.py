"""Watches Devin transcript JSON files for reasoning content.

Transcripts are written to ``%APPDATA%/devin/cli/transcripts/<session_id>.json``
after each turn.  They contain ``steps[].reasoning_content`` which is the raw
thinking text shown by ``Ctrl+O`` in the native CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from .message_projection import is_compaction_prompt
from .paths import default_transcripts_dir as _default_transcripts_dir
from .session_ids import validate_session_id

TranscriptCallback = Callable[[str, list[dict]], Awaitable[None]]


class TranscriptWatcher:
    def __init__(
        self,
        transcripts_dir: str | None = None,
    ):
        self.transcripts_dir = transcripts_dir or _default_transcripts_dir()
        self._mtimes: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # -- one-shot reads ------------------------------------------------------

    def get_transcript(self, session_id: str) -> dict | None:
        path = self._path(session_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_reasoning_steps(self, session_id: str) -> list[dict]:
        transcript = self.get_transcript(session_id)
        if not transcript:
            return []
        steps = []
        for step in transcript.get("steps", []):
            if not isinstance(step, dict):
                continue
            reasoning = step.get("reasoning_content")
            if not isinstance(reasoning, str) or not reasoning:
                continue

            raw_step_id = step.get("step_id")
            if isinstance(raw_step_id, str) and raw_step_id.isdigit():
                step_id: int | None = int(raw_step_id)
            elif isinstance(raw_step_id, (int, float)) and not isinstance(raw_step_id, bool):
                step_id = int(raw_step_id)
            else:
                step_id = None

            raw_metrics = step.get("metrics")
            metrics: dict[str, int | float] = {}
            if isinstance(raw_metrics, dict):
                for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                    value = raw_metrics.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        metrics[key] = value

            tool_calls = []
            raw_calls = step.get("tool_calls")
            if isinstance(raw_calls, list):
                for call in raw_calls:
                    if not isinstance(call, dict):
                        continue
                    arguments = call.get("arguments")
                    if arguments is not None and not isinstance(arguments, dict):
                        arguments = {"value": arguments}
                    call_id = call.get("tool_call_id")
                    tool_calls.append(
                        {
                            "tool_call_id": str(call_id) if call_id is not None else None,
                            "function_name": str(call.get("function_name") or "Tool call"),
                            "arguments": arguments,
                        }
                    )

            timestamp = step.get("timestamp")
            message = step.get("message")
            steps.append(
                {
                    "step_id": step_id,
                    "reasoning_content": reasoning,
                    "timestamp": str(timestamp) if timestamp is not None else None,
                    "tool_calls": tool_calls,
                    "message": str(message) if message is not None else "",
                    "metrics": metrics,
                }
            )
        return steps

    def get_usage_records(self, session_id: str) -> list[dict]:
        """Return normalized model-inference usage records from an ATIF transcript."""
        transcript = self.get_transcript(session_id)
        if not transcript:
            return []
        agent = transcript.get("agent") if isinstance(transcript.get("agent"), dict) else {}
        records: list[dict] = []
        turn_index = 0
        turn_timestamp = None
        for index, step in enumerate(transcript.get("steps", [])):
            if not isinstance(step, dict):
                continue
            if step.get("source") == "user":
                # Internal summary prompts keep the original turn's pricing snapshot.
                if not is_compaction_prompt(step.get("message")):
                    turn_index += 1
                    turn_timestamp = step.get("timestamp")
                continue
            if step.get("source") != "agent":
                continue
            metrics = step.get("metrics")
            if not isinstance(metrics, dict):
                continue
            if not any(key in metrics for key in ("prompt_tokens", "cached_tokens", "completion_tokens")):
                continue
            extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
            telemetry = extra.get("telemetry") if isinstance(extra.get("telemetry"), dict) else {}
            if telemetry and telemetry.get("operation") != "inference":
                continue
            prompt_tokens = metrics.get("prompt_tokens")
            cached_tokens = metrics.get("cached_tokens")
            if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, (int, float)):
                prompt_tokens = 0
            if isinstance(cached_tokens, bool) or not isinstance(cached_tokens, (int, float)):
                cached_tokens = 0
            records.append(
                {
                    "id": step.get("step_id") if step.get("step_id") is not None else f"step-{index}",
                    "timestamp": step.get("timestamp"),
                    "turn_index": max(1, turn_index),
                    "turn_timestamp": turn_timestamp,
                    "model_id": extra.get("generation_model") or step.get("model_id") or agent.get("model_id"),
                    "input_tokens": max(0, int(prompt_tokens) - int(cached_tokens)),
                    "cached_input_tokens": cached_tokens,
                    "output_tokens": metrics.get("completion_tokens"),
                    "context_tokens": max(0, int(prompt_tokens)),
                }
            )
        return records

    def get_final_metrics(self, session_id: str) -> dict | None:
        transcript = self.get_transcript(session_id)
        if not transcript:
            return None
        return transcript.get("final_metrics")

    # -- live watching -------------------------------------------------------

    def _path(self, session_id: str) -> str:
        sid = validate_session_id(session_id)
        root = Path(self.transcripts_dir).resolve()
        candidate = (root / f"{sid}.json").resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise ValueError("Transcript path escapes transcripts directory")
        return str(candidate)

    async def watch(
        self,
        session_id: str,
        callback: TranscriptCallback,
        interval: float = 1.0,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    path = self._path(session_id)
                    if not os.path.exists(path):
                        continue
                    mtime = os.path.getmtime(path)
                    if mtime != self._mtimes.get(path):
                        self._mtimes[path] = mtime
                        steps = self.get_reasoning_steps(session_id)
                        await callback(session_id, steps)
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    def start_watching(
        self,
        session_id: str,
        callback: TranscriptCallback,
        interval: float = 1.0,
    ) -> None:
        self._path(session_id)
        if session_id in self._tasks and not self._tasks[session_id].done():
            return
        self._tasks[session_id] = asyncio.create_task(
            self.watch(session_id, callback, interval)
        )

    def stop_watching(self, session_id: str) -> None:
        task = self._tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    def stop_all(self) -> None:
        for session_id in list(self._tasks):
            self.stop_watching(session_id)
