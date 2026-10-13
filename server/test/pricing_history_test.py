from types import SimpleNamespace

import pytest

from server.pricing_history import PricingHistory, inference_pricing_snapshot, usage_pricing_snapshot


def registry():
    return SimpleNamespace(get_model=lambda mid: {"id": mid.replace(".", "-")})


@pytest.mark.parametrize("inference,expected", [
    ("gpt-6.1-sol-high-priority", True), ("swe-2-high", False), ("gpt-6-1-sol-max", False),
])
def test_legacy_fusion_snapshot_only_applies_to_the_exact_lead(inference, expected):
    snapshot = {"model_id": "fusion-gpt-6-1-sol-high-fast-sidekick-swe-2-high", "input_price": 4}
    prices = inference_pricing_snapshot(snapshot, inference, registry())
    assert (prices is not None) is expected
    if expected:
        assert prices["model_id"] == inference
        assert prices["input_price"] == 4


def test_record_prompt_saves_separate_sidekick_prices(tmp_path):
    history = PricingHistory(str(tmp_path))
    lead = {"id": "lead", "input_price": 2, "cached_input_price": 0.1, "output_price": 10}
    sidekick = {"id": "swe-2-high", "tags": ["Free"]}
    history.record_prompt("s", {
        "id": "fusion-lead-sidekick-swe-2-high", **{k:v for k,v in lead.items() if k != "id"},
        "inference_models": {"lead": lead, "swe-2-high": sidekick},
    })
    snapshot = history.snapshots("s")[0]
    assert snapshot["model_id"] == "fusion-lead-sidekick-swe-2-high"
    assert inference_pricing_snapshot(snapshot, "lead", registry())["input_price"] == 2
    prices = inference_pricing_snapshot(snapshot, "swe-2-high", registry())
    assert prices["was_free"] is True
    assert prices["input_price"] is None


def test_original_turn_timestamp_wins_over_later_replay_timestamp():
    snapshots = [
        {"timestamp": "2026-10-09T12:00:00Z", "input_price": 2},
        {"timestamp": "2026-10-09T13:00:00Z", "input_price": 4},
    ]
    record = {"turn_index": 49, "turn_timestamp": "2026-10-09T12:00:00.010Z",
              "timestamp": "2026-10-09T14:00:00Z"}
    assert usage_pricing_snapshot(record, snapshots) is snapshots[0]
    assert usage_pricing_snapshot({"timestamp": "2026-10-09T13:10:00Z"}, snapshots) is snapshots[1]
    assert usage_pricing_snapshot({"turn_index": 1, "timestamp": "2026-10-09T11:59:59Z"}, snapshots) is None


def test_legacy_undated_snapshots_use_main_turn_index():
    snapshots = [{"model_id": "a"}, {"model_id": "b"}]
    assert usage_pricing_snapshot({"turn_index": 2}, snapshots) is snapshots[1]
    assert usage_pricing_snapshot({"turn_index": 0}, snapshots) is None
