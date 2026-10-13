from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from server.session_history import merge_reasoning_steps, session_cost


@pytest.fixture
def sources():
    db = MagicMock()
    db.get_session.return_value = {"model": "current"}
    db.get_messages.return_value = []
    db.get_usage_turns.return_value = {}
    db.get_latest_main_chain_inference.return_value = None
    transcript = MagicMock()
    transcript.get_usage_records.return_value = []
    registry = MagicMock()
    registry.get_model.side_effect = lambda model_id: {
        "id": model_id,
        "input_price": 10,
        "cached_input_price": 3,
        "output_price": 20,
        "context_window": 1000,
        "tags": [],
    }
    pricing = MagicMock()
    pricing.snapshots.return_value = []
    return SimpleNamespace(db=db, transcript=transcript, registry=registry, pricing=pricing)


def _cost(sources):
    return session_cost(
        "s", db_reader=sources.db, transcript_watcher=sources.transcript,
        model_registry=sources.registry, pricing_history=sources.pricing,
    )


def _merge(transcript, database):
    return merge_reasoning_steps([*transcript, *database])


def _usage(**overrides):
    return {
        "id": "inference-1", "turn_index": 1, "model_id": "current",
        "input_tokens": 1_000_000, "cached_input_tokens": 500_000,
        "output_tokens": 100_000, "context_tokens": 250,
        **overrides,
    }


def test_cost_uses_each_turns_historical_prices_and_keeps_inference_model(sources):
    first = {"model_id": "historic", "input_price": 4, "cached_input_price": 2, "output_price": 8}
    second = {"model_id": "current", "input_price": 1, "cached_input_price": 0.5, "output_price": 2}
    sources.pricing.snapshots.return_value = [first, second]
    records = [_usage(model_id=None), _usage(id="inference-2", turn_index=2)]
    sources.transcript.get_usage_records.return_value = records
    original = deepcopy(records)

    estimate = _cost(sources)

    assert estimate["amount"] == pytest.approx(7.25)
    assert [turn["turn_index"] for turn in estimate["turns"]] == [1, 2]
    assert [turn["amount"] for turn in estimate["turns"]] == pytest.approx([5.8, 1.45])
    assert [model["model_id"] for model in estimate["models"]] == ["historic", "current"]
    assert records == original
    assert estimate["source"] == "atif"
    sources.db.get_messages.assert_not_called()
    sources.db.get_latest_main_chain_inference.assert_not_called()
    sources.registry.refresh.assert_called_once_with()


@pytest.mark.parametrize("turn_index,snapshot_index", [(None, None), (0, None), ("2", 1), (-1, None), (3, None)])
def test_cost_snapshot_lookup_preserves_turn_index_defaults_and_bounds(sources, turn_index, snapshot_index):
    snapshots = [{"model_id": "first"}, {"model_id": "second"}]
    sources.pricing.snapshots.return_value = snapshots
    record = _usage(model_id=None, turn_index=turn_index)
    sources.transcript.get_usage_records.return_value = [record]

    estimate = _cost(sources)

    if snapshot_index is None:
        assert "pricing_snapshot" not in record
        assert estimate["models"][0]["model_id"] == "current"
    else:
        assert "pricing_snapshot" not in record
        assert estimate["models"][0]["model_id"] == snapshots[snapshot_index]["model_id"]


@pytest.mark.parametrize("model_id", ["GLM-5.2-preview", "swe.1.7", "SWE-2-high", "kimi-k2.7-thinking", "swe-1.6"])
def test_cost_does_not_infer_free_pricing_from_model_family(sources, model_id):
    sources.db.get_session.return_value = {"model": model_id}
    sources.transcript.get_usage_records.return_value = [_usage(model_id=None)]

    estimate = _cost(sources)

    assert estimate["amount"] == pytest.approx(13.5)
    assert estimate["hypothetical_amount"] == pytest.approx(13.5)
    assert estimate["models"][0]["was_free"] is False


