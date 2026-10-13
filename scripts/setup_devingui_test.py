from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import setup_devingui as setup_script
from server import installation


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "server").mkdir()
    (root / "scripts").mkdir()
    for name in ("pyproject.toml", "requirements-build.lock", "requirements.lock", "setup-devingui.ps1", "start-devingui.bat", "run.py", "scripts/setup_devingui.py", "scripts/check_installation.py", "frontend/package.json", "frontend/package-lock.json", "frontend/index.html", "frontend/vite.config.ts", "frontend/tsconfig.json", "frontend/tsconfig.node.json", "frontend/tailwind.config.js", "frontend/postcss.config.js"):
        (root / name).write_text("synthetic", encoding="utf-8")
    (root / "requirements.lock").write_text("fastapi==1.2.3 \\\n    --hash=sha256:synthetic\n")
    monkeypatch.setattr(setup_script.shutil, "which", lambda name: str(root / "tools" / name))
    monkeypatch.setattr(installation.importlib.metadata, "version", lambda name: "1.2.3")
    return root


def stub_setup(monkeypatch, version="v24.18.0"):
    calls = []
    def run(command, root, capture=False):
        calls.append((command, root))
        if command[1:] == ["--version"]:
            return SimpleNamespace(stdout=version if Path(command[0]).name == "node" else "11.16.0")
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[-1]) / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(setup_script, "_run", run)
    return calls


@pytest.mark.parametrize("version,supported", [("v22.13.0", True), ("v22.12.0", False), ("v23.9.0", False), ("v24.18.0", True), ("bad", False)])
def test_node_versions_follow_frontend_engine(version, supported):
    assert installation.node_supported(version) is supported


def test_setup_selects_venv_and_installs_locks_then_builds_without_cli_calls(source, monkeypatch):
    calls = stub_setup(monkeypatch)
    setup_script.setup(source)
    commands = [command for command, cwd in calls]
    assert any(command[1:3] == ["-m", "venv"] for command in commands)
    pip = next(command for command in commands if "--require-hashes" in command)
    assert "requirements-build.lock" in pip and "requirements.lock" in pip
    assert pip[0] == str(source / ".venv" / "Scripts" / "python.exe")
    assert "--only-binary" in pip
    assert any("--no-deps" in command and "--no-build-isolation" in command for command in commands)
    assert [command[1:] for command, cwd in calls if cwd == source / "frontend"] == [["ci"], ["run", "build"]]
    assert commands[-1][-1] == "--record"
    assert not any("devin" == Path(command[0]).stem for command in commands)


def test_existing_environment_is_reused_and_never_deleted(source, monkeypatch):
    python = source / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    sentinel = source / ".venv" / "keep-me"
    sentinel.write_text("original")
    calls = stub_setup(monkeypatch)
    setup_script.setup(source)
    assert not any(command[1:3] == ["-m", "venv"] for command, cwd in calls)
    assert sentinel.read_text() == "original"


def test_bad_existing_environment_requires_explicit_repair(source, monkeypatch):
    (source / ".venv").mkdir()
    stub_setup(monkeypatch)
    with pytest.raises(RuntimeError, match="will not delete or replace"):
        setup_script.setup(source)
    assert (source / ".venv").exists()


def test_node_failure_aborts_before_environment_or_installation(source, monkeypatch):
    calls = stub_setup(monkeypatch, version="v23.9.0")
    with pytest.raises(RuntimeError, match="Unsupported Node"):
        setup_script.setup(source)
    assert len(calls) == 1
    assert not (source / ".venv").exists()


def test_failed_build_never_records_success(source, monkeypatch):
    calls = stub_setup(monkeypatch)
    original = setup_script._run
    def run(command, root, capture=False):
        if command[1:] == ["run", "build"]:
            raise subprocess.CalledProcessError(1, command)
        return original(command, root, capture=capture)
    monkeypatch.setattr(setup_script, "_run", run)
    with pytest.raises(subprocess.CalledProcessError):
        setup_script.setup(source)
    assert not any("--record" in command for command, cwd in calls)


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="Windows PowerShell is unavailable")
def test_powershell_bootstrap_uses_explicit_interpreter_from_another_directory(tmp_path):
    root = tmp_path / "checkout with spaces"
    (root / "scripts").mkdir(parents=True)
    script = Path(__file__).resolve().parents[1] / "setup-devingui.ps1"
    shutil.copy(script, root / "setup-devingui.ps1")
    (root / "scripts" / "setup_devingui.py").write_text("import sys\nprint('synthetic-bootstrap-ok')\nsys.exit(0)\n")
    import sys
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(root / "setup-devingui.ps1"),
                             "-Python", sys.executable], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic-bootstrap-ok" in result.stdout
    assert not (root / ".venv").exists()


def test_recorded_installation_requires_rebuild_after_source_or_lock_changes(source):
    (source / ".venv").mkdir()
    (source / "frontend" / "dist" / "assets").mkdir(parents=True)
    (source / "frontend" / "dist" / "index.html").write_text("built")
    installation.record_setup(source)
    assert installation.installation_problems(source) == []
    (source / "frontend" / "src" / "App.tsx").write_text("changed")
    assert any("changed since setup" in problem for problem in installation.installation_problems(source))
    installation.record_setup(source)
    (source / "requirements.lock").write_text("fastapi==9.9.9")
    problems = installation.installation_problems(source)
    assert any("dependency mismatch" in problem for problem in problems)
    assert any("changed since setup" in problem for problem in problems)


def test_missing_packages_and_partial_build_are_actionable(source, monkeypatch):
    def missing(name):
        raise installation.importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(installation.importlib.metadata, "version", missing)
    problems = installation.installation_problems(source)
    assert any("Missing Python dependency" in problem for problem in problems)
    assert any("frontend build" in problem for problem in problems)
    assert any("setup record" in problem for problem in problems)


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="Windows PowerShell is unavailable")
def test_powershell_bootstrap_has_valid_syntax_without_running_setup():
    script = Path(__file__).resolve().parents[1] / "setup-devingui.ps1"
    quoted = str(script).replace("'", "''")
    command = f"$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile('{quoted}', [ref]$tokens, [ref]$errors) > $null; if ($errors.Count) {{ $errors; exit 1 }}"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
