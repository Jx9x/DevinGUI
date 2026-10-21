from __future__ import annotations

import ctypes
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from scripts import export_conversation_to_md as exporter
from server import paths
from server.acp_client import ACPClient
from server.archive_manager import ArchiveManager
from server.compaction_settings import user_config_for_compaction
from server.db_reader import DBReader, _default_db_path
from server.model_registry import ModelRegistry, _cli_dir, _default_registry_dir
from server.transcript_watcher import _default_transcripts_dir
from server.usage_provider import _default_credentials_path

_native_documents_dir = paths._windows_documents_dir


@pytest.fixture(autouse=True)
def synthetic_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("DEVINGUI_DEVIN_BIN", raising=False)
    monkeypatch.delenv("DEVIN_ACP_CMD", raising=False)
    monkeypatch.delenv("DEVIN_GUI_ARCHIVE_DIR", raising=False)
    monkeypatch.delenv("DEVIN_SESSIONS_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    monkeypatch.setattr(paths, "_IS_WINDOWS", True)
    monkeypatch.setattr(paths, "_windows_documents_dir", lambda: tmp_path / "redirected Documents")


def test_shared_defaults_follow_current_windows_user(tmp_path):
    config = tmp_path / "roaming" / "devin"
    assert _default_db_path() == str(config / "cli" / "sessions.db")
    assert _default_transcripts_dir() == str(config / "cli" / "transcripts")
    assert _cli_dir() == str(tmp_path / "local" / "devin" / "cli")
    assert _default_registry_dir() == str(tmp_path / "roaming" / "devingui")
    assert _default_credentials_path() == config / "credentials.toml"
    config.mkdir(parents=True)
    (config / "config.json").write_text(json.dumps({"agent": {"model": "test-model"}}))
    assert user_config_for_compaction(123) == {
        "agent": {"model": "test-model", "compaction_threshold_tokens": 123},
    }


def test_shared_defaults_preserve_non_windows_fallbacks(tmp_path, monkeypatch):
    monkeypatch.delenv("APPDATA")
    monkeypatch.delenv("LOCALAPPDATA")
    home = tmp_path / "home"
    assert _default_db_path() == str(home / ".config" / "devin" / "cli" / "sessions.db")
    assert _default_transcripts_dir() == str(home / ".config" / "devin" / "cli" / "transcripts")
    assert _cli_dir() == str(home / ".local" / "share" / "devin" / "cli")
    assert _default_registry_dir() == str(home / ".config" / "devingui")
    assert _default_credentials_path() == home / ".config" / "devin" / "credentials.toml"


@pytest.mark.parametrize("appdata", [True, False])
@pytest.mark.parametrize("relative", [True, False])
def test_sessions_override_moves_database_but_preserves_other_cli_paths(tmp_path, monkeypatch, appdata, relative):
    if not appdata:
        monkeypatch.delenv("APPDATA")
    config = paths.devin_config_dir()
    monkeypatch.chdir(tmp_path)
    sessions_dir = Path("session storage") if relative else tmp_path / "session storage"
    monkeypatch.setenv("DEVIN_SESSIONS_DIR", str(sessions_dir))
    expected = str(sessions_dir / "cli" / "sessions.db")
    assert DBReader().db_path == _default_db_path() == exporter._default_db_path() == expected
    assert _default_transcripts_dir() == str(config / "cli" / "transcripts")
    assert exporter._default_transcripts_dir() == str(config / "cli" / "transcripts")
    assert _default_credentials_path() == config / "credentials.toml"
    assert _cli_dir() == str(tmp_path / "local" / "devin" / "cli")
    assert not sessions_dir.exists()


def test_empty_sessions_override_preserves_default_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVIN_SESSIONS_DIR", "")
    expected = str(tmp_path / "roaming" / "devin" / "cli" / "sessions.db")
    assert _default_db_path() == exporter._default_db_path() == expected


def test_executable_override_precedes_path_and_install_location(tmp_path, monkeypatch):
    executable = tmp_path / "Program Files" / "Devin" / "devin.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("DEVINGUI_DEVIN_BIN", str(executable))
    which = MagicMock(return_value=str(tmp_path / "other-devin.exe"))
    monkeypatch.setattr(paths.shutil, "which", which)
    assert paths.find_devin_binary() == str(executable)
    which.assert_not_called()


@pytest.mark.parametrize("configured", ["relative/devin.exe", "missing.exe", "."])
def test_invalid_override_does_not_silently_fall_back(tmp_path, monkeypatch, configured):
    value = configured if configured.startswith("relative") else str(tmp_path / configured)
    monkeypatch.setenv("DEVINGUI_DEVIN_BIN", value)
    which = MagicMock(return_value="another-devin.exe")
    monkeypatch.setattr(paths.shutil, "which", which)
    with pytest.raises(FileNotFoundError, match="DEVINGUI_DEVIN_BIN.*absolute.*executable"):
        paths.find_devin_binary()
    which.assert_not_called()


def test_path_returns_resolved_executable(tmp_path, monkeypatch):
    executable = tmp_path / "PATH bin" / "devin.exe"
    monkeypatch.setattr(paths.shutil, "which", lambda name: str(executable))
    assert paths.find_devin_binary() == str(executable)


def test_official_install_location_is_fallback(tmp_path):
    executable = tmp_path / "local" / "devin" / "cli" / "bin" / "devin.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    assert paths.find_devin_binary() == str(executable)


def test_missing_cli_identifies_expected_location_and_remedy(tmp_path):
    with pytest.raises(FileNotFoundError) as error:
        paths.find_devin_binary()
    message = str(error.value)
    assert str(tmp_path / "local" / "devin" / "cli" / "bin" / "devin.exe") in message
    assert "PATH" in message
    assert "DEVINGUI_DEVIN_BIN" in message
    assert "Install" in message


@pytest.mark.asyncio
async def test_acp_and_model_discovery_use_same_executable_without_shell_parsing(tmp_path, monkeypatch):
    executable = tmp_path / "Program Files" / "Devin" / "devin.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("DEVINGUI_DEVIN_BIN", str(executable))
    client = ACPClient(AsyncMock(), model="test-model")
    assert client.cmd == [str(executable), "--model", "test-model", "acp"]
    registry = ModelRegistry(registry_dir=str(tmp_path / "registry"))
    process = MagicMock(returncode=1)
    process.communicate = AsyncMock(return_value=(b"", b""))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("server.model_registry.asyncio.create_subprocess_exec", spawn)
    await registry.refresh_from_cli()
    assert spawn.call_args.args == (str(executable), "models", "list", "--format", "json")


def test_windows_override_preserves_backslashes(monkeypatch):
    configured = r"C:\Program Files\Devin\devin.exe"
    monkeypatch.setenv("DEVINGUI_DEVIN_BIN", configured)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "is_absolute", lambda self: True)
    assert ACPClient(AsyncMock()).cmd == [configured, "acp"]


