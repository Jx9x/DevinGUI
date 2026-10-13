"""CLI cache discovery, bounded envelope reading, and memoized schema decoding."""

from __future__ import annotations

import base64
import glob
import json
import logging
import os
from typing import Any, Callable

from .model_catalog import (
    _CACHE_PAYLOAD_MAX_BYTES,
    _decode_model_cache,
    _iter_wire_fields,
    _model_cache_entries,
)

# Preserve the registry's existing log category for cache diagnostics.
logger = logging.getLogger("server.model_registry")

_CACHE_FILE_MAX_BYTES = 12 * 1024 * 1024
_BASE64_PAYLOAD_MAX_CHARS = 4 * ((_CACHE_PAYLOAD_MAX_BYTES + 2) // 3)


def _discover_files_by_pattern(pattern: str) -> list[str]:
    """Find regular cache files without depending on version or extension."""
    return _sort_cache_files([path for path in glob.glob(pattern) if os.path.isfile(path)])


def _sort_cache_files(paths: list[str]) -> list[str]:
    """Prefer the actual fetch time; copying an old file must not promote it."""
    def freshness(path: str) -> tuple[float, str]:
        fetched = _read_envelope_fetched_at(path)
        try:
            return (float(fetched) if fetched is not None else os.path.getmtime(path), path)
        except OSError:
            return (-1.0, path)

    return sorted(paths, key=freshness, reverse=True)


def _discover_model_cache_files(cli_dir: str) -> list[str]:
    """Find model-config caches across version, separator and extension changes.

    This is an offline/older-CLI fallback. The supported ``models list``
    command is authoritative and does not depend on private cache filenames.
    """
    return _discover_files_by_pattern(os.path.join(cli_dir, "model*config*"))


def _discover_team_settings_files(cli_dir: str) -> list[str]:
    """Find team-settings caches without fixing their suffix or extension."""
    return _discover_files_by_pattern(os.path.join(cli_dir, "team*setting*"))


def _read_cache_bytes(bin_path: str) -> tuple[bytes, int | None]:
    """Return ``(protobuf_bytes, fetched_at_secs)`` from a CLI cache file.

    JSON envelope (v5): base64-decoded payload and validated fetch time (or None).
    Raw protobuf (v2/v4): file bytes and None.
    Read/envelope-decode failure or size limit exceeded: ``(b"", None)``.
    """
    try:
        with open(bin_path, "rb") as f:
            raw = f.read(_CACHE_FILE_MAX_BYTES + 1)
    except OSError as e:
        logger.warning("Failed to read cache file %s: %s", bin_path, e)
        return b"", None
    if len(raw) > _CACHE_FILE_MAX_BYTES:
        logger.warning("Cache file %s exceeds %d bytes", bin_path, _CACHE_FILE_MAX_BYTES)
        return b"", None
    # JSON-envelope files start with '{'; legacy files start with a protobuf
    # tag byte (field 1, length-delimited → 0x0a).
    if raw.lstrip().startswith(b"{"):
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as e:
            logger.warning("Failed to parse JSON cache envelope %s: %s", bin_path, e)
            return b"", None
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(payload, str):
            logger.warning("JSON cache envelope %s has no string payload", bin_path)
            return b"", None
        if len(payload) > _BASE64_PAYLOAD_MAX_CHARS:
            logger.warning(
                "JSON cache envelope %s payload exceeds %d bytes",
                bin_path, _CACHE_PAYLOAD_MAX_BYTES,
            )
            return b"", None
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, base64.binascii.Error) as e:
            logger.warning("Failed to base64-decode payload %s: %s", bin_path, e)
            return b"", None
        if len(data) > _CACHE_PAYLOAD_MAX_BYTES:
            logger.warning(
                "JSON cache envelope %s payload exceeds %d bytes",
                bin_path, _CACHE_PAYLOAD_MAX_BYTES,
            )
            return b"", None
        return data, _envelope_timestamp(envelope)
    if len(raw) > _CACHE_PAYLOAD_MAX_BYTES:
        logger.warning(
            "Raw cache %s exceeds %d bytes", bin_path, _CACHE_PAYLOAD_MAX_BYTES
        )
        return b"", None
    return raw, None


