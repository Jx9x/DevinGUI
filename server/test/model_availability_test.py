"""Catalogue precedence and saved-choice migration, using synthetic metadata."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from server import model_registry as mr


def _model(**updates):
    return {
        "display_name": "Cached Model",
        "family": "family",
        "family_name": "Family",
        "vision": True,
        "tags": ["Recommended"],
        "label_tags": ["1M"],
        "description": "Cache description",
        **updates,
    }


def _cli_model(**updates):
    return {
        "display_name": "Public Model",
        "family": "family",
        "family_name": "Public Family",
        "description": "Public description",
        "tags": ["New"],
        **updates,
    }


def _registry(monkeypatch, *, cached=None, id_map=None, allowed=None, cli=None, **state):
    """Construct only the refresh boundary, without profiles, files, or processes."""
    reg = mr.ModelRegistry.__new__(mr.ModelRegistry)
    reg._cache_candidates = ["cache"]
    reg._team_settings_candidates = ["team"]
    reg._active_cache_path = "previous-cache"
    reg._active_team_settings_path = "previous-team"
    reg._id_map = {"previous": "alias"}
    reg._cli_models = {} if cli is None else cli
    reg._data = {
        "models": {}, "excluded_models": [],
        "last_used_model": None, "last_used_variants": {},
        **state,
    }
    monkeypatch.setattr(mr, "_sort_cache_files", lambda paths: paths)
    monkeypatch.setattr(mr, "_parse_model_cache", lambda path: (
        {} if cached is None else cached, {} if id_map is None else id_map,
    ))
    monkeypatch.setattr(mr, "_parse_allowed_model_ids", lambda path: (
        set() if allowed is None else allowed
    ))
    return reg


def test_refresh_selects_first_parseable_cache_and_nonempty_team_list(monkeypatch):
    cached = {"variant": _model()}
    id_map = {"variant": "family"}
    reg = _registry(monkeypatch)
    reg._cache_candidates = ["broken", "selected", "unused"]
    reg._team_settings_candidates = ["empty", "allowed", "unused-team"]
    calls = []

    def parse_cache(path):
        calls.append(path)
        assert reg._active_cache_path is None
        assert reg._id_map == {"previous": "alias"}
        return (cached, id_map) if path == "selected" else ({}, {"ignored": "alias"})

    def parse_team(path):
        calls.append(path)
        assert reg._active_cache_path == "selected"
        assert reg._id_map is id_map
        assert reg._active_team_settings_path is None
        return {"variant"} if path == "allowed" else set()

    monkeypatch.setattr(mr, "_parse_model_cache", parse_cache)
    monkeypatch.setattr(mr, "_parse_allowed_model_ids", parse_team)
    reg._refresh_from_cache()
    assert calls == ["broken", "selected", "empty", "allowed"]
    assert reg._active_team_settings_path == "allowed"
    assert reg._data["models"]["variant"]["available"] is True


def test_cache_parse_error_resets_only_the_cache_selection(monkeypatch):
    reg = _registry(monkeypatch)
    old_models = reg._data["models"]
    failure = ValueError("decoder failed")
    monkeypatch.setattr(mr, "_parse_model_cache", Mock(side_effect=failure))
    with pytest.raises(ValueError) as raised:
        reg._refresh_from_cache()
    assert raised.value is failure
    assert reg._active_cache_path is None
    assert reg._active_team_settings_path == "previous-team"
    assert reg._id_map == {"previous": "alias"}
    assert reg._data["models"] is old_models


def test_team_parse_error_retains_the_selected_cache_and_map(monkeypatch):
    id_map = {"variant": "family"}
    reg = _registry(monkeypatch, cached={"variant": _model()}, id_map=id_map)
    old_models = reg._data["models"]
    failure = ValueError("team decoder failed")
    monkeypatch.setattr(mr, "_parse_allowed_model_ids", Mock(side_effect=failure))
    with pytest.raises(ValueError) as raised:
        reg._refresh_from_cache()
    assert raised.value is failure
    assert reg._active_cache_path == "cache"
    assert reg._active_team_settings_path is None
    assert reg._id_map is id_map
    assert reg._data["models"] is old_models


def test_cli_metadata_overlays_cache_then_saved_metadata_without_losing_capabilities(monkeypatch):
    cached = {"variant": _model(
        tags=["New", "Free", "Recommended", "Cached", "Recommended"],
        label_tags=["1M", "32K", "Vision", "Info 1M"],
        input_price=9, cached_input_price=2, output_price=7,
        context_window=1_000_000, reasoning="high", is_fast=True,
    )}
    saved = {"variant": _model(
        vision=False, output_price=99, is_new=False, saved_field="retained",
    )}
    cli = {"variant": _cli_model(
        input_price=3, context_window=500_000,
        tags=["New", "Recommended", "Public", "New"],
    )}
    original_cli, original_saved = deepcopy(cli), deepcopy(saved)
    id_map = {"variant": "old-family"}
    reg = _registry(monkeypatch, cached=cached, id_map=id_map, allowed={"other"},
                    cli=cli, models=saved)
    reg._refresh_from_cache()
    entry = reg._data["models"]["variant"]
    assert entry == {
        "display_name": "Public Model", "family": "family",
        "family_name": "Public Family", "vision": True,
        "tags": ["Recommended", "Cached", "New", "Public"],
        "label_tags": ["Vision", "Info 1M", "500K"],
        "description": "Public description", "is_new": False, "available": True,
        "input_price": 3, "cached_input_price": 2, "output_price": 7,
        "context_window": 500_000, "reasoning": "high", "is_fast": True,
        "fusion": None,
    }
    assert cached["variant"]["saved_field"] == "retained"
    assert id_map == {"variant": "family"}
    assert reg._id_map is id_map
    assert cli == original_cli
    assert saved == original_saved


@pytest.mark.parametrize("prices,expected_tags", [
    ({}, ["Free", "Cached"]),
    ({"input_price": None}, ["Cached"]),
    ({"input_price": 0}, ["Cached"]),
    ({"cached_input_price": 1}, ["Free", "Cached"]),
    ({"output_price": 1}, ["Free", "Cached"]),
])
def test_cli_free_tag_removal_depends_on_input_price_presence(monkeypatch, prices, expected_tags):
    reg = _registry(monkeypatch,
                    cached={"variant": _model(tags=["New", "Free", "Cached"])},
                    cli={"variant": _cli_model(tags=[], **prices)})
    reg._refresh_from_cache()
    assert reg._data["models"]["variant"]["tags"] == expected_tags


def test_cli_only_entry_uses_defaults_and_saved_capabilities(monkeypatch):
    saved = {"known": _model(vision=True, label_tags=["2M", "Vision"], is_new=False)}
    reg = _registry(monkeypatch, models=saved, allowed={"unlisted"}, cli={
        "known": _cli_model(context_window=0, tags=[]),
        "new": _cli_model(tags=[]),
    })
    reg._refresh_from_cache()
    known, new = reg._data["models"]["known"], reg._data["models"]["new"]
    assert known["vision"] is True
    assert known["label_tags"] == ["2M", "Vision"]
    assert known["is_new"] is False
    assert new["vision"] is False
    assert new["label_tags"] == []
    assert new["is_new"] is True
    assert new["reasoning"] is None
    assert new["is_fast"] is False
    assert new["fusion"] is None
    assert new["input_price"] is None
    assert {mid for mid, info in reg._data["models"].items() if info["available"]} == {"known", "new"}


def test_cli_fusion_resolution_sees_unselectable_cache_components(monkeypatch):
    cached = {
        "lead-high": _model(reasoning="high", is_fast=False),
        "fusion-sidekick-sk": _model(
            family="sk", family_name="Sidekick", reasoning="medium", is_fast=True,
        ),
    }
    combo = "fusion-lead-high-sidekick-fusion-sidekick-sk"
    reg = _registry(monkeypatch, cached=cached, cli={combo: _cli_model(family="fusion")})
    reg._refresh_from_cache()
    assert set(reg._data["models"]) == {combo}
    entry = reg._data["models"][combo]
    assert entry["reasoning"] == "high"
    assert entry["is_fast"] is True
    assert entry["fusion"] == {
        "lead_family": "family", "lead_name": "Family",
        "sidekick_id": "fusion-sidekick-sk", "sidekick_family": "sk",
        "sidekick_name": "Sidekick", "sidekick_reasoning": "medium",
    }


def test_cli_overlay_error_preserves_partial_cache_and_id_map_updates(monkeypatch):
    cached, id_map = {"seed": _model()}, {"seed": "seed"}
    reg = _registry(monkeypatch, cached=cached, id_map=id_map, cli={
        "first": _cli_model(), "bad": _cli_model(tags=None),
    })
    old_models = reg._data["models"]
    with pytest.raises(TypeError):
        reg._refresh_from_cache()
    assert set(cached) == {"seed", "first"}
    assert id_map == {"seed": "seed", "first": "family"}
    assert reg._id_map is id_map
    assert reg._data["models"] is old_models


def test_empty_catalogue_adds_fallbacks_without_replacing_saved_state(monkeypatch, caplog):
    existing = _model(available=True, is_new=True, input_price=12)
    models = {"adaptive": existing, "custom": _model()}
    excluded, variants = ["missing", "adaptive", "adaptive"], {"family": "missing"}
    reg = _registry(monkeypatch, models=models, excluded_models=excluded,
                    last_used_model="unknown", last_used_variants=variants,
                    allowed={"missing"})
    reg._refresh_from_cache()
    assert reg._data["models"] is models
    assert models["adaptive"] is existing
    assert models["adaptive"]["available"] is True
    assert models["adaptive"]["input_price"] == 12
    assert models["custom"]["vision"] is True
    assert set(models) == set(mr.FALLBACK_MODELS) | {"custom"}
    for mid in set(mr.FALLBACK_MODELS) - {"adaptive"}:
        assert models[mid]["is_new"] is False
        assert models[mid]["available"] is False
        assert models[mid]["input_price"] is None
        assert models[mid]["cached_input_price"] is None
        assert models[mid]["output_price"] is None
    assert reg._data["excluded_models"] is excluded
    assert reg._data["last_used_variants"] is variants
    assert reg._data["last_used_model"] == "unknown"
    assert not caplog.records


@pytest.mark.parametrize("allowed,available,warnings", [
    (set(), {"first", "second"}, 1),
    ({"first", "unknown"}, {"first"}, 0),
    ({"unknown"}, set(), 0),
])
def test_team_availability_and_missing_settings_warning(monkeypatch, caplog, allowed, available, warnings):
    reg = _registry(monkeypatch, cached={"first": _model(), "second": _model()}, allowed=allowed)
    reg._refresh_from_cache()
    assert set(reg._data["models"]) == available
    assert [(record.name, record.getMessage()) for record in caplog.records] == [
        ("server.model_registry", "Team settings unavailable; using all cached variants")
    ] * warnings


def test_internal_roles_reviewers_and_sidekicks_have_distinct_carryover_rules(monkeypatch):
    ids = ["model-review", "subagent-default", "swe-check", "fusion-sidekick-sk",
           "review-model", "fusion-lead-sidekick-sk"]
    models = {mid: _model() for mid in ids}
    original = deepcopy(models)
    reg = _registry(monkeypatch, cached=deepcopy(models), models=models, allowed=set(ids))
    reg._refresh_from_cache()
    assert set(reg._data["models"]) == {
        "subagent-default", "swe-check", "review-model", "fusion-lead-sidekick-sk",
    }
    assert reg._data["models"]["subagent-default"]["available"] is False
    assert reg._data["models"]["swe-check"]["available"] is False
    assert reg._data["models"]["review-model"]["available"] is True
    assert reg._data["models"]["fusion-lead-sidekick-sk"]["available"] is True
    assert models == original


@pytest.mark.parametrize("existing,alias_entry,expected", [
    (None, None, True), ({}, None, True),
    ({"is_new": False}, {"is_new": True}, False),
    ({"is_new": True}, {"is_new": False}, False),
    ({"is_new": True}, {}, True), ({"is_new": None}, None, None),
    (None, {"is_new": None}, False), (None, {"is_new": 0}, False),
])
def test_acknowledgement_migrates_from_alias_without_replacing_existing_flags(
    monkeypatch, existing, alias_entry, expected,
):
    old_models = {}
    if existing is not None:
        old_models["variant"] = existing
    if alias_entry is not None:
        old_models["family"] = alias_entry
    reg = _registry(monkeypatch, cached={"variant": _model()},
                    id_map={"variant": "family"}, models=old_models, allowed={"variant"})
    reg._refresh_from_cache()
    assert reg._data["models"]["variant"]["is_new"] is expected
    assert "family" not in reg._data["models"]


def test_disappeared_entries_keep_custom_metadata_and_receive_only_missing_defaults(monkeypatch):
    old = {"display_name": "Old", "is_new": False, "custom": {"value": 1},
           "input_price": 9, "family_name": None, "label_tags": None}
    reg = _registry(monkeypatch, cached={"new": _model()}, models={"old": old}, allowed={"new"})
    reg._refresh_from_cache()
    carried = reg._data["models"]["old"]
    assert carried is not old
    assert carried["custom"] is old["custom"]
    assert "available" not in old
    assert carried["available"] is False
    assert carried["family"] == "old"
    assert carried["family_name"] is None
    assert carried["label_tags"] is None
    assert carried["input_price"] == 9
    assert carried["cached_input_price"] is None
    assert carried["output_price"] is None
    assert carried["reasoning"] is None
    assert carried["is_fast"] is False
    assert carried["fusion"] is None
    assert "context_window" not in carried


def test_alias_choices_migrate_in_map_order_and_literal_internal_ids_stay_literal(monkeypatch):
    ids = ["first", "recommended", "fusion", "fusion-combo"]
    cached = {mid: _model(tags=["Recommended"] if mid == "recommended" else []) for mid in ids}
    id_map = {"first": "family", "recommended": "family",
              "fusion": "fusion", "fusion-combo": "fusion"}
    reg = _registry(monkeypatch, cached=cached, id_map=id_map,
                    allowed={"first", "recommended", "fusion-combo"},
                    models={"family": {"is_new": False}, "old": _model()},
                    excluded_models=["family", "first", "fusion", "family", "missing", "old"],
                    last_used_model="family", last_used_variants={
                        "family": "first", "old": "old", "alias": "family", "dead": "missing",
                    })
    reg._refresh_from_cache()
    assert reg._data["excluded_models"] == ["first", "recommended", "fusion", "old"]
    assert reg._data["last_used_model"] == "first"
    assert reg._data["last_used_variants"] == {"family": "first", "old": "old"}
    assert "family" not in reg._data["models"]
    assert "fusion" not in reg._data["models"]


def test_alias_is_carried_when_none_of_its_internal_ids_are_available(monkeypatch):
    reg = _registry(monkeypatch, cached={"variant": _model()},
                    id_map={"variant": "family"}, allowed={"unknown"},
                    models={"family": _model(is_new=False)},
                    excluded_models=["family"], last_used_model="family")
    reg._refresh_from_cache()
    assert set(reg._data["models"]) == {"family"}
    assert reg._data["models"]["family"]["available"] is False
    assert reg._data["excluded_models"] == ["family"]
    assert reg._data["last_used_model"] == "family"


@pytest.mark.parametrize("last_used", [None, "", "missing", "variant"])
def test_last_used_values_without_a_replacement_are_unchanged(monkeypatch, last_used):
    variants = {}
    reg = _registry(monkeypatch, cached={"variant": _model()}, allowed={"variant"},
                    last_used_model=last_used, last_used_variants=variants)
    reg._refresh_from_cache()
    assert reg._data["last_used_model"] == last_used
    assert reg._data["last_used_variants"] is variants


@pytest.mark.parametrize("missing_key", ["display_name", "vision", "tags", "description"])
def test_incomplete_metadata_fails_before_replacing_models_or_choices(monkeypatch, missing_key):
    info = _model()
    del info[missing_key]
    models, excluded = {}, ["family", "family"]
    reg = _registry(monkeypatch, cached={"variant": info}, allowed={"variant"},
                    models=models, excluded_models=excluded)
    with pytest.raises(KeyError) as raised:
        reg._refresh_from_cache()
    assert raised.value.args == (missing_key,)
    assert reg._data["models"] is models
    assert reg._data["excluded_models"] is excluded


def test_choice_error_keeps_partial_updates_and_skips_db_and_persistence(monkeypatch):
    reg = _registry(monkeypatch, cached={"variant": _model()},
                    id_map={"variant": "family"}, allowed={"variant"},
                    excluded_models=["family", "missing"], last_used_model="family",
                    last_used_variants=["malformed"])
    old_models = reg._data["models"]
    reg._source_signature = ("previous",)
    reg._rebuild_candidates = Mock()
    reg._get_source_signature = Mock(return_value=("current",))
    reg._discover_from_db = Mock()
    reg._save = Mock()
    with pytest.raises(AttributeError, match="items"):
        reg.refresh(force=True)
    assert reg._data["models"] is not old_models
    assert reg._data["models"]["variant"]["available"] is True
    assert reg._data["excluded_models"] == ["variant"]
    assert reg._data["last_used_model"] == "family"
    assert reg._data["last_used_variants"] == ["malformed"]
    assert reg._source_signature == ("previous",)
    reg._discover_from_db.assert_not_called()
    reg._save.assert_not_called()


def test_refresh_persists_after_catalogue_then_db_discovery(monkeypatch):
    reg = _registry(monkeypatch, cached={"variant": _model()}, allowed={"variant"})
    reg._source_signature = ("previous",)
    calls = []
    reg._rebuild_candidates = lambda: calls.append("rebuild")
    reg._get_source_signature = lambda: ("current",)

    def discover():
        calls.append("db")
        assert reg._data["models"]["variant"]["available"] is True
        assert reg._source_signature == ("previous",)
        reg._data["models"]["db-only"] = _model(available=False)

    def save():
        calls.append("save")
        assert reg._source_signature == ("current",)
        assert set(reg._data["models"]) == {"variant", "db-only"}

    reg._discover_from_db = discover
    reg._save = save
    reg.refresh(force=True)
    assert calls == ["rebuild", "db", "save"]


def test_policy_accepts_sparse_saved_state_and_retains_cache_metadata_identity():
    from server.model_availability import refresh_catalogue

    tags, labels = ["Recommended"], ["Vision"]
    cached = {"variant": _model(tags=tags, label_tags=labels)}
    original = deepcopy(cached)
    data = {"models": {}}
    logger = Mock()
    refresh_catalogue(data, cached, {}, {"variant"},
                      cli_models={}, fallback_models={}, logger=logger)
    assert cached == original
    assert data["models"]["variant"]["tags"] is tags
    assert data["models"]["variant"]["label_tags"] is labels
    assert data["excluded_models"] == []
    assert "last_used_model" not in data
    assert "last_used_variants" not in data
    logger.warning.assert_not_called()


def test_policy_can_build_an_available_catalogue_without_a_registry_or_cache():
    from server.model_availability import refresh_catalogue

    cached, id_map = {}, {}
    data = {"models": {}}
    logger = Mock()
    refresh_catalogue(data, cached, id_map, {"stale"},
                      cli_models={"new": _cli_model(context_window=250_000)},
                      fallback_models={"unused": _model()}, logger=logger)
    assert set(data["models"]) == {"new"}
    assert data["models"]["new"]["available"] is True
    assert data["models"]["new"]["label_tags"] == ["250K"]
    assert id_map == {"new": "family"}
    assert cached["new"]["vision"] is False
    logger.warning.assert_not_called()


def test_policy_uses_supplied_fallbacks_without_migrating_choices():
    from server.model_availability import refresh_catalogue

    existing = _model(available=True)
    models = {"existing": existing}
    excluded, variants = ["missing", "missing"], {"family": "missing"}
    data = {"models": models, "excluded_models": excluded, "last_used_variants": variants}
    fallback = {"provided": _model(vision=False), "existing": _model(vision=False)}
    original = deepcopy(fallback)
    logger = Mock()
    refresh_catalogue(data, {}, {}, set(),
                      cli_models={}, fallback_models=fallback, logger=logger)
    assert set(models) == {"existing", "provided"}
    assert models["existing"] is existing
    assert models["provided"]["available"] is False
    assert models["provided"]["vision"] is False
    assert data["excluded_models"] is excluded
    assert data["last_used_variants"] is variants
    assert fallback == original
    logger.warning.assert_not_called()


def test_policy_reports_missing_team_settings_through_the_supplied_logger(caplog):
    import logging

    from server.model_availability import refresh_catalogue

    logger = logging.getLogger("catalogue-owner")
    refresh_catalogue({"models": {}}, {"variant": _model()}, {}, set(),
                      cli_models={}, fallback_models={}, logger=logger)
    assert [(record.name, record.getMessage()) for record in caplog.records] == [
        ("catalogue-owner", "Team settings unavailable; using all cached variants"),
    ]
