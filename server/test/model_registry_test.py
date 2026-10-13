"""Tests for the fast protobuf wire parser used by the model registry.

The CLI's ``model_configs_v5.*.bin`` cache is ~364 KB of nested protobuf.
``blackboxprotobuf.decode_message`` takes ~20 s on it (it deep-copies an
anonymous typedef per nested message), which stalled the asyncio event loop
when ``refresh()`` ran inside the DB-watcher callback.  ``_decode_model_cache``
decodes only the fields the registry reads, in milliseconds.
"""
import asyncio
import json
import base64
import os
import struct
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from server import model_registry as mr


def test_get_model_captures_each_fusion_component_price_without_live_profiles():
    reg = mr.ModelRegistry.__new__(mr.ModelRegistry)
    reg._id_map = {"gpt-6-1-sol-high-fast": "gpt-6-1-sol-high-priority"}
    reg._data = {"models": {
        "fusion-gpt-6-1-sol-high-fast-sidekick-swe-2-high": {"input_price": 4},
        "gpt-6-1-sol-high-priority": {"input_price": 4, "cached_input_price": 0.2, "output_price": 20},
        "swe-2-high": {"tags": ["Free"]},
    }}
    prices = reg.get_model("fusion-gpt-6-1-sol-high-fast-sidekick-swe-2-high")["inference_models"]
    assert prices["gpt-6-1-sol-high-priority"]["input_price"] == 4
    assert prices["swe-2-high"]["tags"] == ["Free"]
    assert prices["swe-2-high"]["input_price"] is None


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint(field << 3 | wire)


