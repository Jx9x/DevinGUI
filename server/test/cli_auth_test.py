from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from server import cli_diagnostics
from server.acp_client import ACPClient


@pytest.mark.parametrize("line,authenticated,status", [
    ("Not logged in.", False, "setup_required"),
    ("Logged in (via Devin).", True, "ok"),
    ("Logged in.", True, "ok"),
    ("Unrecognized status.", None, "warning"),
    ("", None, "warning"),
])
def test_auth_status_handles_zero_exit_and_excludes_account_details(monkeypatch, line, authenticated, status):
    stdout = line + "\n\nEmail: private@example.test\nAPI key: synthetic-secret" if line else ""
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout, "synthetic-secret"))
    monkeypatch.setattr(cli_diagnostics.subprocess, "run", run)
    result = cli_diagnostics.probe_devin_auth("synthetic-devin")
    assert result["authenticated"] is authenticated
    assert result["status"] == status
    assert "synthetic-secret" not in str(result)
    assert "private@example.test" not in str(result)
    if authenticated is False:
        assert "devin auth login" in result["message"]
    assert run.call_args.args[0] == ["synthetic-devin", "auth", "status"]
    assert run.call_args.kwargs["timeout"] == 30
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("failure", [
    PermissionError("synthetic-secret"),
    subprocess.TimeoutExpired("synthetic-secret", 5),
])
def test_auth_probe_failure_is_unknown_and_does_not_expose_output(monkeypatch, failure):
    monkeypatch.setattr(cli_diagnostics.subprocess, "run", MagicMock(side_effect=failure))
    result = cli_diagnostics.probe_devin_auth("synthetic-devin")
    assert result["authenticated"] is None
    assert result["status"] == "warning"
    assert "devin auth status" in result["message"]
    assert "synthetic-secret" not in str(result)


def test_auth_check_allows_slow_cli_initialization(monkeypatch):
    def slow_status(command, **kwargs):
        # Model a CLI that spends twelve seconds waiting on startup DNS.
        if kwargs["timeout"] < 12:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "Logged in (via Devin).\n", "")

    monkeypatch.setattr(cli_diagnostics.subprocess, "run", slow_status)
    result = cli_diagnostics.probe_devin_auth("synthetic-devin")
    assert result["authenticated"] is True
    assert result["status"] == "ok"


def test_auth_timeout_explains_network_delay_without_claiming_logout(monkeypatch):
    failure = subprocess.TimeoutExpired(
        "synthetic-devin", 30, output=b"Not logged in.\nAPI key: synthetic-secret",
        stderr=b"private@example.test",
    )
    monkeypatch.setattr(cli_diagnostics.subprocess, "run", MagicMock(side_effect=failure))
    result = cli_diagnostics.probe_devin_auth("synthetic-devin")
    assert result["authenticated"] is None
    assert result["status"] == "warning"
    assert "timed out after 30 seconds" in result["message"]
    assert "DNS or network services" in result["message"]
    assert "does not establish that you are signed out" in result["message"]
    assert "devin auth login" not in result["message"]
    assert "synthetic-secret" not in str(result)
    assert "private@example.test" not in str(result)


def test_auth_command_failure_cannot_establish_logout(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 1, "Not logged in.", "synthetic-secret"))
    monkeypatch.setattr(cli_diagnostics.subprocess, "run", run)
    assert cli_diagnostics.probe_devin_auth("synthetic-devin")["authenticated"] is None


@pytest.fixture
def real_cli_client(monkeypatch, tmp_path):
    monkeypatch.delenv("DEVIN_ACP_CMD", raising=False)
    monkeypatch.setattr("server.acp_client._find_devin_binary", lambda: "synthetic-devin")
    return ACPClient(AsyncMock(), cwd=str(tmp_path), model="gpt-6-1-sol-xhigh")


async def test_logged_out_start_is_actionable_and_rechecks_after_login(real_cli_client, monkeypatch):
    probe = MagicMock(side_effect=[
        {"authenticated": False, "message": "Devin CLI is not signed in. Run devin auth login."},
        {"authenticated": True},
    ])
    spawn = AsyncMock(side_effect=OSError("synthetic launch"))
    monkeypatch.setattr("server.acp_client.probe_devin_auth", probe)
    monkeypatch.setattr("server.acp_client.asyncio.create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="devin auth login"):
        await real_cli_client.start()
    spawn.assert_not_called()
    assert real_cli_client.process is None
    assert not real_cli_client._pending
    with pytest.raises(OSError, match="synthetic launch"):
        await real_cli_client.start()
    assert probe.call_count == 2
    spawn.assert_awaited_once()
    assert spawn.call_args.args[0] == "synthetic-devin"


async def test_unknown_auth_status_does_not_block_acp(real_cli_client, monkeypatch):
    monkeypatch.setattr("server.acp_client.probe_devin_auth", lambda *args: {"authenticated": None})
    spawn = AsyncMock(side_effect=OSError("synthetic launch"))
    monkeypatch.setattr("server.acp_client.asyncio.create_subprocess_exec", spawn)
    with pytest.raises(OSError, match="synthetic launch"):
        await real_cli_client.start()
    spawn.assert_awaited_once()


@pytest.mark.parametrize("custom_command", ["argument", "environment"])
async def test_synthetic_acp_commands_skip_real_auth_checks(monkeypatch, custom_command):
    probe = MagicMock(side_effect=AssertionError("must not check real authentication"))
    monkeypatch.setattr("server.acp_client.probe_devin_auth", probe)
    spawn = AsyncMock(side_effect=OSError("synthetic launch"))
    monkeypatch.setattr("server.acp_client.asyncio.create_subprocess_exec", spawn)
    if custom_command == "environment":
        monkeypatch.setenv("DEVIN_ACP_CMD", "synthetic-acp")
        client = ACPClient(AsyncMock())
    else:
        client = ACPClient(AsyncMock(), cmd=["synthetic-acp"])
    with pytest.raises(OSError, match="synthetic launch"):
        await client.start()
    probe.assert_not_called()
    spawn.assert_awaited_once()
