"""Cache formats, decoder fallback, and memoization compatibility contracts."""

from __future__ import annotations

import base64
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call, mock_open, patch

import pytest

from server import model_cache as cache
from server.test.model_registry_test import _cache_blob, _group_blob, _str, _v


@pytest.fixture(autouse=True)
def isolated_parse_cache(monkeypatch):
    monkeypatch.setattr(cache, "_parse_cache", {})


def forbidden_legacy_decoder(monkeypatch):
    decode = Mock(side_effect=AssertionError("legacy decoder must not run"))
    monkeypatch.setitem(sys.modules, "blackboxprotobuf", SimpleNamespace(decode_message=decode))
    return decode


@pytest.mark.parametrize("envelope", [False, True])
def test_cache_reader_preserves_protobuf_bytes_and_fetch_timestamp(tmp_path, envelope):
    blob = _cache_blob([_group_blob()])
    path = tmp_path / "cache.bin"
    if envelope:
        path.write_text(json.dumps({
            "payload": base64.b64encode(blob).decode(), "fetched_at_secs": 123.75,
        }), encoding="utf-8")
    else:
        path.write_bytes(blob)
    assert cache._read_cache_bytes(str(path)) == (blob, 123 if envelope else None)


@pytest.mark.parametrize("raw,warning", [
    (b"{", "Failed to parse JSON cache envelope"),
    (b"{\xff}", "Failed to parse JSON cache envelope"),
    (b"{}", "has no string payload"),
    (b'{"payload": 123}', "has no string payload"),
    (b'{"payload": null}', "has no string payload"),
    (b'{"payload": "%%%", "fetched_at_secs": 123}', "Failed to base64-decode payload"),
])
def test_invalid_cache_envelopes_return_empty_payload_and_warn(tmp_path, caplog, raw, warning):
    path = tmp_path / "cache.bin"
    path.write_bytes(raw)
    assert cache._read_cache_bytes(str(path)) == (b"", None)
    assert warning in caplog.text
    assert caplog.records[-1].name == "server.model_registry"


def test_missing_cache_reader_returns_empty_payload_and_warns(tmp_path, caplog):
    assert cache._read_cache_bytes(str(tmp_path / "missing.bin")) == (b"", None)
    assert "Failed to read cache file" in caplog.text


def test_injected_decoder_runs_once_but_metadata_is_rebuilt(tmp_path):
    path = tmp_path / "models.bin"
    path.write_bytes(b"\x0b")  # Unsupported wire type forces the legacy decoder.
    message = {"1": [{"22": b"rescued", "23": {"23": b"family"}}]}
    decode = Mock(return_value=message)

    models, aliases = cache._parse_model_cache(str(path), decode=decode)
    models["rescued"]["tags"].append("edited")
    aliases.pop("rescued")
    again, aliases_again = cache._parse_model_cache(str(path), decode=decode)

    assert again["rescued"]["tags"] == []
    assert again["rescued"]["family"] == "family"
    assert again["rescued"]["display_name"] == "Rescued"
    assert aliases_again["rescued"] == "family"
    assert aliases_again["MODEL_PRIVATE_11"] == "claude-haiku-4.5"
    assert message == {"1": [{"22": b"rescued", "23": {"23": b"family"}}]}
    decode.assert_called_once_with(b"\x0b")


@pytest.mark.parametrize("parser,warning", [
    ("_parse_model_cache", "Unrecognized model cache layout"),
    ("_parse_allowed_model_ids", "Failed to parse team settings"),
])
def test_malformed_cache_returns_empty_with_warning_and_no_legacy_decoder(
    tmp_path, monkeypatch, caplog, parser, warning,
):
    path = tmp_path / "cache.bin"
    path.write_bytes(b"\x0b")
    decode = forbidden_legacy_decoder(monkeypatch)
    expected = ({}, {}) if parser == "_parse_model_cache" else set()
    assert getattr(cache, parser)(str(path)) == expected
    assert getattr(cache, parser)(str(path)) == expected
    assert cache._parse_cache == {}
    assert warning in caplog.text
    decode.assert_not_called()


