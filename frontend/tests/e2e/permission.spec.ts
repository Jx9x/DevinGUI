import { test, expect } from "@playwright/test";

/**
 * E2E: Trigger a permission request, allow/deny, assert the panel clears.
 *
 * Requires the **real** backend (``python run.py`` with the ``devin`` binary)
 * and the Vite dev server.  Set ``E2E_REAL_BACKEND=1`` to enable these tests.
 * (``E2E_BACKEND=1`` starts the fake-ACP backend, which emits a permission
 * request on every prompt regardless of agent behavior; the
 * ``fake-acp-permission.spec.ts`` spec covers that path instead.)
 *
 * The permission request is triggered by sending a prompt that causes the
 * agent to request permission (e.g. a tool that needs approval).  Because
 * the exact prompt depends on the agent's behavior, this test waits for the
 * permission panel to appear and then clicks Allow or Deny.
 */
const RUN_E2E = !!process.env.E2E_REAL_BACKEND;

test.describe("permission request flow", () => {
  test.skip(!RUN_E2E, "Set E2E_REAL_BACKEND=1 to run E2E tests");

  test("permission panel appears and clears after allow", async ({ page }) => {
    await page.goto("/");

    // Create a new session.
    await page.getByRole("button", { name: "New session" }).first().click();
    const textarea = page.getByRole("textbox");
    await expect(textarea).toBeEnabled({ timeout: 15_000 });

    // Send a prompt that is likely to trigger a permission request.
    await textarea.fill("Run a shell command to list files in the current directory");
    await page.getByRole("button", { name: "Send prompt" }).click();

    // Wait for the permission panel to appear (it contains "Permission request").
    const permissionPanel = page.locator("text=Permission request");
    await expect(permissionPanel).toBeVisible({ timeout: 60_000 });

    // Click Allow.
    await page.getByRole("button", { name: "Allow" }).click();

    // The permission panel should clear after the decision is acknowledged.
    await expect(permissionPanel).toBeHidden({ timeout: 30_000 });
  });
});
