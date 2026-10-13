"""Extracts Windsurf/Devin session tokens from Chromium browser localStorage.

The ``GetPlanStatus`` quota API (see :mod:`server.usage_provider`) is
authenticated with four ``devin_*`` tokens that the Windsurf / Devin web app
stores in ``localStorage`` for the ``https://app.devin.ai`` (preferred) and
``https://windsurf.com`` origins. Chromium browsers persist localStorage in a
LevelDB database under ``<user data>/<profile>/Local Storage/leveldb``.

This module scans those LevelDB files for the four keys without a native
LevelDB binding. The reliable path is the write-ahead log (``*.log``): each
WriteBatch entry stores the full key contiguously (no prefix compression), so
a raw byte search for ``<origin>\\x00<key>`` followed by reading the
length-prefixed value recovers the token. ``*.ldb`` SSTable files use prefix
compression, so they are scanned best-effort (entries whose shared-prefix
length is 0 still contain the full key contiguously).

localStorage values are stored as ``<type_byte><encoded string>`` where
``type_byte`` is ``0x00`` (latin-1) or ``0x01`` (UTF-16LE).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PureWindowsPath
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Origins in preference order — the newer Devin-branded origin wins.
ORIGINS = ("https://app.devin.ai", "https://windsurf.com")

TOKEN_KEYS = (
    "devin_session_token",
    "devin_auth1_token",
    "devin_account_id",
    "devin_primary_org_id",
)

# Chromium browser user-data directories (relative to %LOCALAPPDATA% on Windows).
_WINDOWS_BROWSER_DIRS = (
    r"Google\Chrome\User Data",
    r"Microsoft\Edge\User Data",
    r"BraveSoftware\Brave-Browser\User Data",
    r"Vivaldi\User Data",
    r"Chromium\User Data",
)

_PROFILE_NAMES = ("Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4", "Profile 5")


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))


def find_localstorage_dirs(root: Optional[Path] = None) -> list[Path]:
    """Return all Chromium ``Local Storage/leveldb`` dirs that exist.

    ``root`` overrides ``%LOCALAPPDATA%`` (used by tests).
    """
    base = root if root is not None else _local_appdata()
    dirs: list[Path] = []
    for browser_rel in _WINDOWS_BROWSER_DIRS:
        # Split the configured Windows-style path explicitly instead of
        # relying on the host OS path separator. This keeps diagnostics/tests
        # portable while producing the same path on Windows.
        user_data = base.joinpath(*PureWindowsPath(browser_rel).parts)
        if not user_data.is_dir():
            continue
        # Always check Default plus any Profile N folders actually present.
        profile_candidates: list[str] = list(_PROFILE_NAMES)
        for child in user_data.iterdir():
            if child.is_dir() and child.name.startswith("Profile ") and child.name not in profile_candidates:
                profile_candidates.append(child.name)
        for profile in profile_candidates:
            ldb = user_data / profile / "Local Storage" / "leveldb"
            if ldb.is_dir():
                dirs.append(ldb)
    return dirs


# -- leveldb varint -------------------------------------------------------


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    """Read a little-endian base-128 varint. Returns (value, next_index)."""
    result = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return result, index
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _decode_localstorage_value(raw: bytes) -> Optional[str]:
    """Decode a localStorage value payload (type byte + encoded string)."""
    if not raw:
        return None
    encoding = raw[0]
    payload = raw[1:]
    if encoding == 0x01:  # UTF-16LE
        try:
            return payload.decode("utf-16-le")
        except UnicodeDecodeError:
            return None
    if encoding == 0x00:  # latin-1
        try:
            return payload.decode("latin-1")
        except UnicodeDecodeError:
            return None
    # Unknown encoding — try UTF-8 as a last resort.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan_file_for_key(data: bytes, needle: bytes) -> Optional[str]:
    """Find ``needle`` (the full leveldb key bytes) and read its value.

    In a WriteBatch entry the layout is ``...<key><value_len varint><value>`` so
    immediately after the key comes the value length varint. This holds for
    ``.log`` files reliably and for ``.ldb`` entries whose shared-prefix length
    is 0 (the full key is stored verbatim).
    """
    search_from = 0
    while True:
        pos = data.find(needle, search_from)
        if pos == -1:
            return None
        value_start = pos + len(needle)
        try:
            value_len, value_index = _read_varint(data, value_start)
        except ValueError:
            search_from = value_start
            continue
        if value_len < 0 or value_len > 64 * 1024 or value_index + value_len > len(data):
            # Not a real entry boundary (or a huge bogus length) — keep scanning.
            search_from = value_start
            continue
        raw = data[value_index : value_index + value_len]
        decoded = _decode_localstorage_value(raw)
        if decoded is not None and decoded:
            return decoded
        search_from = value_start


def _scan_dir_for_tokens(leveldb_dir: Path) -> dict[str, str]:
    """Scan one leveldb directory for the four devin_* tokens."""
    found: dict[str, str] = {}
    # Scan .log (WAL) first — most reliable for recent writes — then .ldb
    # SSTables (parsed properly, including snappy decompression) for data that
    # has already been compacted out of the WAL.
    log_files = sorted(leveldb_dir.glob("*.log"))
    ldb_files = sorted(leveldb_dir.glob("*.ldb"))
    for path in log_files:
        if len(found) == len(TOKEN_KEYS):
            return found
        try:
            data = path.read_bytes()
        except OSError as e:
            logger.debug("Could not read %s: %s", path, e)
            continue
        _collect_tokens_from_bytes(data, found)
    for path in ldb_files:
        if len(found) == len(TOKEN_KEYS):
            return found
        try:
            data = path.read_bytes()
        except OSError as e:
            logger.debug("Could not read %s: %s", path, e)
            continue
        try:
            for key_bytes, value_bytes in _iter_sstable_entries(data):
                _maybe_record_token(key_bytes, value_bytes, found)
        except Exception as e:
            logger.debug("Error parsing SSTable %s: %s", path, e)
            continue
    return found


def _collect_tokens_from_bytes(data: bytes, found: dict[str, str]) -> None:
    """Raw-scan a .log file's bytes for full leveldb keys (no prefix compression)."""
    for origin in ORIGINS:
        for key in TOKEN_KEYS:
            if key in found:
                continue
            needle = f"{origin}\x00{key}".encode("utf-8")
            value = _scan_file_for_key(data, needle)
            if value:
                found[key] = value


