import { test, expect } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";

/**
 * E2E: Full browser-to-fake-ACP permission flow.
 *
 * Exercises the entire stack: a fake ACP executable communicating over real
 * stdin/stdout JSON-RPC, the real FastAPI backend, the Vite dev server, and
 * real WebSocket communication.
 *
 * The fake ACP (``fixtures/fake_acp.py``) emits a ``session/request_permission``
 * request on every ``session/prompt``.  This test clicks **Allow** in the GUI,
 * then reads the JSON-RPC response that the backend wrote back to the fake ACP
 * subprocess and asserts the exact ``outcome``.
 *
 * Requires ``E2E_BACKEND=1``.  When unset the test is skipped (not failed),
 * matching the existing spec gating.
 */
const RUN_E2E = process.env.E2E_BACKEND === "1";

// The fake ACP writes the permission response it receives to this file.
// ``playwright.config.ts`` points ``FAKE_ACP_OUTPUT_FILE`` at a fresh path
// inside the isolated ``DEVINGUI_E2E_ROOT`` minted for the run.
const OUTPUT_FILE = process.env.FAKE_ACP_OUTPUT_FILE;

// Known constants advertised by the fake ACP (must match fake_acp.py).
const EXPECTED_OPTION_ID = "allow";

test.describe("fake ACP permission flow (browser → backend → fake ACP)", () => {
  test.skip(!RUN_E2E, "Set E2E_BACKEND=1 to run E2E tests");

  test("fresh setup diagnostics, retry, and optional defaults work without integrations", async ({ page }) => {
    const requests: string[] = [];
    page.on("request", request => { requests.push(request.url()); });
    const response = await page.request.get("/api/diagnostics");
    expect(response.ok()).toBe(true);
    const baseline = await response.json();
    let initialized = false;
    await page.route("**/api/diagnostics*", async route => {
      if (route.request().url().includes("refresh=true")) initialized = true;
      const snapshot = structuredClone(baseline);
      if (!initialized) {
        snapshot.status = "setup_required";
        snapshot.history_ready = false;
        snapshot.checks = [{ id: "database", label: "Session database", status: "setup_required", path: "synthetic/sessions.db",
          message: "Run devin auth login, start a conversation under the same Windows account, then retry." }];
      } else {
        snapshot.checks = snapshot.checks.filter((check: { id: string }) => check.id !== "devin");
      }
      await route.fulfill({ status: 200, json: snapshot });
    });
    await page.goto("/");
    await expect(page.getByText(/Run devin auth login/)).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Assistant" })).toHaveCount(0);
    await expect(page.getByText("Usage unavailable")).toHaveCount(0);
    await page.getByRole("button", { name: "Retry checks" }).click();
    await expect(page.getByText(/Run devin auth login/)).toHaveCount(0);
    await page.getByRole("button", { name: "Diagnostics & integrations" }).click();
    const panel = page.getByRole("dialog", { name: "Diagnostics & optional integrations" });
    await expect(panel).toBeVisible();
    await expect(panel.getByLabel("Enable Devin Desktop quota")).not.toBeChecked();
    await expect(panel.getByLabel("Enable Codex / ChatGPT allowance")).not.toBeChecked();
    await expect(panel.getByLabel("Enable Chat On Steroids conversations")).not.toBeChecked();
    await panel.getByLabel("Enable Chat On Steroids conversations").check();
    await page.getByLabel("Close diagnostics").click();
    await expect(page.getByRole("combobox", { name: "Assistant" })).toBeVisible();
    await page.getByRole("button", { name: "Diagnostics & integrations" }).click();
    await panel.getByLabel("Enable Chat On Steroids conversations").uncheck();
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
    await expect(page.getByRole("combobox", { name: "Assistant" })).toHaveCount(0);
    expect(requests.some(url => /\/api\/(usage|chatgpt)(\/|\?|$)/.test(url))).toBe(false);
    await page.unrouteAll({ behavior: "wait" });
  });

  test("permission request appears, Allow clicks, exact JSON-RPC response asserted", async ({
    page,
  }) => {
    expect(OUTPUT_FILE).toBeTruthy();
    // The run root is fresh — the artifact must not exist yet.
    expect(fs.existsSync(OUTPUT_FILE!)).toBe(
      false,
      "run root must start without the permission-response artifact",
    );

    const serverErrors: string[] = [];
    page.on("response", (response) => {
      if (response.status() >= 500) {
        serverErrors.push(`${response.status()} ${response.url()}`);
      }
    });

    await page.goto("/");

    const project = path.join(process.env.DEVINGUI_E2E_ROOT!, "project");
    fs.mkdirSync(project, { recursive: true });
    await page.getByRole("button", { name: "Choose folder…" }).click();
    const picker = page.getByRole("dialog", { name: "Choose project folder" });
    await picker.getByPlaceholder("Type or paste a path…").fill(project);
    await picker.getByRole("button", { name: "Go", exact: true }).click();
    await picker.getByRole("button", { name: "Confirm", exact: true }).click();

    // Create a new session.
    await page.getByRole("button", { name: "New session" }).first().click();

    // Wait for the prompt textarea to become enabled (session is live).
    const textarea = page.getByRole("textbox");
    await expect(textarea).toBeEnabled({ timeout: 30_000 });

    // Send a prompt — the fake ACP requests permission regardless of text.
    await textarea.fill("do something that needs permission");
    await page.getByRole("button", { name: "Send prompt" }).click();

    // Wait for the permission panel to appear (yellow banner above the chat).
    const permissionBanner = page.locator("div.border-yellow-700", {
      hasText: "Approval required for Fake tool call",
    });
    await expect(permissionBanner).toBeVisible({ timeout: 60_000 });

    const allowButton = permissionBanner.getByRole("button", {
      name: "Allow",
      exact: true,
    });
    const denyButton = permissionBanner.getByRole("button", {
      name: "Deny",
      exact: true,
    });
    await expect(allowButton).toBeVisible();
    await expect(denyButton).toBeVisible();

    await expect
      .poll(() =>
        page.evaluate(() => getComputedStyle(document.body).backgroundColor),
      )
      .toBe("rgb(13, 17, 23)");
    await expect(permissionBanner).toHaveCSS(
      "border-bottom-color",
      "oklch(0.554 0.135 66.442)",
    );
    const sidebarHeader = page.locator("div.border-border").first();
    await expect(sidebarHeader).toHaveCSS("border-color", "rgb(48, 54, 61)");
    await expect(allowButton).toHaveCSS("background-color", "rgb(47, 129, 247)");
    await expect(allowButton).toHaveCSS("border-radius", "6px");
    await expect(permissionBanner.locator("pre")).toHaveCSS("border-radius", "4px");

    await page.screenshot({
      path: path.join(path.dirname(OUTPUT_FILE!), "permission-flow.png"),
    });

    // Click Allow — scoped to the permission banner to avoid matching
    // unrelated sidebar buttons whose accessible name contains "Allow".
    await allowButton.click();

    // The permission panel should clear after the decision is acknowledged.
    await expect(permissionBanner).toBeHidden({ timeout: 30_000 });

    // Read the JSON-RPC response that the backend wrote to the fake ACP.
    await expect.poll(
      async () => fs.existsSync(OUTPUT_FILE),
      { timeout: 30_000, message: "fake ACP output file should exist" },
    ).toBe(true);

    const raw = fs.readFileSync(OUTPUT_FILE, "utf-8");
    const response = JSON.parse(raw);

    // Assert the exact JSON-RPC response structure.
    expect(response.jsonrpc).toBe("2.0");
    expect(response.result).toBeDefined();
    expect(response.result.outcome).toBeDefined();
    expect(response.result.outcome.outcome).toBe("selected");
    expect(response.result.outcome.optionId).toBe(EXPECTED_OPTION_ID);

    expect(serverErrors).toEqual([]);
    await expect(page.locator("div.border-red-800")).toHaveCount(0);
  });
});
