#!/usr/bin/env python3
"""Start the Devin GUI backend with the fake ACP for E2E testing.

Requires ``DEVINGUI_E2E_ROOT`` pointing at the unique empty run directory the
Playwright config created for this run.  Every profile, cache, and artifact
path is redirected under that root so the backend never reads or writes real
user data: ``APPDATA``/``LOCALAPPDATA``/``HOME``/``USERPROFILE``,
``XDG_CONFIG_HOME``/``XDG_DATA_HOME``, the archive/companion directories, the
Codex binary override, the permission-response artifact, and a zero grace
period.  ``DEVIN_ACP_CMD`` points the ACP client at
``frontend/tests/e2e/fixtures/fake_acp.py`` via the current interpreter, and
the model registry is rebuilt with an explicit cache path so it never shells
out to the real ``devin`` CLI.  Then launches uvicorn on 127.0.0.1:18000.

Cross-platform — no shell-specific env-var syntax required.
"""

from __future__ import annotations

import os
import shlex
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_FAKE_ACP = _PROJECT_ROOT / "frontend" / "tests" / "e2e" / "fixtures" / "fake_acp.py"

_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, working_directory TEXT NOT NULL, model TEXT NOT NULL, agent_mode TEXT NOT NULL, created_at TEXT NOT NULL, last_activity_at TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, main_chain_id TEXT);
CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, node_id TEXT, parent_node_id TEXT, chat_message TEXT, created_at TEXT, metadata TEXT);
CREATE TABLE rendered_commits (session_id TEXT, sequence_number INTEGER, rendered_html TEXT, created_at TEXT);
CREATE TABLE prompt_history (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT, session_id TEXT, is_shell INTEGER);
"""


def initialize_database(path: Path) -> None:
    """Create the empty sessions DB the isolated backend reads.

    Refuses to touch an existing file — the run root is expected to be
    fresh, so a present database means the path is not really isolated.
    """
    path = Path(path)
    if path.exists():
        raise SystemExit(f"refusing to overwrite existing database: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)


def configure_profile(root: Path) -> None:
    """Redirect every profile and artifact path under *root* (test seam)."""
    root = Path(root).absolute()
    directories = {
        "appdata": root / "appdata",
        "localappdata": root / "localappdata",
        "home": root / "home",
        "config": root / "config",
        "data": root / "data",
        "archives": root / "archives",
        "companion": root / "companion",
        "results": root / "test-results",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(directories["appdata"])
    os.environ["LOCALAPPDATA"] = str(directories["localappdata"])
    os.environ["HOME"] = str(directories["home"])
    os.environ["USERPROFILE"] = str(directories["home"])
    os.environ["XDG_CONFIG_HOME"] = str(directories["config"])
    os.environ["XDG_DATA_HOME"] = str(directories["data"])
    os.environ["DEVIN_SESSIONS_DIR"] = str(directories["appdata"] / "devin")
    os.environ["DEVIN_GUI_ARCHIVE_DIR"] = str(directories["archives"])
    os.environ["DEVINGUI_CHATGPT_PROFILE"] = str(directories["companion"])
    os.environ["DEVINGUI_CODEX_BIN"] = str(root / "disabled-codex.exe")
    os.environ["DEVINGUI_DEVIN_BIN"] = str(root / "disabled-devin.exe")
    os.environ["FAKE_ACP_OUTPUT_FILE"] = str(
        directories["results"] / "fake_acp_last_permission_response.json"
    )
    os.environ["DEVIN_GRACE_SECONDS"] = "0"
    os.environ["DEVIN_ACP_CMD"] = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(_FAKE_ACP))}"
    )


def main() -> None:
    root_env = os.environ.get("DEVINGUI_E2E_ROOT")
    if not root_env:
        raise SystemExit(
            "DEVINGUI_E2E_ROOT is not set; the fake-ACP backend only runs inside a Playwright E2E run."
        )
    root = Path(root_env)
    if not root.is_absolute():
        raise SystemExit("DEVINGUI_E2E_ROOT must be an absolute path.")
    if os.path.normcase(os.path.realpath(root)) != os.path.normcase(str(root)):
        raise SystemExit("DEVINGUI_E2E_ROOT must not be a symlink or junction.")
    if not root.is_dir() or any(root.iterdir()):
        raise SystemExit(
            "DEVINGUI_E2E_ROOT must point at the unique empty run directory created by playwright.config.ts."
        )

    # Ensure the project root is on ``sys.path`` so ``server.main`` can be
    # imported regardless of the working directory (Playwright runs from
    # ``frontend/``).
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))

    # Run from the project root so relative paths (e.g. the fake ACP's
    # default output location) resolve consistently.
    os.chdir(_PROJECT_ROOT)

    configure_profile(root)
    # FAKE_ACP_OUTPUT_FILE points inside the fresh run root, so this run
    # cannot read a stale permission-response artifact.
    initialize_database(root / "appdata" / "devin" / "cli" / "sessions.db")

    import server.main as backend
    from server.model_registry import ModelRegistry

    registry_dir = root / "appdata" / "devingui"
    registry_dir.mkdir(parents=True, exist_ok=True)
    backend.model_registry = ModelRegistry(
        registry_dir=str(registry_dir),
        db_path=backend.db_reader.db_path,
        cache_path=str(root / "model-cache.bin"),
        team_settings_path=str(root / "team-settings.bin"),
    )

    import uvicorn

    uvicorn.run("server.main:app", host="127.0.0.1", port=18000)


if __name__ == "__main__":
    main()
