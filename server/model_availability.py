"""Catalogue merging, availability, and saved choices without file/process I/O."""

from __future__ import annotations

import logging
import re
from typing import TypedDict

from .model_catalog import _context_tag, _pretty_name, _resolve_fusion_metadata


class RegistryState(TypedDict, total=False):
    """Persisted fields read or updated by a catalogue refresh."""

    models: dict[str, dict]
    excluded_models: list[str]
    last_used_model: str | None
    last_used_variants: dict[str, str]


# Internal model ids that appear in the CLI's model cache / team settings but
# are not selectable models: the ACP agent rejects them with "Model not found".
_INTERNAL_MODEL_IDS = frozenset({"subagent-default", "swe-check"})


def refresh_catalogue(
    data: RegistryState,
    cached: dict[str, dict],
    id_map: dict[str, str],
    allowed_ids: set[str],
    *,
    cli_models: dict[str, dict],
    fallback_models: dict[str, dict],
    logger: logging.Logger,
) -> None:
    """Apply catalogue metadata and migrate saved choices in their existing order.

    CLI overlays mutate ``cached`` and ``id_map``. Saved state is updated in
    stages, so a failure can leave earlier updates visible to the registry.
    The caller retains cache selection, DB discovery, and persistence ownership.
    """
    if cli_models:
        # Availability comes from the same command the user sees in
        # Devin. An old team cache must not hide newly released models.
        # SWE-2 Lightning is omitted by `models list` in CLI 11.3. Retain
        # only cached, team-allowed variants whose standard model is listed.
        lightning_ids = {
            mid for mid in allowed_ids
            if mid.startswith("swe-2-") and mid.endswith("-lightning")
            and cached.get(mid, {}).get("family") == "swe-2"
            and mid.removesuffix("-lightning") in cli_models
        }
        _overlay_cli_metadata(data["models"], cached, id_map, cli_models)
        allowed_ids = set(cli_models) | lightning_ids
        _resolve_fusion_metadata(cached)

    if not cached:
        # Without catalogue metadata, seed only missing entries and leave choices unmigrated.
        for mid, info in fallback_models.items():
            if mid not in data["models"]:
                data["models"][mid] = {
                    **info,
                    "is_new": False,
                    "available": False,
                    "input_price": None,
                    "cached_input_price": None,
                    "output_price": None,
                }
        return

    # Build a reverse map (alias -> [internal_ids]) to migrate state from
    # old alias-keyed registry entries to the new internal-ID entries.
    alias_to_internals: dict[str, list[str]] = {}
    for internal, alias in id_map.items():
        alias_to_internals.setdefault(alias, []).append(internal)

    old_models = data.get("models", {})
    candidate_ids = _available_ids(cached, allowed_ids, logger)
    new_models = _merge_models(cached, id_map, old_models, candidate_ids, alias_to_internals)
    data["models"] = new_models
    _migrate_choices(data, new_models, id_map, alias_to_internals)


def _overlay_cli_metadata(
    old_models: dict[str, dict],
    cached: dict[str, dict],
    id_map: dict[str, str],
    cli_models: dict[str, dict],
) -> None:
    """Overlay public fields on cache and saved metadata, retaining capabilities."""
    for mid, info in cli_models.items():
        # Keep known capabilities if a private cache is moved or
        # renamed beyond recognition. The public list omits vision.
        metadata = {**old_models.get(mid, {}), **cached.get(mid, {})}
        cache_tags = metadata.get("tags", [])
        tags = [tag for tag in cache_tags if tag != "New"
                and not (tag == "Free" and "input_price" in info)]
        cached[mid] = {
            "vision": False, "label_tags": [], "reasoning": None,
            "is_fast": False, "fusion": None,
            **metadata, **info,
            "tags": list(dict.fromkeys([*tags, *info["tags"]])),
        }
        tag = _context_tag(cached[mid].get("context_window", 0))
        if tag:
            cached[mid]["label_tags"] = [
                item for item in cached[mid]["label_tags"] if not re.fullmatch(r"\d+[KM]", item)
            ] + [tag]
        id_map[mid] = info["family"]


def _available_ids(
    cached: dict[str, dict], allowed_ids: set[str], logger: logging.Logger,
) -> set[str]:
    # Determine which internal IDs are selectable. ``allowed_ids`` from
    # team_settings are already internal IDs (field 22 values).
    if allowed_ids:
        candidate_ids = {mid for mid in allowed_ids if mid in cached}
    else:
        logger.warning("Team settings unavailable; using all cached variants")
        candidate_ids = set(cached)

    # Internal reviewers, ACP roles, and fusion sidekicks are not standalone choices.
    # Filter them before merging so a refresh cannot make them selectable, regardless
    # of the user's exclusions.
    return {
        mid
        for mid in candidate_ids
        if not mid.endswith("-review")
        and mid not in _INTERNAL_MODEL_IDS
        and not mid.startswith("fusion-sidekick-")
    }


