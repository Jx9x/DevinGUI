import struct
from pathlib import Path

from server.browser_session import (
    extract_devin_tokens,
    find_localstorage_dirs,
    _scan_file_for_key,
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _writebatch_entry(key: bytes, value: bytes) -> bytes:
    """A leveldb WriteBatch Put entry: type(1) + key_len + key + value_len + value."""
    return (
        b"\x01"
        + _varint(len(key))
        + key
        + _varint(len(value))
        + value
    )


def _wal_record(payload: bytes, record_type: int = 1) -> bytes:
    """Wrap a payload as a single full WAL record (7-byte header)."""
    # checksum(4) + length(2) + type(1). Checksum is not validated by the scanner.
    return struct.pack("<IHB", 0, len(payload), record_type) + payload


def _localstorage_value(text: str, *, utf16: bool = True) -> bytes:
    if utf16:
        return b"\x01" + text.encode("utf-16-le")
    return b"\x00" + text.encode("latin-1")


def _build_log_file(tokens: dict[str, str], origin: str = "https://app.devin.ai") -> bytes:
    """Build a minimal .log (WAL) file containing one WriteBatch with the tokens."""
    entries = b""
    for key, value in tokens.items():
        full_key = f"{origin}\x00{key}".encode("utf-8")
        entries += _writebatch_entry(full_key, _localstorage_value(value))
    # WriteBatch: 8-byte sequence + varint(count) + entries
    batch = struct.pack("<Q", 0) + _varint(len(tokens)) + entries
    return _wal_record(batch)


def test_scan_file_for_key_finds_utf16_value(tmp_path: Path):
    tokens = {"devin_session_token": "sess-abc"}
    log_bytes = _build_log_file(tokens)
    needle = "https://app.devin.ai\x00devin_session_token".encode("utf-8")
    assert _scan_file_for_key(log_bytes, needle) == "sess-abc"


def test_scan_file_for_key_finds_latin1_value():
    raw = (
        b"https://windsurf.com\x00devin_account_id"
        + _varint(len(_localstorage_value("acct-1", utf16=False)))
        + _localstorage_value("acct-1", utf16=False)
    )
    needle = "https://windsurf.com\x00devin_account_id".encode("utf-8")
    assert _scan_file_for_key(raw, needle) == "acct-1"


def test_scan_file_for_key_returns_none_when_missing():
    assert _scan_file_for_key(b"nothing here", b"missing-key") is None


def test_extract_devin_tokens_from_chrome_log(tmp_path: Path):
    tokens = {
        "devin_session_token": "sess-token",
        "devin_auth1_token": "auth1-token",
        "devin_account_id": "acct-id",
        "devin_primary_org_id": "org-id",
    }
    leveldb = (
        tmp_path
        / "Google"
        / "Chrome"
        / "User Data"
        / "Default"
        / "Local Storage"
        / "leveldb"
    )
    leveldb.mkdir(parents=True)
    (leveldb / "000000.log").write_bytes(_build_log_file(tokens))

    found = extract_devin_tokens(root=tmp_path)
    assert found == tokens


def test_scan_dir_prefers_app_devin_ai_origin(tmp_path: Path):
    from server.browser_session import _scan_dir_for_tokens

    # Same key in both origins; the app.devin.ai value should win because it is
    # scanned first per file.
    leveldb = (
        tmp_path
        / "Microsoft"
        / "Edge"
        / "User Data"
        / "Default"
        / "Local Storage"
        / "leveldb"
    )
    leveldb.mkdir(parents=True)
    legacy = {"devin_session_token": "legacy-session"}
    preferred = {"devin_session_token": "preferred-session"}
    # Write the legacy origin block first, then the preferred origin — the
    # scanner checks origins in ORIGINS order (preferred first) per file.
    data = _build_log_file(legacy, origin="https://windsurf.com") + _build_log_file(
        preferred, origin="https://app.devin.ai"
    )
    (leveldb / "000000.log").write_bytes(data)

    found = _scan_dir_for_tokens(leveldb)
    assert found["devin_session_token"] == "preferred-session"


def test_extract_devin_tokens_returns_none_when_no_browser(tmp_path: Path):
    assert extract_devin_tokens(root=tmp_path) is None


def test_find_localstorage_dirs_skips_missing(tmp_path: Path):
    # Only Edge exists.
    edge = tmp_path / "Microsoft" / "Edge" / "User Data" / "Default" / "Local Storage" / "leveldb"
    edge.mkdir(parents=True)
    dirs = find_localstorage_dirs(root=tmp_path)
    assert dirs == [edge]


# -- snappy decompressor --------------------------------------------------


def test_snappy_literal_only():
    from server.browser_session import _snappy_decompress

    # uncompressed len=5 (0x05), literal tag (5-1)<<2=0x10, then "Hello"
    assert _snappy_decompress(b"\x05\x10Hello") == b"Hello"


def test_snappy_copy_with_2_byte_offset():
    from server.browser_session import _snappy_decompress

    # len=6; literal "abc" (tag 0x08); copy len=3 offset=3 (tag 0x0a, 03 00) -> "abcabc"
    assert _snappy_decompress(b"\x06\x08abc\x0a\x03\x00") == b"abcabc"


def test_snappy_overlapping_copy():
    from server.browser_session import _snappy_decompress

    # len=6; literal "ab" (tag 0x04 = (2-1)<<2|0); copy len=4 offset=1
    # (tag 0x01 = type1, length=4, offset byte=1) -> "abbbbb"
    assert _snappy_decompress(b"\x06\x04ab\x01\x01") == b"abbbbb"


def test_snappy_long_literal_1byte_length():
    from server.browser_session import _snappy_decompress

    # 100-byte literal: code=60 -> tag 0xf0, then 1 byte length (99), then 100 bytes
    payload = b"x" * 100
    blob = b"\x64\xf0\x63" + payload  # uncompressed len=100, tag 0xf0, len byte 99
    assert _snappy_decompress(blob) == payload


# -- SSTable (.ldb) parsing ------------------------------------------------


def _build_uncompressed_sstable(entries: list[tuple[bytes, bytes]]) -> bytes:
    """Build a minimal .ldb with one uncompressed data block + index block."""
    import struct

    def varint(v: int) -> bytes:
        out = bytearray()
        while v >= 0x80:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        out.append(v & 0x7F)
        return bytes(out)

    # Data block: prefix-compressed entries + restart array (1 restart at 0).
    data_block = bytearray()
    last_key = b""
    for key, value in entries:
        shared = 0
        while shared < min(len(last_key), len(key)) and last_key[shared] == key[shared]:
            shared += 1
        suffix = key[shared:]
        data_block += varint(shared) + varint(len(suffix)) + varint(len(value)) + suffix + value
        last_key = key
    # 1 restart point at offset 0, then num_restarts=1
    data_block += struct.pack("<I", 0) + struct.pack("<I", 1)
    data_trailer = b"\x00\x00\x00\x00\x00"  # compression=0, crc=0

    # Index block: one entry pointing at the data block.
    data_offset = 0
    data_size = len(data_block)
    index_value = varint(data_offset) + varint(data_size)
    # Index entry key = last data key (largest). Use the last entry's key.
    index_key = entries[-1][0] if entries else b"\xff"
    index_block = bytearray()
    index_block += varint(0) + varint(len(index_key)) + varint(len(index_value)) + index_key + index_value
    index_block += struct.pack("<I", 0) + struct.pack("<I", 1)
    index_trailer = b"\x00\x00\x00\x00\x00"

    index_offset = data_offset + data_size + len(data_trailer)
    index_size = len(index_block)

    # Footer: metaindex_handle(0,0), index_handle, padding to 40 bytes, magic.
    footer = bytearray()
    footer += varint(0) + varint(0)  # metaindex handle
    footer += varint(index_offset) + varint(index_size)
    footer += b"\x00" * (40 - len(footer))
    footer += struct.pack("<Q", 0xDB4775248B80FB57)

    return bytes(data_block) + data_trailer + bytes(index_block) + index_trailer + bytes(footer)


def test_iter_sstable_entries_uncompressed_with_prefix_sharing():
    from server.browser_session import _iter_sstable_entries

    k1 = b"https://app.devin.ai\x00devin_account_id"
    k2 = b"https://app.devin.ai\x00devin_session_token"  # shares prefix with k1
    v1 = _localstorage_value("acct-xyz")
    v2 = _localstorage_value("sess-xyz")
    sstable = _build_uncompressed_sstable([(k1, v1), (k2, v2)])

    entries = list(_iter_sstable_entries(sstable))
    assert entries == [(k1, v1), (k2, v2)]


def test_extract_devin_tokens_from_ldb_sstable(tmp_path: Path):
    """End-to-end: tokens only in a compacted .ldb (not .log) are recovered."""
    tokens = {
        "devin_session_token": "sess-token",
        "devin_auth1_token": "auth1-token",
        "devin_account_id": "acct-id",
        "devin_primary_org_id": "org-id",
    }
    entries = [
        (f"https://app.devin.ai\x00{k}".encode("utf-8"), _localstorage_value(v))
        for k, v in tokens.items()
    ]
    sstable = _build_uncompressed_sstable(entries)
    leveldb = (
        tmp_path / "Google" / "Chrome" / "User Data" / "Default" / "Local Storage" / "leveldb"
    )
    leveldb.mkdir(parents=True)
    (leveldb / "000100.ldb").write_bytes(sstable)

    found = extract_devin_tokens(root=tmp_path)
    assert found == tokens
