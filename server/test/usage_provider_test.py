from server.usage_provider import (
    UsageProvider,
    UsageProviderError,
    decode_response,
    encode_request,
    _ProtoReader,
)


def test_encode_request_round_trips_auth_token_and_bool():
    body = encode_request("abc-123", include_top_up_status=True)
    # Field 1 (auth_token): tag 0x0a, length 7, "abc-123"
    assert body[:1] == b"\x0a"
    assert body[1:2] == b"\x07"
    assert body[2:9] == b"abc-123"
    # Field 2 (include_top_up_status): tag 0x10, varint 1
    assert body[9:11] == b"\x10\x01"


def test_encode_request_false_bool():
    body = encode_request("tok", include_top_up_status=False)
    assert body.endswith(b"\x10\x00")


def test_decode_request_via_reader():
    body = encode_request("secret-token", include_top_up_status=True)
    reader = _ProtoReader(body)
    fields = []
    while True:
        f = reader.next_field()
        if f is None:
            break
        number, wire = f
        if number == 1:
            fields.append((1, reader.read_string()))
        elif number == 2:
            fields.append((2, reader.read_varint()))
        else:
            reader.skip(wire)
    assert fields == [(1, "secret-token"), (2, 1)]


def _encode_timestamp(seconds: int) -> bytes:
    # field 1 varint seconds
    out = bytearray()
    tag = (1 << 3) | 0  # varint
    # varint encode tag
    v = tag
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    v = seconds
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _ld(field_number: int, payload: bytes) -> bytes:
    """Encode a length-delimited field."""
    tag = (field_number << 3) | 2
    out = bytearray()
    v = tag
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    v = len(payload)
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    out += payload
    return bytes(out)


def _varint_field(field_number: int, value: int) -> bytes:
    tag = (field_number << 3) | 0
    out = bytearray()
    v = tag
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    v = value
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def test_decode_response_extracts_quota_fields():
    plan_info = _ld(2, b"Pro")  # field 2 = plan_name
    plan_status = (
        _ld(1, plan_info)
        + _ld(3, _encode_timestamp(1_750_000_000))  # plan_end
        + _varint_field(14, 95)  # daily_remaining_percent
        + _varint_field(15, 90)  # weekly_remaining_percent
        + _varint_field(17, 1_750_100_000)  # daily_reset_unix
        + _varint_field(18, 1_750_200_000)  # weekly_reset_unix
    )
    response = _ld(1, plan_status)

    decoded = decode_response(response)
    assert decoded["plan_name"] == "Pro"
    assert decoded["plan_end_unix"] == 1_750_000_000
    assert decoded["daily_remaining_percent"] == 95
    assert decoded["weekly_remaining_percent"] == 90
    assert decoded["daily_reset_unix"] == 1_750_100_000
    assert decoded["weekly_reset_unix"] == 1_750_200_000


def test_decode_response_missing_fields_are_none():
    assert decode_response(b"") == {
        "plan_name": None,
        "plan_end_unix": None,
        "daily_remaining_percent": None,
        "weekly_remaining_percent": None,
        "daily_reset_unix": None,
        "weekly_reset_unix": None,
    }


def test_decode_response_skips_unknown_fields():
    # Insert an unknown varint field and an unknown length-delimited field;
    # the decoder must skip them and still parse the known ones.
    plan_status = _varint_field(99, 12345) + _ld(200, b"junk") + _varint_field(15, 80)
    response = _ld(1, plan_status) + _ld(7, b"top-level-junk")
    decoded = decode_response(response)
    assert decoded["weekly_remaining_percent"] == 80


def test_usage_provider_rejects_missing_api_key():
    try:
        UsageProvider("")
    except UsageProviderError as e:
        assert "api_key" in str(e).lower()
    else:
        raise AssertionError("expected UsageProviderError for missing api key")
