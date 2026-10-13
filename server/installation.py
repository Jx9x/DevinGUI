from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path

_MARKER = "devingui-setup.json"


def node_supported(version: str) -> bool:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    if not match:
        return False
    major, minor, _ = map(int, match.groups())
    return (major == 22 and minor >= 13) or major >= 24


def setup_fingerprint(root: Path) -> str:
    paths = [root / name for name in (
        "pyproject.toml", "requirements-build.lock", "requirements.lock",
        "setup-devingui.ps1", "start-devingui.bat", "run.py",
        "scripts/setup_devingui.py", "scripts/check_installation.py",
        "frontend/package.json", "frontend/package-lock.json", "frontend/index.html",
        "frontend/vite.config.ts", "frontend/tsconfig.json", "frontend/tsconfig.node.json",
        "frontend/tailwind.config.js", "frontend/postcss.config.js",
    )]
    paths.extend((root / "server").glob("*.py"))
    paths.extend(path for path in (root / "frontend" / "src").rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def installation_problems(root: Path, *, check_marker: bool = True) -> list[str]:
    problems = []
    if sys.version_info < (3, 12):
        problems.append("Python 3.12+ is required. The selected interpreter is " + sys.executable)
    if not (root / ".venv").is_dir():
        problems.append("The project .venv is missing.")
    for name, version in re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^\s;\\]+)", (root / "requirements.lock").read_text(encoding="utf-8")):
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"Missing Python dependency: {name}=={version}.")
            continue
        if installed != version:
            problems.append(f"Python dependency mismatch: {name} requires {version}, found {installed}.")
    if not (root / "frontend" / "dist" / "index.html").is_file() or not (root / "frontend" / "dist" / "assets").is_dir():
        problems.append("The frontend build is missing or incomplete.")
    if check_marker:
        try:
            marker = json.loads((root / ".venv" / _MARKER).read_text(encoding="utf-8"))
            if not isinstance(marker, dict) or marker.get("version") != 1 or marker.get("fingerprint") != setup_fingerprint(root):
                problems.append("Source or dependency locks changed since setup. Re-run setup after pulling updates.")
        except (OSError, ValueError):
            problems.append("No valid successful-setup record was found.")
    return problems


def record_setup(root: Path) -> None:
    problems = installation_problems(root, check_marker=False)
    if problems:
        raise RuntimeError(" ".join(problems))
    (root / ".venv" / _MARKER).write_text(
        json.dumps({"version": 1, "fingerprint": setup_fingerprint(root)}, indent=2), encoding="utf-8",
    )
