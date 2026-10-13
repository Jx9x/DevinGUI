from __future__ import annotations

import json
import sys
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.codex_usage_provider import CodexUsageError, CodexUsageProvider, decode_rate_limits, find_codex_binary
from server.main import app


def limits(used=26):
    return {"primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": 1_790_888_939},
            "secondary": {"usedPercent": 20, "windowDurationMins": 10_080, "resetsAt": 1_791_401_270},
            "planType": "plus"}


def test_prefers_codex_bucket_over_legacy_and_model_specific_limits():
    data = decode_rate_limits({"rateLimits": limits(99), "rateLimitsByLimitId": {
        "codex_other": limits(75), "codex": limits(),
    }})
    assert data["primary"] == {"used_percent": 26, "window_minutes": 300, "reset_unix": 1_790_888_939}
    assert data["secondary"]["used_percent"] == 20


def test_legacy_response_keeps_missing_values_unknown():
    data = decode_rate_limits({"rateLimits": {"primary": {"usedPercent": None}, "secondary": None}})
    assert data["primary"] == {"used_percent": None, "window_minutes": None, "reset_unix": None}
    assert data["secondary"] is None


def test_does_not_substitute_an_unrelated_model_bucket():
    with pytest.raises(CodexUsageError, match="not available"):
        decode_rate_limits({"rateLimits": limits(), "rateLimitsByLimitId": {"codex_other": limits(75)}})


@pytest.mark.parametrize("value", [True, "26", float("nan"), float("inf")])
def test_rejects_invalid_percentages(value):
    with pytest.raises(CodexUsageError, match="invalid quota window"):
        decode_rate_limits({"rateLimits": limits(value)})


def test_clamps_percentages_and_accepts_a_single_window():
    data = decode_rate_limits({"rateLimits": {"primary": limits(125)["primary"], "secondary": None}})
    assert data["primary"]["used_percent"] == 100
    assert data["secondary"] is None


def fake_server(tmp_path, *, account_type="chatgpt", error=False, hang=False):
    script = tmp_path / "fake_codex.py"
    log = tmp_path / "requests.jsonl"
    script.write_text(f"""
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    with open({str(log)!r}, 'a') as log:
        log.write(json.dumps(message) + '\\n')
    method = message['method']
    if method == 'initialized':
        continue
    if {hang!r}:
        time.sleep(30)
    if method == 'initialize':
        result = {{'userAgent': 'fake'}}
    elif method == 'account/read':
        result = {{'account': {{'type': {account_type!r}, 'email': 'person@example.com', 'planType': 'plus'}}}}
    elif method == 'account/rateLimits/read':
        print(json.dumps({{'method': 'account/rateLimits/updated', 'params': {{}}}}), flush=True)
        if {error!r}:
            print(json.dumps({{'id': message['id'], 'error': {{'message': 'private-upstream-token'}}}}), flush=True)
            continue
        result = {{'rateLimitsByLimitId': {{'codex': {limits()!r}}}}}
    else:
        raise AssertionError('Unexpected operation: ' + method)
    print(json.dumps({{'id': message['id'], 'result': result}}), flush=True)
""", encoding="utf-8")
    return CodexUsageProvider([sys.executable, str(script)], timeout=5), log


def test_real_stdio_handshake_reads_quotas_without_starting_a_turn(tmp_path):
    provider, log = fake_server(tmp_path)
    snapshot = provider.snapshot()
    assert snapshot["error"] is None
    assert snapshot["account_email"] == "person@example.com"
    assert snapshot["primary"]["used_percent"] == 26
    assert snapshot["secondary"]["window_minutes"] == 10_080
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    assert [request["method"] for request in requests] == [
        "initialize", "initialized", "account/read", "account/rateLimits/read",
    ]
    assert requests[2]["params"] == {"refreshToken": False}


@pytest.mark.parametrize("account_type", [None, "apiKey"])
def test_requires_chatgpt_sign_in_without_prompting_for_login(tmp_path, account_type):
    provider, log = fake_server(tmp_path, account_type=account_type)
    snapshot = provider.snapshot()
    assert "codex login" in snapshot["error"]
    assert snapshot["primary"] is None
    assert "account/rateLimits/read" not in log.read_text()


def test_timeout_terminates_the_usage_process(tmp_path):
    provider, _ = fake_server(tmp_path, hang=True)
    provider.timeout = 0.3
    start = time.monotonic()
    snapshot = provider.snapshot()
    assert "timed out" in snapshot["error"]
    assert time.monotonic() - start < 4


def test_rpc_errors_do_not_expose_upstream_details(tmp_path):
    provider, _ = fake_server(tmp_path, error=True)
    snapshot = provider.snapshot()
    assert snapshot["error"] is not None
    assert "private-upstream-token" not in json.dumps(snapshot)


def test_caches_reads_and_marks_previous_quotas_stale_after_failure(tmp_path):
    provider, _ = fake_server(tmp_path)
    first = provider.snapshot()
    with patch.object(provider, "_read_limits", side_effect=CodexUsageError("Refresh failed")) as fetch:
        assert provider.snapshot() == first
        fetch.assert_not_called()
        stale = provider.snapshot(force=True)
    assert stale["primary"] == first["primary"]
    assert stale["fetched_at"] == first["fetched_at"]
    assert stale["stale"] is True
    assert stale["error"] == "Refresh failed"
    provider.snapshot(force=True)
    assert provider.snapshot()["stale"] is False


def test_configured_binary_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVINGUI_CODEX_BIN", str(tmp_path / "missing.exe"))
    with pytest.raises(CodexUsageError, match="must point"):
        find_codex_binary()


def test_codex_usage_endpoint_is_independent_and_disables_http_caching(tmp_path):
    provider, _ = fake_server(tmp_path)
    with patch("server.main._codex_usage_provider", provider), patch("server.main.UsageProvider.fetch") as devin_fetch:
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/usage/codex?refresh=true")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["primary"]["used_percent"] == 26
    devin_fetch.assert_not_called()


def test_codex_endpoint_returns_200_when_not_signed_in(tmp_path):
    provider, _ = fake_server(tmp_path, account_type="apiKey")
    with patch("server.main._codex_usage_provider", provider):
        response = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)).get("/api/usage/codex")
    assert response.status_code == 200
    assert response.json()["primary"] is None
    assert response.json()["error"] is not None
