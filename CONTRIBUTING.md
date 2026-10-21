# Contributing

Complete the [Quick start](README.md#quick-start) setup first.

## Development server

For hot reload, run the backend and the Vite dev server in separate terminals:

```powershell
.\.venv\Scripts\python.exe run.py --reload
```

```powershell
cd frontend
npm run dev
```

Open **http://localhost:5173**. The dev server proxies `/api` and `/ws` to
port 8000 and must run on port 5173 or 5174.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary :all: -r requirements-build.lock -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -m pytest server/test/ scripts/ run_test.py
```

```powershell
cd frontend
npm test
npm run build
npm run lint
```

Tests use synthetic data and fake Devin processes, so they never need an account.
To be safe, run them in a terminal where:

- `APPDATA`, `LOCALAPPDATA`, `HOME`, and `USERPROFILE` point at a temporary folder
- `DEVINGUI_DEVIN_BIN` points at a nonexistent path
- `DEVIN_SESSIONS_DIR` is unset or points inside the temporary folder

End-to-end tests:

- Playwright permission test: set `E2E_BACKEND=1` and run `npm run e2e` in `frontend/`.
- Tests that need a real signed-in CLI: set `E2E_REAL_BACKEND=1`.

## Dependency locks

`requirements*.lock` pin every Python dependency with hashes. To regenerate them
after changing `pyproject.toml`:

```bash
uv pip compile pyproject.toml --generate-hashes --no-header --no-annotate --universal --python-version 3.12 --exclude-newer 2026-10-03T00:00:00Z -o requirements.lock
uv pip compile pyproject.toml --extra dev --constraints requirements.lock --generate-hashes --no-header --no-annotate --universal --python-version 3.12 --exclude-newer 2026-10-03T00:00:00Z -o requirements-dev.lock
printf 'setuptools==83.0.0\n' > build-requirements.txt
uv pip compile build-requirements.txt --generate-hashes --no-header --no-annotate --universal --python-version 3.12 --exclude-newer 2026-10-03T00:00:00Z -o requirements-build.lock
```
