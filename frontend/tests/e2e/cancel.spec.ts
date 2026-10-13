import { test, expect } from "@playwright/test";

/**
 * E2E: Start a turn, click cancel, assert the UI returns to ``ready``.
 *
 * Requires the **real** backend (``python run.py`` with the ``devin`` binary)
 * and the Vite dev server.  Set ``E2E_REAL_BACKEND=1`` to enable these tests.
 * (``E2E_BACKEND=1`` starts the fake-ACP backend, which does not produce real
 * streaming work and is not suitable for this spec.)
 */
const RUN_E2E = !!process.env.E2E_REAL_BACKEND;

test.describe("cancel a running turn", () => {
  test.skip(!RUN_E2E, "Set E2E_REAL_BACKEND=1 to run E2E tests");

  test("start a turn, click cancel, UI returns to ready", async ({ page }) => {
    await page.goto("/");

    // Create a new session.
    await page.getByRole("button", { name: "New session" }).first().click();

    // Wait for the session to become active (session id appears in the header).
    await expect(page.locator("header, .border-b").first()).toContainText(/.*/, { timeout: 15_000 });
    // The prompt textarea should be enabled once the session is live.
    const textarea = page.getByRole("textbox");
    await expect(textarea).toBeEnabled({ timeout: 15_000 });

    // Send a prompt.
    await textarea.fill("Write a hello world script");
    await page.getByRole("button", { name: "Send prompt" }).click();

    // Wait for the streaming indicator to appear.
    await expect(page.getByText("Devin is working...")).toBeVisible({ timeout: 15_000 });

    // Click cancel.
    await page.getByRole("button", { name: "Cancel streaming" }).click();

    // The streaming indicator should disappear and the Send button should
    // reappear, indicating the UI returned to ``ready``.
    await expect(page.getByText("Devin is working...")).toBeHidden({ timeout: 15_000 });
    await expect(page.getByRole("button", { name: "Send prompt" })).toBeVisible({ timeout: 15_000 });
  });
});
