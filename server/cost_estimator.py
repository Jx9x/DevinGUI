from __future__ import annotations

import math
from typing import Any, Iterable

from .pricing_history import inference_pricing_snapshot


# Hypothetical USD per million tokens: input, cached input, output.
HYPOTHETICAL_FREE_PRICES = {
    "glm-5.2": (1.40, 0.26, 4.40),
    "swe-1.6": (0.95, 0.19, 4.00),
    "swe-1.7": (0.95, 0.19, 4.00),
    "swe-2": (0.95, 0.19, 4.00),
    "kimi-k2.7": (0.95, 0.19, 4.00),
}


class CostEstimator:
    def __init__(self, model_registry: Any):
        self.model_registry = model_registry

    def estimate(
        self,
        usage_records: Iterable[dict[str, Any]],
        fallback_model_id: str | None = None,
        source: str = "atif",
    ) -> dict[str, Any]:
        """Return deduplicated USD estimates with model and step breakdowns.

        Historical snapshots take precedence over current catalog rates.
        Unpriced steps contribute tokens but no amount and make totals incomplete.
        Free steps cost zero; hypothetical costs use fallback rates when available.
        """
        fields = ("input_tokens", "cached_input_tokens", "output_tokens")
        amount_fields = ("input_amount", "cached_input_amount", "output_amount")
        totals = dict.fromkeys(fields, 0)
        model_totals: dict[str, dict[str, Any]] = {}
        reasons: set[str] = set()
        seen: set[str] = set()
        step_estimates: list[dict[str, Any]] = []

        for index, record in enumerate(usage_records):
            key = str(record.get("id") or f"{record.get('timestamp') or ''}:{index}")
            if key in seen:
                continue
            seen.add(key)
            turn_index = self._tokens(record.get("turn_index", 1))
            cache_creation_tokens = self._tokens(record.get("cache_creation_tokens"))
            # Without a separate cache-write rate, writes use the ordinary input rate.
            tokens = {
                "input_tokens": self._tokens(record.get("input_tokens")) + cache_creation_tokens,
                "cached_input_tokens": self._tokens(record.get("cached_input_tokens")),
                "output_tokens": self._tokens(record.get("output_tokens")),
            }
            for field in fields:
                totals[field] += tokens[field]

            raw_snapshot = record.get("pricing_snapshot")
            snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
            model_id = record.get("model_id") or fallback_model_id or snapshot.get("model_id")
            model = self.model_registry.get_model(model_id) if model_id else None
            resolved_id = model["id"] if model else str(model_id or "unknown")
            entry = model_totals.setdefault(resolved_id, self._model_usage(resolved_id, model))
            step_model = self._model_usage(resolved_id, model)
            step_model.update(tokens, cache_creation_tokens=cache_creation_tokens)
            if snapshot.get("model_id"):
                snapshot = inference_pricing_snapshot(snapshot, model_id, self.model_registry) or {}
            prices = tuple(snapshot.get(field, (model or {}).get(field)) for field in (
                "input_price", "cached_input_price", "output_price",
            ))
            # An explicit historical paid status overrides today's free promotion.
            is_free = snapshot.get("was_free") is True or (
                "was_free" not in snapshot and model is not None
                and "Free" in model.get("tags", [])
                and all(price is None or price == 0 for price in prices)
            )
            hypothetical_prices = self._hypothetical_prices(model_id, model or {}) if is_free else None
            approximate = hypothetical_prices is not None
            if hypothetical_prices is not None:
                prices = hypothetical_prices
            priced = all(self._valid_price(price) for price in prices)
            step_reasons = []
            if not model and not is_free and not (snapshot and priced):
                step_reasons.append("unknown_model")
            elif not priced and not is_free:
                step_reasons.append("missing_pricing")
            if cache_creation_tokens:
                step_reasons.append("cache_write_input_rate_fallback")
            if approximate:
                step_reasons.append("hypothetical_price_fallback")
            unknown = bool(set(step_reasons) & {"unknown_model", "missing_pricing"})
            step_model["unpriced_steps"] = int(unknown)
            step_model["free_steps"] = int(is_free and not unknown)
            step_model["paid_steps"] = int(not is_free and not unknown)
            step_model["was_free"] = is_free and not unknown
            step_model["hypothetical_estimated"] = approximate
            for token_field, amount_field, price in zip(fields, amount_fields, prices):
                hypothetical = tokens[token_field] * float(price) / 1_000_000 if priced and not unknown else 0.0
                charged = hypothetical if not is_free else 0.0
                step_model[amount_field] = charged
                step_model[f"hypothetical_{amount_field}"] = hypothetical
                step_model["amount"] += charged
                step_model["hypothetical_amount"] += hypothetical
            for field in (*fields, "cache_creation_tokens", "amount", *amount_fields,
                          "hypothetical_amount", *(f"hypothetical_{field}" for field in amount_fields),
                          "paid_steps", "free_steps", "unpriced_steps"):
                entry[field] += step_model[field]
            entry["was_free"] = entry["free_steps"] > 0 and entry["paid_steps"] == entry["unpriced_steps"] == 0
            entry["hypothetical_estimated"] |= approximate
            reasons.update(step_reasons)
            step_estimates.append({
                "id": key, "turn_index": turn_index,
                **{field: step_model[field] for field in (
                    *fields, "amount", *amount_fields, "hypothetical_amount",
                    *(f"hypothetical_{field}" for field in amount_fields),
                    "paid_steps", "free_steps", "unpriced_steps", "hypothetical_estimated",
                )},
                "currency": "USD", "models": [step_model],
                "priced_steps": int(not unknown), "complete": not unknown,
                "estimated": True, "source": source, "reasons": step_reasons,
            })

        models = list(model_totals.values())
        unpriced_steps = sum(model["unpriced_steps"] for model in models)
        return {
            **totals,
            **{field: sum(model[field] for model in models) for field in (
                "amount", *amount_fields, "hypothetical_amount",
                *(f"hypothetical_{field}" for field in amount_fields),
                "paid_steps", "free_steps",
            )},
            "currency": "USD", "models": models,
            "priced_steps": len(seen) - unpriced_steps, "unpriced_steps": unpriced_steps,
            "complete": bool(seen) and unpriced_steps == 0, "estimated": True,
            "hypothetical_estimated": any(model["hypothetical_estimated"] for model in models),
            "source": source, "reasons": sorted(reasons), "steps": step_estimates,
        }

    @staticmethod
    def _valid_price(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0

    @staticmethod
    def _model_usage(model_id: str, model: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "model_id": model_id,
            "display_name": (model or {}).get("display_name", model_id),
            **dict.fromkeys(("input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens",
                             "paid_steps", "free_steps", "unpriced_steps"), 0),
            **dict.fromkeys(("amount", "input_amount", "cached_input_amount", "output_amount",
                             "hypothetical_amount", "hypothetical_input_amount", "hypothetical_cached_input_amount",
                             "hypothetical_output_amount"), 0.0),
            "was_free": False, "hypothetical_estimated": False,
        }

    def estimate_turns(
        self,
        usage_records: Iterable[dict[str, Any]],
        fallback_model_id: str | None = None,
        source: str = "atif",
    ) -> list[dict[str, Any]]:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for record in usage_records:
            turn_index = self._tokens(record.get("turn_index"))
            grouped.setdefault(turn_index, []).append(record)
        return [
            {"turn_index": turn_index, **self.estimate(records, fallback_model_id, source)}
            for turn_index, records in sorted(grouped.items())
        ]

    @staticmethod
    def _hypothetical_prices(model_id: str | None, model: dict[str, Any]) -> tuple[float, float, float] | None:
        candidates = [
            model_id or "",
            str(model.get("id") or ""),
            str(model.get("display_name") or ""),
        ]
        for candidate in candidates:
            normalized = candidate.lower().replace("-", ".")
            for family, prices in HYPOTHETICAL_FREE_PRICES.items():
                if family.replace("-", ".") in normalized:
                    return prices
        return None

    @staticmethod
    def _tokens(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 0
        return max(0, int(value))


def full_prompt_tokens(metrics: dict[str, Any]) -> int:
    return sum(CostEstimator._tokens(metrics.get(key)) for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens"))


def db_usage_records(
    messages: Iterable[dict[str, Any]], *, turns: dict[Any, tuple[int, Any]] | None = None,
) -> list[dict[str, Any]]:
    records = []
    turn_index = 0
    for message in messages:
        if turns is None and message.get("role") == "user":
            turn_index += 1
        if message.get("role") != "assistant" or not isinstance(message.get("metrics"), dict):
            continue
        metrics = message["metrics"]
        if not any(key in metrics for key in ("input_tokens", "cache_read_tokens", "output_tokens")):
            continue
        turn, timestamp = (
            turns.get(message.get("node_id"), (0, None))
            if turns is not None else (max(1, turn_index), None)
        )
        record = {
            "id": message.get("message_id") or message.get("row_id"),
            "timestamp": message.get("created_at"),
            "turn_index": turn,
            "model_id": message.get("model_id"),
            "input_tokens": metrics.get("input_tokens"),
            "cache_creation_tokens": metrics.get("cache_creation_tokens"),
            "cached_input_tokens": metrics.get("cache_read_tokens"),
            "output_tokens": metrics.get("output_tokens"),
            "context_tokens": full_prompt_tokens(metrics),
        }
        if turns is not None:
            record["turn_timestamp"] = timestamp
        records.append(record)
    return records
