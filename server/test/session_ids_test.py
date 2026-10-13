import pytest
from fastapi import HTTPException

from server.session_ids import session_id_is_valid, valid_session_id, validate_session_id


@pytest.mark.parametrize("value", [
    "s",
    "s-1",
    "session-A",
    "fake-1234567",
    "brave-newt-42",
    "with_underscore",
    "MixedCase-9",
    "0starts-with-digit",
    "00000000-0000-4000-8000-000000000002",
    "x" * 128,
    "COM10",
    "conman",
    "lpt10",
])
def test_valid_session_ids_are_accepted(value):
    assert session_id_is_valid(value)
    assert validate_session_id(value) is value


@pytest.mark.parametrize("value", [
    "",
    "-dash",
    "_underscore",
    ".dot",
    "..",
    "../x",
    "..\\x",
    "..\\..\\transcripts\\synthetic-secret",
    "a/b",
    "a\\b",
    "C:\\x",
    "\\\\server\\share",
    "has space",
    "has\ttab",
    "has\nnewline",
    "x" * 129,
    "x.json",
    "id\x00x",
    "id:x",
    "a:b",
    "ünïcödé",
    "trailing.",
    "trailing ",
    "CON",
    "con",
    "PRN",
    "AUX",
    "NUL",
    "nul",
    "COM1",
    "com9",
    "LPT1",
    "lpt9",
    None,
    0,
    1,
    1.5,
    True,
    False,
    b"s-1",
    ["s"],
    {"s": 1},
    object(),
])
def test_invalid_session_ids_are_rejected(value):
    assert not session_id_is_valid(value)
    with pytest.raises(ValueError, match="Invalid session ID"):
        validate_session_id(value)


def test_rest_dependency_returns_valid_id():
    assert valid_session_id("s-1") == "s-1"


@pytest.mark.parametrize("value", ["..", "..\\x", "NUL", "x" * 129])
def test_rest_dependency_maps_invalid_ids_to_422(value):
    with pytest.raises(HTTPException) as exc:
        valid_session_id(value)
    assert exc.value.status_code == 422
    assert exc.value.detail == "Invalid session ID"
