import { existsSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@playwright/test", () => ({
  defineConfig: (config: unknown) => config,
  devices: { "Desktop Chrome": {} },
}));

vi.mock("vite", () => ({
  defineConfig: (config: unknown) => config,
}));

vi.mock("@vitejs/plugin-react", () => ({
  default: () => ({ name: "vite:react" }),
}));

const ORIGINAL_ENV = {
  DEVINGUI_E2E_ROOT: process.env.DEVINGUI_E2E_ROOT,
  FAKE_ACP_OUTPUT_FILE: process.env.FAKE_ACP_OUTPUT_FILE,
  TEST_WORKER_INDEX: process.env.TEST_WORKER_INDEX,
};

const ownedRoots = new Set<string>();

async function importConfig() {
  const preExisting = process.env.DEVINGUI_E2E_ROOT;
  vi.resetModules();
  const config = (await import("../playwright.config.ts")).default as Record<string, any>;
  const root = process.env.DEVINGUI_E2E_ROOT;
  if (root && root !== preExisting) {
    ownedRoots.add(root);
  }
  return config;
}

afterEach(() => {
  vi.unstubAllEnvs();
  for (const [key, value] of Object.entries(ORIGINAL_ENV)) {
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
  }
  for (const root of ownedRoots) {
    rmSync(root, { recursive: true, force: true });
  }
  ownedRoots.clear();
});

