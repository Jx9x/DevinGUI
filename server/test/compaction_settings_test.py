import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.acp_client import ACPClient
from server.compaction_settings import compaction_thresholds


@pytest.fixture(autouse=True)
def fake_devin_binary(monkeypatch):
    monkeypatch.setattr("server.acp_client._find_devin_binary", lambda: "synthetic-devin")
    monkeypatch.setattr("server.acp_client.probe_devin", lambda *args: {"compaction_supported": True})
    monkeypatch.setattr("server.acp_client.probe_devin_auth", lambda *args: {"authenticated": True})


def write_rule(project):
    directory = project / ".devin"
    directory.mkdir()
    (directory / "devingui.local.json").write_text(json.dumps({
        "model_compaction": {
            "gpt-6-1-sol-*": {
                "spawn_tokens": 300000, "apply_tokens": 300000, "hard_tokens": 450000,
            },
        },
    }))


def test_rule_is_limited_to_project_and_model(tmp_path):
    write_rule(tmp_path)
    for model in ("gpt-6-1-sol-max", "gpt-6-1-sol-low-priority"):
        assert compaction_thresholds(str(tmp_path), model) == (300000, 300000, 450000)
    for model in (None, "swe-2-max", "gpt-6-sol-max", "fusion-gpt-6-1-sol-max-sidekick-swe-2-high"):
        assert compaction_thresholds(str(tmp_path), model) is None
    assert compaction_thresholds(str(tmp_path / "other-project"), "gpt-6-1-sol-max") is None


def test_invalid_threshold_order_is_rejected(tmp_path):
    write_rule(tmp_path)
    path = tmp_path / ".devin" / "devingui.local.json"
    path.write_text(path.read_text().replace('"apply_tokens": 300000', '"apply_tokens": 250000'))
    with pytest.raises(ValueError, match="spawn <= apply <= hard"):
        compaction_thresholds(str(tmp_path), "gpt-6-1-sol-max")


@pytest.mark.asyncio
async def test_launch_uses_isolated_config_and_cleans_up(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    write_rule(project)
    appdata = tmp_path / "appdata"
    (appdata / "devin").mkdir(parents=True)
    global_path = appdata / "devin" / "config.json"
    original = {"agent": {"model": "swe-2-max", "compaction_threshold_tokens": 200000},
                "permissions": {"allow": ["Read(**)"]}, "subagents_enabled": False}
    global_path.write_text(json.dumps(original))
    original_bytes = global_path.read_bytes()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("DEVIN_ACP_CMD", raising=False)
    async def failed_launch(*args, **kwargs):
        config_path = Path(args[args.index("--config") + 1])
        isolated = json.loads(config_path.read_text())
        assert isolated["agent"]["compaction_threshold_tokens"] == 300000
        isolated["agent"]["compaction_threshold_tokens"] = 200000
        assert isolated == original
        raise OSError("test launch failure")

    spawn = AsyncMock(side_effect=failed_launch)
    monkeypatch.setattr("server.acp_client.asyncio.create_subprocess_exec", spawn)
    client = ACPClient(AsyncMock(), cwd=str(project), model="gpt-6-1-sol-max")
    with pytest.raises(OSError, match="test launch failure"):
        await client.start()
    args = spawn.call_args.args
    assert args[args.index("--compaction-thresholds") + 1] == "300000,300000,450000"
    config_path = Path(args[args.index("--config") + 1])
    assert not config_path.exists()
    assert global_path.read_bytes() == original_bytes
    assert client._compaction_config_dir is None


def test_disabled_rules_explicitly_use_cli_defaults(tmp_path):
    write_rule(tmp_path)
    path = tmp_path / ".devin" / "devingui.local.json"
    settings = json.loads(path.read_text())
    settings["model_compaction"]["gpt-6-1-sol-*"]["enabled"] = False
    path.write_text(json.dumps(settings))
    assert compaction_thresholds(str(tmp_path), "gpt-6-1-sol-max") is None


@pytest.mark.asyncio
async def test_unsupported_compaction_does_not_spawn_or_silently_drop_thresholds(tmp_path, monkeypatch):
    write_rule(tmp_path)
    monkeypatch.delenv("DEVIN_ACP_CMD", raising=False)
    monkeypatch.setattr("server.acp_client.probe_devin", lambda *args: {"compaction_supported": False})
    spawn = AsyncMock()
    monkeypatch.setattr("server.acp_client.asyncio.create_subprocess_exec", spawn)
    client = ACPClient(AsyncMock(), cwd=str(tmp_path), model="gpt-6-1-sol-max")
    with pytest.raises(RuntimeError, match='"enabled": false'):
        await client.start()
    spawn.assert_not_called()
    assert "--compaction-thresholds" in client.cmd
    assert client._compaction_config_dir is None


def test_other_models_and_custom_commands_keep_default_launch(tmp_path, monkeypatch):
    write_rule(tmp_path)
    monkeypatch.delenv("DEVIN_ACP_CMD", raising=False)
    client = ACPClient(AsyncMock(), cwd=str(tmp_path), model="swe-2-max")
    assert "--compaction-thresholds" not in client.cmd
    custom = ACPClient(AsyncMock(), cwd=str(tmp_path), model="gpt-6-1-sol-max", cmd=["fake-acp"])
    assert custom.cmd == ["fake-acp"]
