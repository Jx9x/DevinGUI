from server.cost_estimator import CostEstimator, db_usage_records, full_prompt_tokens

import pytest


class Registry:
    def __init__(self):
        self.models = {
            "model-a": {
                "id": "model-a",
                "display_name": "Model A",
                "tags": [],
                "input_price": 2.0,
                "cached_input_price": 0.5,
                "output_price": 10.0,
            },
            "free": {
                "id": "free",
                "display_name": "GLM-5.2 Free",
                "tags": ["Free"],
                "input_price": None,
                "cached_input_price": None,
                "output_price": None,
            },
            "swe-2-max": {
                "id": "swe-2-max",
                "display_name": "SWE-2 Max",
                "tags": ["Free"],
                "input_price": None,
                "cached_input_price": None,
                "output_price": None,
            },
        }

    def get_model(self, model_id):
        return self.models.get(model_id)


def test_estimate_prices_input_cached_output_and_cache_creation_once():
    estimate = CostEstimator(Registry()).estimate(
        [
            {
                "id": "one",
                "model_id": "model-a",
                "input_tokens": 1_000_000,
                "cache_creation_tokens": 100_000,
                "cached_input_tokens": 2_000_000,
                "output_tokens": 500_000,
            },
            {"id": "one", "model_id": "model-a", "input_tokens": 99_999_999},
        ],
        source="sqlite",
    )

    assert estimate["amount"] == 8.2
    assert estimate["input_tokens"] == 1_100_000
    assert estimate["cached_input_tokens"] == 2_000_000
    assert estimate["output_tokens"] == 500_000
    assert estimate["complete"] is True
    assert estimate["reasons"] == ["cache_write_input_rate_fallback"]


def test_estimate_marks_unknown_prices_partial_and_explicit_free_zero():
    estimate = CostEstimator(Registry()).estimate(
        [
            {"id": "free", "model_id": "free", "input_tokens": 100, "output_tokens": 20},
            {"id": "unknown", "model_id": "missing", "input_tokens": 100, "output_tokens": 20},
        ]
    )

    assert estimate["amount"] == 0
    assert estimate["priced_steps"] == 1
    assert estimate["unpriced_steps"] == 1
    assert estimate["complete"] is False
    assert estimate["reasons"] == ["hypothetical_price_fallback", "unknown_model"]


def test_estimate_aggregates_per_model_tokens_and_cost_breakdown():
    estimate = CostEstimator(Registry()).estimate(
        [
            {
                "id": "a1",
                "model_id": "model-a",
                "input_tokens": 1_000_000,
                "cache_creation_tokens": 100_000,
                "cached_input_tokens": 2_000_000,
                "output_tokens": 500_000,
            },
            {
                "id": "a2",
                "model_id": "model-a",
                "input_tokens": 500_000,
                "cached_input_tokens": 1_000_000,
                "output_tokens": 250_000,
            },
            {
                "id": "f1",
                "model_id": "free",
                "input_tokens": 100_000,
                "cached_input_tokens": 50_000,
                "output_tokens": 10_000,
            },
        ]
    )

    by_id = {m["model_id"]: m for m in estimate["models"]}

    # Paid model: tokens summed across both records; cache_creation tracked
    # separately and folded into input_tokens (1.6M = 1.5M + 100k cache write).
    paid = by_id["model-a"]
    assert paid["input_tokens"] == 1_600_000
    assert paid["cached_input_tokens"] == 3_000_000
    assert paid["output_tokens"] == 750_000
    assert paid["cache_creation_tokens"] == 100_000
    # Per-category charged amounts: 1.6M*$2 + 3M*$0.5 + 750k*$10 = 12.2
    assert paid["input_amount"] == 3.2
    assert paid["cached_input_amount"] == 1.5
    assert paid["output_amount"] == 7.5
    assert paid["amount"] == 12.2
    # Hypothetical equals charged for a paid model.
    assert paid["hypothetical_amount"] == 12.2
    assert paid["hypothetical_input_amount"] == 3.2
    assert paid["hypothetical_cached_input_amount"] == 1.5
    assert paid["hypothetical_output_amount"] == 7.5
    assert paid["was_free"] is False

    # Free model: charged amounts are zero, hypothetical uses fallback prices.
    free = by_id["free"]
    assert free["input_tokens"] == 100_000
    assert free["cached_input_tokens"] == 50_000
    assert free["output_tokens"] == 10_000
    assert free["amount"] == 0.0
    assert free["input_amount"] == 0.0
    assert free["cached_input_amount"] == 0.0
    assert free["output_amount"] == 0.0
    # GLM-5.2 fallback prices (1.40, 0.26, 4.40) per million.
    assert free["hypothetical_amount"] == pytest.approx(0.197)
    assert free["was_free"] is True

    # Conversation totals equal the sum of per-model amounts.
    assert estimate["amount"] == 12.2
    assert estimate["hypothetical_amount"] == pytest.approx(12.397)