def test_custom_acp_command_keeps_test_override_independent(monkeypatch):
    monkeypatch.setenv("DEVINGUI_DEVIN_BIN", "invalid.exe")
    monkeypatch.setenv("DEVIN_ACP_CMD", '"fake ACP" --test')
    assert ACPClient(AsyncMock()).cmd == ["fake ACP", "--test"]
    assert ACPClient(AsyncMock(), cmd=["explicit-fake"]).cmd == ["explicit-fake"]


def test_new_archives_use_known_documents_folder(tmp_path):
    assert paths.default_archive_root() == tmp_path / "redirected Documents" / "Devin_Chat"


def test_archive_override_precedes_legacy_and_known_documents(tmp_path, monkeypatch):
    legacy = tmp_path / "home" / "Documents" / "Devin_Chat"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("DEVIN_GUI_ARCHIVE_DIR", "~/custom archives")
    assert paths.default_archive_root() == tmp_path / "home" / "custom archives"


def test_existing_archive_root_and_index_are_preserved(tmp_path):
    legacy = tmp_path / "home" / "Documents" / "Devin_Chat"
    legacy.mkdir(parents=True)
    index = legacy / ".archive_index.json"
    original = json.dumps({"version": 1, "sessions": {"old-session": {"archive_path": "original"}}})
    index.write_text(original)
    manager = ArchiveManager(MagicMock(), MagicMock())
    assert manager.archive_root == legacy
    assert manager.archived_session_ids() == {"old-session"}
    assert index.read_text() == original
    assert not (tmp_path / "redirected Documents").exists()


def test_documents_lookup_failure_uses_home_fallback(tmp_path, monkeypatch):
    def unavailable():
        raise OSError("Known Folder lookup unavailable")
    monkeypatch.setattr(paths, "_windows_documents_dir", unavailable)
    assert paths.documents_dir() == tmp_path / "home" / "Documents"


def test_non_windows_documents_does_not_call_windows_api(tmp_path, monkeypatch):
    lookup = MagicMock(side_effect=AssertionError("unexpected Windows API call"))
    monkeypatch.setattr(paths, "_windows_documents_dir", lookup)
    monkeypatch.setattr(paths, "_IS_WINDOWS", False)
    assert paths.documents_dir() == tmp_path / "home" / "Documents"
    lookup.assert_not_called()


@pytest.mark.parametrize("operation", ["list_sessions", "list_projects", "get_prompt_history"])
def test_fresh_install_has_empty_lists_without_creating_cli_data(tmp_path, caplog, operation):
    database = tmp_path / "new profile" / "sessions.db"
    reader = DBReader(str(database))
    with caplog.at_level("INFO"):
        assert getattr(reader, operation)() == []
    assert str(database) in caplog.text
    assert "conversation" in caplog.text
    assert not database.parent.exists()


