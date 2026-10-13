"""Stored message conversion and browser history projection without database I/O.

Queries own row selection and ordering; projection consumes that order and the
supplied main-turn identities without fetching or sorting rows.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol

from .image_content import image_mime_type


class StoredMessageRow(Protocol):
    """The key access shared by SQLite rows and in-memory dictionaries."""

    def __getitem__(self, key: str) -> Any: ...


class MessageDecoder(Protocol):
    """Conversion hooks retained by DBReader and resolved for each row."""

    def _safe_json(self, raw: Any) -> Any: ...
    def _ts_to_iso(self, raw: Any) -> str | None: ...
    def _content_to_text(self, content: Any) -> str | None: ...
    def _thinking_to_text(self, value: Any) -> str | None: ...
    def _normalize_metrics(self, value: Any) -> dict[str, int | float | None] | None: ...
    def _image_metadata(self, message: Any) -> list[dict]: ...


def decode_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def timestamp_to_iso(raw: Any) -> str | None:
    """Convert SQLite timestamps to ISO-8601 without discarding ISO strings."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            numeric = float(stripped)
        except ValueError:
            # Devin versions have used RFC3339 strings with nanosecond
            # precision. Browsers can parse these directly, so preserve
            # the original value instead of returning null.
            return stripped
        raw = numeric
    try:
        value = float(raw)
        if not math.isfinite(value):
            return None
        # Accommodate milliseconds/microseconds/nanoseconds as well as
        # the epoch-seconds values used by current Devin builds.
        while abs(value) > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def content_to_text(
    content: Any,
    *,
    nested_text: Callable[[Any], str | None] | None = None,
) -> str | None:
    """Normalize string or structured message content for the browser."""
    convert_nested = content_to_text if nested_text is None else nested_text
    if content is None:
        return None
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = convert_nested(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        for key in ("text", "content", "output", "message"):
            if key in content:
                text = convert_nested(content[key])
                if text is not None:
                    return text
        try:
            return json.dumps(content, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


def is_compaction_prompt(content: Any) -> bool:
    """Recognize internal summary prompts that do not start a user turn."""
    text = (content_to_text(content) or "").strip().lower()
    return text.startswith((
        "conversation to summarize:",
        "now summarize the conversation above as per the format given.",
    )) or (text.startswith("<summary>") and text.endswith("</summary>"))


def thinking_to_text(
    value: Any,
    *,
    convert_text: Callable[[Any], str | None] = content_to_text,
) -> str | None:
    """Normalize persisted ``thinking`` from a structured payload or legacy string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("thinking", "text", "content", "reasoning_content"):
            if key in value:
                text = convert_text(value[key])
                if text:
                    return text
        return None
    return convert_text(value)


def normalize_metrics(value: Any) -> dict[str, int | float | None] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int | float | None] = {}
    for key in (
        "ttft_ms",
        "total_time_ms",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        metric = value.get(key)
        if metric is None and key == "cache_creation_tokens" and key in value:
            result[key] = None
        elif (
            isinstance(metric, (int, float))
            and not isinstance(metric, bool)
            and math.isfinite(metric)
        ):
            result[key] = metric

    # Devin's stored rate divides by total_time_ms - ttft_ms. Providers
    # can buffer reasoning until the end, leaving a 1 ms denominator for
    # hundreds of tokens. Use the whole inference as a conservative,
    # model-independent estimate, including reasoning and tool output.
    output_tokens = result.get("output_tokens")
    total_time_ms = result.get("total_time_ms")
    if (
        output_tokens is not None and output_tokens > 0
        and total_time_ms is not None and total_time_ms > 0
    ):
        rate = output_tokens * 1000 / total_time_ms
        if math.isfinite(rate):
            result["tokens_per_sec"] = rate
    return result or None


def image_metadata(message: Any) -> list[dict]:
    images = message.get("images") if isinstance(message, dict) else None
    if not isinstance(images, list):
        return []
    result = []
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            continue
        mime = image_mime_type(image.get("mime_type"))
        if not mime or not isinstance(image.get("base64_data"), str) or not image["base64_data"]:
            continue
        entry = {"index": index, "mime_type": mime}
        for key in ("width", "height"):
            value = image.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                entry[key] = value
        result.append(entry)
    return result


def project_messages(
    rows: Iterable[StoredMessageRow], *, decoder: MessageDecoder,
) -> list[dict]:
    """Project rows in query order, retaining the reader's conversion hooks."""
    results = []
    for row in rows:
        cm = decoder._safe_json(row["chat_message"])
        chat_metadata = (cm.get("metadata") or {}) if isinstance(cm, dict) else {}
        if not isinstance(chat_metadata, dict):
            chat_metadata = {}
        created_at = chat_metadata.get("created_at") or row["created_at"]
        role = cm.get("role") if isinstance(cm, dict) else None
        message_id = cm.get("message_id") if isinstance(cm, dict) else None
        finish_reason = (
            chat_metadata.get("finish_reason")
            if isinstance(chat_metadata, dict)
            else None
        )
        model_id = None
        if isinstance(chat_metadata, dict):
            model_id = (
                chat_metadata.get("generation_model")
                or chat_metadata.get("model_id")
                or chat_metadata.get("model")
            )
        if model_id is None and isinstance(cm, dict):
            model_id = cm.get("model_id") or cm.get("model")
        node_id = row["node_id"]
        parent_node_id = row["parent_node_id"]
        results.append(
            {
                "row_id": int(row["row_id"]),
                "node_id": node_id
                if isinstance(node_id, (int, str)) or node_id is None
                else str(node_id),
                "parent_node_id": parent_node_id
                if isinstance(parent_node_id, (int, str)) or parent_node_id is None
                else str(parent_node_id),
                "created_at": decoder._ts_to_iso(created_at),
                "role": str(role) if role is not None else None,
                "content": decoder._content_to_text(cm.get("content"))
                if isinstance(cm, dict)
                else decoder._content_to_text(cm),
                "images": decoder._image_metadata(cm),
                "message_id": str(message_id) if message_id is not None else None,
                "model_id": str(model_id) if model_id is not None else None,
                "metrics": decoder._normalize_metrics(chat_metadata.get("metrics"))
                if isinstance(chat_metadata, dict)
                else None,
                "finish_reason": str(finish_reason)
                if finish_reason is not None
                else None,
                # The UI doesn't use version-dependent row metadata. Omit it so
                # malformed fields cannot make validation reject the whole history.
                "metadata": None,
            }
        )
    return results


def project_reasoning(
    rows: Iterable[StoredMessageRow],
    *,
    turn_nodes: set,
    turn_message_ids: set,
    decoder: MessageDecoder,
) -> list[dict]:
    """Project reasoning and mark the longest measured main-turn sample."""
    steps: list[dict] = []
    throughput_sample: dict | None = None
    for row in rows:
        cm = decoder._safe_json(row["chat_message"])
        if not isinstance(cm, dict):
            continue
        reasoning = decoder._thinking_to_text(cm.get("thinking"))
        if not reasoning:
            continue

        metadata = cm.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        message_metrics = decoder._normalize_metrics(metadata.get("metrics"))
        reasoning_metrics: dict[str, int | float] = {}
        if isinstance(message_metrics, dict):
            metric_map = {
                "input_tokens": "prompt_tokens",
                "output_tokens": "completion_tokens",
                "cache_read_tokens": "cached_tokens",
                "total_time_ms": "total_time_ms",
                "tokens_per_sec": "tokens_per_sec",
            }
            for source_key, target_key in metric_map.items():
                value = message_metrics.get(source_key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    reasoning_metrics[target_key] = value

        created_at = metadata.get("created_at") or row["created_at"]
        step = {
            "step_id": None,
            "reasoning_content": reasoning,
            "timestamp": decoder._ts_to_iso(created_at),
            "tool_calls": [],
            # Reasoning and output belong to the same generation.
            # Preserve that link so the timeline can place this block
            # before its answer even when created_at records the end
            # of inference, after the streamed answer first arrived.
            "message": decoder._content_to_text(cm.get("content")) or "",
            "metrics": reasoning_metrics,
        }
        steps.append(step)
        model_id = (
            metadata.get("generation_model")
            or metadata.get("model_id")
            or metadata.get("model")
            or cm.get("model_id")
            or cm.get("model")
        )
        if (
            (row["node_id"] in turn_nodes or cm.get("message_id") in turn_message_ids)
            and model_id != "compactor"
            and "tokens_per_sec" in reasoning_metrics
            and (
                throughput_sample is None
                or len(reasoning) >= len(throughput_sample["reasoning_content"])
            )
        ):
            throughput_sample = step
    if throughput_sample is not None:
        throughput_sample["is_throughput_sample"] = True
    return steps
