from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import cli_diagnostics, diagnostics
from server.db_reader import DBReader
from server.diagnostics_routes import create_diagnostics_router
from server.test.db_reader_test import _make_db


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("DEVINGUI_CHATGPT_PROFILE", str(tmp_path / "companion"))
    monkeypatch.setattr(diagnostics, "find_codex_binary", MagicMock(side_effect=diagnostics.CodexUsageError("Install Codex")))
    monkeypatch.setattr(diagnostics, "find_devin_binary", lambda: "synthetic-devin")
    monkeypatch.setattr(diagnostics, "probe_devin", lambda *args, **kwargs: {
        "status": "ok", "version": cli_diagnostics.TESTED_CLI_VERSION,
        "tested_version": cli_diagnostics.TESTED_CLI_VERSION, "executable": "synthetic-devin",
        "compaction_supported": True, "message": "Current tested CLI",
    })
    monkeypatch.setattr(diagnostics, "probe_devin_auth", lambda *args: {
        "authenticated": True, "status": "ok", "message": "Signed in",
    })
    monkeypatch.setattr(diagnostics, "tool_version", lambda name: None)
    return SimpleNamespace(reader=DBReader(str(tmp_path / "cli" / "sessions.db")),
                           transcripts=str(tmp_path / "transcripts"), frontend=tmp_path / "frontend")


def snapshot(environment):
    return diagnostics.collect_diagnostics(environment.reader, environment.transcripts, environment.frontend)


def test_fresh_profile_explains_setup_without_creating_cli_data(environment):
    result = snapshot(environment)
    database = next(check for check in result["checks"] if check["id"] == "database")
    assert database["status"] == "setup_required"
    assert "devin auth login" in database["message"]
    assert "same Windows account" in database["message"]
    assert database["path"] == environment.reader.db_path
    assert result["history_ready"] is False
    assert not Path(environment.reader.db_path).parent.exists()
    assert not Path(environment.transcripts).exists()
    assert "credentials.toml" not in str(result["optional_integrations"]["devin_usage"]["message"])


def test_retry_picks_up_database_and_complete_frontend(environment):
    Path(environment.reader.db_path).parent.mkdir()
    _make_db(Path(environment.reader.db_path))
    (environment.frontend / "assets").mkdir(parents=True)
    (environment.frontend / "index.html").write_text("<html></html>")
    result = snapshot(environment)
    assert result["history_ready"] is True
    assert next(check for check in result["checks"] if check["id"] == "frontend")["status"] == "ok"
    assert next(check for check in result["checks"] if check["id"] == "transcripts")["status"] == "warning"
    assert result["status"] == "ready"


def test_logout_requires_sign_in_even_when_history_and_frontend_are_ready(environment, monkeypatch):
    Path(environment.reader.db_path).parent.mkdir()
    _make_db(Path(environment.reader.db_path))
    (environment.frontend / "assets").mkdir(parents=True)
    (environment.frontend / "index.html").write_text("<html></html>")
    probe = MagicMock(side_effect=[
        {"authenticated": False, "status": "setup_required", "message": "Run devin auth login"},
        {"authenticated": True, "status": "ok", "message": "Signed in"},
    ])
    monkeypatch.setattr(diagnostics, "probe_devin_auth", probe)
    logged_out = snapshot(environment)
    assert logged_out["status"] == "setup_required"
    assert logged_out["history_ready"] is True
    check = next(check for check in logged_out["checks"] if check["id"] == "devin_auth")
    assert check["status"] == "setup_required"
    assert "devin auth login" in check["message"]
    assert snapshot(environment)["status"] == "ready"
    assert probe.call_count == 2


def test_schema_reports_required_columns_but_accepts_additions(environment):
    database = Path(environment.reader.db_path)
    database.parent.mkdir()
    _make_db(database)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE sessions ADD COLUMN future_field TEXT")
    assert environment.reader.inspect_schema()["status"] == "ok"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE message_nodes RENAME COLUMN chat_message TO changed_message")
    result = environment.reader.inspect_schema()
    assert result["status"] == "incompatible"
    assert "message_nodes.chat_message" in result["missing"]
    assert snapshot(environment)["history_ready"] is False


def test_corrupt_database_is_not_mistaken_for_a_fresh_profile(environment):
    database = Path(environment.reader.db_path)
    database.parent.mkdir()
    database.write_bytes(b"not a SQLite database")
    assert environment.reader.inspect_schema()["status"] == "corrupt"
    database_check = next(check for check in snapshot(environment)["checks"] if check["id"] == "database")
    assert database_check["status"] == "error"
    assert "corrupt" in database_check["message"]
    assert database.read_bytes() == b"not a SQLite database"


@pytest.mark.parametrize("failure", [PermissionError("Access denied"), sqlite3.OperationalError("Database locked")])
def test_database_access_failure_is_separate_from_corruption(environment, monkeypatch, failure):
    monkeypatch.setattr(environment.reader, "_connect", MagicMock(side_effect=failure))
    assert environment.reader.inspect_schema()["status"] == "unreadable"
    database_check = next(check for check in snapshot(environment)["checks"] if check["id"] == "database")
    assert database_check["status"] == "error"
    assert "permissions" in database_check["message"]


