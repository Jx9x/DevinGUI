import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "./App";
import { api } from "./api/client";
import type { DiagnosticsSnapshot } from "./diagnostics";
import type { ReactNode } from "react";

const controls = vi.hoisted(() => ({ refresh: vi.fn(async () => {}), companion: vi.fn() }));
vi.mock("./hooks/useWebSocket", () => ({ useWebSocket: () => ({ connected: true, send: vi.fn(() => ({ sent: true })), cancelQueuedPrompt: () => false }) }));
vi.mock("./hooks/useSessions", () => ({ useSessions: () => ({ sessions: [], projects: [], selectedProject: null, refresh: controls.refresh }) }));
vi.mock("./hooks/useModels", () => ({ useModels: () => ({ models: [], selectableModels: [], lastUsedVariants: {}, selectedModel: null, refresh: controls.refresh }) }));
vi.mock("./components/ChatGPTPane", () => ({ ChatGPTPane: ({ header }: { header: ReactNode }) => { controls.companion(); return <>{header}<p>Optional companion mounted</p></>; } }));
vi.mock("./api/client", () => ({ api: { getDiagnostics: vi.fn(), getUsage: vi.fn(async () => null), getCodexUsage: vi.fn(async () => null) } }));

const ready: DiagnosticsSnapshot = {
  status: "ready", history_ready: true,
  cli: { executable: "synthetic-devin", version: "3000.11.3", tested_version: "3000.11.3", compaction_supported: true, status: "ok", message: "Tested CLI" },
  checks: [{ id: "database", label: "Session database", status: "ok", path: "synthetic/sessions.db", message: "History ready" }],
  optional_integrations: {
    devin_usage: { label: "Devin Desktop quota", status: "setup_required", message: "Requires Desktop sign-in" },
    codex_usage: { label: "Codex allowance", status: "setup_required", message: "Requires Codex login" },
    chatgpt: { label: "Companion", status: "setup_required", message: "Requires custom companion" },
  },
};

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
  vi.mocked(api.getDiagnostics).mockResolvedValue(ready);
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

function settingsButton() {
  return screen.getAllByRole("button", { name: "Diagnostics & integrations" })[0];
}

describe("setup and optional integrations", () => {
  it("shows core chat without optional requests, panes, or unavailable placeholders", async () => {
    render(<App />);
    await waitFor(() => expect(api.getDiagnostics).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "New session" })).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Assistant" })).toBeNull();
    expect(screen.queryByText("Usage unavailable")).toBeNull();
    expect(api.getUsage).not.toHaveBeenCalled();
    expect(api.getCodexUsage).not.toHaveBeenCalled();
    expect(controls.companion).not.toHaveBeenCalled();
    expect(screen.getAllByRole("status", { name: "Devin status: Ready" })).toHaveLength(2);
    expect(screen.queryByLabelText("Setup needs attention")).toBeNull();
  });

  it("explains a missing database and refreshes lists after initialization", async () => {
    const fresh: DiagnosticsSnapshot = { ...ready, status: "setup_required", history_ready: false,
      checks: [{ id: "database", label: "Session database", status: "setup_required", path: "synthetic/sessions.db", message: "Run devin auth login, start a conversation under the same Windows account, then retry." }] };
    vi.mocked(api.getDiagnostics).mockResolvedValueOnce(fresh);
    render(<App />);
    expect(await screen.findByText(/Run devin auth login/)).toBeTruthy();
    expect(within(settingsButton()).getByLabelText("Setup needs attention")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry checks" }));
    await waitFor(() => expect(screen.queryByText(/Run devin auth login/)).toBeNull());
    expect(api.getDiagnostics).toHaveBeenLastCalledWith(true);
    await waitFor(() => expect(controls.refresh).toHaveBeenCalledTimes(2));
  });

  it("explains CLI logout with existing history and clears the banner after login", async () => {
    const loggedOut: DiagnosticsSnapshot = { ...ready, status: "setup_required",
      checks: [...ready.checks, { id: "devin_auth", label: "Devin CLI sign-in", status: "setup_required", path: null, message: "Devin CLI is not signed in. Run devin auth login." }] };
    vi.mocked(api.getDiagnostics).mockResolvedValueOnce(loggedOut);
    render(<App />);
    expect(await screen.findByText(/Devin CLI is not signed in/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry checks" }));
    await waitFor(() => expect(screen.queryByText(/Devin CLI is not signed in/)).toBeNull());
    expect(api.getDiagnostics).toHaveBeenLastCalledWith(true);
    expect(screen.getByRole("button", { name: "New session" })).toBeTruthy();
  });

  it("enables each integration separately and can disable the active companion pane", async () => {
    render(<App />);
    fireEvent.click(settingsButton());
    const panel = screen.getByRole("dialog", { name: "Diagnostics & optional integrations" });
    fireEvent.click(within(panel).getByLabelText("Enable Codex / ChatGPT allowance"));
    await waitFor(() => expect(api.getCodexUsage).toHaveBeenCalledTimes(1));
    expect(api.getUsage).not.toHaveBeenCalled();
    expect(controls.companion).not.toHaveBeenCalled();
    fireEvent.click(within(panel).getByLabelText("Enable Chat On Steroids conversations"));
    fireEvent.click(within(panel).getByLabelText("Close diagnostics"));
    fireEvent.change(screen.getAllByRole("combobox", { name: "Assistant" })[0], { target: { value: "chatgpt" } });
    expect(screen.getByText("Optional companion mounted")).toBeTruthy();
    fireEvent.click(settingsButton());
    fireEvent.click(screen.getByLabelText("Enable Chat On Steroids conversations"));
    fireEvent.click(screen.getByLabelText("Close diagnostics"));
    expect(screen.queryByRole("combobox", { name: "Assistant" })).toBeNull();
    expect(screen.getAllByRole("status", { name: "Devin status: Ready" })).toHaveLength(2);
    expect(window.localStorage.getItem("devin-gui-integrations-v1")).toContain('"codexUsage":true');
  });

  it("retains working chat when diagnostics fails and supports Escape and focus return", async () => {
    vi.mocked(api.getDiagnostics).mockRejectedValue(new Error("Diagnostics unavailable"));
    render(<App />);
    expect(await screen.findByText(/Could not check local setup/)).toBeTruthy();
    const opener = settingsButton();
    opener.focus();
    fireEvent.click(opener);
    await act(async () => {});
    expect(document.activeElement).toBe(screen.getByLabelText("Close diagnostics"));
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
    expect(screen.getByRole("button", { name: "New session" })).toBeTruthy();
  });
});
