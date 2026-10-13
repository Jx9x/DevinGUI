"""Stored message conversion contracts and projection without database access."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from server.db_reader import DBReader
from server.message_projection import (
    content_to_text, decode_json, image_metadata, normalize_metrics,
    project_messages, project_reasoning, thinking_to_text, timestamp_to_iso,
)


@pytest.fixture
def reader():
    """Use synthetic cursor rows while retaining the reader's real converters."""
    reader = DBReader("unused.db")
    connection = MagicMock()
    reader._connect = MagicMock(return_value=connection)
    reader._latest_turn_nodes = MagicMock(return_value=({1, 2}, {"replayed"}))
    return reader, connection, connection.cursor.return_value


def row(message, node=1, **overrides):
    return {
        "row_id": "7", "node_id": node, "parent_node_id": None,
        "chat_message": message, "created_at": "row-time", "metadata": "internal",
        **overrides,
    }


@pytest.mark.parametrize("raw,expected", [
    (None, None), (b'{"value": 1}', {"value": 1}), (b'"\xff"', "\ufffd"),
    (b'not-json\xff', "not-json\ufffd"), ('"text"', "text"), (42, 42), ([1], [1]),
])
def test_json_decoding_preserves_permissive_values(raw, expected):
    assert DBReader("unused.db")._safe_json(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (None, None), (b"  ", None), ("  original-time  ", "original-time"),
    ("2026-10-08T12:00:00.123456789Z", "2026-10-08T12:00:00.123456789Z"),
    (0, "1970-01-01T00:00:00+00:00"),
    (1_700_000_000, "2023-11-14T22:13:20+00:00"),
    (1_700_000_000_000, "2023-11-14T22:13:20+00:00"),
    (1_700_000_000_000_000, "2023-11-14T22:13:20+00:00"),
    (1_700_000_000_000_000_000, "2023-11-14T22:13:20+00:00"),
    (float("nan"), None), ({}, None),
])
def test_timestamp_units_and_original_strings(raw, expected):
    assert DBReader._ts_to_iso(raw) == expected