def _v(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _f32(field: int, raw: int) -> bytes:
    return _tag(field, 5) + struct.pack("<I", raw)


def _ld(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _str(field: int, text) -> bytes:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return _ld(field, text)


def _group_blob(
    internal_id=b"test-model-medium",
    group_label=b"Test Model Medium",
    alias=b"test-model",
    base_name=b"Test Model",
    context_window=1_000_000,
    max_output=128_000,
    vision=1,
    cost_raw=struct.unpack("<I", struct.pack("<f", 2.5))[0],
    promo=1,
    recommended=1,
    labels=(),
    prices=((b"Input", 2.0), (b"Cached input", 0.5), (b"Output", 10.0)),
):
    config = (
        _v(4, context_window)
        + _ld(6, _v(11, vision))
        + _v(13, max_output)
        + _str(23, alias.decode())
    )
    display = _str(1, base_name.decode())
    for lname, lval in labels:
        inner = _str(2, lval) if isinstance(lval, str) else (_v(1, lval) if lval is not None else b"")
        display += _ld(2, _str(1, lname) + (_ld(2, inner) if inner else b""))
    blob = (
        _str(1, group_label.decode())
        + _f32(3, cost_raw)
        + _ld(19, _v(1, promo))
        + _str(22, internal_id.decode())
        + _ld(23, config)
        + _ld(30, display)
        + _v(31, recommended)
    )
    for component, price in prices:
        price_raw = struct.unpack("<I", struct.pack("<f", price))[0]
        blob += _ld(32, _str(1, component) + _f32(2, price_raw))
    return blob


def _cache_blob(groups) -> bytes:
    return b"".join(_ld(1, g) for g in groups)


def _write_cache(tmp_path, blob: bytes, envelope: bool = False):
    path = tmp_path / "model_configs_v5.deadbeef.bin"
    if envelope:
        path.write_text(json.dumps({"payload": base64.b64encode(blob).decode(), "fetched_at_secs": 1700000000}))
    else:
        path.write_bytes(blob)
    return str(path)


def test_decode_model_cache_extracts_all_read_fields():
    blob = _cache_blob([
        _group_blob(labels=[("Effort", "Medium"), ("Thinking", 1), ("Fast Mode", None)]),
    ])
    msg = mr._decode_model_cache(blob)
    groups = msg.get("1", [])
    assert len(groups) == 1
    g = groups[0]
    assert g["22"] == b"test-model-medium"
    assert g["1"] == b"Test Model Medium"
    assert g["3"] == struct.unpack("<I", struct.pack("<f", 2.5))[0]
    assert g["19"] == {"1": 1}
    assert g["31"] == 1
    assert g["23"]["4"] == 1_000_000
    assert g["23"]["6"] == {"11": 1}
    assert g["23"]["13"] == 128_000
    assert g["23"]["23"] == b"test-model"
    assert g["30"]["1"] == b"Test Model"
    assert g["30"]["2"][0]["1"] == b"Effort"
    assert g["30"]["2"][0]["2"] == {"2": b"Medium"}
    assert g["30"]["2"][1]["2"] == {"1": 1}
    assert len(g["32"]) == 3
    assert g["32"][0]["1"] == b"Input"


def test_parse_model_cache_builds_entries(tmp_path):
    path = _write_cache(
        tmp_path,
        _cache_blob([
            _group_blob(labels=[("Effort", "Medium"), ("Thinking", 1), ("Fast Mode", "x")]),
            _group_blob(internal_id=b"test-model-fast", group_label=b"Test Model Fast",
                        alias=b"test-model", base_name=b"Test Model", promo=0,
                        recommended=0, vision=0, prices=[]),
        ]),
    )
    parsed, id_map = mr._parse_model_cache(path)
    assert id_map["test-model-medium"] == "test-model"
    assert id_map["test-model-fast"] == "test-model"
    entry = parsed["test-model-medium"]
    assert entry["display_name"] == "Test Model Medium"
    assert entry["family"] == "test-model"
    assert entry["family_name"] == "Test Model"
    assert entry["vision"] is True
    assert entry["context_window"] == 1_000_000
    assert entry["max_output_tokens"] == 128_000
    assert entry["is_recommended"] is True
    assert entry["input_price"] == pytest.approx(2.0)
    assert entry["cached_input_price"] == pytest.approx(0.5)
    assert entry["output_price"] == pytest.approx(10.0)
    # promo flag present but priced -> not free
    assert entry["is_promo"] is False
    assert "Thinking" in entry["label_tags"]
    assert "1M" in entry["label_tags"]
    fast = parsed["test-model-fast"]
    assert fast["is_promo"] is False
    assert fast["is_recommended"] is False
    assert fast["vision"] is False


def test_parse_model_cache_json_envelope(tmp_path):
    path = _write_cache(tmp_path, _cache_blob([_group_blob()]), envelope=True)
    parsed, _ = mr._parse_model_cache(path)
    assert "test-model-medium" in parsed


def test_parse_model_cache_malformed_returns_empty_gracefully(tmp_path):
    path = _write_cache(tmp_path, b"\xfb\xff\xff\xff\xff\xff\xff\xff\xff\xff\xffgarbage")
    # Must not raise; empty is acceptable, as is a blackboxprotobuf rescue.
    parsed, _ = mr._parse_model_cache(path)
    assert parsed == {}


def test_parse_allowed_model_ids(tmp_path):
    path = tmp_path / "team_settings.deadbeef.bin"
    path.write_bytes(_str(7, "model-a") + _str(7, "model-b"))
    assert mr._parse_allowed_model_ids(str(path)) == {"model-a", "model-b"}


def test_parse_model_cache_is_memoized_per_file_version(tmp_path, monkeypatch):
    calls = []
    orig = mr._decode_model_cache
    monkeypatch.setattr(mr, "_decode_model_cache", lambda d: calls.append(1) or orig(d))
    path = _write_cache(tmp_path, _cache_blob([_group_blob()]))
    mr._parse_model_cache(path)
    mr._parse_model_cache(path)
    assert calls == [1]
    # Rewriting the file invalidates the memo entry.
    path2 = _write_cache(tmp_path, _cache_blob([_group_blob(internal_id=b"other-medium")]))
    mr._parse_model_cache(path2)
    assert calls == [1, 1]


def test_decode_model_cache_scales_linearly():
    # 434 real groups decode in ~20 ms; a regression back to blackboxprotobuf
    # would take seconds. Generous bound for slow CI machines.
    blob = _cache_blob([_group_blob(internal_id=f"m{i}".encode()) for i in range(400)])
    start = time.monotonic()
    msg = mr._decode_model_cache(blob)
    elapsed = time.monotonic() - start
    assert len(msg["1"]) == 400
    assert elapsed < 2.0


def _registry(tmp_path, excluded):
    """Build a ModelRegistry seeded with a prior exclusion list."""
    reg_dir = tmp_path / "reg"
    reg_dir.mkdir()
    (reg_dir / "model_registry.json").write_text(
        json.dumps({"models": {}, "excluded_models": excluded, "last_used_model": None})
    )
    cache = _write_cache(
        tmp_path,
        _cache_blob([
            # 'fusion' is both a real internal ID and the family alias shared
            # by every fusion-* composite variant.
            _group_blob(internal_id=b"fusion", group_label=b"Fusion",
                        alias=b"fusion", base_name=b"Fusion"),
            _group_blob(internal_id=b"fusion-lead-sidekick-a",
                        group_label=b"Fusion (Lead + A)", alias=b"fusion",
                        base_name=b"Fusion"),
            _group_blob(internal_id=b"fusion-lead-sidekick-b",
                        group_label=b"Fusion (Lead + B)", alias=b"fusion",
                        base_name=b"Fusion"),
            _group_blob(internal_id=b"fusion-sidekick-a",
                        group_label=b"Fusion Sidekick (A)",
                        alias=b"fusion-sidekick-a", base_name=b"Fusion Sidekick"),
            # A second family used to verify legacy alias exclusions still
            # expand to all variants.
            _group_blob(internal_id=b"fam-medium", group_label=b"Fam Medium",
                        alias=b"fam", base_name=b"Fam"),
            _group_blob(internal_id=b"fam-high", group_label=b"Fam High",
                        alias=b"fam", base_name=b"Fam"),
        ]),
    )
    return mr.ModelRegistry(
        registry_dir=str(reg_dir),
        db_path=str(tmp_path / "none.db"),
        cache_path=cache,
        # A nonexistent file → empty allowed_ids → all cached models become
        # candidates.  (Auto-discovery would read the real CLI team settings.)
        team_settings_path=str(tmp_path / "no_team_settings.bin"),
    )


def test_excluding_alias_colliding_id_does_not_cascade(tmp_path):
    # Excluding the 'fusion' model must not exclude the fusion-* variants
    # that merely share it as their family alias.
    reg = _registry(tmp_path, excluded=["fusion"])
    excluded = set(reg._data["excluded_models"])
    assert "fusion" in excluded
    assert "fusion-lead-sidekick-a" not in excluded
    assert "fusion-lead-sidekick-b" not in excluded


def test_legacy_alias_exclusion_still_expands(tmp_path):
    # An excluded ID that is only an alias (not a live model key) still
    # applies to every variant in the family.
    reg = _registry(tmp_path, excluded=["fam"])
    excluded = set(reg._data["excluded_models"])
    assert {"fam-medium", "fam-high"} <= excluded


def test_excluded_internal_id_not_in_candidates_stays_literal(tmp_path):
    # 'fusion' is a real internal ID AND the family alias of every fusion-*
    # combo.  When it drops out of the candidate set (e.g. the CLI catalog
    # lists only combos, never a bare 'fusion'), its exclusion must stay
    # literal — expanding it through the alias would nuke the whole family.
    reg_dir = tmp_path / "reg"
    reg_dir.mkdir()
    (reg_dir / "model_registry.json").write_text(
        json.dumps({"models": {}, "excluded_models": ["fusion"], "last_used_model": None})
    )
    cache = _write_cache(
        tmp_path,
        _cache_blob([
            _group_blob(internal_id=b"fusion", group_label=b"Fusion",
                        alias=b"fusion", base_name=b"Fusion"),
            _group_blob(internal_id=b"fusion-lead-sidekick-a",
                        group_label=b"Fusion (Lead + A)", alias=b"fusion",
                        base_name=b"Fusion"),
            _group_blob(internal_id=b"fusion-lead-sidekick-b",
                        group_label=b"Fusion (Lead + B)", alias=b"fusion",
                        base_name=b"Fusion"),
        ]),
    )
    # Team settings allow only the combos — 'fusion' itself is not a candidate.
    team_settings = tmp_path / "team_settings.bin"
    team_settings.write_bytes(
        _str(7, "fusion-lead-sidekick-a") + _str(7, "fusion-lead-sidekick-b")
    )
    reg = mr.ModelRegistry(
        registry_dir=str(reg_dir),
        db_path=str(tmp_path / "none.db"),
        cache_path=cache,
        team_settings_path=str(team_settings),
    )
    assert "fusion" not in reg._data["models"]
    excluded = set(reg._data["excluded_models"])
    assert "fusion" in excluded
    assert "fusion-lead-sidekick-a" not in excluded
    assert "fusion-lead-sidekick-b" not in excluded


def test_fusion_sidekicks_are_not_selectable(tmp_path):
    reg = _registry(tmp_path, excluded=[])
    assert "fusion-sidekick-a" not in reg._data["models"]
    assert "fusion-lead-sidekick-a" in reg._data["models"]


def _custom_registry(tmp_path, groups):
    """Build a ModelRegistry over a caller-supplied group list.

    Seeds an empty registry JSON only on first call so a second call against
    the same tmp_path reloads persisted state (used by the last-used-variant
    persistence test).
    """
    reg_dir = tmp_path / "reg"
    reg_dir.mkdir(exist_ok=True)
    json_path = reg_dir / "model_registry.json"
    if not json_path.exists():
        json_path.write_text(
            json.dumps({"models": {}, "excluded_models": [], "last_used_model": None})
        )
    cache = _write_cache(tmp_path, _cache_blob(groups))
    return mr.ModelRegistry(
        registry_dir=str(reg_dir),
        db_path=str(tmp_path / "none.db"),
        cache_path=cache,
        team_settings_path=str(tmp_path / "no_team_settings.bin"),
    )


_REASONING_GROUPS = [
    _group_blob(internal_id=b"lead-high", group_label=b"Lead High",
                alias=b"lead", base_name=b"Lead",
                labels=[("Effort", "High"), ("Fast Mode", None)]),
    _group_blob(internal_id=b"lead-high-priority", group_label=b"Lead High Priority",
                alias=b"lead", base_name=b"Lead",
                labels=[("Reasoning Effort", "XHigh"), ("Fast Mode", 1)]),
    _group_blob(internal_id=b"sk-medium", group_label=b"SK Medium",
                alias=b"sk", base_name=b"SK",
                labels=[("Reasoning Effort", "Medium")]),
    _group_blob(internal_id=b"plain", group_label=b"Plain",
                alias=b"plain", base_name=b"Plain"),
]


def test_parse_model_cache_derives_reasoning_and_fast(tmp_path):
    path = _write_cache(tmp_path, _cache_blob(_REASONING_GROUPS))
    parsed, _ = mr._parse_model_cache(path)
    assert parsed["lead-high"]["reasoning"] == "high"
    assert parsed["lead-high-priority"]["reasoning"] == "xhigh"
    assert parsed["sk-medium"]["reasoning"] == "medium"
    assert parsed["plain"]["reasoning"] is None
    # Only "Fast Mode: 1" marks the fast variant; a bare "Fast Mode" label
    # merely advertises availability.
    assert parsed["lead-high"]["is_fast"] is False
    assert parsed["lead-high-priority"]["is_fast"] is True
    assert parsed["sk-medium"]["is_fast"] is False
    assert parsed["plain"]["is_fast"] is False
    for entry in parsed.values():
        assert entry["fusion"] is None


def test_parse_model_cache_derives_fusion_structure(tmp_path):
    fusion_labels = [("Lead", "Lead"), ("Effort", "High"), ("Sidekick", "SK Medium")]
    path = _write_cache(
        tmp_path,
        _cache_blob(_REASONING_GROUPS + [
            _group_blob(internal_id=b"fusion-lead-high-sidekick-sk-medium",
                        group_label=b"Fusion (Lead + SK Medium)",
                        alias=b"fusion", base_name=b"Fusion",
                        labels=fusion_labels),
            _group_blob(internal_id=b"fusion-lead-high-fast-sidekick-sk-medium",
                        group_label=b"Fusion (Lead Fast + SK Medium)",
                        alias=b"fusion", base_name=b"Fusion",
                        labels=fusion_labels + [("Fast Mode", 1)]),
        ]),
    )
    parsed, _ = mr._parse_model_cache(path)
    fusion = parsed["fusion-lead-high-sidekick-sk-medium"]
    assert fusion["reasoning"] == "high"
    assert fusion["is_fast"] is False
    assert fusion["fusion"] == {
        "lead_family": "lead",
        "lead_name": "Lead",
        "sidekick_id": "sk-medium",
        "sidekick_family": "sk",
        "sidekick_name": "SK",
        "sidekick_reasoning": "medium",
    }
    fast_fusion = parsed["fusion-lead-high-fast-sidekick-sk-medium"]
    assert fast_fusion["is_fast"] is True
    # "lead-high-fast" is not a cache entry; the -fast suffix is stripped to
    # resolve the family via "lead-high".
    assert fast_fusion["fusion"]["lead_family"] == "lead"


def test_reasoning_key_canonicalizes():
    assert mr._reasoning_key("No Thinking") == "none"
    assert mr._reasoning_key("None") == "none"
    assert mr._reasoning_key("X-High") == "xhigh"
    assert mr._reasoning_key("XHigh") == "xhigh"
    assert mr._reasoning_key("Max") == "max"
    assert mr._reasoning_key("Minimal") == "minimal"


def test_set_last_used_model_records_variants(tmp_path):
    groups = [
        _group_blob(internal_id=b"fam-medium", group_label=b"Fam Medium",
                    alias=b"fam", base_name=b"Fam",
                    labels=[("Effort", "Medium")]),
        _group_blob(internal_id=b"fam-high", group_label=b"Fam High",
                    alias=b"fam", base_name=b"Fam",
                    labels=[("Effort", "High")]),
        _group_blob(internal_id=b"sk-medium", group_label=b"SK Medium",
                    alias=b"sk", base_name=b"SK",
                    labels=[("Reasoning Effort", "Medium")]),
        _group_blob(internal_id=b"fusion-fam-high-sidekick-sk-medium",
                    group_label=b"Fusion (Fam + SK Medium)",
                    alias=b"fusion", base_name=b"Fusion",
                    labels=[("Lead", "Fam"), ("Effort", "High"), ("Sidekick", "SK Medium")]),
    ]
    reg = _custom_registry(tmp_path, groups)
    reg.set_last_used_model("fam-high")
    assert reg.get_last_used_variants()["fam"] == "fam-high"

    fusion_id = "fusion-fam-high-sidekick-sk-medium"
    reg.set_last_used_model(fusion_id)
    variants = reg.get_last_used_variants()
    assert variants["fusion:fam"] == fusion_id
    assert variants["sidekick:sk"] == "sk-medium"

    # A fresh registry over the same directory/cache sees the persisted map.
    reg2 = _custom_registry(tmp_path, groups)
    persisted = reg2.get_last_used_variants()
    assert persisted["fam"] == "fam-high"
    assert persisted["fusion:fam"] == fusion_id
    assert persisted["sidekick:sk"] == "sk-medium"


def test_bulk_exclude_include(tmp_path):
    reg = _registry(tmp_path, excluded=[])
    reg.exclude_models(["fam-medium", "fam-high"])
    excluded = {m["id"] for m in reg.get_all_models() if m["excluded"]}
    assert {"fam-medium", "fam-high"} <= excluded
    reg.include_models(["fam-high"])
    excluded = {m["id"] for m in reg.get_all_models() if m["excluded"]}
    assert "fam-high" not in excluded
    assert "fam-medium" in excluded


def test_get_all_models_exposes_new_fields(tmp_path):
    reg = _registry(tmp_path, excluded=[])
    for model in reg.get_all_models():
        assert "reasoning" in model
        assert "is_fast" in model
        assert "fusion" in model


def _envelope_file(path, payload, fetched, mtime):
    path.write_text(json.dumps({
        "payload": base64.b64encode(payload).decode(), "fetched_at_secs": fetched,
    }))
    os.utime(path, (mtime, mtime))
    return str(path)


def _auto_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "_cli_dir", lambda: str(tmp_path))
    return mr.ModelRegistry(registry_dir=str(tmp_path / "reg"))


def _catalog(*ids):
    return {"families": [{
        "family_uid": "grok-4-7", "family_label": "Grok 4.7", "slug": "grok-4.7",
        "variants": [{
            "model_uid": mid, "label": "Grok 4.7 " + mid.rsplit("-", 1)[-1].capitalize(),
            "max_context_tokens": 500_000, "max_output_tokens": 100_000,
            "cost_summary": "$2 / 1M Input · $0.5 / 1M Cached input · $6 / 1M Output",
            "is_new": True,
        } for mid in ids],
    }]}


def _cli_process(monkeypatch, data, returncode=0):
    process = MagicMock()
    process.returncode = returncode
    stdout = data if isinstance(data, bytes) else json.dumps(data).encode()
    process.communicate = AsyncMock(return_value=(stdout, b""))
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(mr.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(mr, "_find_devin_binary", lambda: "fake-devin")
    return launch, process


def test_cache_discovery_handles_filename_changes_and_copied_files(tmp_path, monkeypatch):
    blob = _cache_blob([_group_blob()])
    old = _envelope_file(tmp_path / "model_configs_v5.old.bin", blob, 100, 900)
    new = _envelope_file(tmp_path / "model-configs-v99-new.cache", blob, 200, 300)
    team_old = _envelope_file(tmp_path / "team_settings.old.bin", _str(7, "wrong-model"), 100, 900)
    team_new = _envelope_file(tmp_path / "team-settings-v99.json", _str(7, "test-model-medium"), 200, 300)
    (tmp_path / "model_configs_directory.bin").mkdir()
    assert mr._discover_model_cache_files(str(tmp_path)) == [new, old]
    assert mr._discover_team_settings_files(str(tmp_path)) == [team_new, team_old]
    reg = _auto_registry(tmp_path, monkeypatch)
    assert reg.get_cache_info() == {"path": new, "updated_at": 200.0}
    assert reg._active_team_settings_path == team_new
    assert [m["id"] for m in reg.get_selectable_models()] == ["test-model-medium"]


def test_cache_rotation_and_corrupt_newest_candidate(tmp_path, monkeypatch):
    old = _envelope_file(tmp_path / "model_configs_v5.old.bin", _cache_blob([_group_blob()]), 100, 100)
    reg = _auto_registry(tmp_path, monkeypatch)
    broken = tmp_path / "model_configs_v100.new.json"
    broken.write_text('{"payload": "not-base64", "fetched_at_secs": 300}')
    reg.refresh()
    assert reg.get_cache_info()["path"] == old
    new = _envelope_file(
        tmp_path / "model_config_v99.cache",
        _cache_blob([_group_blob(internal_id=b"grok-4-7-high")]), 200, 200,
    )
    reg.refresh()
    assert reg.get_cache_info()["path"] == new
    assert "grok-4-7-high" in {m["id"] for m in reg.get_selectable_models()}


@pytest.mark.parametrize("timestamp", ["bad", None, True, float("nan"), float("inf"), -1])
def test_bad_envelope_timestamp_uses_mtime(tmp_path, timestamp):
    path = _envelope_file(tmp_path / "model_configs.json", b"x", timestamp, 123)
    assert mr._read_envelope_fetched_at(path) is None
    assert mr._sort_cache_files([path]) == [path]


def test_cache_envelope_can_have_leading_whitespace(tmp_path):
    path = tmp_path / "model_configs.json"
    path.write_text(' \n' + json.dumps({"payload": base64.b64encode(_cache_blob([_group_blob()])).decode(), "fetched_at_secs": 123}))
    assert mr._read_envelope_fetched_at(str(path)) == 123
    assert "test-model-medium" in mr._parse_model_cache(str(path))[0]


@pytest.mark.parametrize("data", [None, [], {}, {"families": []}, {"families": [None]},
                                  {"families": [{"variants": [None]}]},
                                  {"families": [{"variants": [{"model_uid": 123}]}]}])
def test_cli_catalog_rejects_unexpected_schema(data):
    with pytest.raises(ValueError):
        mr._parse_cli_model_list(data)


@pytest.mark.asyncio
async def test_live_catalog_discovers_models_missing_from_caches(tmp_path, monkeypatch):
    _write_cache(tmp_path, _cache_blob([_group_blob()]))
    (tmp_path / "team_settings.bin").write_bytes(_str(7, "test-model-medium"))
    reg = _auto_registry(tmp_path, monkeypatch)
    assert "grok-4-7-high" not in reg._data["models"]
    launch, _ = _cli_process(monkeypatch, _catalog("grok-4-7-high", "grok-4-7-xhigh"))
    await reg.refresh_from_cli()
    launch.assert_awaited_once()
    assert launch.call_args.args == ("fake-devin", "models", "list", "--format", "json")
    models = {m["id"]: m for m in reg.get_selectable_models()}
    assert set(models) == {"grok-4-7-high", "grok-4-7-xhigh"}
    assert models["grok-4-7-high"]["reasoning"] == "high"
    assert models["grok-4-7-xhigh"]["reasoning"] == "xhigh"
    assert models["grok-4-7-high"]["family_name"] == "Grok 4.7"
    assert models["grok-4-7-high"]["cached_input_price"] == 0.5
    assert reg.get_model("grok-4-7-high")["context_window"] == 500_000
    assert models["grok-4-7-high"]["label_tags"] == ["500K"]
    assert models["grok-4-7-high"]["is_new"] is True
    assert reg.get_cache_info()["source"] == "devin models list"
    assert reg._data["models"]["test-model-medium"]["available"] is False
    # A regular cache refresh cannot overwrite authoritative CLI availability.
    reg.refresh(force=True)
    assert {m["id"] for m in reg.get_selectable_models()} == set(models)


@pytest.mark.asyncio
async def test_cli_catalog_preserves_cache_capabilities_and_user_choices(tmp_path, monkeypatch):
    _write_cache(tmp_path, _cache_blob([_group_blob(
        internal_id=b"grok-4-7-high", alias=b"grok-4-7", base_name=b"Grok 4.7",
    )]))
    reg = _auto_registry(tmp_path, monkeypatch)
    reg.acknowledge_new_model("grok-4-7-high")
    reg.exclude_model("grok-4-7-high")
    _cli_process(monkeypatch, _catalog("grok-4-7-high"))
    await reg.refresh_from_cli()
    entry = next(m for m in reg.get_all_models() if m["id"] == "grok-4-7-high")
    assert entry["vision"] is True
    assert entry["excluded"] is True
    assert entry["is_new"] is False
    assert entry["label_tags"] == ["500K"]  # No stale 1M cache tag.
    assert entry["cached_input_price"] == 0.5
    assert reg.get_selectable_models() == []
    # Later CLI versions can move the private metadata cache completely.
    # Known capabilities and preferences still survive the next restart.
    (tmp_path / "model_configs_v5.deadbeef.bin").unlink()
    reloaded = _auto_registry(tmp_path, monkeypatch)
    entry = next(m for m in reloaded.get_all_models() if m["id"] == "grok-4-7-high")
    assert entry["vision"] is True
    assert entry["excluded"] is True
    assert entry["available"] is True
    assert entry["is_new"] is False


@pytest.mark.asyncio
async def test_cli_catalog_survives_restart_and_unavailable_cli(tmp_path, monkeypatch):
    reg = _auto_registry(tmp_path, monkeypatch)
    _cli_process(monkeypatch, _catalog("grok-4-7-high"))
    await reg.refresh_from_cli()
    reg.exclude_model("grok-4-7-high")
    reg = _auto_registry(tmp_path, monkeypatch)
    updated = reg.get_cache_info()["updated_at"]
    monkeypatch.setattr(mr.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError))
    await reg.refresh_from_cli()
    model = next(m for m in reg.get_all_models() if m["id"] == "grok-4-7-high")
    assert model["available"] is True
    assert model["excluded"] is True
    assert reg.get_cache_info()["updated_at"] == updated


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout,code", [(b"unknown subcommand", 2), (b"not JSON", 0),
                                        (b'{"families": []}', 0)])
