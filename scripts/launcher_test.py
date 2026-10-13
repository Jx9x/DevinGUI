"""Regression tests for ``start-devingui.bat`` and ``check_backend_port.py``.

The batch is exercised through real ``cmd /c`` invocations of a copy inside a
throwaway directory, with ``python.cmd``/``npm.cmd``/``wt.cmd`` stubs on PATH
that record their argv.  No real Python, npm, or Windows Terminal runs, and
nothing is ever killed.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import check_backend_port as checker

REPO_ROOT = Path(__file__).resolve().parents[1]
BAT_SOURCE = REPO_ROOT / "start-devingui.bat"


def _fake_socket_module(bind_errors=None, unsupported_families=(), socket_errors=None):
    """Build a fake ``socket`` module namespace.

    ``bind_errors`` maps ``(family, host)`` to the OSError the fake raises on
    bind.  ``unsupported_families`` fail at ``socket()`` creation time with
    ``EAFNOSUPPORT`` (a genuinely absent family).  ``socket_errors`` maps
    family to the OSError raised at creation for any other failure.
    """
    bind_errors = bind_errors or {}
    socket_errors = socket_errors or {}
    calls = {"bind": [], "exclusive": []}

    class FakeSocket:
        def __init__(self, family):
            self.family = family

        def setsockopt(self, level, option, value):
            calls["exclusive"].append((level, option, value))

        def bind(self, address):
            calls["bind"].append(address)
            error = bind_errors.get((self.family, address[0]))
            if error is not None:
                raise error

        def close(self):
            pass

    def socket_factory(family, sock_type):
        if family in unsupported_families:
            raise OSError(errno.EAFNOSUPPORT, "address family not supported")
        if family in socket_errors:
            raise socket_errors[family]
        return FakeSocket(family)

    module = SimpleNamespace(
        socket=socket_factory,
        SOCK_STREAM=checker.socket.SOCK_STREAM,
        SOL_SOCKET=checker.socket.SOL_SOCKET,
        SO_EXCLUSIVEADDRUSE=getattr(checker.socket, "SO_EXCLUSIVEADDRUSE", 23),
        AF_INET=checker.socket.AF_INET,
        AF_INET6=checker.socket.AF_INET6,
    )
    return module, calls


def test_checker_reports_free_when_bind_succeeds(monkeypatch):
    fake, calls = _fake_socket_module()
    monkeypatch.setattr(checker, "socket", fake)
    assert checker.main() == 0
    bound_hosts = {addr[0] for addr in calls["bind"]}
    assert bound_hosts == {"127.0.0.1", "::1"}
    if sys.platform == "win32":
        assert calls["exclusive"], "Windows probe must request SO_EXCLUSIVEADDRUSE"


def test_checker_reports_occupied_on_bind_error(monkeypatch):
    fake, _ = _fake_socket_module(
        bind_errors={(checker.socket.AF_INET, "127.0.0.1"): OSError(errno.EADDRINUSE, "in use")}
    )
    monkeypatch.setattr(checker, "socket", fake)
    assert checker.main() == 1


def test_checker_skips_unsupported_ipv6_family(monkeypatch):
    fake, calls = _fake_socket_module(unsupported_families={checker.socket.AF_INET6})
    monkeypatch.setattr(checker, "socket", fake)
    assert checker.main() == 0
    assert [addr[0] for addr in calls["bind"]] == ["127.0.0.1"]


@pytest.mark.parametrize("error", [OSError(errno.EMFILE, "too many files"), OSError(errno.EACCES, "denied")])
def test_checker_fails_closed_on_socket_creation_error(monkeypatch, error):
    fake, _ = _fake_socket_module(
        socket_errors={checker.socket.AF_INET: error, checker.socket.AF_INET6: error}
    )
    monkeypatch.setattr(checker, "socket", fake)
    assert checker.main() == 1


def test_checker_ipv4_creation_failure_cannot_be_masked_by_ipv6_skip(monkeypatch):
    fake, _ = _fake_socket_module(
        unsupported_families={checker.socket.AF_INET, checker.socket.AF_INET6}
    )
    monkeypatch.setattr(checker, "socket", fake)
    assert checker.main() == 1


def test_checker_ipv4_missing_address_is_unverifiable(monkeypatch):
    fake, _ = _fake_socket_module(
        bind_errors={(checker.socket.AF_INET, "127.0.0.1"): OSError(errno.EADDRNOTAVAIL, "no addr")}
    )
    monkeypatch.setattr(checker, "socket", fake)
    assert checker.main() == 1


def test_checker_fails_closed_on_unexpected_probe_error(monkeypatch):
    def boom(family, sock_type):
        raise RuntimeError("probe infrastructure unavailable")

    fake = SimpleNamespace(
        socket=boom,
        SOCK_STREAM=checker.socket.SOCK_STREAM,
        SOL_SOCKET=checker.socket.SOL_SOCKET,
        SO_EXCLUSIVEADDRUSE=getattr(checker.socket, "SO_EXCLUSIVEADDRUSE", 23),
        AF_INET=checker.socket.AF_INET,
        AF_INET6=checker.socket.AF_INET6,
    )
    monkeypatch.setattr(checker, "socket", fake)
    assert checker.main() == 1


def test_launcher_and_checker_never_kill_processes():
    bat = BAT_SOURCE.read_text(encoding="utf-8").lower()
    assert "taskkill" not in bat
    source = (Path(__file__).parent / "check_backend_port.py").read_text(encoding="utf-8")
    assert "taskkill" not in source
    assert "subprocess" not in source
    assert ".connect(" not in source


@pytest.fixture
def launcher(tmp_path):
    """Copy the launcher into an isolated tree with stub executables."""
    root = tmp_path / "app with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "frontend").mkdir(parents=True)
    shutil.copy(BAT_SOURCE, root / "start-devingui.bat")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"

    (bindir / "python.cmd").write_text(
        "@echo off\r\n"
        f'echo python %* >> "{log}"\r\n'
        'if /i "%~n1"=="check_installation" exit /b %STUB_SETUP_EXIT%\r\n'
        'if /i "%~n1"=="check_backend_port" exit /b %STUB_CHECK_EXIT%\r\n'
        "exit /b 0\r\n"
    )
    (bindir / "npm.cmd").write_text(
        "@echo off\r\n"
        f'echo npm %* >> "{log}"\r\n'
        "exit /b 99\r\n"
    )
    (bindir / "wt.cmd").write_text(
        "@echo off\r\n"
        f'echo wt %* >> "{log}"\r\n'
        "exit /b 0\r\n"
    )

    def run(*args, check_exit=0, setup_exit=0):
        env = dict(os.environ)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        env["STUB_CHECK_EXIT"] = str(check_exit)
        env["STUB_SETUP_EXIT"] = str(setup_exit)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            ["cmd", "/c", str(root / "start-devingui.bat"), *args],
            cwd=root, env=env, capture_output=True, text=True, input="\n", timeout=30,
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    return run


@pytest.mark.skipif(os.name != "nt", reason="batch launcher is Windows-only")
@pytest.mark.parametrize("args", [(), ("_tabbed",)])
def test_occupied_port_aborts_before_build_or_server(launcher, args):
    result, calls = launcher(*args, check_exit=1)
    assert result.returncode != 0
    assert "check_installation" in calls[0]
    assert "check_backend_port" in calls[1]
    assert len(calls) == 2, f"expected setup and port checks only, got {calls}"
    combined = result.stdout + result.stderr
    assert "8000" in combined


@pytest.mark.skipif(os.name != "nt", reason="batch launcher is Windows-only")
@pytest.mark.parametrize("args", [(), ("_tabbed",)])
def test_incomplete_setup_aborts_with_instructions_before_port_or_server(launcher, args):
    result, calls = launcher(*args, setup_exit=1)
    assert result.returncode != 0
    assert len(calls) == 1 and "check_installation" in calls[0]
    assert "setup-devingui.ps1" in result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="batch launcher is Windows-only")
@pytest.mark.parametrize("args", [(), ("_tabbed",)])
def test_success_runs_setup_check_port_probe_then_server_in_current_console(launcher, args):
    result, calls = launcher(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 3
    assert "check_installation" in calls[0]
    assert "check_backend_port" in calls[1]
    assert "python" in calls[2] and calls[2].rstrip().endswith("run.py")
    assert not any("npm" in call for call in calls)
    assert not any(call.startswith("wt ") for call in calls)


@pytest.mark.skipif(os.name != "nt", reason="batch launcher is Windows-only")
@pytest.mark.parametrize("args", [(), ("_tabbed",)])
def test_venv_python_preferred_over_path_stub(launcher, tmp_path, args):
    root = tmp_path / "app with spaces"
    venv_scripts = root / ".venv" / "Scripts"
    venv_scripts.mkdir(parents=True)
    (venv_scripts / "python.exe").write_text("not a real interpreter")
    result, calls = launcher(*args)
    assert result.returncode != 0
    assert calls == [], f"PATH python stub must not be invoked: {calls}"
    assert "setup-devingui.ps1" in (result.stdout + result.stderr)
