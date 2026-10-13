import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_INTEGRATIONS, readIntegrationSettings, saveIntegrationSettings } from "./integrationSettings";

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("optional integration preferences", () => {
  it("starts all integrations disabled for a fresh browser", () => {
    expect(readIntegrationSettings()).toEqual(DEFAULT_INTEGRATIONS);
  });

  it("persists independent choices without credentials", () => {
    const settings = { devinUsage: false, codexUsage: true, chatgpt: false };
    expect(saveIntegrationSettings(settings)).toBe(true);
    expect(readIntegrationSettings()).toEqual(settings);
  });

  it.each(["broken", "null", '{"devinUsage":"true","codexUsage":1}', "[]"])("rejects unsafe stored preferences: %s", (value) => {
    window.localStorage.setItem("devin-gui-integrations-v1", value);
    expect(readIntegrationSettings()).toEqual(DEFAULT_INTEGRATIONS);
  });

  it("works for this tab when browser storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Blocked"); });
    expect(readIntegrationSettings()).toEqual(DEFAULT_INTEGRATIONS);
    expect(saveIntegrationSettings({ ...DEFAULT_INTEGRATIONS, chatgpt: true })).toBe(false);
  });
});