def test_unrecognized_layout_is_not_memoized(tmp_path):
    path = tmp_path / "cache.bin"
    path.write_bytes(b"\x0b")
    decode = Mock(return_value={})
    assert cache._parse_model_cache(str(path), decode=decode) == ({}, {})
    assert cache._parse_model_cache(str(path), decode=decode) == ({}, {})
    assert decode.call_count == 2
    assert cache._parse_cache == {}


def test_team_settings_fast_path_ignores_wrong_wire_types_and_empty_ids(tmp_path, monkeypatch):
    path = tmp_path / "team.bin"
    path.write_bytes(_str(7, "a") + _str(7, "a") + _str(7, "") + _v(7, 42) + _str(8, "other"))
    decode = forbidden_legacy_decoder(monkeypatch)
    assert cache._parse_allowed_model_ids(str(path)) == {"a"}
    decode.assert_not_called()


def test_team_settings_wire_failure_rejects_the_entire_blob(tmp_path, caplog):
    path = tmp_path / "team.bin"
    path.write_bytes(_str(7, "partial") + b"\x0b")
    assert cache._parse_allowed_model_ids(str(path)) == set()
    assert "Failed to parse team settings" in caplog.text
    assert cache._parse_cache == {}


def test_team_settings_without_allowed_field_warns_and_returns_empty(tmp_path, caplog):
    path = tmp_path / "team.bin"
    path.write_bytes(_str(8, "other"))
    assert cache._parse_allowed_model_ids(str(path)) == set()
    assert cache._parse_allowed_model_ids(str(path)) == set()
    assert "has no allowed models" in caplog.text


def test_empty_team_settings_result_is_memoized(tmp_path, monkeypatch):
    path = tmp_path / "team.bin"
    path.write_bytes(_str(8, "other"))
    calls = []
    original = cache._iter_wire_fields
    monkeypatch.setattr(cache, "_iter_wire_fields", lambda d: calls.append(d) or original(d))
    assert cache._parse_allowed_model_ids(str(path)) == set()
    assert cache._parse_allowed_model_ids(str(path)) == set()
    assert len(calls) == 1


def test_cache_reader_reads_at_most_file_max_plus_one(tmp_path):
    blob = _cache_blob([_group_blob()])
    handle_mock = mock_open(read_data=blob)
    with patch("builtins.open", handle_mock):
        data, _ = cache._read_cache_bytes(str(tmp_path / "cache.bin"))
    handle = handle_mock.return_value.__enter__.return_value
    assert handle.read.call_args_list == [call(cache._CACHE_FILE_MAX_BYTES + 1)]
    assert data == blob


def test_oversized_cache_file_is_rejected(tmp_path, caplog):
    path = tmp_path / "big.bin"
    path.write_bytes(b"\x00" * (cache._CACHE_FILE_MAX_BYTES + 1))
    assert cache._read_cache_bytes(str(path)) == (b"", None)
    assert "exceeds" in caplog.text


def test_oversized_raw_cache_payload_is_rejected(tmp_path, caplog):
    path = tmp_path / "big.bin"
    path.write_bytes(b"\x0a" + b"\x00" * cache._CACHE_PAYLOAD_MAX_BYTES)
    assert cache._read_cache_bytes(str(path)) == (b"", None)
    assert "exceeds" in caplog.text


