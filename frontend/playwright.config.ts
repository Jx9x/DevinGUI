import { defineConfig, devices } from "@playwright/test";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Playwright E2E configuration.
 *
 * Two mutually-exclusive modes, selected by environment variable:
 *
 * - ``E2E_BACKEND=1``    — starts a backend pointed at the **fake ACP**
 *   executable (``scripts/start_e2e_backend.py``) plus the Vite dev server,
 *   on the dedicated fake ports 18000/5174.  Only
 *   ``fake-acp-permission.spec.ts`` runs in this mode; the other specs
 *   require the real ``devin`` agent.  Each run gets a unique temp root via
 *   ``DEVINGUI_E2E_ROOT`` so profiles and artifacts never touch real data.
 *
 * - ``E2E_REAL_BACKEND=1`` — starts only the Vite dev server.  You must start
 *   the real backend manually (``python run.py``) in a separate terminal so
 *   the specs can talk to the real ``devin`` agent.  The three original specs
 *   (``cancel``, ``permission``, ``session-switch``) run in this mode;
 *   ``fake-acp-permission`` is skipped.
 *
 * When neither variable is set, all E2E specs are skipped so unit-test CI is
 * not blocked.
 */
const FAKE_BACKEND = process.env.E2E_BACKEND === "1";
const REAL_BACKEND = process.env.E2E_REAL_BACKEND === "1";

if (FAKE_BACKEND && REAL_BACKEND) {
  throw new Error("E2E_BACKEND and E2E_REAL_BACKEND are mutually exclusive.");
}

const IS_WORKER = process.env.TEST_WORKER_INDEX !== undefined;
let E2E_ROOT: string | undefined;
if (FAKE_BACKEND) {
  if (IS_WORKER) {
    const inherited = process.env.DEVINGUI_E2E_ROOT;
    if (!inherited || !existsSync(inherited)) {
      throw new Error(
        "fake backend workers require an existing DEVINGUI_E2E_ROOT inherited from the main process.",
      );
    }
    E2E_ROOT = inherited;
  } else {
    E2E_ROOT = mkdtempSync(join(tmpdir(), "devingui-e2e-"));
  }
}

const PROJECT_ROOT = (() => {
  try {
    return fileURLToPath(new URL("../", import.meta.url));
  } catch {
    return join(process.cwd(), "..");
  }
})();
const VENV_PYTHON = join(
  PROJECT_ROOT,
  ".venv",
  process.platform === "win32" ? join("Scripts", "python.exe") : join("bin", "python"),
);
const BACKEND_PYTHON = existsSync(VENV_PYTHON) ? `"${VENV_PYTHON}"` : "python";

if (E2E_ROOT) {
  process.env.DEVINGUI_E2E_ROOT = E2E_ROOT;
  process.env.FAKE_ACP_OUTPUT_FILE = join(
    E2E_ROOT,
    "test-results",
    "fake_acp_last_permission_response.json",
  );
  console.log(`[e2e] fake backend run root: ${E2E_ROOT}`);
}

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: FAKE_BACKEND ? "fake-acp-permission.spec.ts" : undefined,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: FAKE_BACKEND ? "http://127.0.0.1:5174" : "http://localhost:5173",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: FAKE_BACKEND
    ? [
        {
          // Backend with the fake ACP binary substituted via DEVIN_ACP_CMD.
          // The helper script isolates every profile/artifact path under
          // DEVINGUI_E2E_ROOT and launches uvicorn on port 18000 so the
          // fake-acp-permission E2E test can exercise the full stack without
          // the real ``devin`` binary.
          command: `${BACKEND_PYTHON} ../scripts/start_e2e_backend.py`,
          url: "http://127.0.0.1:18000/api/health",
          reuseExistingServer: false,
          timeout: 60_000,
          env: {
            ...process.env,
            DEVINGUI_E2E_ROOT: E2E_ROOT!,
            FAKE_ACP_OUTPUT_FILE: join(
              E2E_ROOT!,
              "test-results",
              "fake_acp_last_permission_response.json",
            ),
          },
        },
        {
          command: "npm run dev",
          url: "http://127.0.0.1:5174",
          reuseExistingServer: false,
          timeout: 60_000,
          env: { ...process.env, E2E_BACKEND: "1" },
        },
      ]
    : REAL_BACKEND
      ? {
          // Only the Vite dev server is started automatically; the real
          // backend (``python run.py`` with the ``devin`` binary) must be
          // started manually in a separate terminal.
          command: "npm run dev",
          url: "http://localhost:5173",
          reuseExistingServer: !process.env.CI,
          timeout: 60_000,
        }
      : undefined,
});
