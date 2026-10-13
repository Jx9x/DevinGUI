"""Session costs and reasoning assembly shared by REST and live updates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, TYPE_CHECKING

from .cost_estimator import CostEstimator, db_usage_records, full_prompt_tokens
from .pricing_history import inference_pricing_snapshot, usage_pricing_snapshot

if TYPE_CHECKING:
    from .db_reader import DBReader
    from .model_registry import ModelRegistry
    from .pricing_history import PricingHistory
    from .transcript_watcher import TranscriptWatcher


def session_cost(
    session_id: str,
    *,
    db_reader: DBReader,
    transcript_watcher: TranscriptWatcher,
    model_registry: ModelRegistry,
    pricing_history: PricingHistory,
) -> dict[str, Any]:
    """Assemble historical pricing, per-turn estimates, and latest context usage."""
    session = db_reader.get_session(session_id) or {}
    model_registry.refresh()
    estimator = CostEstimator(model_registry)
    records = transcript_watcher.get_usage_records(session_id)
    source = "atif"
    if not records:
        records = db_usage_records(
            db_reader.get_messages(session_id), turns=db_reader.get_usage_turns(session_id),
        )
        source = "sqlite"
    snapshots = pricing_history.snapshots(session_id)
    records = [dict(record) for record in records]
    for record in records:
        record.pop("pricing_snapshot", None)
        snapshot = usage_pricing_snapshot(record, snapshots)
        if snapshot is not None:
            model_id = record.get("model_id") or snapshot.get("model_id") or session.get("model")
            record["model_id"] = model_id
            prices = inference_pricing_snapshot(snapshot, model_id, model_registry) if model_id else None
            if prices is not None:
                record["pricing_snapshot"] = prices
    estimate = estimator.estimate(records, session.get("model"), source=source)
    estimate["turns"] = estimator.estimate_turns(records, session.get("model"), source=source)
    # Context usage is one inference's full prompt, not a conversation total.
    # Transcript prompt_tokens include cached tokens; SQLite stores cache
    # reads/writes separately. Prefer the newest non-compactor main-chain
    # inference so branch usage does not replace main-agent context.
    context: dict[str, int | None] = {"used": None, "size": None}
    latest = records[-1] if records else None
    if source == "sqlite":
        inference = db_reader.get_latest_main_chain_inference(session_id)
        if inference:
            latest = {"model_id": inference["model_id"], "context_tokens": full_prompt_tokens(inference["metrics"])}
    if latest:
        used = int(latest.get("context_tokens") or 0)
        model_id = latest.get("model_id") or session.get("model")
        model_info = model_registry.get_model(model_id) if model_id else None
        size = int((model_info or {}).get("context_window") or 0) or None
        if used > 0:
            context = {"used": used, "size": size}
    estimate["context"] = context
    return estimate


def _reasoning_sort_key(step: dict[str, Any]) -> tuple[bool, str]:
    timestamp = step.get("timestamp")
    sort_value = str(timestamp or "")
    if isinstance(timestamp, str) and timestamp:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            sort_value = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
        except (ValueError, OverflowError):
            pass
    return (timestamp is None, sort_value)


def merge_reasoning_steps(steps: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge transcript reasoning with reasoning persisted in sessions.db.

    Keep the transcript's richer tool-call/final-message metadata while
    enriching matching blocks with the database's inference timing and usage.
    Pass transcript steps before database steps to retain that priority.
    """
    combined: list[dict] = []
    seen: dict[tuple[str, str | None], dict] = {}
    for step in steps:
        content = str(step.get("reasoning_content") or "")
        normalized = " ".join(content.split())
        if not normalized:
            continue
        # Identical short thoughts can recur in separate generations.
        timestamp = step.get("timestamp")
        if timestamp:
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                # Match SQLite and transcript timestamps at UTC millisecond precision.
                timestamp = parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")
            except (ValueError, OverflowError):
                pass
        key = (normalized, timestamp)
        if key in seen:
            existing = seen[key]
            # Repeated text can also occur on another branch. Once a current
            # main-turn sample is matched, keep its usage/timing together.
            if not existing.get("is_throughput_sample") or step.get("is_throughput_sample"):
                existing["metrics"] = {
                    **existing.get("metrics", {}), **step.get("metrics", {})
                }
            if step.get("is_throughput_sample"):
                existing["is_throughput_sample"] = True
            if not existing.get("message") and step.get("message"):
                existing["message"] = step["message"]
        else:
            entry = {**step, "metrics": dict(step.get("metrics", {}))}
            seen[key] = entry
            combined.append(entry)

    combined.sort(key=_reasoning_sort_key)
    return combined
