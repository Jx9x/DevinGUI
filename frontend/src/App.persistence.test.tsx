import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Project, Session, WSInbound, WSOutbound } from "./types";
import { readPromptDraft, savePromptDraft } from "./promptPersistence";
import App from "./App";

const fixture = vi.hoisted(() => ({
  project: {
    path: "C:/projects/example",
    name: "example",
    session_count: 1,
    last_activity_at: null,
  } as Project,
  session: {
    id: "session-example",
    title: "Example conversation",
    working_directory: "C:/projects/example",
    model: null,
    agent_mode: null,
    created_at: null,
    last_activity_at: null,
    hidden: null,
  } as Session,
  receive: (_message: WSInbound) => {},
  send: vi.fn((_message: WSOutbound) => ({ sent: true })),
}));

vi.mock("./hooks/useWebSocket", () => ({
  useWebSocket: (receive: (message: WSInbound) => void) => {
    fixture.receive = receive;
    return { send: fixture.send, connected: true, cancelQueuedPrompt: () => false };
  },
}));
vi.mock("./hooks/useModels", () => ({
  useModels: () => ({
    models: [], selectableModels: [], selectedModel: null, lastUsedVariants: {},
    hasNewModels: false, cacheUpdatedAt: null, error: null,
    refresh: vi.fn(async () => {}), setSelectedModel: vi.fn(),
    excludeModel: vi.fn(), includeModel: vi.fn(), excludeModels: vi.fn(),
    includeModels: vi.fn(), acknowledgeModel: vi.fn(),
  }),
}));
vi.mock("./components/ChatGPTPane", () => ({ ChatGPTPane: () => null }));
vi.mock("./hooks/useDiagnostics", () => ({ useDiagnostics: () => ({ snapshot: null, loading: false, error: null, refresh: async () => {} }) }));
vi.mock("./api/client", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    getSessions: vi.fn(async (cwd?: string) => !cwd || cwd === fixture.project.path ? [fixture.session] : []),
    getProjects: vi.fn(async () => [fixture.project]),
    getUsage: vi.fn(async () => null),
    getCodexUsage: vi.fn(async () => null),
  },
}));

beforeEach(() => {
  window.localStorage.clear();
  fixture.send.mockClear();
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

describe("new session project selection", () => {
  it("requires a project and sends its folder as the working directory", async () => {
    render(<App />);
    await screen.findByRole("option", { name: "example (1)" });
    const newSession = screen.getByRole("button", { name: "New session" }) as HTMLButtonElement;
    expect(newSession.disabled).toBe(true);
    expect(newSession.title).toBe("Select a project folder first");
    fireEvent.click(newSession);
    expect(fixture.send).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole("combobox"), { target: { value: fixture.project.path } });
    expect(newSession.disabled).toBe(false);
    fireEvent.click(newSession);
    expect(fixture.send).toHaveBeenCalledWith(expect.objectContaining({
      type: "new_session", cwd: fixture.project.path,
    }));

    fixture.send.mockClear();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "" } });
    expect(newSession.disabled).toBe(true);
    fireEvent.click(newSession);
    expect(fixture.send).not.toHaveBeenCalled();
  });
});

describe("unfinished Devin prompt recovery", () => {
  it("makes an earlier conversation's draft available after starting a new chat", async () => {
    savePromptDraft(fixture.session.id, "Prompt from the previous conversation");
    render(<App />);
    await screen.findByRole("option", { name: "example (1)" });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: fixture.project.path } });

    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    const request = fixture.send.mock.calls.find(([message]) => message.type === "new_session")?.[0];
    if (!request || request.type !== "new_session" || !request.requestId) throw new Error("Expected new session request");
    const requestId = request.requestId;
    await act(async () => {
      fixture.receive({ type: "session_created", sessionId: "session-new", requestId });
    });
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");

    fireEvent.click(screen.getByRole("button", { name: "Saved drafts (1)" }));
    expect(screen.getByRole("dialog", { name: "Saved Devin drafts" })).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "Saved draft for Example conversation" }) as HTMLTextAreaElement).value).toBe("Prompt from the previous conversation");

    fireEvent.click(screen.getByRole("button", { name: "Open conversation" }));
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Prompt from the previous conversation");
  });

  it.each([
    ["selected project", true],
    ["All Projects", false],
  ])("reopens the folder and draft from %s after a page remount", async (_label, selectProject) => {
    const first = render(<App />);
    const projectSelect = first.container.querySelector("select") as HTMLSelectElement;
    await screen.findByRole("option", { name: "example (1)" });
    if (selectProject) {
      fireEvent.change(projectSelect, { target: { value: fixture.project.path } });
    }
    fireEvent.click(await screen.findByRole("button", { name: /Example conversation session-example/ }));
    const request = fixture.send.mock.calls.find(([message]) => message.type === "view_history")?.[0];
    if (!request || request.type !== "view_history" || !request.requestId) throw new Error("Expected history request");
    const requestId = request.requestId;
    await act(async () => {
      fixture.receive({ type: "history_loaded", sessionId: fixture.session.id, messages: [], reasoning: [], readOnly: false, requestId });
    });

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "A prompt still in progress" } });
    expect(readPromptDraft(fixture.session.id)).toBe("A prompt still in progress");
    first.unmount();
    fixture.send.mockClear();

    const second = render(<App />);
    await waitFor(() => {
      expect((second.container.querySelector("select") as HTMLSelectElement).value).toBe(fixture.project.path);
      expect(fixture.send.mock.calls.some(([message]) => message.type === "view_history" && message.sessionId === fixture.session.id)).toBe(true);
    });
    const historyRequest = fixture.send.mock.calls.find(([message]) => message.type === "view_history")?.[0];
    if (!historyRequest || historyRequest.type !== "view_history" || !historyRequest.requestId) throw new Error("Expected history request");
    const historyRequestId = historyRequest.requestId;
    await act(async () => {
      fixture.receive({ type: "history_loaded", sessionId: fixture.session.id, messages: [], reasoning: [], readOnly: true, requestId: historyRequestId });
    });
    const restoredPrompt = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(restoredPrompt.value).toBe("A prompt still in progress");
    expect(restoredPrompt.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Start conversation" }));
    const resumeRequest = fixture.send.mock.calls.find(([message]) => message.type === "resume_session")?.[0];
    if (!resumeRequest || resumeRequest.type !== "resume_session" || !resumeRequest.requestId) throw new Error("Expected resume request");
    const resumeRequestId = resumeRequest.requestId;
    await act(async () => {
      fixture.receive({ type: "session_resumed", sessionId: fixture.session.id, requestId: resumeRequestId });
    });
    expect(restoredPrompt.disabled).toBe(false);
    expect(restoredPrompt.value).toBe("A prompt still in progress");
    expect(fixture.send.mock.calls.some(([message]) => message.type === "prompt")).toBe(false);
  });
});