@pytest.mark.parametrize("snapshot", [{}, {"was_free": False}, "invalid"])
def test_cost_ignores_unidentified_historical_prices(sources, snapshot):
    sources.pricing.snapshots.return_value = [snapshot]
    record = _usage(model_id="swe-2-high")
    sources.transcript.get_usage_records.return_value = [record]

    estimate = _cost(sources)

    assert estimate["amount"] == pytest.approx(13.5)
    assert "pricing_snapshot" not in record


def test_cost_sqlite_assigns_prices_by_user_turn_without_mutating_messages(sources):
    messages = [
        {"role": "user", "node_id": 1},
        {"role": "assistant", "node_id": 2, "message_id": "a", "metrics": {"input_tokens": 1_000_000}},
        {"role": "user", "node_id": 3},
        {"role": "assistant", "node_id": 4, "message_id": "b", "model_id": "current",
         "metrics": {"input_tokens": 1_000_000, "cache_creation_tokens": 500_000}},
    ]
    original = deepcopy(messages)
    sources.db.get_messages.return_value = messages
    sources.db.get_usage_turns.return_value = {1: (1, None), 2: (1, None), 3: (2, None), 4: (2, None)}
    sources.pricing.snapshots.return_value = [
        {"model_id": "historic", "input_price": 4}, {"model_id": "current", "input_price": 1},
    ]

    estimate = _cost(sources)

    assert estimate["source"] == "sqlite"
    assert [turn["amount"] for turn in estimate["turns"]] == pytest.approx([4, 1.5])
    assert estimate["input_tokens"] == 2_500_000
    assert estimate["context"] == {"used": 1_500_000, "size": 1000}
    assert messages == original


def test_cost_fusion_uses_actual_lead_and_sidekick_prices_across_all_steps(sources):
    fusion = "fusion-gpt-6-1-sol-high-sidekick-swe-2-high"
    sources.db.get_session.return_value = {"model": fusion}
    models = {
        "gpt-6-1-sol-high": {"id": "gpt-6-1-sol-high", "input_price": 2,
                              "cached_input_price": 0.1, "output_price": 10},
        "swe-2-high": {"id": "swe-2-high", "tags": ["Free"]},
        fusion: {"id": fusion},
    }
    sources.registry.get_model.side_effect = models.get
    sources.pricing.snapshots.return_value = [{
        "model_id": fusion, "timestamp": "2026-10-09T12:00:00Z",
        "was_free": False, "input_price": 2, "cached_input_price": 0.1, "output_price": 10,
    }]
    records = [_usage(model_id="gpt-6-1-sol-high", input_tokens=808_163,
                      cached_input_tokens=10_233_918, output_tokens=35_968)]
    # Reproduce the screenshot's full SWE-2 usage over all 407 calls. Inflated
    # legacy branch indices cannot change model pricing or drop these tokens.
    for index in range(407):
        tokens = {field: total // 407 + int(index < total % 407) for field, total in (
            ("input_tokens", 1_681_492), ("cached_input_tokens", 39_151_989), ("output_tokens", 274_992),
        )}
        records.append(_usage(id=f"sidekick-{index}", model_id="swe-2-high", turn_index=49,
                              timestamp="2026-10-09T12:30:00Z", **tokens))
    records.append(_usage(id="compactor", model_id="compactor"))
    sources.transcript.get_usage_records.return_value = records
    original = deepcopy(records)

    estimate = _cost(sources)
    swe = next(model for model in estimate["models"] if model["model_id"] == "swe-2-high")
    assert swe["amount"] == 0
    assert swe["free_steps"] == 407
    assert swe["paid_steps"] == 0
    assert swe["cached_input_tokens"] == 39_151_989
    assert swe["hypothetical_amount"] == pytest.approx(10.13626331)
    assert estimate["amount"] == pytest.approx(2.9993978)
    assert estimate["hypothetical_amount"] == pytest.approx(13.13566111)
    assert estimate["priced_steps"] == 408
    assert estimate["unpriced_steps"] == 1
    assert records == original


