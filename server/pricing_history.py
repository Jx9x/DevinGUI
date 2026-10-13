from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any


def _model_key(model_id: str, model_registry: Any) -> str:
    model = model_registry.get_model(model_id)
    resolved = model.get("id", model_id) if model else model_id
    return str(resolved).lower().replace(".", "-").replace("-priority", "-fast")


def inference_pricing_snapshot(
    snapshot: dict[str, Any], model_id: str, model_registry: Any,
) -> dict[str, Any] | None:
    """Return only prices belonging to this inference, including legacy Fusion leads."""
    target = _model_key(model_id, model_registry)
    model_prices = snapshot.get("model_prices")
    if isinstance(model_prices, dict):
        for priced_id, prices in model_prices.items():
            if isinstance(prices, dict) and _model_key(priced_id, model_registry) == target:
                return {**prices, "model_id": model_id}
    snapshot_id = snapshot.get("model_id")
    if not isinstance(snapshot_id, str):
        return None
    key = _model_key(snapshot_id, model_registry)
    if key == target:
        return {**snapshot, "model_id": model_id}
    if key.startswith("fusion-") and "-sidekick-" in key:
        lead_id = key[len("fusion-"):].split("-sidekick-", 1)[0]
        if _model_key(lead_id, model_registry) == target:
            return {**snapshot, "model_id": model_id}
    return None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def usage_pricing_snapshot(
    record: dict[str, Any], snapshots: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Use original turn time, rather than branch/replay order, to locate pricing.

    Choose the latest dated snapshot at or before that time.
    If record time or dated history is unavailable, use the one-based turn index.
    """
    timestamp = _timestamp(record.get("turn_timestamp"))
    if timestamp is None:
        timestamp = _timestamp(record.get("timestamp"))
    dated = [
        (time, index, snapshot) for index, snapshot in enumerate(snapshots)
        if isinstance(snapshot, dict)
        and (time := _timestamp(snapshot.get("timestamp"))) is not None
    ]
    if timestamp is not None and dated:
        eligible = [entry for entry in dated if entry[0] <= timestamp]
        return max(eligible, key=lambda entry: (entry[0], entry[1]))[2] if eligible else None
    turn_index = int(record.get("turn_index") or 0)
    snapshot = snapshots[turn_index - 1] if 0 < turn_index <= len(snapshots) else None
    return snapshot if isinstance(snapshot, dict) else None


def _prices(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_id": model.get("id"),
        "was_free": "Free" in model.get("tags", []),
        **{key: model.get(key) for key in ("input_price", "cached_input_price", "output_price")},
    }


class PricingHistory:
    """Preserve prompt-time catalog rates across later price or promotion changes."""

    def __init__(self, registry_dir: str):
        self.path = os.path.join(registry_dir, "pricing_history.json")

    def record_prompt(self, session_id: str, model: dict[str, Any] | None) -> None:
        if not model:
            return
        data = self._load()
        entries = data.setdefault(session_id, [])
        entries.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model_id": model.get("id"),
                "display_name": model.get("display_name") or model.get("id"),
                "was_free": "Free" in model.get("tags", []),
                "input_price": model.get("input_price"),
                "cached_input_price": model.get("cached_input_price"),
                "output_price": model.get("output_price"),
                "model_prices": {
                    mid: _prices(info) for mid, info in model.get("inference_models", {}).items()
                    if isinstance(info, dict)
                },
            }
        )
        self._save(data)

    def snapshots(self, session_id: str) -> list[dict[str, Any]]:
        entries = self._load().get(session_id, [])
        return entries if isinstance(entries, list) else []

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = f"{self.path}.tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
        os.replace(temporary, self.path)