def test_oversized_base64_payload_is_rejected_before_decode(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cache, "_CACHE_PAYLOAD_MAX_BYTES", 8)
    monkeypatch.setattr(cache, "_BASE64_PAYLOAD_MAX_CHARS", 4 * ((8 + 2) // 3))
    payload = base64.b64encode(b"\x00" * 100).decode()
    assert len(payload) > cache._BASE64_PAYLOAD_MAX_CHARS
    decode = Mock(wraps=base64.b64decode)
    monkeypatch.setattr(cache.base64, "b64decode", decode)
    path = tmp_path / "env.bin"
    path.write_text(
        json.dumps({"payload": payload, "fetched_at_secs": 5}), encoding="utf-8"
    )
    assert cache._read_cache_bytes(str(path)) == (b"", None)
    decode.assert_not_called()
    assert "exceeds" in caplog.text


def test_boundary_base64_payload_decoding_over_the_limit_is_rejected(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cache, "_CACHE_PAYLOAD_MAX_BYTES", 8)
    monkeypatch.setattr(cache, "_BASE64_PAYLOAD_MAX_CHARS", 4 * ((8 + 2) // 3))
    payload = base64.b64encode(b"\x00" * 9).decode()
    assert len(payload) == cache._BASE64_PAYLOAD_MAX_CHARS
    path = tmp_path / "env.bin"
    path.write_text(
        json.dumps({"payload": payload, "fetched_at_secs": 5}), encoding="utf-8"
    )
    assert cache._read_cache_bytes(str(path)) == (b"", None)
    assert "exceeds" in caplog.text


def test_deeply_nested_envelope_does_not_crash_readers(tmp_path):
    path = tmp_path / "env.bin"
    path.write_bytes(b'{"a":' + b"[" * 5000 + b"]" * 5000 + b"}")
    assert cache._read_cache_bytes(str(path)) == (b"", None)
    assert cache._read_envelope_fetched_at(str(path)) is None


def test_freshness_reader_bounds_total_read(tmp_path):
    data = json.dumps(
        {"fetched_at_secs": 5, "payload": "AA==", "pad": "x" * 200}
    ).encode()
    handle_mock = mock_open(read_data=data)
    with patch("builtins.open", handle_mock):
        assert cache._read_envelope_fetched_at(str(tmp_path / "env.bin")) == 5
    handle = handle_mock.return_value.__enter__.return_value
    requested = [c.args[0] for c in handle.read.call_args_list]
    assert requested[0] == 64
    assert sum(requested) <= cache._CACHE_FILE_MAX_BYTES + 1


def test_freshness_reader_rejects_oversized_envelope_before_json_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_FILE_MAX_BYTES", 100)
    pad = "x" * (101 - len(json.dumps({"fetched_at_secs": 5, "pad": ""})))
    data = json.dumps({"fetched_at_secs": 5, "pad": pad}).encode()
    assert len(data) == 101
    loads = Mock(wraps=json.loads)
    monkeypatch.setattr(cache.json, "loads", loads)
    path = tmp_path / "env.bin"
    path.write_bytes(data)
    assert cache._read_envelope_fetched_at(str(path)) is None
    loads.assert_not_called()


@pytest.mark.parametrize("change", ["mtime", "size", "path"])
def test_parse_memo_key_includes_path_nanosecond_mtime_and_size(tmp_path, change):
    path = tmp_path / "cache.bin"
    path.write_bytes(b"abc")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    value = {"decoded": True}
    cache._parse_cache_put(str(path), value)
    assert cache._parse_cache_get(str(path)) is value
    if change == "path":
        path = tmp_path / "other.bin"
        path.write_bytes(b"abc")
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    elif change == "size":
        path.write_bytes(b"abcd")
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    else:
        os.utime(path, ns=(2_000_000_000, 2_000_000_000))
    assert cache._parse_cache_get(str(path)) is None


def test_parse_memo_evicts_oldest_entry_without_promoting_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_PARSE_CACHE_MAX", 2)
    paths = [str(tmp_path / name) for name in ("a.bin", "b.bin", "c.bin")]
    for path in paths:
        with open(path, "wb") as handle:
            handle.write(b"x")
    cache._parse_cache_put(paths[0], "a")
    cache._parse_cache_put(paths[1], "b")
    assert cache._parse_cache_get(paths[0]) == "a"
    cache._parse_cache_put(paths[2], "c")
    assert cache._parse_cache_get(paths[0]) is None
    assert cache._parse_cache_get(paths[1]) == "b"
    assert cache._parse_cache_get(paths[2]) == "c"
    assert len(cache._parse_cache) == 2


def test_parse_memo_ignores_files_that_cannot_be_statted(tmp_path):
    path = str(tmp_path / "missing.bin")
    cache._parse_cache_put(path, {"decoded": True})
    assert cache._parse_cache_get(path) is None
    assert cache._parse_cache == {}


def test_freshness_sort_uses_path_to_break_ties_and_missing_files_sort_last(tmp_path):
    paths = [tmp_path / name for name in ("a.bin", "b.bin")]
    for path in paths:
        path.write_bytes(b"raw")
        os.utime(path, (123, 123))
    missing = str(tmp_path / "missing.bin")
    assert cache._sort_cache_files([str(paths[0]), missing, str(paths[1])]) == [
        str(paths[1]), str(paths[0]), missing,
    ]