@pytest.mark.parametrize("literal", [
    "float('inf')", "float('-inf')", "float('nan')",
    "'inf'", "'-Infinity'", "'nan'", "'1e999'",
    "b'inf'", "b'-inf'", "b'nan'",
])
def test_timestamp_nonfinite_values_return_none_in_bounded_subprocess(literal):
    script = (
        "from server.message_projection import timestamp_to_iso;"
        f"print(repr(timestamp_to_iso({literal})))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "None"


@pytest.mark.parametrize("content,expected", [
    (None, None), (b"text\xff", "text\ufffd"),
    ([None, "", 0, False, {"text": "last"}], "0\nFalse\nlast"),
    ({"text": "", "content": "later"}, ""),
    ({"text": None, "content": {"output": "next"}}, "next"),
    ({"unknown": "\u00e9"}, '{\n  "unknown": "\u00e9"\n}'),
    ({"unknown": {1, 2}}, "{'unknown': {1, 2}}"),
])
def test_content_conversion_keeps_key_precedence_and_fallbacks(content, expected):
    assert DBReader._content_to_text(content) == expected


@pytest.mark.parametrize("thinking,expected", [
    ("", None), ({"thinking": "", "text": "next"}, "next"),
    ({"thinking": 0, "text": "later"}, "0"), ({"unknown": "ignored"}, None),
    (["first", {"content": "second"}], "first\nsecond"),
])
def test_thinking_conversion_keeps_truthy_precedence(thinking, expected):
    assert DBReader._thinking_to_text(thinking) == expected


@pytest.mark.parametrize("metrics,expected", [
    (None, None), ({"input_tokens": True, "output_tokens": float("inf")}, None),
    ({"cache_creation_tokens": None}, {"cache_creation_tokens": None}),
    ({"output_tokens": 10, "total_time_ms": 200, "ttft_ms": 199, "tokens_per_sec": 999},
     {"ttft_ms": 199, "total_time_ms": 200, "output_tokens": 10, "tokens_per_sec": 50}),
])
def test_metrics_keep_null_cache_creation_and_recompute_whole_inference_rate(metrics, expected):
    assert DBReader._normalize_metrics(metrics) == expected


def test_image_projection_keeps_indices_without_decoding_or_mutation():
    message = {"images": [None, {"mime_type": " IMAGE/PNG ", "base64_data": "invalid!", "width": 3, "height": True},
                          {"mime_type": "image/jpeg", "base64_data": "", "width": 4}]}
    original = deepcopy(message)
    assert DBReader._image_metadata(message) == [{"index": 1, "mime_type": "image/png", "width": 3}]
    assert message == original


def test_message_wire_values_keep_metadata_precedence_and_row_order(reader):
    decoder, connection, cursor = reader
    message = {"role": 5, "message_id": False, "content": {"text": "answer"}, "model": "fallback",
               "metadata": {"created_at": 0, "generation_model": "", "model_id": "", "model": 0, "finish_reason": False}}
    cursor.fetchall.return_value = [row(message, node=1.5, parent_node_id=True), row("raw-text", node=None)]
    assert decoder.get_messages("session") == [
        {"row_id": 7, "node_id": "1.5", "parent_node_id": True, "created_at": "row-time",
         "role": "5", "content": "answer", "images": [], "message_id": "False", "model_id": "0",
         "metrics": None, "finish_reason": "False", "metadata": None},
        {"row_id": 7, "node_id": None, "parent_node_id": None, "created_at": "row-time",
         "role": None, "content": "raw-text", "images": [], "message_id": None, "model_id": None,
         "metrics": None, "finish_reason": None, "metadata": None},
    ]
    connection.close.assert_called_once_with()


@pytest.mark.parametrize("metadata", ["malformed", ["malformed"], 5, True, None, {}])
def test_message_nonmapping_metadata_projects_complete_row_and_closes_connection(reader, metadata):
    decoder, connection, cursor = reader
    message = {"role": "assistant", "content": "answer", "model": "top-model",
               "images": [{"mime_type": "image/png", "base64_data": "opaque"}],
               "metadata": metadata}
    cursor.fetchall.return_value = [row(message)]
    assert decoder.get_messages("session") == [{
        "row_id": 7, "node_id": 1, "parent_node_id": None, "created_at": "row-time",
        "role": "assistant", "content": "answer",
        "images": [{"index": 0, "mime_type": "image/png"}],
        "message_id": None, "model_id": "top-model",
        "metrics": None, "finish_reason": None, "metadata": None,
    }]
    connection.close.assert_called_once_with()


def test_reasoning_skips_unrenderable_rows_and_normalizes_nonmapping_metadata(reader):
    decoder, connection, cursor = reader
    cursor.fetchall.return_value = [row("raw"), row({"thinking": ""}),
                                   row({"thinking": "kept", "content": {"text": "answer"}, "metadata": "malformed"})]
    assert decoder.get_reasoning_steps("session") == [{
        "step_id": None, "reasoning_content": "kept", "timestamp": "row-time",
        "tool_calls": [], "message": "answer", "metrics": {},
    }]
    connection.close.assert_called_once_with()


@pytest.mark.parametrize("match", ["node", "message", "neither"])
def test_reasoning_sample_keeps_later_ties_and_excludes_compactor_and_outside_nodes(reader, match):
    decoder, connection, cursor = reader
    measured = {"thinking": "first", "message_id": "replayed", "metadata": {
        "model": "selected", "metrics": {"output_tokens": 10, "total_time_ms": 200, "cache_creation_tokens": None},
    }}
    decoder._latest_turn_nodes.return_value = ({1, 2} if match == "node" else set(),
                                              {"replayed"} if match == "message" else set())
    cursor.fetchall.return_value = [
        row(measured, node=1), row({**measured, "thinking": "equal"}, node=2),
        row({**measured, "message_id": "outside", "thinking": "much longer outside"}, node=99),
        row({**measured, "thinking": "much longer compaction", "metadata": {**measured["metadata"], "model": "compactor"}}, node=1),
    ]
    steps = decoder.get_reasoning_steps("session")
    assert [step["reasoning_content"] for step in steps] == ["first", "equal", "much longer outside", "much longer compaction"]
    assert [step["reasoning_content"] for step in steps if step.get("is_throughput_sample")] == ([] if match == "neither" else ["equal"])
    assert steps[0]["metrics"] == {"completion_tokens": 10, "total_time_ms": 200, "tokens_per_sec": 50}
    connection.close.assert_called_once_with()


def test_message_hook_order_resolves_instance_overrides_for_each_row(reader):
    decoder, connection, cursor = reader
    source = {"role": "assistant", "content": "answer", "metadata": {"metrics": {}}}
    cursor.fetchall.return_value = [row(source), row(source)]
    events = []
    decoder._safe_json = lambda value: events.append("json") or value
    decoder._ts_to_iso = lambda value: events.append("timestamp") or value

    def content(value):
        events.append("text-first")
        decoder._content_to_text = lambda value: events.append("text-second") or "second"
        return "first"

    decoder._content_to_text = content
    decoder._image_metadata = lambda value: events.append("images") or []
    decoder._normalize_metrics = lambda value: events.append("metrics") or None
    assert [message["content"] for message in decoder.get_messages("session")] == ["first", "second"]
    assert events == ["json", "timestamp", "text-first", "images", "metrics",
                      "json", "timestamp", "text-second", "images", "metrics"]
    connection.close.assert_called_once_with()


@pytest.mark.parametrize("method,hook", [("get_messages", "_image_metadata"),
                                         ("get_reasoning_steps", "_ts_to_iso"),
                                         ("get_reasoning_steps", "_safe_json")])
def test_conversion_failures_keep_query_and_connection_cleanup_boundary(reader, method, hook):
    decoder, connection, cursor = reader
    cursor.fetchall.return_value = [row({"role": "assistant", "content": "answer", "thinking": "thought"})]
    setattr(decoder, hook, MagicMock(side_effect=RuntimeError("conversion failed")))
    with pytest.raises(RuntimeError, match="conversion failed"):
        getattr(decoder, method)("session")
    assert cursor.execute.called and cursor.fetchall.called
    connection.close.assert_called_once_with()


@pytest.mark.parametrize("kind", ["content", "thinking"])
def test_recursive_text_conversion_resolves_live_base_class_hook(monkeypatch, kind):
    convert_content, convert_thinking = DBReader._content_to_text, DBReader._thinking_to_text
    calls = []

    def first(value):
        calls.append(value)
        monkeypatch.setattr(DBReader, "_content_to_text", staticmethod(lambda value: "updated:" + value))
        return "" if kind == "thinking" else "first"

    monkeypatch.setattr(DBReader, "_content_to_text", staticmethod(first))
    if kind == "content":
        assert convert_content(["one", "two"]) == "first\nupdated:two"
    else:
        assert convert_thinking({"thinking": "one", "text": "two"}) == "updated:two"
    assert calls == ["one"]


@pytest.fixture
def pure_decoder():
    """Supply canonical converters without a database reader or connection."""
    return SimpleNamespace(
        _safe_json=decode_json, _ts_to_iso=timestamp_to_iso,
        _content_to_text=content_to_text, _thinking_to_text=thinking_to_text,
        _normalize_metrics=normalize_metrics, _image_metadata=image_metadata,
    )


@pytest.mark.parametrize("encoded", [False, True])
def test_pure_messages_consume_one_pass_rows_in_source_order_without_mutation(pure_decoder, encoded):
    messages = [
        {"role": "assistant", "message_id": "first", "content": ["answer", {"text": "next"}],
         "images": [{"mime_type": "image/png", "base64_data": "opaque", "width": 3}],
         "metadata": {"metrics": {"output_tokens": 4, "total_time_ms": 100}}},
        {"role": "user", "message_id": "second", "content": "earlier row id"},
    ]
    rows = [row(json.dumps(message).encode() if encoded else message, row_id=row_id)
            for message, row_id in zip(messages, [9, 2])]
    original = deepcopy(rows)
    projected = project_messages(iter(rows), decoder=pure_decoder)
    assert [(message["row_id"], message["message_id"], message["content"])
            for message in projected] == [(9, "first", "answer\nnext"), (2, "second", "earlier row id")]
    assert projected[0]["images"] == [{"index": 0, "mime_type": "image/png", "width": 3}]
    assert projected[0]["metrics"]["tokens_per_sec"] == 40
    assert all(message["metadata"] is None for message in projected)
    projected[0]["metrics"]["output_tokens"] = 99
    projected[0]["images"][0]["width"] = 99
    assert rows == original


@pytest.mark.parametrize("match", ["node", "message", "neither"])
def test_pure_reasoning_uses_explicit_turn_context_without_mutating_inputs(pure_decoder, match):
    measured = {"message_id": "replayed", "thinking": {"text": "thought"},
                "content": {"output": "answer"}, "metadata": {
                    "created_at": "original-time", "generation_model": "main-model",
                    "metrics": {"input_tokens": 10, "output_tokens": 20, "total_time_ms": 500},
                }}
    rows = [
        row(measured, node=5),
        row({**measured, "thinking": "much longer compaction", "metadata": {
            **measured["metadata"], "generation_model": "compactor",
        }}, node=5),
        row({**measured, "message_id": "outside", "thinking": "much longer other branch"}, node=99),
    ]
    nodes, message_ids = ({5} if match == "node" else set(),
                          {"replayed"} if match == "message" else set())
    original = deepcopy((rows, nodes, message_ids))
    steps = project_reasoning(iter(rows), turn_nodes=nodes, turn_message_ids=message_ids, decoder=pure_decoder)
    assert [step["reasoning_content"] for step in steps if step.get("is_throughput_sample")] == (
        [] if match == "neither" else ["thought"]
    )
    assert steps[0]["timestamp"] == "original-time" and steps[0]["message"] == "answer"
    assert steps[0]["metrics"] == {
        "prompt_tokens": 10, "completion_tokens": 20, "total_time_ms": 500, "tokens_per_sec": 40,
    }
    steps[0]["metrics"]["completion_tokens"] = 99
    steps[0]["tool_calls"].append({"name": "new"})
    assert steps[1]["tool_calls"] == []
    assert (rows, nodes, message_ids) == original


def test_pure_reasoning_resolves_decoder_hooks_in_order_for_each_row(pure_decoder):
    events = []
    message = {"thinking": "thought", "content": "answer"}
    pure_decoder._safe_json = lambda value: events.append("json") or value

    def thinking(value):
        events.append("thinking-first")
        pure_decoder._thinking_to_text = lambda value: events.append("thinking-second") or "second"
        return "first"

    pure_decoder._thinking_to_text = thinking
    pure_decoder._normalize_metrics = lambda value: events.append("metrics") or None
    pure_decoder._ts_to_iso = lambda value: events.append("timestamp") or value
    pure_decoder._content_to_text = lambda value: events.append("text") or value
    steps = project_reasoning(iter([row(message), row(message)]),
                              turn_nodes=set(), turn_message_ids=set(), decoder=pure_decoder)
    assert [step["reasoning_content"] for step in steps] == ["first", "second"]
    assert events == ["json", "thinking-first", "metrics", "timestamp", "text",
                      "json", "thinking-second", "metrics", "timestamp", "text"]


@pytest.mark.parametrize("project", ["messages", "reasoning"])
def test_pure_projection_propagates_decoder_failure_without_consuming_later_rows(pure_decoder, project):
    consumed = []

    def rows():
        consumed.append(1)
        yield row({"thinking": "thought", "content": "answer"})
        consumed.append(2)
        yield row({"thinking": "later", "content": "later"})

    pure_decoder._normalize_metrics = MagicMock(side_effect=ValueError("invalid metrics"))
    with pytest.raises(ValueError, match="invalid metrics"):
        if project == "messages":
            project_messages(rows(), decoder=pure_decoder)
        else:
            project_reasoning(rows(), turn_nodes=set(), turn_message_ids=set(), decoder=pure_decoder)
    assert consumed == [1]


@pytest.mark.parametrize("kind", ["content", "thinking"])
def test_nested_conversion_uses_supplied_callable_without_checking_its_truthiness(kind):
    class Converter:
        def __bool__(self):
            raise AssertionError("converter truthiness must not be evaluated")

        def __call__(self, value):
            return "converted:" + value

    if kind == "content":
        assert content_to_text(["one", "two"], nested_text=Converter()) == "converted:one\nconverted:two"
    else:
        assert thinking_to_text({"text": "one"}, convert_text=Converter()) == "converted:one"
