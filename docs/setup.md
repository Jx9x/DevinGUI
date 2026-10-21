# Setup and troubleshooting

For the basic install, see [Quick start](../README.md#quick-start).

## Setup options

- **PowerShell blocks the script:**

  ```powershell
  powershell -ExecutionPolicy Bypass -File .\setup-devingui.ps1
  ```

- **Wrong Python picked:** `.\setup-devingui.ps1 -Python C:\Path\To\python.exe`
- **After updating the code:** rerun setup. The launcher detects changed source or
  dependencies and tells you when setup is out of date.

### Manual setup

If you can't use the setup script, run these from the project folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary :all: -r requirements-build.lock -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
cd frontend; npm ci; npm run build; cd ..
.\.venv\Scripts\python.exe scripts\check_installation.py --record
.\.venv\Scripts\python.exe run.py
```

`--record` lets `start-devingui.bat` recognize the manual setup.

## Troubleshooting

Open **Diagnostics & integrations** in the header first. It checks Python, Node,
the Devin executable and version, CLI sign-in, the history database, and the
frontend build, and says what to fix. Click **Retry checks** after a fix; no
restart is needed. The server console prints the same checks at startup.

| Problem | Fix |
| --- | --- |
| "Devin CLI is not signed in" | Run `devin auth login` as the same Windows user that runs the server, then **Retry checks**. |
| "No Devin session database yet" | Run `devin` once and send a message, then **Retry checks**. |
| Devin executable not found | Make sure `devin` works in a new terminal, or set `DEVINGUI_DEVIN_BIN` (see [Configuration](#configuration)). |
| Launcher says setup is missing or out of date | Rerun `setup-devingui.ps1`. |
| Port 8000 is already in use | Stop the previous Devin GUI server with `Ctrl+C` in its server window, then relaunch. If another program owns the port, close it or run `.\.venv\Scripts\python.exe run.py --port 8001` and open that port. |
| Folder is rejected | Use a plain local folder, not a network drive, symlink, junction, or some synced folders. |
| CLI version warning | The GUI usually still works. If something breaks, check for a newer GUI release or use the tested CLI version. |

Setup rebuilds files but does not restart an already-running server. After an
update, stop the old server with `Ctrl+C`, relaunch `start-devingui.bat`, and
refresh the browser so both the backend and UI use the new version.

### Reporting an issue

Include your Devin CLI version (`devin --version`) and the JSON from
**http://127.0.0.1:8000/api/diagnostics**. It contains setup findings and local
paths, but no credentials or conversation content. Remove anything you consider
private, such as your Windows username in paths.

## Optional integrations

Off by default and not needed for Devin chat. Enable them in
**Diagnostics & integrations**, which shows what each one requires.

- **Devin quota**: daily and weekly usage beside the prompt. Requires a separate
  **Devin Desktop** sign-in; CLI sign-in alone is not enough.
- **Codex / ChatGPT allowance**: the shared ChatGPT plan allowance, read from a
  local Codex CLI signed in with `codex login` (ChatGPT account, not an API key).
  Set `DEVINGUI_CODEX_BIN` if Codex isn't found automatically.

## Configuration

Set these environment variables before starting the server:

| Variable | Purpose |
| --- | --- |
| `DEVINGUI_DEVIN_BIN` | Absolute path to `devin.exe` if it isn't on PATH or in `%LOCALAPPDATA%\devin\cli\bin`. |
| `DEVIN_SESSIONS_DIR` | Alternate base directory for session history (CLI 3000.11.92+). The database is at `<base>\cli\sessions.db`. Set the same value for the CLI and GUI. Transcripts stay in their default location. |
| `DEVIN_GUI_ARCHIVE_DIR` | Folder for archived conversation exports. |
| `DEVINGUI_CODEX_BIN` | Absolute path to the Codex executable (optional integration). |

```powershell
$env:DEVINGUI_DEVIN_BIN = 'D:\Tools\Devin\devin.exe'
.\start-devingui.bat
```

## Data locations

For the current Windows user:

| Data | Location | Access |
| --- | --- | --- |
| Devin conversations | `%APPDATA%\devin\cli\sessions.db`, `...\transcripts` | Read-only |
| Devin settings | `%APPDATA%\devin\config.json` | Read-only |
| GUI model/pricing state | `%APPDATA%\devingui` | Read/write |
| Unsent drafts | Browser storage | Read/write |
| Archived exports | `Documents\Devin_Chat` | Write |

With `DEVIN_SESSIONS_DIR` set, the GUI reads `cli\sessions.db` under that directory.
Transcripts and configuration keep the locations listed above. Markdown exports
follow the same split; an explicit `--db` pointing to a different database still
uses its adjacent `transcripts` directory.

## Compatibility with Devin CLI updates

The GUI talks to Devin through the CLI's Agent Client Protocol (ACP), but it also
reads Devin's local database and transcript formats, which can change between CLI
releases. Each GUI release is tested against one CLI version, listed in
[Requirements](../README.md#requirements). Diagnostics warn when your version
differs and check the database schema, but newer CLI releases can still break
history display or compaction.

The **3000.11.92** compatibility check covers CLI version/flag parsing, an
unauthenticated ACP handshake, the CLI's empty database and embedded migrations,
read-only history and export, and automated ACP/UI regression tests using
synthetic data. It does not include a signed-in inference run. The new
`DEVIN_SESSIONS_DIR` option is supported;
see the [CLI release notes](https://docs.devin.ai/cli/changelog/stable#v3000-11-92).
