"""Model registry for the Devin GUI.

Maintains a JSON file with model metadata (display name, vision capability,
pricing tags), an exclusion list, and the last-used model. Discovers models
through the CLI's public JSON catalog, its protobuf cache and the sessions DB,
marking new ones as ``is_new`` so the UI can show a "new model" indicator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from .paths import (
    cli_data_dir as _cli_dir,
    find_devin_binary as _find_devin_binary,
    gui_data_dir as _default_registry_dir,
)
from .model_availability import _INTERNAL_MODEL_IDS, refresh_catalogue
from .model_cache import (
    _discover_model_cache_files as _discover_model_cache_candidates,
    _discover_team_settings_files as _discover_team_settings_candidates,
    _envelope_timestamp,
    _parse_allowed_model_ids,
    _parse_model_cache as _load_model_cache,
    _read_envelope_fetched_at,
    _sort_cache_files,
)
# Retain existing parser/normalizer imports while using their canonical module.
from .model_catalog import (
    _context_tag,
    _decode_model_cache,
    _parse_cli_model_list,
    _pretty_name,
    _reasoning_key,
    _resolve_fusion_metadata,
)

logger = logging.getLogger(__name__)


def _default_model_cache_path() -> str:
    return os.path.join(_cli_dir(), "model_configs_v2.bin")


def _default_model_cache_fallback_path() -> str:
    return os.path.join(_cli_dir(), "model_configs_v4.bin")


def _default_team_settings_path() -> str:
    return os.path.join(_cli_dir(), "team_settings.bin")


# Preserve the CLI-directory and decoder hooks used by existing consumers.
def _discover_model_cache_files(cli_dir: str | None = None) -> list[str]:
    """Find model-config caches across version, separator and extension changes.

    This is an offline/older-CLI fallback. The supported ``models list``
    command is authoritative and does not depend on private cache filenames.
    """
    return _discover_model_cache_candidates(cli_dir or _cli_dir())


def _discover_team_settings_files(cli_dir: str | None = None) -> list[str]:
    """Find team-settings caches without fixing their suffix or extension."""
    return _discover_team_settings_candidates(cli_dir or _cli_dir())


def _parse_model_cache(bin_path: str) -> tuple[dict[str, dict], dict[str, str]]:
    """Parse cache metadata through the existing overridable decoder hook."""
    return _load_model_cache(bin_path, decode=_decode_model_cache)


# Legacy CLI aliases used when no catalogue metadata is available.
CLI_MODEL_IDS = [
    "adaptive", "claude-fable-5", "claude-haiku-4.5", "claude-opus-4.5",
    "claude-opus-4.6", "claude-opus-4.7", "claude-opus-4.8",
    "claude-sonnet-4.5", "claude-sonnet-4.6", "claude-sonnet-5",
    "deepseek-v4-pro", "gemini-3-flash", "gemini-3.1-pro",
    "gemini-3.5-flash", "glm-5.2", "gpt-5.2", "gpt-5.3-codex",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "kimi-k2.6", "kimi-k2.7",
    "swe-1.5", "swe-1.6", "swe-1.6-fast", "swe-1.7",
    "swe-1.7-lightning", "swe-2",
]


# Without CLI or cache metadata, seed unavailable entries keyed by legacy aliases.
FALLBACK_MODELS: dict[str, dict] = {
    mid: {
        "display_name": _pretty_name(mid),
        "family": mid,
        "family_name": _pretty_name(mid),
        "vision": False,
        "tags": [],
        "label_tags": [],
        "description": "",
        "reasoning": None,
        "is_fast": False,
        "fusion": None,
    }
    for mid in CLI_MODEL_IDS
}


# Throttle both successful and failed requests. Cache/DB refreshes never run
# a subprocess; only an explicit model-list request uses the public CLI API.
_CLI_REFRESH_INTERVAL = 60.0
_CLI_REFRESH_TIMEOUT = 15.0


class ModelRegistry:
    """Manages model metadata, exclusions, and last-used model."""

    def __init__(
        self,
        registry_dir: str | None = None,
        db_path: str | None = None,
        cache_path: str | None = None,
        team_settings_path: str | None = None,
    ):
        self.registry_dir = registry_dir or _default_registry_dir()
        self.registry_path = os.path.join(self.registry_dir, "model_registry.json")
        self.db_path = db_path
        # Discover versioned/hash-suffixed caches to follow CLI filename changes.
        # An explicit cache_path overrides discovery.
        self._auto_discover_cache = cache_path is None
        if cache_path:
            self._cache_candidates: list[str] = [cache_path]
        else:
            self._cache_candidates = _discover_model_cache_files()
        self.cache_path = self._cache_candidates[0] if self._cache_candidates else _default_model_cache_path()
        # Keep the legacy v4 path for compatibility and include it as a fallback.
        self.cache_fallback_path = None if cache_path else _default_model_cache_fallback_path()
        if self.cache_fallback_path and self.cache_fallback_path not in self._cache_candidates:
            self._cache_candidates.append(self.cache_fallback_path)
        # Same discovery for team_settings: the CLI now writes
        # team_settings.<hash>.bin alongside the legacy team_settings.bin.
        self._auto_discover_team_settings = team_settings_path is None
        if team_settings_path:
            self._team_settings_candidates: list[str] = [team_settings_path]
        else:
            self._team_settings_candidates = _discover_team_settings_files()
        self.team_settings_path = (
            self._team_settings_candidates[0]
            if self._team_settings_candidates
            else _default_team_settings_path()
        )
        self._active_cache_path: str | None = None
        self._active_team_settings_path: str | None = None
        self._data: dict = {}
        self._id_map: dict[str, str] = {}
        self._cli_models: dict[str, dict] = {}
        self._cli_updated_at: float | None = None
        self._cli_last_attempt: float | None = None
        self._cli_refresh_lock = asyncio.Lock()
        self._source_signature: tuple[tuple[str, int, int] | tuple[str, None, None], ...] | None = None
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if os.path.exists(self.registry_path):
            try:
                with open(self.registry_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load model registry: %s", e)
                self._data = {}
        self._data.setdefault("models", {})
        self._data.setdefault("excluded_models", [])
        self._data.setdefault("last_used_model", None)
        self._data.setdefault("last_used_variants", {})
        # Retain a successful catalog across offline restarts. Explicit cache
        # paths are deterministic overrides and must not inherit CLI state.
        if self._auto_discover_cache and isinstance(self._data.get("cli_catalog"), dict):
            snapshot = self._data["cli_catalog"]
            try:
                self._cli_models = _parse_cli_model_list(snapshot.get("data"))
                self._cli_updated_at = _envelope_timestamp(snapshot)
            except ValueError:
                pass
        self.refresh(force=True)

    def _save(self) -> None:
        try:
            os.makedirs(self.registry_dir, exist_ok=True)
            with open(self.registry_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except OSError as e:
            logger.error("Failed to save model registry: %s", e)

    # -- cache refresh -------------------------------------------------------

    async def refresh_from_cli(self) -> None:
        """Refresh availability through the public CLI without blocking asyncio.

        This avoids relying on private filenames, cache locations or stale
        team allowlists. Older/offline CLIs leave the last good catalog (or
        the existing cache fallback) intact. Never runs a model/prompt.
        """
        if not self._auto_discover_cache:
            return
        async with self._cli_refresh_lock:
            now = time.monotonic()
            if self._cli_last_attempt is not None and now - self._cli_last_attempt < _CLI_REFRESH_INTERVAL:
                return
            self._cli_last_attempt = now
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    _find_devin_binary(), "models", "list", "--format", "json",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
                )
                stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=_CLI_REFRESH_TIMEOUT)
                if process.returncode != 0:
                    logger.warning("CLI model discovery exited with code %s; keeping existing catalog", process.returncode)
                    return
                data = json.loads(stdout)
                models = _parse_cli_model_list(data)
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                logger.warning("CLI model discovery unavailable (%s); keeping existing catalog", exc)
                return
            finally:
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.communicate()
            self._cli_models = models
            self._cli_updated_at = time.time()
            self._data["cli_catalog"] = {"data": data, "fetched_at_secs": self._cli_updated_at}
            self.refresh(force=True)
            logger.info("Discovered %d models via devin models list", len(models))

    def _get_source_signature(self) -> tuple[tuple[str, int, int] | tuple[str, None, None], ...]:
        # Track every candidate cache and team-settings file so a refresh is
        # triggered when the CLI writes a new hash-suffixed file (or rotates
        # an existing one), not just when the legacy fixed-name file changes.
        paths = list(self._cache_candidates) + list(self._team_settings_candidates)
        signature = []
        for path in paths:
            try:
                stat = os.stat(path)
                signature.append((path, stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((path, None, None))
        return tuple(signature)

    def refresh(self, force: bool = False) -> None:
        # Re-discover candidate files before computing the signature so a
        # newly-written hash-suffixed cache file (e.g. when the CLI updates
        # in the background) is picked up without restarting the server.
        self._rebuild_candidates()
        signature = self._get_source_signature()
        if not force and signature == self._source_signature:
            return
        self._refresh_from_cache()
        self._discover_from_db()
        self._source_signature = signature
        self._save()

    def _rebuild_candidates(self) -> None:
        """Discover newly written caches/settings without changing explicit paths."""
        if self._auto_discover_cache:
            self._cache_candidates = _discover_model_cache_files()
            if self.cache_fallback_path and self.cache_fallback_path not in self._cache_candidates:
                self._cache_candidates.append(self.cache_fallback_path)
        if self._auto_discover_team_settings:
            self._team_settings_candidates = _discover_team_settings_files()

    def _refresh_from_cache(self) -> None:
        """Apply CLI/cache metadata and migrate saved model choices."""
        # Prefer envelope fetch time (mtime for legacy files), not filename version.
        # Older caches can remain readable but stale. Try them only if fresher
        # candidates cannot be parsed.
        cached: dict[str, dict] = {}
        id_map: dict[str, str] = {}
        self._active_cache_path = None
        for path in _sort_cache_files(self._cache_candidates):
            parsed, parsed_map = _parse_model_cache(path)
            if parsed:
                cached = parsed
                id_map = parsed_map
                self._active_cache_path = path
                break
        self._id_map = id_map
        # Hash-suffixed and fixed-name team settings can coexist; prefer the freshest
        # file with a non-empty allowlist.
        allowed_ids: set[str] = set()
        self._active_team_settings_path = None
        for path in _sort_cache_files(self._team_settings_candidates):
            parsed_ids = _parse_allowed_model_ids(path)
            if parsed_ids:
                allowed_ids = parsed_ids
                self._active_team_settings_path = path
                break

        refresh_catalogue(
            self._data, cached, id_map, allowed_ids,
            cli_models=self._cli_models,
            fallback_models=FALLBACK_MODELS,
            logger=logger,
        )

    # -- DB discovery --------------------------------------------------------

    def _discover_from_db(self) -> None:
        """Check the sessions DB for models not yet in the registry."""
        if not self.db_path or not os.path.exists(self.db_path):
            return
        try:
            uri = Path(self.db_path).absolute().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT model FROM sessions WHERE model IS NOT NULL"
            )
            db_models = {row["model"] for row in cur.fetchall()}
            conn.close()
        except Exception as e:
            logger.warning("Failed to query models from DB: %s", e)
            return

        for mid in db_models:
            # Keep known registry IDs; unknown internal IDs may map to a family alias.
            normalized = self._id_map.get(mid, mid)
            key = mid if mid in self._data["models"] else normalized
            if key in self._data["models"]:
                continue
            self._data["models"][key] = {
                "display_name": _pretty_name(key),
                "family": key,
                "family_name": _pretty_name(key),
                "vision": False,
                "tags": [],
                "label_tags": [],
                "description": "",
                "is_new": True,
                "available": False,
                "input_price": None,
                "cached_input_price": None,
                "output_price": None,
                "reasoning": None,
                "is_fast": False,
                "fusion": None,
            }

    # -- public API ----------------------------------------------------------

    def get_cache_info(self) -> dict:
        """Return ``path`` and ``updated_at`` (Unix seconds) for the staleness UI.

        Public CLI: path None, CLI refresh time, and source ``devin models list``.
        Parsed cache: path and envelope fetch time, falling back to mtime.
        Neither source: path and updated_at both None.
        Fetch time avoids treating copied/backed-up caches as fresh.
        """
        if self._cli_models:
            return {"path": None, "updated_at": self._cli_updated_at, "source": "devin models list"}
        path = self._active_cache_path
        if not path:
            return {"path": None, "updated_at": None}
        fetched = _read_envelope_fetched_at(path)
        if fetched is not None:
            return {"path": path, "updated_at": float(fetched)}
        try:
            return {"path": path, "updated_at": os.path.getmtime(path)}
        except OSError:
            return {"path": path, "updated_at": None}

    def get_all_models(self) -> list[dict]:
        """Return all known models with metadata and exclusion status."""
        models = []
        for mid, info in self._data["models"].items():
            models.append({
                "id": mid,
                "display_name": info.get("display_name", _pretty_name(mid)),
                "family": info.get("family", mid),
                "family_name": info.get("family_name", info.get("display_name", _pretty_name(mid))),
                "vision": info.get("vision", False),
                "tags": info.get("tags", []),
                "label_tags": info.get("label_tags", []),
                "description": info.get("description", ""),
                "is_new": info.get("is_new", False),
                "available": info.get("available", True),
                "excluded": mid in self._data["excluded_models"],
                "input_price": info.get("input_price"),
                "cached_input_price": info.get("cached_input_price"),
                "output_price": info.get("output_price"),
                "reasoning": info.get("reasoning"),
                "is_fast": info.get("is_fast", False),
                "fusion": info.get("fusion"),
            })
        # Sort: non-excluded first, then by family then display name so
        # variants of the same model stay grouped together.
        models.sort(key=lambda m: (
            m["excluded"],
            m.get("family_name", m["display_name"]).lower(),
            m["display_name"].lower(),
        ))
        return models

    def get_model(self, model_id: str | None) -> dict | None:
        """Return pricing metadata for a registry ID or mapped alias, or None if unknown."""
        if not model_id:
            return None
        normalized = model_id
        if normalized not in self._data["models"]:
            normalized = self._id_map.get(model_id, model_id)
        if normalized not in self._data["models"]:
            normalized = next(
                (internal for internal, alias in self._id_map.items() if alias == model_id and internal in self._data["models"]),
                model_id,
            )
        info = self._data["models"].get(normalized)
        if info is None:
            return None
        model = {
            "id": normalized,
            "display_name": info.get("display_name", _pretty_name(normalized)),
            "tags": info.get("tags", []),
            "input_price": info.get("input_price"),
            "cached_input_price": info.get("cached_input_price"),
            "output_price": info.get("output_price"),
            "context_window": info.get("context_window", 0),
        }
        if normalized.startswith("fusion-") and "-sidekick-" in normalized:
            lead_id, sidekick_id = normalized[len("fusion-"):].split("-sidekick-", 1)
            model["inference_models"] = {
                component["id"]: component
                for mid in (lead_id, sidekick_id)
                if (component := self.get_model(mid)) is not None
            }
        return model

    def get_selectable_models(self) -> list[dict]:
        """Return available, non-excluded models, most recently used first."""
        all_models = self.get_all_models()
        selectable = [m for m in all_models if not m["excluded"] and m["available"]]
        usage = self._get_model_usage()
        selectable.sort(
            key=lambda m: usage.get(m["id"], 0),
            reverse=True,
        )
        return selectable

    def _get_model_usage(self) -> dict[str, int]:
        """Return each model ID's latest DB activity timestamp, or {} if unavailable.

        Keep known registry IDs; other IDs use the cache's internal-to-family map.
        """
        if not self.db_path or not os.path.exists(self.db_path):
            return {}
        try:
            uri = Path(self.db_path).absolute().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT model, MAX(last_activity_at) AS last
                FROM sessions
                WHERE model IS NOT NULL
                GROUP BY model
                """
            )
            result = {row["model"]: row["last"] for row in cur.fetchall()}
            conn.close()
            normalized = {}
            for mid, ts in result.items():
                key = mid if mid in self._data["models"] else self._id_map.get(mid, mid)
                if key not in normalized or ts > normalized[key]:
                    normalized[key] = ts
            return normalized
        except Exception as e:
            logger.warning("Failed to query model usage: %s", e)
            return {}

    def get_recently_used(self, limit: int = 5) -> list[dict]:
        """Return recently used, available, non-excluded models, most recent first."""
        selectable = self.get_selectable_models()
        usage = self._get_model_usage()
        recent = [m for m in selectable if m["id"] in usage]
        recent.sort(key=lambda m: usage.get(m["id"], 0), reverse=True)
        return recent[:limit]

    def exclude_model(self, model_id: str) -> None:
        if model_id not in self._data["excluded_models"]:
            self._data["excluded_models"].append(model_id)
            self._save()

    def include_model(self, model_id: str) -> None:
        if model_id in self._data["excluded_models"]:
            self._data["excluded_models"].remove(model_id)
            self._save()

    def exclude_models(self, model_ids: list[str]) -> None:
        changed = False
        for mid in model_ids:
            if mid not in self._data["excluded_models"]:
                self._data["excluded_models"].append(mid)
                changed = True
        if changed:
            self._save()

    def include_models(self, model_ids: list[str]) -> None:
        before = len(self._data["excluded_models"])
        remove = set(model_ids)
        self._data["excluded_models"] = [
            m for m in self._data["excluded_models"] if m not in remove
        ]
        if len(self._data["excluded_models"]) != before:
            self._save()

    def get_last_used_model(self) -> str | None:
        last = self._data.get("last_used_model")
        if last and last in self._data["models"]:
            if last not in self._data["excluded_models"]:
                return last
        recent = self.get_recently_used(limit=1)
        if recent:
            return recent[0]["id"]
        selectable = self.get_selectable_models()
        if selectable:
            return selectable[0]["id"]
        return None

    def get_last_used_variants(self) -> dict[str, str]:
        """Return the per-family last-used variant map.

        Keys are family aliases (e.g. ``glm-5.2``); fusion combos record two
        entries: ``fusion:<lead_family>`` -> the fusion id and
        ``sidekick:<sidekick_family>`` -> the sidekick variant id.
        """
        return dict(self._data.get("last_used_variants", {}))

    def set_last_used_model(self, model_id: str) -> None:
        self._data["last_used_model"] = model_id
        info = self._data["models"].get(model_id)
        if info is not None:
            # Clear the is_new flag since the user has now seen/used it
            info["is_new"] = False
            variants = self._data.setdefault("last_used_variants", {})
            fusion = info.get("fusion")
            if fusion:
                variants[f"fusion:{fusion['lead_family']}"] = model_id
                variants[f"sidekick:{fusion['sidekick_family']}"] = fusion["sidekick_id"]
            else:
                variants[info.get("family", model_id)] = model_id
        self._save()

    def acknowledge_new_model(self, model_id: str) -> None:
        if model_id in self._data["models"]:
            self._data["models"][model_id]["is_new"] = False
            self._save()

    def has_new_models(self) -> bool:
        """Check if any non-excluded model has is_new=True."""
        return any(
            info.get("is_new", False)
            and mid not in self._data["excluded_models"]
            for mid, info in self._data["models"].items()
        )

    def update_model(self, model_id: str, updates: dict) -> None:
        """Update metadata for a model (display_name, vision, tags, description)."""
        if model_id not in self._data["models"]:
            self._data["models"][model_id] = {
                "display_name": _pretty_name(model_id),
                "family": model_id,
                "family_name": _pretty_name(model_id),
                "vision": False,
                "tags": [],
                "label_tags": [],
                "description": "",
                "is_new": True,
                "input_price": None,
                "cached_input_price": None,
                "output_price": None,
                "reasoning": None,
                "is_fast": False,
                "fusion": None,
            }
        for key in ("display_name", "vision", "tags", "description"):
            if key in updates:
                self._data["models"][model_id][key] = updates[key]
        self._save()