async def test_failed_cli_refresh_keeps_cache_fallback(tmp_path, monkeypatch, stdout, code):
    _write_cache(tmp_path, _cache_blob([_group_blob()]))
    reg = _auto_registry(tmp_path, monkeypatch)
    _cli_process(monkeypatch, stdout, code)
    await reg.refresh_from_cli()
    assert [m["id"] for m in reg.get_selectable_models()] == ["test-model-medium"]
    assert "cli_catalog" not in reg._data


@pytest.mark.asyncio
async def test_cli_refresh_throttles_concurrent_requests_and_retries_later(tmp_path, monkeypatch):
    reg = _auto_registry(tmp_path, monkeypatch)
    launch, _ = _cli_process(monkeypatch, _catalog("grok-4-7-high"))
    await asyncio.gather(reg.refresh_from_cli(), reg.refresh_from_cli())
    assert launch.await_count == 1
    reg._cli_last_attempt -= mr._CLI_REFRESH_INTERVAL + 1
    await reg.refresh_from_cli()
    assert launch.await_count == 2


@pytest.mark.asyncio
async def test_cli_timeout_kills_process_and_preserves_models(tmp_path, monkeypatch):
    reg = _auto_registry(tmp_path, monkeypatch)
    launch, process = _cli_process(monkeypatch, _catalog("grok-4-7-high"))
    process.returncode = None
    process.communicate.side_effect = [asyncio.TimeoutError, (b"", b"")]
    before = reg.get_all_models()
    await reg.refresh_from_cli()
    process.kill.assert_called_once()
    assert process.communicate.await_count == 2
    assert reg.get_all_models() == before
    await reg.refresh_from_cli()
    assert launch.await_count == 1  # Failures are throttled too.


