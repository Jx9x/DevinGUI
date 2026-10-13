from __future__ import annotations

import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from .chatgpt_adapter import ChatOnSteroidsClient
from .cli_diagnostics import TESTED_CLI_VERSION, probe_devin, probe_devin_auth
from .codex_usage_provider import CodexUsageError, find_codex_binary
from .db_reader import DBReader
from .installation import node_supported
from .paths import cli_data_dir, default_credentials_path, find_devin_binary


def tool_version(name: str) -> str | None:
    executable = shutil.which(name)
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=5,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}),
        )
        return result.stdout.strip()[:100] if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _exists(path: Path, *, directory: bool = False) -> bool:
    try:
        mode = path.stat().st_mode
        return stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    except OSError:
        return False


def frontend_available(directory: str | Path) -> bool:
    root = Path(directory)
    return _exists(root / "index.html") and _exists(root / "assets", directory=True)


def collect_diagnostics(reader: DBReader, transcripts_dir: str, frontend_dist: str | Path, *, refresh: bool = False) -> dict:
    """Inspect prerequisites and CLI sign-in without exposing account details."""
    try:
        cli = probe_devin(find_devin_binary(), refresh=refresh)
    except (OSError, ValueError) as error:
        cli = {"executable": None, "version": None, "tested_version": TESTED_CLI_VERSION,
               "compaction_supported": None, "status": "error", "message": str(error)}
    checks = [
        {"id": "python", "label": "Backend Python", "status": "ok" if sys.version_info >= (3, 12) else "error",
         "path": sys.executable, "message": f"Python {platform.python_version()}; requires Python 3.12+."},
        {"id": "devin", "label": "Devin CLI", "status": cli["status"],
         "path": cli["executable"], "message": cli["message"]},
    ]
    if cli["executable"]:
        auth = probe_devin_auth(cli["executable"])
        checks.append({"id": "devin_auth", "label": "Devin CLI sign-in", "status": auth["status"],
                       "path": None, "message": auth["message"]})
    for name in ("node", "npm"):
        version = tool_version(name)
        supported = bool(version) and (name != "node" or node_supported(version))
        message = f"{version}. Build-time tool; not required for normal launch." if version else (
            "Not detected. Needed for setup/build; not required for normal launch after a successful build."
        )
        if version and not supported:
            message += " Setup requires Node.js 22.13+ on 22 LTS, or 24+."
        checks.append({"id": name, "label": name.capitalize(), "status": "ok" if supported else "warning",
                       "path": None, "message": message})
    schema = reader.inspect_schema()
    database_messages = {
        "ok": "Required history tables and columns are present. This structural check does not guarantee every transcript/cache payload or future CLI semantic change is compatible.",
        "missing": "No Devin session database yet. Run devin auth login, then start a conversation with devin under the same Windows account as this server. Retry checks after Devin creates the database. DevinGUI will not create it.",
        "incompatible": "History storage is incompatible with this GUI release. Start the current official stable CLI, then check for a DevinGUI update and retry. No database migration or repair is performed.",
        "unreadable": "The existing database could not be read. Check file permissions and the Windows account running this server, then retry. The file has not been replaced or repaired.",
        "corrupt": "SQLite reports that the existing database is corrupt or is not a valid SQLite file. Check the reported path and seek official Devin support for recovery. DevinGUI will not replace or repair it.",
    }
    checks.append({"id": "database", "label": "Session database / history schema", "path": reader.db_path,
                   "status": {"ok": "ok", "missing": "setup_required"}.get(schema["status"], "error"),
                   "message": database_messages[schema["status"]] + (" Missing: " + ", ".join(schema["missing"]) if schema["missing"] else "")})
    for name, missing in schema["features"].items():
        checks.append({"id": name, "label": name.replace("_", " ").capitalize(), "path": None,
                       "status": "warning" if missing else "ok",
                       "message": "Optional history detail unavailable. Missing: " + ", ".join(missing) if missing else "Required columns are present."})
    transcripts = Path(transcripts_dir)
    checks.append({"id": "transcripts", "label": "Transcripts", "path": str(transcripts),
                   "status": "ok" if _exists(transcripts, directory=True) else "warning",
                   "message": "Directory exists. Transcript parsing is feature-specific; presence does not establish format compatibility."
                   if _exists(transcripts, directory=True) else "Directory missing or inaccessible. Transcript-only reasoning and metadata may be unavailable; chat and database history can still work."})
    cache = Path(cli_data_dir())
    checks.append({"id": "model_cache", "label": "Model-cache fallback", "path": str(cache),
                   "status": "ok" if _exists(cache, directory=True) else "warning",
                   "message": "Private caches are optional. Model discovery prefers devin models list --format json; unsupported caches are skipped."})
    checks.append({"id": "frontend", "label": "Frontend build", "path": str(Path(frontend_dist).resolve()),
                   "status": "ok" if frontend_available(frontend_dist) else "error",
                   "message": "Built index.html and assets directory are present."
                   if frontend_available(frontend_dist) else "Frontend build missing or incomplete. Run setup-devingui.ps1 from the repository root, or npm ci and npm run build in frontend. Restart the backend after building; development mode can use Vite separately."})
    checks.append({"id": "compaction", "label": "Custom model compaction", "path": None,
                   "status": "ok" if cli["compaction_supported"] else "warning",
                   "message": "The CLI accepts the hidden compaction flag (parse-only check). Threshold behavior must be retested on CLI updates."
                   if cli["compaction_supported"] else 'Hidden compaction flag support could not be confirmed. Configured custom thresholds will not be silently discarded. Set "enabled": false on the project rule to explicitly use CLI defaults.'})
    credentials_present = _exists(default_credentials_path())
    try:
        codex = find_codex_binary()
    except (CodexUsageError, OSError):
        codex = None
    profile = ChatOnSteroidsClient().profile
    companion_present = profile is not None and _exists(profile / "devin-gui-api" / "connection.json")
    optional = {
        "devin_usage": {"label": "Devin Desktop quota", "status": "ok" if credentials_present else "setup_required",
                        "message": "Desktop credentials file detected; validity is checked only when enabled."
                        if credentials_present else "Optional. Requires a separate Devin Desktop sign-in; CLI sign-in alone is not enough."},
        "codex_usage": {"label": "Codex / ChatGPT allowance", "status": "ok" if codex else "setup_required",
                        "message": "Codex executable detected. Requires codex login with a suitable ChatGPT account; authentication is checked only when enabled."
                        if codex else "Optional. Install Codex and use codex login with a suitable ChatGPT account. API-key-only login cannot report plan allowance."},
        "chatgpt": {"label": "Chat On Steroids conversations", "status": "ok" if companion_present else "setup_required",
                    "message": "Companion discovery file detected; connection is checked only when enabled."
                    if companion_present else "Optional. Requires the separately installed custom Chat On Steroids companion, not included here. Run it under the same Windows account."},
    }
    core = [check for check in checks if check["id"] in {"python", "devin", "devin_auth", "database", "frontend"}]
    status = "error" if any(check["status"] == "error" for check in core) else (
        "setup_required" if any(check["status"] == "setup_required" for check in core) else "ready"
    )
    return {"status": status, "history_ready": schema["status"] == "ok", "cli": cli,
            "checks": checks, "optional_integrations": optional}


def print_startup_checks(root: Path) -> None:
    from .paths import default_transcripts_dir

    result = collect_diagnostics(DBReader(), default_transcripts_dir(), root / "frontend" / "dist")
    print(f"  Diagnostics: {result['status']} (details: /api/diagnostics)")
    for check in result["checks"]:
        if check["status"] != "ok" or check["id"] in {"python", "devin", "database"}:
            print(f"  [{check['status']}] {check['label']}: {check['message']}")
            if check["path"]:
                print(f"    {check['path']}")
