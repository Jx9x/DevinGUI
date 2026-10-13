"""Fetch account quotas from GetPlanStatus (ConnectRPC over protobuf).

ACP does not expose account quotas. The provider uses the ``windsurf_api_key``
loaded from Devin Desktop's ``credentials.toml``. A minimal protobuf codec
handles the request and quota fields.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .paths import default_credentials_path as _default_credentials_path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 and earlier
    import tomli as tomllib

logger = logging.getLogger(__name__)

GET_PLAN_STATUS_URL = (
    "https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetPlanStatus"
)

# -- protobuf wire format -------------------------------------------------

_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_LENGTH_DELIMITED = 2
_WIRE_FIXED32 = 5


def _encode_varint(value: int) -> bytes:
    if value < 0:
        # Treat negative values as unsigned 64-bit (two's complement).
        value &= (1 << 64) - 1
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _encode_field_key(field_number: int, wire_type: int) -> bytes:
    return _encode_varint((field_number << 3) | wire_type)


def _encode_string_field(field_number: int, text: str) -> bytes:
    encoded = text.encode("utf-8")
    return _encode_field_key(field_number, _WIRE_LENGTH_DELIMITED) + _encode_varint(len(encoded)) + encoded


def _encode_varint_field(field_number: int, value: int) -> bytes:
    return _encode_field_key(field_number, _WIRE_VARINT) + _encode_varint(value)


def encode_request(auth_token: str, include_top_up_status: bool = True) -> bytes:
    """Encode the GetPlanStatus request protobuf.

    Field 1 (string): auth_token
    Field 2 (bool/varint): include_top_up_status
    """
    return _encode_string_field(1, auth_token) + _encode_varint_field(
        2, 1 if include_top_up_status else 0
    )


class _ProtoReader:
    """Minimal forward-only protobuf reader."""

    def __init__(self, data: bytes):
        self._data = data
        self._index = 0

    @property
    def at_end(self) -> bool:
        return self._index >= len(self._data)

    def read_varint(self) -> int:
        result = 0
        shift = 0
        while self._index < len(self._data):
            byte = self._data[self._index]
            self._index += 1
            result |= (byte & 0x7F) << shift
            if byte & 0x80 == 0:
                return result
            shift += 7
            if shift >= 64:
                raise ValueError("truncated protobuf varint")
        raise ValueError("truncated protobuf varint")

    def read_length_delimited(self) -> bytes:
        length = self.read_varint()
        if length < 0 or self._index + length > len(self._data):
            raise ValueError("truncated length-delimited field")
        chunk = self._data[self._index : self._index + length]
        self._index += length
        return chunk

    def read_string(self) -> str:
        return self.read_length_delimited().decode("utf-8")

    def next_field(self) -> Optional[tuple[int, int]]:
        if self.at_end:
            return None
        key = self.read_varint()
        field_number = key >> 3
        if field_number <= 0:
            raise ValueError("malformed protobuf field key")
        wire_type = key & 0x07
        return field_number, wire_type

    def skip(self, wire_type: int) -> None:
        if wire_type == _WIRE_VARINT:
            self.read_varint()
        elif wire_type == _WIRE_FIXED64:
            self._index += 8
        elif wire_type == _WIRE_LENGTH_DELIMITED:
            self.read_length_delimited()
        elif wire_type == _WIRE_FIXED32:
            self._index += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")


def _decode_timestamp(data: bytes) -> Optional[int]:
    """Decode Timestamp seconds, ignoring nanos and defaulting missing seconds to 0."""
    reader = _ProtoReader(data)
    seconds = 0
    while True:
        field = reader.next_field()
        if field is None:
            break
        number, wire = field
        if number == 1 and wire == _WIRE_VARINT:
            seconds = reader.read_varint()
        else:
            reader.skip(wire)
    return seconds


def decode_response(data: bytes) -> dict[str, Any]:
    """Return plan name, plan end/reset Unix times, and remaining quota percentages.

    Missing response fields are ``None``.
    """
    result: dict[str, Any] = {
        "plan_name": None,
        "plan_end_unix": None,
        "daily_remaining_percent": None,
        "weekly_remaining_percent": None,
        "daily_reset_unix": None,
        "weekly_reset_unix": None,
    }
    reader = _ProtoReader(data)
    while True:
        field = reader.next_field()
        if field is None:
            break
        number, wire = field
        if number == 1 and wire == _WIRE_LENGTH_DELIMITED:
            _decode_plan_status(reader.read_length_delimited(), result)
        else:
            reader.skip(wire)
    return result


def _decode_plan_status(data: bytes, result: dict[str, Any]) -> None:
    reader = _ProtoReader(data)
    while True:
        field = reader.next_field()
        if field is None:
            break
        number, wire = field
        if number == 1 and wire == _WIRE_LENGTH_DELIMITED:
            result["plan_name"] = _decode_plan_info(reader.read_length_delimited())
        elif number == 3 and wire == _WIRE_LENGTH_DELIMITED:
            result["plan_end_unix"] = _decode_timestamp(reader.read_length_delimited())
        elif number == 14 and wire == _WIRE_VARINT:
            result["daily_remaining_percent"] = reader.read_varint()
        elif number == 15 and wire == _WIRE_VARINT:
            result["weekly_remaining_percent"] = reader.read_varint()
        elif number == 17 and wire == _WIRE_VARINT:
            result["daily_reset_unix"] = reader.read_varint()
        elif number == 18 and wire == _WIRE_VARINT:
            result["weekly_reset_unix"] = reader.read_varint()
        else:
            reader.skip(wire)


def _decode_plan_info(data: bytes) -> Optional[str]:
    reader = _ProtoReader(data)
    plan_name: Optional[str] = None
    while True:
        field = reader.next_field()
        if field is None:
            break
        number, wire = field
        if number == 2 and wire == _WIRE_LENGTH_DELIMITED:
            plan_name = reader.read_string()
        else:
            reader.skip(wire)
    return plan_name


# -- HTTP fetch -----------------------------------------------------------


class UsageProviderError(Exception):
    """Raised when the GetPlanStatus call fails or the payload is unusable."""


class UsageProvider:
    """Calls GetPlanStatus with the windsurf_api_key from credentials.toml."""

    def __init__(self, api_key: str, *, url: str = GET_PLAN_STATUS_URL):
        if not api_key:
            raise UsageProviderError("missing windsurf_api_key")
        self._api_key = api_key
        self._url = url

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/proto",
            "Connect-Protocol-Version": "1",
            "x-auth-token": self._api_key,
        }

    async def fetch(self, *, timeout: float = 15.0) -> dict[str, Any]:
        """POST the protobuf request and return the decoded quota dict.

        Raises ``UsageProviderError`` on network errors, non-200 responses,
        or unparseable protobuf.
        """
        import httpx

        body = encode_request(self._api_key, include_top_up_status=True)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(self._url, content=body, headers=self._headers())
        except Exception as e:
            raise UsageProviderError(f"GetPlanStatus request failed: {e}") from e
        if response.status_code != 200:
            snippet = response.text.strip()[:200] if response.text else f"<binary {len(response.content)} bytes>"
            raise UsageProviderError(f"GetPlanStatus HTTP {response.status_code}: {snippet}")
        try:
            return decode_response(response.content)
        except ValueError as e:
            raise UsageProviderError(f"GetPlanStatus parse error: {e}") from e


def load_api_key(credentials_path: Optional[Path] = None) -> Optional[str]:
    """Read the ``windsurf_api_key`` from Devin Desktop's credentials.toml.

    Returns ``None`` if the file is missing, unreadable, invalid TOML, or lacks the key.
    """
    path = credentials_path or _default_credentials_path()
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return data.get("windsurf_api_key")
    except Exception as e:
        logger.warning("Could not read %s: %s", path, e)
        return None