def test_partial_frontend_and_missing_build_tools_are_actionable(environment):
    environment.frontend.mkdir()
    result = snapshot(environment)
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["frontend"]["status"] == "error"
    assert "setup-devingui.ps1" in checks["frontend"]["message"]
    assert checks["node"]["status"] == "warning"
    assert "not required for normal launch" in checks["node"]["message"]


def test_optional_checks_never_read_credentials_or_contact_providers(environment, monkeypatch):
    credentials = diagnostics.default_credentials_path()
    credentials.parent.mkdir(parents=True)
    credentials.write_text("private-token=DO-NOT-EXPOSE")
    discovery = diagnostics.ChatOnSteroidsClient().profile / "devin-gui-api" / "connection.json"
    discovery.parent.mkdir(parents=True)
    discovery.write_text("private-token=DO-NOT-EXPOSE")
    monkeypatch.setattr(diagnostics, "find_codex_binary", lambda: "synthetic-codex")
    result = snapshot(environment)
    assert all(check["status"] == "ok" for check in result["optional_integrations"].values())
    assert "DO-NOT-EXPOSE" not in str(result)


def test_route_rechecks_readiness_without_changing_health_contract(environment):
    app = FastAPI()
    app.include_router(create_diagnostics_router(
        get_db_reader=lambda: environment.reader,
        get_transcripts_dir=lambda: environment.transcripts,
        frontend_dist=environment.frontend,
    ))
    with TestClient(app) as client:
        assert client.get("/api/diagnostics").json()["history_ready"] is False
        Path(environment.reader.db_path).parent.mkdir()
        _make_db(Path(environment.reader.db_path))
        assert client.get("/api/diagnostics?refresh=true").json()["history_ready"] is True


@pytest.mark.parametrize("version,status", [("3000.11.3", "ok"), ("3000.12.0", "warning")])
def test_cli_probe_records_one_tested_version_and_safe_compaction_probe(tmp_path, monkeypatch, version, status):
    executable = tmp_path / "devin.exe"
    executable.touch()
    run = MagicMock(side_effect=[
        subprocess.CompletedProcess([], 0, f"devin {version} (synthetic)\n", ""),
        subprocess.CompletedProcess([], 0, "Usage: devin.exe acp [OPTIONS]\n", ""),
    ])
    monkeypatch.setattr(cli_diagnostics.subprocess, "run", run)
    result = cli_diagnostics.probe_devin(str(executable), refresh=True)
    assert result["status"] == status
    assert result["tested_version"] == "3000.11.3"
    assert result["version"] == version
    assert result["compaction_supported"] is True
    assert run.call_args_list[1].args[0] == [str(executable), "--compaction-thresholds", "1,1,1", "acp", "--help"]
    assert all(call.kwargs["timeout"] == 5 for call in run.call_args_list)
    cli_diagnostics.probe_devin(str(executable))
    assert run.call_count == 2


def test_probe_does_not_forward_cli_stderr(tmp_path, monkeypatch):
    executable = tmp_path / "devin.exe"
    executable.touch()
    monkeypatch.setattr(cli_diagnostics.subprocess, "run", MagicMock(side_effect=[
        subprocess.CompletedProcess([], 0, "devin 3000.11.3", ""),
        subprocess.CompletedProcess([], 2, "", "unexpected argument and private-token"),
    ]))
    result = cli_diagnostics.probe_devin(str(executable), refresh=True)
    assert result["compaction_supported"] is False
    assert "private-token" not in str(result)


def test_diagnostics_is_protected_by_existing_loopback_boundary(monkeypatch):
    from server import main
    collect = MagicMock(side_effect=AssertionError("must not reach checks"))
    monkeypatch.setattr("server.diagnostics_routes.collect_diagnostics", collect)
    client = TestClient(main.app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))
    assert client.get("/api/diagnostics", headers={"Origin": "https://unrelated.test"}).status_code == 403
    collect.assert_not_called()


def test_schema_inspection_is_read_only_and_incomplete_optional_features_are_separate(environment):
    database = Path(environment.reader.db_path)
    database.parent.mkdir()
    _make_db(database)
    before = database.read_bytes()
    result = environment.reader.inspect_schema()
    assert result["status"] == "ok"
    assert result["features"]["rendered_history"]
    assert database.read_bytes() == before


def test_unsupported_node_is_a_build_warning_not_a_chat_failure(environment, monkeypatch):
    monkeypatch.setattr(diagnostics, "tool_version", lambda name: "v23.9.0" if name == "node" else "11.0.0")
    result = snapshot(environment)
    node = next(check for check in result["checks"] if check["id"] == "node")
    assert node["status"] == "warning"
    assert "22.13+" in node["message"]


def test_probe_timeout_is_bounded_and_retryable(tmp_path, monkeypatch):
    executable = tmp_path / "devin.exe"
    executable.touch()
    monkeypatch.setattr(cli_diagnostics.subprocess, "run", MagicMock(side_effect=subprocess.TimeoutExpired("devin", 5)))
    result = cli_diagnostics.probe_devin(str(executable), refresh=True)
    assert result["status"] == "error"
    assert result["version"] is None
    assert result["compaction_supported"] is None
    assert "Retry" in result["message"]