def _merge_models(
    cached: dict[str, dict],
    id_map: dict[str, str],
    old_models: dict[str, dict],
    candidate_ids: set[str],
    alias_to_internals: dict[str, list[str]],
) -> dict[str, dict]:
    # Identify old alias keys that have been replaced by internal-ID
    # entries in the new cache — their state is migrated, so they should
    # NOT be carried over as unavailable stubs.
    replaced_aliases = {
        alias
        for alias, internals in alias_to_internals.items()
        if any(i in candidate_ids for i in internals)
    }

    new_models: dict[str, dict] = {}
    for mid in candidate_ids:
        info = cached[mid]
        existing = old_models.get(mid)
        alias = id_map.get(mid)

        # Preserve acknowledgement from either the variant or its legacy family alias,
        # so a DB-discovered stub cannot mark an acknowledged family as new again.
        is_new = True
        if existing is not None:
            is_new = existing.get("is_new", True)
        if alias and alias != mid:
            old_alias_entry = old_models.get(alias)
            if old_alias_entry is not None and not old_alias_entry.get("is_new", True):
                is_new = False

        new_models[mid] = {
            "display_name": info["display_name"],
            "family": info.get("family", mid),
            "family_name": info.get("family_name", info["display_name"]),
            "vision": info["vision"],
            "tags": info["tags"],
            "label_tags": info.get("label_tags", []),
            "description": info["description"],
            "is_new": is_new,
            "available": True,
            "input_price": info.get("input_price"),
            "cached_input_price": info.get("cached_input_price"),
            "output_price": info.get("output_price"),
            "context_window": info.get("context_window", 0),
            "reasoning": info.get("reasoning"),
            "is_fast": info.get("is_fast", False),
            "fusion": info.get("fusion"),
        }

    # Keep saved entries outside the candidate set as unavailable to preserve user state.
    # Drop replaced aliases, reviewers, and fusion sidekicks, but keep ACP-internal roles.
    for old_mid, old_info in old_models.items():
        if old_mid in new_models or old_mid in replaced_aliases:
            continue
        if old_mid.endswith("-review") or old_mid.startswith("fusion-sidekick-"):
            continue
        carried = dict(old_info)
        carried["available"] = False
        carried.setdefault("family", old_mid)
        carried.setdefault("family_name", old_info.get("display_name", _pretty_name(old_mid)))
        carried.setdefault("label_tags", [])
        carried.setdefault("input_price", None)
        carried.setdefault("cached_input_price", None)
        carried.setdefault("output_price", None)
        carried.setdefault("reasoning", None)
        carried.setdefault("is_fast", False)
        carried.setdefault("fusion", None)
        new_models[old_mid] = carried
    return new_models


def _migrate_choices(
    data: RegistryState,
    new_models: dict[str, dict],
    id_map: dict[str, str],
    alias_to_internals: dict[str, list[str]],
) -> None:
    new_excluded: list[str] = []
    for excluded_id in data.get("excluded_models", []):
        if excluded_id in new_models or excluded_id in id_map:
            # Check literal IDs before family aliases: ``fusion`` can mean either.
            # Membership in id_map keeps an internal ID literal even when it is not a
            # current candidate, preventing one exclusion from hiding its whole family.
            if excluded_id not in new_excluded:
                new_excluded.append(excluded_id)
            continue
        internals = alias_to_internals.get(excluded_id, [])
        if internals and any(i in new_models for i in internals):
            # Old alias-based exclusion → apply to all variants in the family
            for internal in internals:
                if internal in new_models and internal not in new_excluded:
                    new_excluded.append(internal)
    data["excluded_models"] = new_excluded

    # Keep choices for unavailable models; remove only IDs absent from new_models.
    variants = data.get("last_used_variants")
    if variants:
        data["last_used_variants"] = {
            key: mid for key, mid in variants.items() if mid in new_models
        }

    # Migrate last_used_model from an old alias to the first available variant
    # in ID-map order. Recommendation tags do not affect this selection.
    last_used = data.get("last_used_model")
    if last_used and last_used not in new_models:
        internals = alias_to_internals.get(last_used, [])
        replacement = next(
            (i for i in internals if new_models.get(i, {}).get("available")),
            None,
        )
        if replacement:
            data["last_used_model"] = replacement
