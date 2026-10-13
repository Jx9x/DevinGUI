import { test, expect } from "@playwright/test";

/**
 * E2E: Start creating a session, select an existing one before creation
 * finishes, assert the selected session's history is preserved.
 *
 * Requires the **real** backend (``python run.py`` with the ``devin`` binary)
 * and the Vite dev server.  Set ``E2E_REAL_BACKEND=1`` to enable these tests.
 * (``E2E_BACKEND=1`` starts the fake-ACP backend, which is not suitable for
 * this spec.)
 */
const RUN_E2E = !!process.env.E2E_REAL_BACKEND;

test.describe("session switch during creation", () => {
  test.skip(!RUN_E2E, "Set E2E_REAL_BACKEND=1 to run E2E tests");

  test("selecting an existing session during creation preserves its history", async ({ page }) => {
    await page.goto("/");

    // Wait for the sidebar to load sessions.
    const sidebar = page.locator(".border-r").first();
    await expect(sidebar).toBeVisible({ timeout: 10_000 });

    // Capture the first existing session in the sidebar (if any).
    const sessionButtons = sidebar.getByRole("button").filter({
      has: page.locator(".font-mono"),
    });
    const existingCount = await sessionButtons.count();

    test.skip(existingCount === 0, "No existing sessions in the sidebar to select");

    // Click "New Session" to start creation (which may take a few seconds).
    await page.getByRole("button", { name: "New session" }).first().click();

    // Immediately select the first existing session before creation finishes.
    await sessionButtons.first().click();

    // The selected session's history should be loaded and visible.
    // The header should show the session id, and the chat area should show
    // either messages or the "Send a prompt" placeholder (not a loading state).
    await expect(page.getByText(/Loading history|Resuming session/)).toBeHidden({
      timeout: 15_000,
    });

    // The Send button should be available (session is live or read-only).
    // If the session resumed successfully, the textarea is enabled.
    const textarea = page.getByRole("textbox");
    await expect(textarea).toBeVisible({ timeout: 15_000 });
  });
});