def test_cost_missing_sidekick_prices_stay_unknown_instead_of_assuming_free(sources):
    sources.registry.get_model.side_effect = lambda mid: {"id": mid, "tags": []}
    sources.pricing.snapshots.return_value = [{
        "model_id": "fusion-lead-sidekick-swe-2-high", "input_price": 2,
        "cached_input_price": 0.1, "output_price": 10, "was_free": False,
    }]
    sources.transcript.get_usage_records.return_value = [_usage(model_id="swe-2-high")]

    estimate = _cost(sources)

    assert estimate["unpriced_steps"] == 1
    assert estimate["complete"] is False
    assert estimate["models"][0]["free_steps"] == 0
    assert estimate["models"][0]["input_tokens"] == 1_000_000


def test_cost_can_report_main_chain_context_without_priced_usage(sources):
    sources.db.get_session.return_value = None
    sources.db.get_latest_main_chain_inference.return_value = {
        "model_id": "current", "metrics": {"input_tokens": 10, "cache_read_tokens": 20},
    }

    estimate = _cost(sources)

    assert estimate["amount"] == 0
    assert estimate["complete"] is False
    assert estimate["turns"] == []
    assert estimate["context"] == {"used": 30, "size": 1000}
    assert estimate["source"] == "sqlite"


def test_cost_empty_session_has_no_usage_or_context(sources):
    sources.db.get_session.return_value = None

    estimate = _cost(sources)

    assert estimate["steps"] == estimate["turns"] == estimate["models"] == []
    assert estimate["context"] == {"used": None, "size": None}
    assert estimate["complete"] is False


def test_cost_zero_main_chain_context_does_not_reuse_older_positive_usage(sources):
    sources.db.get_messages.return_value = [
        {"role": "assistant", "model_id": "current", "metrics": {"input_tokens": 500}},
    ]
    sources.db.get_latest_main_chain_inference.return_value = {"model_id": "current", "metrics": {}}

    assert _cost(sources)["context"] == {"used": None, "size": None}


@pytest.mark.parametrize("field", ["turn_index", "context_tokens"])
def test_cost_invalid_numeric_metadata_still_raises(sources, field):
    sources.transcript.get_usage_records.return_value = [_usage(**{field: "not a number"})]

    with pytest.raises(ValueError, match="invalid literal for int"):
        _cost(sources)


def test_cost_pricing_read_error_propagates_before_context_lookup(sources):
    sources.pricing.snapshots.side_effect = OSError("pricing unreadable")

    with pytest.raises(OSError, match="pricing unreadable"):
        _cost(sources)

    sources.db.get_latest_main_chain_inference.assert_not_called()


@pytest.mark.parametrize(
    "first_timestamp,second_timestamp",
    [("2026-10-08T12:00:00", "2026-10-08T08:00:00-04:00"),
     ("2026-10-08T12:00:00.123100Z", "2026-10-08T12:00:00.123900+00:00"),
     ("invalid", "invalid")],
)
def test_reasoning_match_uses_normalized_instant_but_preserves_original_metadata(first_timestamp, second_timestamp):
    transcript = {"step_id": 1, "reasoning_content": "Thought\nwith spaces",
                  "timestamp": first_timestamp, "metrics": {"prompt_tokens": 50},
                  "tool_calls": [{"function_name": "read"}], "message": ""}
    database = {"step_id": 2, "reasoning_content": "Thought with   spaces",
                "timestamp": second_timestamp, "metrics": {"output_tokens": 5},
                "tool_calls": [], "message": "answer"}
    original = deepcopy([transcript, database])

    result = _merge([transcript], [database])

    assert result == [{**transcript, "message": "answer", "metrics": {"prompt_tokens": 50, "output_tokens": 5}}]
    result[0]["metrics"]["prompt_tokens"] = 99
    assert [transcript, database] == original


