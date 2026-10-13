import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { WSInbound, WSOutbound } from "./types";
import App from "./App";

const transport = vi.hoisted(() => ({
  receive: (_message: WSInbound) => {},
  send: vi.fn((_message: WSOutbound) => ({ sent: true })),
  refresh: vi.fn(async () => {}),
}));

vi.mock("./hooks/useWebSocket", () => ({
  useWebSocket: (receive: (message: WSInbound) => void) => {
    transport.receive = receive;
    return { send: transport.send, connected: true, cancelQueuedPrompt: () => false };
  },
}));
vi.mock("./hooks/useSessions", () => ({
  useSessions: () => ({ sessions: [], projects: [], selectedProject: "C:/projects/example", refresh: transport.refresh }),
}));
vi.mock("./hooks/useModels", () => ({
  useModels: () => ({ models: [], selectableModels: [], recentModels: [], selectedModel: null, lastUsedVariants: {}, refresh: transport.refresh, setSelectedModel: vi.fn(), excludeModels: vi.fn(), includeModels: vi.fn() }),
}));
vi.mock("./api/client", () => ({ api: { getUsage: async () => null, getCodexUsage: async () => null } }));
vi.mock("./hooks/useDiagnostics", () => ({ useDiagnostics: () => ({ snapshot: null, loading: false, error: null, refresh: async () => {} }) }));