def _envelope_timestamp(envelope: dict) -> int | None:
    val = envelope.get("fetched_at_secs")
    if type(val) in (int, float) and 0 < val < 1e12:
        return int(val)
    return None


def _read_envelope_fetched_at(bin_path: str) -> int | None:
    """Return ``fetched_at_secs`` from a JSON-envelope cache file, else None.

    Used to surface the CLI's server-side fetch time (more accurate than the
    file's mtime, which can be touched by copies/backups) to the staleness UI.
    """
    try:
        with open(bin_path, "rb") as f:
            head = f.read(64)
            if not head.lstrip().startswith(b"{"):
                return None
            raw = f.read(_CACHE_FILE_MAX_BYTES + 1 - len(head))
            if len(head) + len(raw) > _CACHE_FILE_MAX_BYTES:
                return None
        envelope = json.loads(head + raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return None
    return _envelope_timestamp(envelope) if isinstance(envelope, dict) else None


# Memoize by (path, mtime_ns, size) so unchanged files are not re-decoded.
# Bound entries because the CLI creates new hash-suffixed files on session start.
_PARSE_CACHE_MAX = 8
_parse_cache: dict[tuple[str, int, int], Any] = {}


def _parse_cache_get(path: str) -> Any:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return _parse_cache.get((path, stat.st_mtime_ns, stat.st_size))


def _parse_cache_put(path: str, value: Any) -> None:
    try:
        stat = os.stat(path)
    except OSError:
        return
    key = (path, stat.st_mtime_ns, stat.st_size)
    if len(_parse_cache) >= _PARSE_CACHE_MAX:
        _parse_cache.pop(next(iter(_parse_cache)))
    _parse_cache[key] = value


def _parse_allowed_model_ids(bin_path: str) -> set[str]:
    if not os.path.exists(bin_path):
        logger.warning("Team settings file not found: %s", bin_path)
        return set()

    cached = _parse_cache_get(bin_path)
    if cached is not None:
        return cached

    raw, _fetched = _read_cache_bytes(bin_path)
    if not raw:
        return set()

    # Field 7 is a repeated string of allowed internal model IDs.
    try:
        allowed = {
            v.decode("utf-8", errors="replace")
            for f, w, v in _iter_wire_fields(raw)
            if f == 7 and w == 2 and v
        }
        allowed.discard("")
    except ValueError as e:
        logger.warning("Failed to parse team settings %s: %s", bin_path, e)
        return set()

    if not allowed:
        # Do not infer allowed models from unknown fields. Cache the empty result.
        logger.warning("Team settings %s has no allowed models", bin_path)
    _parse_cache_put(bin_path, allowed)
    return allowed


def _parse_model_cache(
    bin_path: str, *, decode: Callable[[bytes], dict] = _decode_model_cache,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Return ``({internal_id: metadata}, {internal_id: family_alias})``.

    Key variants by CLI-selectable internal ID (field 22), not shared aliases,
    so pricing and capabilities stay variant-specific.
    Read raw protobuf or JSON envelopes via :func:`_read_cache_bytes`.
    Missing/unreadable cache or unrecognized layout: ``({}, {})``.
    """
    if not os.path.exists(bin_path):
        logger.warning("Model cache file not found: %s", bin_path)
        return {}, {}

    cached_message = _parse_cache_get(bin_path)
    if cached_message is not None:
        message = cached_message
    else:
        data, _fetched = _read_cache_bytes(bin_path)
        if not data:
            return {}, {}
        message = decode(data)
        if not message.get("1"):
            # Reject unrecognized layouts without generic decoding or memoization.
            logger.warning("Unrecognized model cache layout in %s", bin_path)
            return {}, {}
        _parse_cache_put(bin_path, message)

    # Rebuild metadata so caller mutations never affect cached wire fields.
    return _model_cache_entries(message)