@pytest.mark.parametrize(
    "first_sample,second_sample,completion",
    [(False, False, 2), (False, True, 2), (True, False, 1), (True, True, 2)],
)
def test_reasoning_sample_precedence_keeps_metrics_and_enriches_message(first_sample, second_sample, completion):
    first = {"reasoning_content": "thought", "metrics": {"completion_tokens": 1},
             "is_throughput_sample": first_sample, "message": ""}
    second = {"reasoning_content": "thought", "metrics": {"completion_tokens": 2},
              "is_throughput_sample": second_sample, "message": "answer"}

    result = _merge([first], [second])

    assert len(result) == 1
    assert result[0]["metrics"] == {"completion_tokens": completion}
    assert result[0]["is_throughput_sample"] is (first_sample or second_sample)
    assert result[0]["message"] == "answer"
    assert first["message"] == ""


def test_reasoning_sorts_chronologically_with_missing_timestamp_last():
    steps = [
        {"step_id": "missing", "timestamp": None},
        {"step_id": "invalid", "timestamp": "invalid"},
        {"step_id": "earlier-instant", "timestamp": "2026-10-08T09:00:00Z"},
        {"step_id": "later-instant", "timestamp": "2026-10-08T08:00:00-04:00"},
        {"step_id": "empty", "timestamp": ""},
    ]

    result = _merge([], [{**step, "reasoning_content": "same text"} for step in steps])

    assert [step["step_id"] for step in result] == ["empty", "earlier-instant", "later-instant", "invalid", "missing"]
    assert all(step["metrics"] == {} for step in result)


@pytest.mark.parametrize(
    "timestamps,expected",
    [
        (
            ["2026-10-08T08:00:00-04:00", "2026-10-08T09:00:00Z", "2026-10-08T12:00:00+05:00"],
            ["2026-10-08T12:00:00+05:00", "2026-10-08T09:00:00Z", "2026-10-08T08:00:00-04:00"],
        ),
        (
            ["2026-10-08T10:00:00", "2026-10-08T10:30:00Z", "2026-10-08T09:00:00-02:00"],
            ["2026-10-08T10:00:00", "2026-10-08T10:30:00Z", "2026-10-08T09:00:00-02:00"],
        ),
        (
            ["2026-10-08T12:00:00.0009Z", "2026-10-08T12:00:00.0010Z", "2026-10-08T13:00:00.0005+01:00"],
            ["2026-10-08T13:00:00.0005+01:00", "2026-10-08T12:00:00.0009Z", "2026-10-08T12:00:00.0010Z"],
        ),
    ],
)
def test_reasoning_sort_uses_normalized_instants_across_offsets(timestamps, expected):
    steps = [
        {"reasoning_content": f"thought {index}", "timestamp": timestamp}
        for index, timestamp in enumerate(timestamps)
    ]

    result = _merge([], steps)

    assert [step["timestamp"] for step in result] == expected


def test_reasoning_equal_instants_keep_insertion_order_and_original_strings():
    steps = [
        {"reasoning_content": "first thought", "timestamp": "2026-10-08T12:00:00Z"},
        {"reasoning_content": "second thought", "timestamp": "2026-10-08T08:00:00-04:00"},
        {"reasoning_content": "third thought", "timestamp": "2026-10-08T12:00:00.000000+00:00"},
    ]
    original = deepcopy(steps)

    result = _merge([], steps)

    assert [step["reasoning_content"] for step in result] == [
        "first thought", "second thought", "third thought",
    ]
    assert [step["timestamp"] for step in result] == [
        "2026-10-08T12:00:00Z", "2026-10-08T08:00:00-04:00", "2026-10-08T12:00:00.000000+00:00",
    ]
    assert steps == original


def test_reasoning_distinct_milliseconds_and_blank_blocks():
    steps = [
        {"reasoning_content": content} for content in (None, "", " \n\t ")
    ] + [
        {"reasoning_content": "repeated", "timestamp": "2026-10-08T12:00:00.0009Z"},
        {"reasoning_content": "repeated", "timestamp": "2026-10-08T12:00:00.0010Z"},
    ]

    result = _merge(steps, [])

    assert [step["timestamp"] for step in result] == [
        "2026-10-08T12:00:00.0009Z", "2026-10-08T12:00:00.0010Z",
    ]