beforeEach(() => {
  transport.send.mockClear();
  window.sessionStorage.clear();
  window.localStorage.clear();
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

function switchAssistant(value: "devin" | "chatgpt") {
  fireEvent.change(screen.getAllByRole("combobox", { name: "Assistant" })[0], { target: { value } });
}

describe("independent assistant panes", () => {
  beforeEach(() => window.localStorage.setItem("devin-gui-integrations-v1", JSON.stringify({ chatgpt: true })));

  it("restores a ChatGPT draft after the tab's session storage is lost", async () => {
    const first = render(<App />);
    switchAssistant("chatgpt");
    fireEvent.change(screen.getByLabelText("Message ChatGPT"), { target: { value: "Unsent ChatGPT prompt" } });
    await waitFor(() => expect(window.localStorage.getItem("devin-gui-chatgpt-v1")).toContain("Unsent ChatGPT prompt"));

    first.unmount();
    window.sessionStorage.clear();
    render(<App />);
    switchAssistant("chatgpt");
    expect((screen.getByLabelText("Message ChatGPT") as HTMLTextAreaElement).value).toBe("Unsent ChatGPT prompt");
  });

  it("preserves both drafts and receives Devin output while ChatGPT is visible without cancelling", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    const request = transport.send.mock.calls[0][0];
    if (request.type !== "new_session" || !request.requestId) throw new Error("Expected correlated new session request");
    const requestId = request.requestId;
    await act(async () => { transport.receive({ type: "session_created", sessionId: "devin-live", requestId }); });
    transport.send.mockClear();
    const devinDraft = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(devinDraft, { target: { value: "Devin-only draft" } });
    switchAssistant("chatgpt");
    fireEvent.change(screen.getByLabelText("Message ChatGPT"), { target: { value: "ChatGPT-only draft" } });

    await act(async () => {
      transport.receive({ type: "session_update", sessionId: "devin-live", update: { type: "agent_message_chunk", messageId: "response-1", chunk: "Output received in background" } });
    });
    expect(screen.getByRole("option", { name: "Devin · Working" })).toBeTruthy();
    expect((screen.getByLabelText("Message ChatGPT") as HTMLTextAreaElement).value).toBe("ChatGPT-only draft");
    expect((screen.getByRole("button", { name: "Send to ChatGPT" }) as HTMLButtonElement).disabled).toBe(true);

    switchAssistant("devin");
    expect(screen.getByRole("textbox")).toBe(devinDraft);
    expect(devinDraft.value).toBe("Devin-only draft");
    expect(screen.getByText("Output received in background")).toBeTruthy();
    switchAssistant("chatgpt");
    expect((screen.getByLabelText("Message ChatGPT") as HTMLTextAreaElement).value).toBe("ChatGPT-only draft");
    expect(transport.send).not.toHaveBeenCalled();
  });

  it("keeps multiple ChatGPT drafts independent through the assistant selector", async () => {
    render(<App />);
    await act(async () => {});
    switchAssistant("chatgpt");
    expect((screen.getByRole("combobox", { name: "Assistant" }) as HTMLSelectElement).value).toBe("chatgpt");
    fireEvent.change(screen.getByLabelText("Message ChatGPT"), { target: { value: "First prompt" } });
    fireEvent.click(screen.getByRole("button", { name: "New ChatGPT draft" }));
    expect((screen.getByLabelText("Message ChatGPT") as HTMLTextAreaElement).value).toBe("");
    fireEvent.change(screen.getByLabelText("Message ChatGPT"), { target: { value: "Second prompt" } });
    fireEvent.click(screen.getByRole("button", { name: /Draft 1/ }));
    expect((screen.getByLabelText("Message ChatGPT") as HTMLTextAreaElement).value).toBe("First prompt");
    switchAssistant("devin");
    expect(screen.getAllByRole("combobox", { name: "Assistant" }).every(element => (element as HTMLSelectElement).value === "devin")).toBe(true);
  });
});

describe("permission panel", () => {
  async function openDevinSession(): Promise<void> {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    const request = transport.send.mock.calls[0][0];
    if (request.type !== "new_session" || !request.requestId) throw new Error("Expected correlated new session request");
    const requestId = request.requestId;
    await act(async () => { transport.receive({ type: "session_created", sessionId: "devin-live", requestId }); });
  }

  it("shows the exact command from data.detail for an exec permission request", async () => {
    await openDevinSession();
    await act(async () => {
      transport.receive({
        type: "request_permission",
        sessionId: "devin-live",
        requestId: "perm-1",
        toolCallId: "exec_0",
        data: {
          toolCallId: "exec_0",
          detail: "rm -rf C:/proj/victim",
          toolCall: { toolCallId: "exec_0", title: "Ran rm", kind: "execute" },
          options: [
            { optionId: "allow_once", name: "Allow", kind: "allow_once" },
            { optionId: "reject_once", name: "Reject", kind: "reject_once" },
          ],
        },
      });
    });
    expect(screen.getByText("Approval required for Ran rm")).toBeTruthy();
    expect(screen.getByText("rm -rf C:/proj/victim")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Allow" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("shows path and diff detail for an edit permission request", async () => {
    await openDevinSession();
    await act(async () => {
      transport.receive({
        type: "request_permission",
        sessionId: "devin-live",
        requestId: "perm-2",
        toolCallId: "edit_1",
        data: {
          toolCallId: "edit_1",
          detail: "C:/proj/app.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3",
          toolCall: { toolCallId: "edit_1", title: "Edit file", kind: "edit" },
          options: [{ optionId: "allow_once", name: "Allow", kind: "allow_once" }],
        },
      });
    });
    expect(screen.getByText("Approval required for Edit file")).toBeTruthy();
    expect(screen.getByText(/C:\/proj\/app\.py/)).toBeTruthy();
    expect(screen.getByText(/\+y = 3/)).toBeTruthy();
  });

  it("shows the tool call preview when the permission request is bare", async () => {
    await openDevinSession();
    // Real `devin acp` sends the diff/command on the `tool_call` update and
    // only a bare {toolCallId} inside the permission request's toolCall.
    await act(async () => {
      transport.receive({
        type: "session_update",
        sessionId: "devin-live",
        update: {
          type: "tool_call",
          toolCallId: "edit_1",
          title: "Edit file",
          kind: "edit",
          content: [{ text: "C:/proj/app.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3" }],
        },
      });
    });
    await act(async () => {
      transport.receive({
        type: "request_permission",
        sessionId: "devin-live",
        requestId: "perm-4",
        toolCallId: "edit_1",
        data: {
          toolCallId: "edit_1",
          toolCall: { toolCallId: "edit_1" },
          options: [{ optionId: "allow_once", name: "Allow", kind: "allow_once" }],
        },
      });
    });
    expect(screen.getByText("Approval required for Edit file")).toBeTruthy();
    expect(screen.getByText(/C:\/proj\/app\.py/)).toBeTruthy();
    expect(screen.getByText(/\+y = 3/)).toBeTruthy();
  });

  it("falls back to the generic notice when the request carries no detail", async () => {
    await openDevinSession();
    await act(async () => {
      transport.receive({
        type: "request_permission",
        sessionId: "devin-live",
        requestId: "perm-3",
        toolCallId: "mcp_0",
        data: {
          toolCallId: "mcp_0",
          toolCall: { toolCallId: "mcp_0", title: "MCP tool", kind: "other" },
          options: [{ optionId: "allow_once", name: "Allow", kind: "allow_once" }],
        },
      });
    });
    expect(screen.getByText(/did not include the exact command or arguments/)).toBeTruthy();
  });
});

describe("error banner", () => {
  it("surfaces a null-requestId prompt error in the banner while the turn keeps working", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    const createRequest = transport.send.mock.calls[0][0];
    if (createRequest.type !== "new_session" || !createRequest.requestId) throw new Error("Expected correlated new session request");
    const requestId = createRequest.requestId;
    await act(async () => { transport.receive({ type: "session_created", sessionId: "devin-live", requestId }); });
    transport.send.mockClear();

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "keep working" } });
    fireEvent.click(screen.getByRole("button", { name: "Send prompt" }));
    const promptRequest = transport.send.mock.calls.find(([m]) => m.type === "prompt")?.[0];
    if (!promptRequest || promptRequest.type !== "prompt" || !promptRequest.requestId) throw new Error("Expected correlated prompt request");
    expect(screen.getAllByRole("status", { name: "Devin status: Working" }).length).toBeGreaterThan(0);

    await act(async () => {
      transport.receive({ type: "error", sessionId: "devin-live", requestId: null, operation: "prompt", message: "uncorrelated null boom" });
    });
    expect(screen.getByText("uncorrelated null boom")).toBeTruthy();
    expect(screen.getAllByRole("status", { name: "Devin status: Working" }).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Cancel streaming" })).toBeTruthy();

    await act(async () => {
      transport.receive({ type: "error", sessionId: "devin-live", requestId: "req-stale", operation: "prompt", message: "stale boom" });
    });
    expect(screen.queryByText("stale boom")).toBeNull();
    expect(screen.getAllByRole("status", { name: "Devin status: Working" }).length).toBeGreaterThan(0);
  });
});
