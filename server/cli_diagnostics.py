from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

TESTED_CLI_VERSION = "3000.11.92"
_PROBE_TIMEOUT = 5
# The CLI can fetch remote feature flags before reporting sign-in.
_AUTH_PROBE_TIMEOUT = 30
_CACHE_SECONDS = 60
_lock = threading.Lock()
_cached: tuple[tuple[str, int, int], float, dict] | None = None


def _run(executable: str, *arguments: str, timeout: float = _PROBE_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [executable, *arguments], stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}),
    )


def probe_devin_auth(executable: str) -> dict:
    """Check sign-in afresh; return fixed messages without account details.

    The tested CLI exits zero even when logged out. Unknown output or a
    failed probe cannot establish logout and must not prevent ACP startup.
    """
    result = {
        "authenticated": None, "status": "warning",
        "message": "Could not confirm Devin CLI sign-in. Run devin auth status under the same Windows account as this server. If logged out, run devin auth login, then retry.",
    }
    try:
        status = _run(executable, "auth", "status", timeout=_AUTH_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        result["message"] = (
            f"Devin CLI sign-in check timed out after {_AUTH_PROBE_TIMEOUT} seconds. "
            "The CLI may be waiting on DNS or network services. This does not establish that you are signed out. "
            "Run devin auth status under the same Windows account as this server, then retry checks."
        )
        return result
    except (OSError, subprocess.SubprocessError):
        return result
    lines = status.stdout.strip().splitlines()
    if status.returncode != 0 or not lines:
        return result
    first_line = lines[0].strip()
    if first_line == "Not logged in.":
        result.update(
            authenticated=False, status="setup_required",
            message="Devin CLI is not signed in. Run devin auth login in a terminal under the same Windows account as this server, then start a new session. Saved conversations remain available.",
        )
    elif re.fullmatch(r"Logged in(?: \(via [^\r\n()]+\))?\.", first_line):
        result.update(authenticated=True, status="ok", message="Devin CLI reports that it is signed in.")
    return result


def probe_devin(executable: str, *, refresh: bool = False) -> dict:
    """Read the version and test flag parsing with help only, never inference.

    Cache by executable identity for one minute. Errors expose no CLI output.
    The compaction probe checks syntax support, not threshold semantics.
    """
    global _cached
    result = {
        "executable": executable, "version": None, "tested_version": TESTED_CLI_VERSION,
        "status": "error", "compaction_supported": None,
        "message": "Could not check the Devin CLI. Retry checks; verify the executable and installation.",
    }
    with _lock:
        try:
            metadata = Path(executable).stat()
            signature = (executable, metadata.st_mtime_ns, metadata.st_size)
            now = time.monotonic()
            if not refresh and _cached and _cached[0] == signature and now - _cached[1] < _CACHE_SECONDS:
                return dict(_cached[2])
            version = _run(executable, "--version")
            match = re.fullmatch(r"devin(?:\.exe)?\s+([\w.+-]+)(?:\s+\([^\r\n]*\))?", version.stdout.strip())
            if version.returncode == 0 and match:
                result["version"] = match[1]
                tested = match[1] == TESTED_CLI_VERSION
                result["status"] = "ok" if tested else "warning"
                result["message"] = (
                    f"Devin {match[1]} matches this GUI release's tested CLI."
                    if tested else f"This GUI release was tested with {TESTED_CLI_VERSION}, not {match[1]}. Check for a DevinGUI update."
                )
                try:
                    help_result = _run(executable, "--compaction-thresholds", "1,1,1", "acp", "--help")
                    result["compaction_supported"] = help_result.returncode == 0 and "acp" in help_result.stdout
                except (OSError, subprocess.SubprocessError):
                    pass
            _cached = (signature, now, dict(result))
        except (OSError, subprocess.SubprocessError):
            pass
    return result
