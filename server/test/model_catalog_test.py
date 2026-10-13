"""Wire parsing and pure catalogue-decoding compatibility contracts."""

import struct

import pytest

from server import model_catalog as catalog
from server.test.model_registry_test import (
    _cache_blob, _f32, _group_blob, _ld, _str, _tag, _v, _varint,
)


@pytest.mark.parametrize("data,expected", [
    (_v(200, 16_384), (200, 0, 16_384)),
    (_ld(200, b"payload"), (200, 2, b"payload")),
    (_f32(200, 0xFEDCBA98), (200, 5, 0xFEDCBA98)),
    (_tag(200, 1) + struct.pack("<Q", 0xFEDCBA9876543210), (200, 1, 0xFEDCBA9876543210)),
])
def test_wire_parser_preserves_supported_scalar_types(data, expected):
    assert list(catalog._iter_wire_fields(data)) == [expected]


@pytest.mark.parametrize("tail", [
    b"\x80",
    _tag(2, 0) + b"\x80",
    _tag(2, 2) + b"\x80",
    _tag(2, 2) + b"\x05abc",
    _tag(2, 5) + b"abc",
    _tag(2, 1) + b"abcdefg",
])
def test_wire_parser_rejects_truncated_trailing_fields(tail):
    with pytest.raises(ValueError):
        list(catalog._iter_wire_fields(_v(1, 7) + tail))


@pytest.mark.parametrize("data", [
    b"\x80",
    b"\x80" * 9 + b"\x02",
    b"\x80" * 9 + b"\x01",
    b"\x80" * 10 + b"\x01",
    _varint(1 << 32),
    _varint((1 << 29) << 3),
    _tag(0, 0) + b"\x01",
    _tag(0, 2) + b"\x00",
    _tag(2, 0) + b"\x80" * 10 + b"\x01",
    _tag(2, 0) + b"\xff" * 9 + b"\x7f",
    _tag(2, 2) + _varint(10) + b"ab",
    _tag(2, 2) + _varint(1 << 40) + b"ab",
    _tag(2, 2) + b"\x80",
    _tag(2, 5) + b"abc",
    _tag(2, 1) + b"abcdefg",
])
def test_wire_parser_rejects_malformed_or_out_of_range_input(data):
    with pytest.raises(ValueError):
        list(catalog._iter_wire_fields(data))


def test_wire_parser_rejects_oversized_payload():
    data = _v(1, 1) * (catalog._CACHE_PAYLOAD_MAX_BYTES // 2 + 1)
    with pytest.raises(ValueError):
        list(catalog._iter_wire_fields(data))


def test_wire_parser_bounds_field_count():
    assert len(list(catalog._iter_wire_fields(_v(1, 1) * 100_000))) == 100_000
    with pytest.raises(ValueError):
        list(catalog._iter_wire_fields(_v(1, 1) * 100_001))


def test_wire_parser_accepts_maximum_legal_values():
    assert list(catalog._iter_wire_fields(_v(1, 2**64 - 1))) == [(1, 0, 2**64 - 1)]
    max_field = 2**29 - 1
    assert list(catalog._iter_wire_fields(_v(max_field, 1))) == [(max_field, 0, 1)]


@pytest.mark.parametrize("wire", [3, 4, 6, 7])
def test_wire_parser_rejects_unsupported_wire_types(wire):
    with pytest.raises(ValueError, match=f"unsupported protobuf wire type {wire}"):
        list(catalog._iter_wire_fields(_tag(2, wire)))


def test_deeply_nested_opaque_wire_bytes_do_not_recurse():
    blob = b"\x00"
    for _ in range(500):
        blob = _ld(99, blob)
    group = _str(22, "model") + _ld(99, blob)
    assert catalog._decode_model_cache(_cache_blob([group])) == {"1": [{"22": b"model"}]}


def test_deeply_nested_field_one_blobs_are_schema_bounded():
    blob = _v(1, 1)
    for _ in range(500):
        blob = _ld(1, blob)
    assert isinstance(catalog._decode_model_cache(blob), dict)


def test_decode_returns_empty_for_malformed_and_unrecognized_layouts():
    assert catalog._decode_model_cache(b"\x0b") == {}
    assert catalog._decode_model_cache(_str(9, "no field one")) == {}
    assert catalog._decode_model_cache(b"") == {}


def test_singular_fields_use_last_matching_wire_type():
    fields = catalog._wire_map(_str(22, "first") + _str(22, "last") + _v(22, 123))
    assert catalog._wire_last(fields, 22, 2) == b"last"
    assert catalog._wire_last(fields, 22, 0) == 123
    assert catalog._wire_last(fields, 23, 2) is None


def test_model_decoder_ignores_unknown_fields_and_malformed_repeated_groups():
    data = _cache_blob([b"\x0b", _group_blob()]) + _v(1, 123) + _str(99, "unknown")
    assert catalog._decode_model_cache(data) == catalog._decode_model_cache(_cache_blob([_group_blob()]))


def test_model_decoder_skips_malformed_nested_metadata_without_losing_model():
    group = _str(22, "model") + _ld(23, b"\x0b") + _ld(30, b"\x0b") + _ld(32, b"\x0b")
    assert catalog._decode_model_cache(_cache_blob([group])) == {"1": [{"22": b"model"}]}


def test_model_decoder_preserves_string_and_integer_label_values():
    label = _str(1, "Effort") + _ld(2, _str(2, "High") + _v(1, 1))
    group = _str(22, "model") + _ld(30, _ld(2, label))
    assert catalog._decode_model_cache(_cache_blob([group])) == {"1": [{
        "22": b"model", "30": {"2": [{"1": b"Effort", "2": {"2": b"High", "1": 1}}]},
    }]}


def test_model_decoder_preserves_duplicate_groups_and_prices_in_wire_order():
    group = _str(22, "model") + _ld(32, _str(1, "Input") + _f32(2, 1)) + _ld(32, _str(1, "Input") + _f32(2, 2))
    message = catalog._decode_model_cache(_cache_blob([group, group]))
    assert len(message["1"]) == 2
    assert message["1"][0]["32"] == [{"1": b"Input", "2": 1}, {"1": b"Input", "2": 2}]


def test_cli_catalog_derives_defaults_without_mutating_input():
    data = {"families": [{"variants": [{
        "model_uid": "sample-model", "max_context_tokens": True,
        "max_output_tokens": -1, "description": 123,
    }]}]}
    assert catalog._parse_cli_model_list(data) == {"sample-model": {
        "display_name": "Sample Model", "family": "sample-model", "family_name": "Sample Model",
        "description": "Sample Model", "tags": [],
    }}
    assert data == {"families": [{"variants": [{
        "model_uid": "sample-model", "max_context_tokens": True,
        "max_output_tokens": -1, "description": 123,
    }]}]}


def test_cli_catalog_keeps_last_duplicate_model_and_checks_fast_variant_family():
    data = {"families": [
        {"family_uid": "base", "variants": [{"model_uid": "model", "label": "Original"}]},
        {"family_uid": "other", "variants": [
            {"model_uid": "model-fast", "label": "Other Fast"},
            {"model_uid": "model-priority", "label": "Other Priority"},
            {"model_uid": "model", "label": "Replacement"},
        ]},
    ]}
    models = catalog._parse_cli_model_list(data)
    assert models["model"]["display_name"] == "Replacement"
    assert models["model-fast"]["is_fast"] is True
    assert models["model-priority"]["is_fast"] is True
    data["families"][1]["variants"].pop()
    assert "is_fast" not in catalog._parse_cli_model_list(data)["model-fast"]