def _maybe_record_token(key_bytes: bytes, value_bytes: bytes, found: dict[str, str]) -> None:
    """If ``key_bytes`` is a devin_* localStorage key, decode and record its value."""
    if not key_bytes:
        return
    sep = key_bytes.find(b"\x00")
    if sep <= 0:
        return
    origin = key_bytes[:sep].decode("utf-8", errors="replace")
    key_name = key_bytes[sep + 1 :].decode("utf-8", errors="replace")
    if origin not in ORIGINS or key_name not in TOKEN_KEYS or key_name in found:
        return
    decoded = _decode_localstorage_value(value_bytes)
    if decoded:
        found[key_name] = decoded


# -- snappy decompression (leveldb raw format, no framing) ----------------


def _snappy_decompress(data: bytes) -> bytes:
    """Decompress a raw snappy-encoded blob (the format leveldb uses for blocks)."""
    out = bytearray()
    i = 0
    # First, the uncompressed length is a varint.
    uncompressed_len, i = _read_varint(data, i)
    while i < len(data):
        tag = data[i]
        i += 1
        tag_type = tag & 0x03
        if tag_type == 0:  # literal
            code = tag >> 2
            if code < 60:
                literal_len = code + 1
            elif code == 60:
                literal_len = data[i] + 1
                i += 1
            elif code == 61:
                literal_len = (data[i] | (data[i + 1] << 8)) + 1
                i += 2
            elif code == 62:
                literal_len = (data[i] | (data[i + 1] << 8) | (data[i + 2] << 16)) + 1
                i += 3
            else:  # code == 63
                literal_len = (
                    data[i] | (data[i + 1] << 8) | (data[i + 2] << 16) | (data[i + 3] << 24)
                ) + 1
                i += 4
            out += data[i : i + literal_len]
            i += literal_len
        elif tag_type == 1:  # copy with 1-byte offset
            length = ((tag >> 2) & 0x07) + 4
            offset = ((tag >> 5) & 0x07) << 8 | data[i]
            i += 1
            _snappy_copy(out, offset, length)
        elif tag_type == 2:  # copy with 2-byte offset
            length = (tag >> 2) + 1
            offset = data[i] | (data[i + 1] << 8)
            i += 2
            _snappy_copy(out, offset, length)
        else:  # tag_type == 3, copy with 4-byte offset
            length = (tag >> 2) + 1
            offset = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16) | (data[i + 3] << 24)
            i += 4
            _snappy_copy(out, offset, length)
    if len(out) != uncompressed_len:
        # Not fatal — the scanner only needs the key/value region, which may be
        # intact even if our length check is off by trailing padding.
        logger.debug("snappy length mismatch: got %d expected %d", len(out), uncompressed_len)
    return bytes(out)


def _snappy_copy(out: bytearray, offset: int, length: int) -> None:
    start = len(out) - offset
    if start < 0:
        raise ValueError("snappy copy offset before start of output")
    for j in range(length):
        out.append(out[start + j])