describe("playwright config fake backend mode", () => {
  it("refuses to start when both backend flags are set", async () => {
    vi.stubEnv("E2E_BACKEND", "1");
    vi.stubEnv("E2E_REAL_BACKEND", "1");
    await expect(importConfig()).rejects.toThrow(/mutually exclusive/i);
  });

  it("fake mode uses dedicated ports, its own run root, and only the fake spec", async () => {
    vi.stubEnv("E2E_BACKEND", "1");
    const config = await importConfig();

    const root = process.env.DEVINGUI_E2E_ROOT!;
    expect(root.startsWith(join(tmpdir(), "devingui-e2e-"))).toBe(true);
    expect(existsSync(root)).toBe(true);
    expect(readdirSync(root)).toHaveLength(0);

    expect(config.use.baseURL).toBe("http://127.0.0.1:5174");
    expect(config.testMatch).toBe("fake-acp-permission.spec.ts");
    const [backend, vite] = config.webServer;
    expect(backend.command).toContain("start_e2e_backend.py");
    expect(backend.url).toBe("http://127.0.0.1:18000/api/health");
    expect(backend.reuseExistingServer).toBe(false);
    expect(backend.env.DEVINGUI_E2E_ROOT).toBe(root);
    const outputFile = join(root, "test-results", "fake_acp_last_permission_response.json");
    expect(backend.env.FAKE_ACP_OUTPUT_FILE).toBe(outputFile);
    expect(process.env.FAKE_ACP_OUTPUT_FILE).toBe(outputFile);
    expect(vite.url).toBe("http://127.0.0.1:5174");
    expect(vite.reuseExistingServer).toBe(false);
    expect(vite.env.E2E_BACKEND).toBe("1");
  });

  it("main process ignores an inherited root and leaves it untouched", async () => {
    const foreign = mkdtempSync(join(tmpdir(), "devingui-e2e-foreign-"));
    ownedRoots.add(foreign);
    const sentinel = join(foreign, "sentinel.txt");
    writeFileSync(sentinel, "do not touch");
    vi.stubEnv("E2E_BACKEND", "1");
    vi.stubEnv("DEVINGUI_E2E_ROOT", foreign);
    const config = await importConfig();

    const minted = process.env.DEVINGUI_E2E_ROOT!;
    expect(minted).not.toBe(foreign);
    expect(config.webServer[0].env.DEVINGUI_E2E_ROOT).toBe(minted);
    expect(existsSync(sentinel)).toBe(true);
  });

  it("fresh main-process runs mint unique roots", async () => {
    vi.stubEnv("E2E_BACKEND", "1");
    await importConfig();
    const first = process.env.DEVINGUI_E2E_ROOT;
    await importConfig();
    const second = process.env.DEVINGUI_E2E_ROOT;
    expect(second).not.toBe(first);
    expect(existsSync(first!)).toBe(true);
    expect(existsSync(second!)).toBe(true);
  });

  it("worker reuses the inherited run root", async () => {
    const shared = mkdtempSync(join(tmpdir(), "devingui-e2e-shared-"));
    ownedRoots.add(shared);
    vi.stubEnv("E2E_BACKEND", "1");
    vi.stubEnv("DEVINGUI_E2E_ROOT", shared);
    vi.stubEnv("TEST_WORKER_INDEX", "0");
    const config = await importConfig();

    expect(process.env.DEVINGUI_E2E_ROOT).toBe(shared);
    expect(process.env.FAKE_ACP_OUTPUT_FILE).toBe(
      join(shared, "test-results", "fake_acp_last_permission_response.json"),
    );
    expect(config.use.baseURL).toBe("http://127.0.0.1:5174");
  });

  it("worker without an inherited root fails fast", async () => {
    vi.stubEnv("E2E_BACKEND", "1");
    vi.stubEnv("TEST_WORKER_INDEX", "0");
    delete process.env.DEVINGUI_E2E_ROOT;
    await expect(importConfig()).rejects.toThrow(/DEVINGUI_E2E_ROOT/i);
  });

  it("worker with a missing inherited root fails fast", async () => {
    vi.stubEnv("E2E_BACKEND", "1");
    vi.stubEnv("TEST_WORKER_INDEX", "0");
    vi.stubEnv("DEVINGUI_E2E_ROOT", join(tmpdir(), "devingui-e2e-nonexistent-dir"));
    await expect(importConfig()).rejects.toThrow(/DEVINGUI_E2E_ROOT/i);
  });

  it("real mode keeps the existing localhost defaults", async () => {
    vi.stubEnv("E2E_REAL_BACKEND", "1");
    const config = await importConfig();

    expect(config.use.baseURL).toBe("http://localhost:5173");
    expect(config.testMatch).toBeUndefined();
    expect(config.webServer.url).toBe("http://localhost:5173");
    expect(config.webServer.reuseExistingServer).toBe(true);
    expect(process.env.DEVINGUI_E2E_ROOT).toBeUndefined();
  });

  it("default mode starts no servers", async () => {
    const config = await importConfig();
    expect(config.webServer).toBeUndefined();
    expect(config.use.baseURL).toBe("http://localhost:5173");
  });
});

async function importViteConfig() {
  vi.resetModules();
  const exported = (await import("../vite.config.ts")).default as any;
  return typeof exported === "function"
    ? ((await exported({ command: "serve", mode: "development" })) as Record<string, any>)
    : (exported as Record<string, any>);
}

describe("vite config fake backend mode", () => {
  it("fake mode binds 127.0.0.1:5174 strictly and proxies to the fake backend", async () => {
    vi.stubEnv("E2E_BACKEND", "1");
    const config = await importViteConfig();

    expect(config.server.host).toBe("127.0.0.1");
    expect(config.server.port).toBe(5174);
    expect(config.server.strictPort).toBe(true);
    expect(config.server.proxy["/api"]).toBe("http://127.0.0.1:18000");
    expect(config.server.proxy["/ws"].target).toBe("ws://127.0.0.1:18000");
    expect(config.server.proxy["/ws"].ws).toBe(true);
  });

  it("normal mode keeps its proxy and refuses an unsupported fallback port", async () => {
    const config = await importViteConfig();

    expect(config.server.host).toBeUndefined();
    expect(config.server.port).toBe(5173);
    expect(config.server.strictPort).toBe(true);
    expect(config.server.proxy["/api"]).toBe("http://localhost:8000");
    expect(config.server.proxy["/ws"].target).toBe("ws://localhost:8000");
  });
});
