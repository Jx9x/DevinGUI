"""Profile-isolation contract for the fake-ACP E2E backend launcher."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import start_e2e_backend as helper
from server.db_reader import DBReader
from server.model_registry import ModelRegistry

ISOLATED_ENV_VARS = (
    "APPDATA",
    "LOCALAPPDATA",
    "HOME",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "DEVIN_GUI_ARCHIVE_DIR",
    "DEVINGUI_CHATGPT_PROFILE",
    "DEVINGUI_CODEX_BIN",
    "DEVINGUI_DEVIN_BIN",
    "FAKE_ACP_OUTPUT_FILE",
)


@pytest.fixture
def preserved_env():
    saved = dict(os.environ)
    cwd = os.getcwd()
    yield
    os.environ.clear()
    os.environ.update(saved)
    os.chdir(cwd)


def test_configure_profile_redirects_all_paths_under_root(tmp_path, preserved_env):
    root = tmp_path / "run-root"
    root.mkdir()

    helper.configure_profile(root)

    for var in ISOLATED_ENV_VARS:
        value = Path(os.environ[var])
        assert value.is_relative_to(root), f"{var} escaped the run root: {value}"
        assert not value.is_relative_to(_REPO_ROOT), f"{var} still points at the repo"
    assert os.environ["DEVIN_GRACE_SECONDS"] == "0"
    assert os.environ["DEVINGUI_CODEX_BIN"].endswith("disabled-codex.exe")
    assert not Path(os.environ["DEVINGUI_CODEX_BIN"]).exists()
    assert os.environ["DEVINGUI_DEVIN_BIN"].endswith("disabled-devin.exe")
    assert not Path(os.environ["DEVINGUI_DEVIN_BIN"]).exists()
    output = Path(os.environ["FAKE_ACP_OUTPUT_FILE"])
    assert output.name == "fake_acp_last_permission_response.json"
    assert output.parent == root / "test-results"
    assert not output.exists(), "helper must not pre-create the shared-artifact name"


def test_acp_command_uses_current_interpreter_with_quoted_paths(tmp_path, preserved_env, monkeypatch):
    root = tmp_path / "run-root"
    root.mkdir()
    spaced = r"C:\Program Files\Python312\python.exe" if os.name == "nt" else "/opt/py dir/python3"
    monkeypatch.setattr(sys, "executable", spaced)

    helper.configure_profile(root)

    argv = shlex.split(os.environ["DEVIN_ACP_CMD"])
    assert argv == [spaced, str(helper._FAKE_ACP)]
    assert Path(argv[1]).exists()


def test_main_requires_devingui_e2e_root(tmp_path, preserved_env, monkeypatch):
    monkeypatch.delenv("DEVINGUI_E2E_ROOT", raising=False)
    with pytest.raises(SystemExit):
        helper.main()

    monkeypatch.setenv("DEVINGUI_E2E_ROOT", "relative/run-root")
    with pytest.raises(SystemExit):
        helper.main()

    missing = tmp_path / "missing"
    monkeypatch.setenv("DEVINGUI_E2E_ROOT", str(missing))
    with pytest.raises(SystemExit):
        helper.main()

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "leftover").write_text("x")
    monkeypatch.setenv("DEVINGUI_E2E_ROOT", str(nonempty))
    with pytest.raises(SystemExit):
        helper.main()


@pytest.mark.skipif(os.name != "nt", reason="junction creation is a Windows test")
def test_main_rejects_junction_run_root(tmp_path, preserved_env, monkeypatch):
    target = tmp_path / "real-target"
    target.mkdir()
    junction = tmp_path / "junction-root"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True, capture_output=True,
    )
    monkeypatch.setenv("DEVINGUI_E2E_ROOT", str(junction))
    with pytest.raises(SystemExit):
        helper.main()


def test_main_isolates_registry_and_binds_fake_port(tmp_path, preserved_env, monkeypatch):
    root = tmp_path / "run-root"
    root.mkdir()
    monkeypatch.setenv("DEVINGUI_E2E_ROOT", str(root))

    fake_backend = ModuleType("server.main")
    fake_backend.db_reader = SimpleNamespace(db_path=str(root / "fake.db"))
    fake_backend.model_registry = None
    fake_backend.app = object()
    monkeypatch.setitem(sys.modules, "server.main", fake_backend)
    monkeypatch.setattr("server.main", fake_backend, raising=False)
    fake_uvicorn = ModuleType("uvicorn")
    fake_uvicorn.run = MagicMock()
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    helper.main()

    registry = fake_backend.model_registry
    assert isinstance(registry, ModelRegistry)
    assert registry._auto_discover_cache is False, "explicit cache path must disable real CLI discovery"
    assert registry._auto_discover_team_settings is False
    assert Path(registry.registry_dir).is_relative_to(root)
    assert Path(registry.cache_path).is_relative_to(root)
    fake_uvicorn.run.assert_called_once_with(
        "server.main:app", host="127.0.0.1", port=18000
    )
    assert Path(os.environ["FAKE_ACP_OUTPUT_FILE"]).is_relative_to(root)


def test_fake_acp_options_match_real_schema_names():
    import importlib.util

    fixture = _REPO_ROOT / "frontend" / "tests" / "e2e" / "fixtures" / "fake_acp.py"
    spec = importlib.util.spec_from_file_location("fake_acp_fixture", fixture)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    by_id = {opt["optionId"]: opt for opt in module.PERMISSION_OPTIONS}
    assert by_id["allow"]["name"] == "Allow" and by_id["allow"]["kind"] == "allow_once"
    assert by_id["deny"]["name"] == "Deny" and by_id["deny"]["kind"] == "reject_once"
    assert all("title" not in opt for opt in module.PERMISSION_OPTIONS)


def test_initialize_database_fixture_reads_empty(tmp_path):
    db = tmp_path / "appdata" / "devin" / "cli" / "sessions.db"

    helper.initialize_database(db)

    reader = DBReader(str(db))
    assert reader.list_sessions() == []
    assert reader.list_projects() == []
    assert reader.get_prompt_history() == []
    assert reader.get_messages("fake-session-001") == []
    assert reader.get_reasoning_steps("fake-session-001") == []
    assert reader.get_rendered_commits("fake-session-001") == []
    assert reader.get_session("fake-session-001") is None


def test_initialize_database_refuses_existing_file(tmp_path):
    db = tmp_path / "sessions.db"
    helper.initialize_database(db)
    with pytest.raises(SystemExit):
        helper.initialize_database(db)
