# Agent guide

Devin GUI is an unofficial local browser interface for the Devin CLI. A Python
FastAPI backend talks to the CLI through the Agent Client Protocol (ACP) and
reads its history. The frontend uses React, TypeScript, Vite, and Tailwind CSS.
Windows is the tested platform.

Read [README.md](README.md) for user behavior,
[CONTRIBUTING.md](CONTRIBUTING.md) for development and dependency updates, and
[docs/setup.md](docs/setup.md) for configuration and data locations. Follow any
more specific `AGENTS.md` instructions in the directories you change.

## Development

- Use Python 3.12+ and a Node.js version allowed by `frontend/package.json`.
- Inspect the existing environment before installing dependencies. Use the
  project interpreter with `-m pip` and `-m pytest`. Python dependencies are
  pinned with hashes in `requirements*.lock`; frontend dependencies use
  `frontend/package-lock.json` and `npm ci` from `frontend/`.
- `setup-devingui.ps1` creates `.venv`, installs dependencies, builds the
  frontend, and records installation state. See `CONTRIBUTING.md` for installing
  test dependencies. Unit tests use synthetic data and need no signed-in account.
- For the hot-reload dev servers, see `CONTRIBUTING.md`. The normal launcher
  serves the built UI from `frontend/dist`.

## Where changes belong

- `run.py` starts the server. `server/main.py` wires shared services, routers,
  and WebSocket command dispatch. REST handlers and request models belong in
  feature modules named `*_routes.py`; register API routes before static UI routes.
- `server/acp_client.py` owns CLI process communication and protocol I/O.
  `acp_configuration.py` selects and verifies session options; `acp_updates.py`
  translates updates without owning live session state.
- `server/session_manager.py` owns `ManagedSession` state and the registry.
  `session_transitions.py`, `turn_commands.py`, `turn_coordinator.py`,
  `permission_coordinator.py`, and `session_notifications.py` separate startup,
  prompts, turn completion, permissions, and delivery/replay.
- `server/db_reader.py` reads SQLite history; `transcript_watcher.py` follows
  transcript updates. `message_projection.py` and `session_history.py` assemble
  display history. `archive_manager.py` manages exports and the archive index.
- Model discovery and persistence live in `server/model_*.py`. Cost estimates
  and historical prices live in `cost_estimator.py` and `pricing_history.py`.
  Keep per-user paths and Devin executable discovery in `server/paths.py`.
- `frontend/src/App.tsx` connects the UI. `hooks/useWebSocket.ts` handles transport
  and reconnects; `hooks/useChatSession.ts` and `chatSessionState.ts` handle chat
  state. `types.ts` and `api/client.ts` define and validate backend contracts.
- `frontend/src/components/` contains UI components. `historyTimeline.ts` merges
  messages, reasoning, and tools; `promptPersistence.ts` stores drafts and the
  active conversation. Keep optional integration settings in `integrationSettings.ts`.
- `scripts/` contains setup, installation checks, export utilities, and test helpers.

## Behavior to preserve

- Keep the server local. Preserve loopback binding in `run.py` and the peer,
  Host, Origin, and cross-site checks in `server/local_boundary.py` for both HTTP
  and WebSocket requests. Development origins are limited to ports 5173 and 5174.
- Keep GUI access to Devin's database, transcripts, and configuration read-only.
  Archiving writes a separate export and hides the session through the GUI index;
  it never deletes or edits the original conversation.
- Opening history is read-only. Resuming starts a CLI session explicitly. Preserve
  the distinction between viewing, loading, resuming, and reconnecting.
- Live sessions outlast individual sockets. Preserve generation checks,
  transition locks, and session identity checks so stale callbacks and cleanup
  cannot affect a replacement session.
- Preserve permission request correlation, acknowledgement, timeout rejection,
  and reconnect behavior. Bypass approvals is an explicit user choice; interrupted
  browser decisions require an explicit retry.
- Treat REST response shapes, WebSocket keys, request IDs, and error behavior as
  compatibility contracts. Update backend producers, frontend types/validators,
  reducers, and relevant tests together when a contract intentionally changes.
- History combines ACP updates, database rows, and transcripts. Preserve ordering,
  compaction boundaries, duplicate suppression, and historical pricing. Transcript
  fallback text must not become a second output stream during a live turn.
- Optional quota and companion integrations stay off by default. Use fake data
  for authentication and usage tests, and keep credentials and conversation
  content out of logs, fixtures, screenshots, and public diagnostics.

## Verification

Choose checks that exercise the behavior you changed. Backend tests live in
`server/test/` as `*_test.py`; frontend unit tests use `*.test.ts` and
`*.test.tsx`. Extend existing fake ACP/WebSocket objects and temporary data.

Before backend tests, follow the profile isolation instructions in
[CONTRIBUTING.md](CONTRIBUTING.md#tests): use temporary `APPDATA`, `LOCALAPPDATA`,
`HOME`, and `USERPROFILE` locations and a nonexistent `DEVINGUI_DEVIN_BIN`.
Do not read real browser profiles, credentials, or conversations during tests.

From the repository root, with test dependencies installed:

```powershell
.\.venv\Scripts\python.exe -m pytest server/test/ scripts/ run_test.py
```

From `frontend/`:

```powershell
npm test
npm run build
npm run lint
```

`npm run build` checks TypeScript and builds the UI. Frontend formatting follows
`frontend/.prettierrc`; format the files you change without unrelated rewrites.

For account-free Playwright coverage, set `E2E_BACKEND=1` and run `npm run e2e`
from `frontend/`. It uses the fake ACP backend on isolated ports and temporary
profiles. `E2E_REAL_BACKEND=1` uses a real signed-in CLI and requires a manually
started backend; run that mode only when the user requests live integration
testing. The two modes are mutually exclusive.

For documentation-only edits, check paths, commands, and the diff. Report which
checks ran and any failures or checks you could not run.

## Release hygiene

Keep virtual environments, `node_modules`, `frontend/dist`, generated TypeScript
files, credentials, local databases, transcripts, and debug traces out of source
changes. Update dependency lockfiles deliberately when dependencies change.
Keep public documentation independent of local tools, personal paths, and account data.