@pytest.mark.asyncio
async def test_explicit_cache_path_never_launches_cli(tmp_path, monkeypatch):
    reg = _custom_registry(tmp_path, [_group_blob()])
    launch, _ = _cli_process(monkeypatch, _catalog("grok-4-7-high"))
    await reg.refresh_from_cli()
    launch.assert_not_awaited()
    assert [m["id"] for m in reg.get_selectable_models()] == ["test-model-medium"]


@pytest.mark.asyncio
async def test_cli_only_catalog_resolves_fusion_and_free_models(tmp_path, monkeypatch):
    reg = _auto_registry(tmp_path, monkeypatch)
    data = {"families": [
        {"family_uid": "lead", "family_label": "Lead", "variants": [
            {"model_uid": "lead-high", "label": "Lead High", "cost_summary": "Free"},
            {"model_uid": "lead-high-fast", "label": "Lead High Fast"},
        ]},
        {"family_uid": "sk", "family_label": "SK", "variants": [
            {"model_uid": "sk-medium", "label": "SK Medium"},
        ]},
        {"family_uid": "fast-family", "family_label": "Fast Model", "variants": [
            {"model_uid": "fast-family", "label": "Fast Model"},
        ]},
        {"family_uid": "fusion", "family_label": "Fusion", "variants": [
            {"model_uid": "fusion-lead-high-fast-sidekick-sk-medium", "label": "Fusion (Lead + SK)"},
        ]},
    ]}
    _cli_process(monkeypatch, data)
    await reg.refresh_from_cli()
    models = {m["id"]: m for m in reg.get_selectable_models()}
    assert models["lead-high"]["tags"] == ["Free"]
    assert models["lead-high"]["input_price"] == 0.0
    assert models["lead-high-fast"]["is_fast"] is True
    assert models["fast-family"]["is_fast"] is False
    fusion = models["fusion-lead-high-fast-sidekick-sk-medium"]
    assert fusion["is_fast"] is True
    assert fusion["reasoning"] == "high"
    assert fusion["fusion"]["lead_family"] == "lead"
    assert fusion["fusion"]["sidekick_reasoning"] == "medium"
