"""Declared interpreter floor and runtime dependencies must match the docs."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCKS = ("requirements.lock", "requirements-dev.lock", "requirements-build.lock")
OBSOLETE_PARSERS = ("blackboxprotobuf", "bbpb", "protobuf")


def _toml() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _project() -> dict:
    return _toml()["project"]


def test_requires_python_floor_matches_readme_prerequisite():
    assert _project()["requires-python"] == ">=3.12"
    assert "Python 3.12+" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_websockets_is_a_pinned_runtime_dependency():
    dependencies = _project()["dependencies"]
    assert "websockets==15.0.1" in dependencies
    assert "uvicorn>=0.32.0" in dependencies
    assert all(not dep.startswith("uvicorn[") for dep in dependencies)


def test_http_client_and_patched_anyio_are_runtime_dependencies():
    dependencies = _project()["dependencies"]
    assert "httpx>=0.28.1,<1" in dependencies
    assert "anyio>=4.14.2,<5" in dependencies


def test_no_obsolete_parser_dependency_is_declared():
    project = _project()
    declared = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)
    names = {re.split(r"[<>=!~\[]", dep, maxsplit=1)[0].strip().lower() for dep in declared}
    assert names.isdisjoint(OBSOLETE_PARSERS)


def test_build_system_is_pinned():
    build = _toml()["build-system"]
    assert build["requires"] == ["setuptools==83.0.0"]
    assert build["build-backend"] == "setuptools.build_meta"


def test_locks_contain_only_exact_hashed_pins():
    for lock in LOCKS:
        lines = [l for l in (ROOT / lock).read_text(encoding="utf-8").splitlines() if l]
        entries = 0
        i = 0
        while i < len(lines):
            entry = lines[i]
            assert not entry.startswith((" ", "\t")), (lock, entry)
            assert re.fullmatch(
                r"[A-Za-z0-9_.\-]+==[^\s\\;]+( ; [^\\]+)?\s*\\", entry
            ), (lock, entry)
            entries += 1
            i += 1
            hashes = 0
            while i < len(lines) and lines[i].startswith((" ", "\t")):
                assert re.fullmatch(
                    r"\s+--hash=sha256:[0-9a-f]{64}\s*\\?", lines[i]
                ), (lock, lines[i])
                hashes += 1
                i += 1
            assert hashes > 0, (lock, entry)
        assert entries > 0, lock


def test_locks_do_not_resolve_obsolete_parsers():
    for lock in LOCKS:
        names = {
            l.split("==")[0].strip().lower()
            for l in (ROOT / lock).read_text(encoding="utf-8").splitlines()
            if "==" in l and not l.startswith((" ", "\t"))
        }
        assert names.isdisjoint(OBSOLETE_PARSERS), lock
