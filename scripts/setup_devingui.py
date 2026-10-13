from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.installation import node_supported


def _run(command: list[str], root: Path, *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=root, check=True, text=True,
                          **({"capture_output": True, "timeout": 10} if capture else {}))


def setup(root: Path) -> None:
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12+ is required. Specify a supported interpreter with setup-devingui.ps1 -Python <absolute-path>.")
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("Install Node.js 22.13+ on the 22 LTS line, or Node.js 24+, with npm, then re-run setup.")
    version = _run([node, "--version"], root, capture=True).stdout.strip()
    if not node_supported(version):
        raise RuntimeError(f"Unsupported Node.js version {version}. Requires 22.13+ on 22 LTS or 24+.")
    npm_version = _run([npm, "--version"], root, capture=True).stdout.strip()
    print(f"[setup] Python {sys.version.split()[0]}: {sys.executable}")
    print(f"[setup] Node {version}, npm {npm_version}")
    environment = root / ".venv"
    python = environment / "Scripts" / "python.exe"
    if environment.exists():
        if not python.is_file():
            raise RuntimeError("The existing .venv is not a usable Windows environment. Choose another checkout or repair it explicitly; setup will not delete or replace it.")
        _run([str(python), "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"], root, capture=True)
    else:
        _run([sys.executable, "-m", "venv", str(environment)], root)
    _run([str(python), "-m", "pip", "install", "--require-hashes", "--only-binary", ":all:",
          "-r", "requirements-build.lock", "-r", "requirements.lock"], root)
    _run([str(python), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."], root)
    _run([npm, "ci"], root / "frontend")
    _run([npm, "run", "build"], root / "frontend")
    _run([str(python), str(root / "scripts" / "check_installation.py"), "--record"], root)
    print("[setup] Complete. Launch start-devingui.bat. Devin is installed and signed in separately; optional integrations are not required.")


def main() -> int:
    try:
        setup(ROOT)
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"[setup] Failed: {error}", file=sys.stderr)
        print("[setup] Fix the reported prerequisite and re-run setup. Existing environments and Devin data are not deleted or replaced.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