def test_estimate_prices_swe2_free_promo_with_hypothetical_fallback():
    estimate = CostEstimator(Registry()).estimate(
        [
            {
                "id": "s1",
                "model_id": "swe-2-max",
                "input_tokens": 100_000,
                "cached_input_tokens": 50_000,
                "output_tokens": 10_000,
            },
        ]
    )

    # Charged at $0 like every free-on-plan model, but priced via the SWE
    # fallback (0.95, 0.19, 4.00) so the step isn't reported as unpriced.
    assert estimate["amount"] == 0.0
    assert estimate["hypothetical_amount"] == pytest.approx(0.1445)
    assert estimate["unpriced_steps"] == 0
    assert estimate["complete"] is True
    assert estimate["models"][0]["was_free"] is True


def test_db_usage_records_only_include_assistant_inferences():
    records = db_usage_records(
        [
            {"role": "user", "metrics": {"input_tokens": 99}},
            {
                "row_id": 3,
                "role": "assistant",
                "metrics": {
                    "input_tokens": 12,
                    "cache_creation_tokens": 3,
                    "cache_read_tokens": 25,
                    "output_tokens": 7,
                },
            },
        ]
    )

    assert records == [
        {
            "id": 3,
            "timestamp": None,
            "turn_index": 1,
            "model_id": None,
            "input_tokens": 12,
            "cache_creation_tokens": 3,
            "cached_input_tokens": 25,
            "output_tokens": 7,
            "context_tokens": 40,
        }
    ]


def test_full_prompt_tokens_treats_missing_cache_writes_as_zero():
    metrics = {
        "input_tokens": 2,
        "cache_read_tokens": 168089,
        "cache_creation_tokens": None,
    }

    assert full_prompt_tokens(metrics) == 168091


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_free_paid_and_unpriced_usage_is_complete_and_order_independent(reverse):
    records = [
        {"id": "paid", "model_id": "model-a", "input_tokens": 1_000_000},
        {"id": "free", "model_id": "model-a", "input_tokens": 2_000_000,
         "pricing_snapshot": {"model_id": "model-a", "was_free": True}},
        {"id": "unknown", "model_id": "model-a", "input_tokens": 3_000_000,
         "pricing_snapshot": {"model_id": "model-a", "input_price": None}},
    ]
    estimate = CostEstimator(Registry()).estimate(reversed(records) if reverse else records)
    model = estimate["models"][0]

    assert model["input_tokens"] == 6_000_000
    assert model["amount"] == 2.0
    assert model["free_steps"] == model["paid_steps"] == model["unpriced_steps"] == 1
    assert model["was_free"] is False
    assert estimate["complete"] is False
    assert estimate["priced_steps"] == 2
    assert estimate["unpriced_steps"] == 1


def test_unpriced_model_tokens_are_visible_in_model_totals():
    estimate = CostEstimator(Registry()).estimate([
        {"model_id": "compactor", "input_tokens": 50_000, "output_tokens": 1000},
    ])
    assert estimate["models"][0]["model_id"] == "compactor"
    assert estimate["models"][0]["input_tokens"] == 50_000
    assert estimate["models"][0]["unpriced_steps"] == 1
    assert estimate["models"][0]["was_free"] is False


def test_free_catalog_with_explicit_zero_rates_has_hypothetical_estimate():
    registry = Registry()
    registry.models["swe-2-max"].update(input_price=0.0, cached_input_price=0.0, output_price=0.0)
    estimate = CostEstimator(registry).estimate([
        {"model_id": "swe-2-max", "input_tokens": 1_000_000},
    ])
    assert estimate["amount"] == 0
    assert estimate["hypothetical_amount"] == 0.95
    assert estimate["hypothetical_estimated"] is True
    assert estimate["free_steps"] == 1


def test_historical_paid_rates_override_current_free_promotion():
    estimate = CostEstimator(Registry()).estimate([
        {"model_id": "swe-2-max", "input_tokens": 1_000_000,
         "pricing_snapshot": {"model_id": "swe-2-max", "was_free": False,
                              "input_price": 3, "cached_input_price": 1, "output_price": 5}},
    ])
    assert estimate["amount"] == 3
    assert estimate["models"][0]["was_free"] is False


def test_saved_historical_prices_remain_usable_for_a_retired_model():
    estimate = CostEstimator(Registry()).estimate([
        {"model_id": "retired", "input_tokens": 1_000_000,
         "pricing_snapshot": {"model_id": "retired", "was_free": False,
                              "input_price": 2, "cached_input_price": 0.1, "output_price": 10}},
    ])
    assert estimate["amount"] == 2
    assert estimate["complete"] is True


def test_estimator_rejects_snapshot_from_a_different_inference_model():
    estimate = CostEstimator(Registry()).estimate([
        {"model_id": "swe-2-max", "input_tokens": 1_000_000,
         "pricing_snapshot": {"model_id": "fusion-model-a-sidekick-swe-2-max", "was_free": False,
                              "input_price": 100, "cached_input_price": 50, "output_price": 200}},
    ])
    assert estimate["amount"] == 0
    assert estimate["hypothetical_amount"] == 0.95


@pytest.mark.parametrize("price", [True, -1, float("inf"), float("nan")])
def test_invalid_model_prices_are_unpriced(price):
    registry = Registry()
    registry.models["model-a"]["input_price"] = price
    estimate = CostEstimator(registry).estimate([{"model_id": "model-a", "input_tokens": 100}])
    assert estimate["unpriced_steps"] == 1
    assert estimate["amount"] == 0
