"""``run.py`` must bind loopback only and never trust forwarded headers."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

import run


@pytest.fixture(autouse=True)
def synthetic_startup_checks(monkeypatch):
    monkeypatch.setattr(run, "print_startup_checks", lambda root: None)


def _run_main(*argv):
    with (
        patch.object(sys, "argv", ["run.py", *argv]),
        patch("uvicorn.run") as uvicorn_run,
    ):
        run.main()
    return uvicorn_run


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "127.0.0.1"),
    ("localhost", "127.0.0.1"),
    ("LOCALHOST", "127.0.0.1"),
    ("::1", "::1"),
    ("::ffff:127.0.0.1", "127.0.0.1"),
    ("::ffff:7f00:1", "127.0.0.1"),
])
def test_loopback_hosts_bind_normalized_with_proxy_headers_off(host, expected):
    uvicorn_run = _run_main("--host", host)
    uvicorn_run.assert_called_once()
    kwargs = uvicorn_run.call_args.kwargs
    assert kwargs["host"] == expected
    assert kwargs["proxy_headers"] is False


def test_default_host_binds_loopback():
    uvicorn_run = _run_main()
    assert uvicorn_run.call_args.kwargs["host"] == "127.0.0.1"
    assert uvicorn_run.call_args.kwargs["proxy_headers"] is False


@pytest.mark.parametrize("host", [
    "0.0.0.0",
    "::",
    "0",
    "203.0.113.10",
    "192.168.1.20",
    "10.0.0.5",
    "8.8.8.8",
    "2001:db8::1",
    "evil.test",
    "localhost.evil.test",
    "devin.local",
    "not-an-ip",
    "localhost.",
    "127.1",
    "999.999.999.999",
    "127.000.0.1",
    "fe80::1%eth0",
    "::1%lo",
    "::ffff:203.0.113.10",
    "::ffff:8.8.8.8",
])
def test_non_loopback_or_invalid_hosts_exit_before_uvicorn(host):
    with (
        patch.object(sys, "argv", ["run.py", "--host", host]),
        patch("uvicorn.run") as uvicorn_run,
        pytest.raises(SystemExit) as excinfo,
    ):
        run.main()
    assert excinfo.value.code == 2
    uvicorn_run.assert_not_called()