def test_new_database_is_picked_up_without_restarting_reader(tmp_path):
    from server.test.db_reader_test import _make_db

    database = tmp_path / "sessions.db"
    reader = DBReader(str(database))
    assert reader.list_sessions() == []
    _make_db(database)
    assert {session["id"] for session in reader.list_sessions()} == {"session-1", "empty-session"}


def test_missing_session_database_has_actionable_error(tmp_path):
    database = tmp_path / "missing" / "sessions.db"
    with pytest.raises(FileNotFoundError, match="Start a conversation") as error:
        DBReader(str(database)).get_session("unknown-session")
    assert str(database) in str(error.value)
    assert not database.exists()


@pytest.mark.parametrize("status,has_pointer", [(0, True), (-1, True), (-1, False), (0, False)])
def test_known_folder_api_handles_hresult_and_frees_native_memory(monkeypatch, status, has_pointer):
    buffer = ctypes.create_unicode_buffer(r"C:\Redirected Documents")

    def lookup(folder, flags, token, output):
        assert bytes(ctypes.cast(folder, ctypes.POINTER(paths._GUID)).contents) == UUID(
            "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
        ).bytes_le
        assert flags == 0
        assert token is None
        if has_pointer:
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(buffer, ctypes.c_void_p)
        return status

    free = MagicMock()
    libraries = {
        "shell32": SimpleNamespace(SHGetKnownFolderPath=MagicMock(side_effect=lookup)),
        "ole32": SimpleNamespace(CoTaskMemFree=free),
    }
    monkeypatch.setattr(ctypes, "WinDLL", libraries.__getitem__, raising=False)
    if status == 0 and has_pointer:
        assert _native_documents_dir() == Path(r"C:\Redirected Documents")
    else:
        with pytest.raises(OSError, match="Documents folder lookup failed"):
            _native_documents_dir()
    if has_pointer:
        free.assert_called_once()
        assert free.call_args.args[0].value == ctypes.cast(buffer, ctypes.c_void_p).value
    else:
        free.assert_not_called()


@pytest.mark.parametrize("operation", ["list_sessions", "list_projects", "get_prompt_history"])
def test_existing_unreadable_database_is_not_treated_as_empty(tmp_path, monkeypatch, operation):
    database = tmp_path / "sessions.db"
    database.touch()
    monkeypatch.setattr(sqlite3, "connect", MagicMock(side_effect=sqlite3.OperationalError("access denied")))
    with pytest.raises(sqlite3.OperationalError, match="Cannot read Devin session database") as error:
        getattr(DBReader(str(database)), operation)()
    assert str(database) in str(error.value)


@pytest.mark.parametrize("operation", ["list_sessions", "list_projects", "get_prompt_history"])
def test_existing_database_schema_errors_are_not_treated_as_empty(tmp_path, operation):
    database = tmp_path / "sessions.db"
    database.touch()
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        getattr(DBReader(str(database)), operation)()


@pytest.mark.asyncio
async def test_invalid_executable_override_preserves_model_catalog_and_logs_remedy(tmp_path, monkeypatch, caplog):
    registry = ModelRegistry(registry_dir=str(tmp_path / "registry"))
    catalog = {"last-good-model": {"display_name": "Kept"}}
    registry._cli_models = catalog
    monkeypatch.setenv("DEVINGUI_DEVIN_BIN", str(tmp_path / "missing-devin.exe"))
    spawn = AsyncMock()
    monkeypatch.setattr("server.model_registry.asyncio.create_subprocess_exec", spawn)
    await registry.refresh_from_cli()
    spawn.assert_not_called()
    assert registry._cli_models is catalog
    assert "DEVINGUI_DEVIN_BIN" in caplog.text
    assert str(tmp_path / "missing-devin.exe") in caplog.text


def test_database_paths_with_uri_characters_remain_read_only(tmp_path):
    directory = tmp_path / "profile #1"
    directory.mkdir()
    database = directory / "sessions.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('original')")
        conn.execute("CREATE TABLE sessions (model TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('stored-model')")
    conn = DBReader(str(database))._connect()
    try:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "original"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE sample SET value = 'changed'")
    finally:
        conn.close()
    registry = ModelRegistry(registry_dir=str(tmp_path / "registry"), db_path=str(database))
    assert registry.get_model("stored-model")["id"] == "stored-model"


@pytest.mark.parametrize("endpoint", ["/api/sessions", "/api/projects", "/api/prompt-history"])
def test_first_run_listing_routes_return_empty_success(tmp_path, monkeypatch, endpoint):
    from fastapi.testclient import TestClient
    from server import main

    database = tmp_path / "new profile" / "sessions.db"
    reader = DBReader(str(database))
    monkeypatch.setattr(main, "db_reader", reader)
    monkeypatch.setattr(main, "archive_manager", ArchiveManager(reader, MagicMock(), tmp_path / "archives"))
    client = TestClient(main.app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))
    response = client.get(endpoint)
    assert response.status_code == 200
    assert response.json() == []
    assert not database.parent.exists()