# -- SSTable (.ldb) parsing ------------------------------------------------

_SSTABLE_MAGIC = 0xDB4775248B80FB57


def _iter_sstable_entries(data: bytes):
    """Yield (key, value) byte pairs from a leveldb SSTable (.ldb) file.

    Handles snappy-compressed and uncompressed data blocks. Skips blocks that
    cannot be parsed. The index block is read to locate data blocks; meta
    blocks (e.g. the filter block) are ignored.
    """
    if len(data) < 48:
        return
    footer = data[-48:]
    import struct

    if struct.unpack("<Q", footer[-8:])[0] != _SSTABLE_MAGIC:
        return
    i = 0
    _metaindex_handle, i = _read_handle(footer, i)
    index_handle, i = _read_handle(footer, i)
    index_off, index_size = index_handle
    index_block = _read_block(data, index_off, index_size)
    if index_block is None:
        return
    # The index block is itself a sequence of entries whose keys are the
    # largest key in each data block and whose values are BlockHandles.
    for _key, value_bytes in _iter_data_block_entries(index_block):
        handle_off, handle_size = _read_handle(value_bytes, 0)[0]
        block = _read_block(data, handle_off, handle_size)
        if block is None:
            continue
        yield from _iter_data_block_entries(block)


def _read_handle(data: bytes, i: int) -> tuple[tuple[int, int], int]:
    off, i = _read_varint(data, i)
    size, i = _read_varint(data, i)
    return (off, size), i


def _read_block(data: bytes, offset: int, size: int) -> Optional[bytes]:
    """Read a block and decompress it if its 5-byte trailer says so."""
    if offset + size + 5 > len(data):
        return None
    block = data[offset : offset + size]
    compression = data[offset + size]
    if compression == 0:
        return block
    if compression == 1:
        try:
            return _snappy_decompress(block)
        except Exception as e:
            logger.debug("snappy decompress failed: %s", e)
            return None
    # Unsupported compression (e.g. zstd) — skip.
    logger.debug("unsupported block compression type %d", compression)
    return None


def _iter_data_block_entries(block: bytes):
    """Yield (key, value) pairs from a leveldb data block (prefix-compressed)."""
    if len(block) < 4:
        return
    # The last 4 bytes are the number of restart points (uint32 LE). Each
    # restart point is a uint32 LE offset. Entries live before the restart
    # array. We only need the restart count to bound the entry region; we walk
    # entries sequentially from offset 0.
    import struct

    num_restarts = struct.unpack("<I", block[-4:])[0]
    restart_array_start = len(block) - 4 - num_restarts * 4
    if restart_array_start < 0:
        return
    i = 0
    last_key = b""
    while i < restart_array_start:
        try:
            shared_len, i = _read_varint(block, i)
            unshared_len, i = _read_varint(block, i)
            value_len, i = _read_varint(block, i)
        except ValueError:
            return
        if i + unshared_len + value_len > restart_array_start:
            return
        key_suffix = block[i : i + unshared_len]
        i += unshared_len
        value = block[i : i + value_len]
        i += value_len
        if shared_len > len(last_key):
            # Corrupt entry — bail out.
            return
        full_key = last_key[:shared_len] + key_suffix
        last_key = full_key
        yield full_key, value


def extract_devin_tokens(root: Optional[Path] = None) -> Optional[dict[str, str]]:
    """Scan all Chromium browsers and return the four devin_* tokens.

    Returns ``None`` if the tokens could not be found in any browser. When
    multiple browsers/profiles have them, the first complete set found wins
    (Chrome and Edge are scanned before other Chromium forks).
    """
    best: dict[str, str] = {}
    for leveldb_dir in find_localstorage_dirs(root):
        if len(best) == len(TOKEN_KEYS):
            break
        try:
            scanned = _scan_dir_for_tokens(leveldb_dir)
        except Exception as e:
            logger.debug("Error scanning %s: %s", leveldb_dir, e)
            continue
        for key, value in scanned.items():
            best.setdefault(key, value)
        if len(best) == len(TOKEN_KEYS):
            logger.info("Found devin session tokens in %s", leveldb_dir)
            return best
    if len(best) == len(TOKEN_KEYS):
        return best
    if best:
        logger.warning(
            "Partial devin session tokens found (%d/%d): %s",
            len(best),
            len(TOKEN_KEYS),
            ", ".join(sorted(best)),
        )
    return None


# -- helpers for tests / manual inspection --------------------------------


def iter_candidate_dirs(root: Optional[Path] = None) -> Iterable[Path]:
    """Yield candidate leveldb directories (for diagnostics)."""
    yield from find_localstorage_dirs(root)
