import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api, ApiError } from "./client";

const mockFetch = vi.fn();
const diagnostic = { label: "Check", status: "ok", message: "Ready" };
const diagnosticsBody = {
  status: "ready", history_ready: true,
  cli: { executable: "synthetic-devin", version: "3000.11.3", tested_version: "3000.11.3", compaction_supported: true, status: "ok", message: "Tested CLI" },
  checks: [{ ...diagnostic, id: "database", path: "synthetic/sessions.db" }],
  optional_integrations: { devin_usage: diagnostic, codex_usage: diagnostic, chatgpt: diagnostic },
};

describe("api client", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", mockFetch);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("reads independent ChatGPT quotas and can bypass the server cache", async () => {
    const body = {
      primary: { used_percent: 26, window_minutes: 300, reset_unix: 1_790_888_939 },
      secondary: null, plan_name: "plus", account_email: "person@example.com", fetched_at: 123,
      error: null, stale: false,
    };
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify(body), { status: 200 }));
    expect(await api.getCodexUsage(true)).toEqual(body);
    expect(mockFetch).toHaveBeenLastCalledWith("/api/usage/codex?refresh=true", undefined);
  });

  it("rejects malformed ChatGPT quotas before they reach the usage box", async () => {
    const body = { primary: { used_percent: "26", window_minutes: 300, reset_unix: null },
      secondary: null, plan_name: null, account_email: null, fetched_at: 123, error: null, stale: false };
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify(body), { status: 200 }));
    await expect(api.getCodexUsage()).rejects.toThrow("Invalid ChatGPT usage response shape");
  });

  it("getModels returns a valid models response", async () => {
    const responseBody = {
      models: [
        {
          id: "gpt-4",
          display_name: "GPT-4",
          vision: false,
          tags: ["New"],
          description: "A model",
          is_new: true,
          available: true,
          excluded: false,
        },
      ],
      selectable: [
        {
          id: "gpt-4",
          display_name: "GPT-4",
          vision: false,
          tags: ["New"],
          description: "A model",
          is_new: true,
          available: true,
          excluded: false,
        },
      ],
      recent: [],
      last_used: null,
      has_new: true,
    };
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify(responseBody), { status: 200 }));

    const result = await api.getModels();
    expect(result.models).toHaveLength(1);
    expect(result.models[0].id).toBe("gpt-4");
    expect(mockFetch).toHaveBeenCalledWith("/api/models", undefined);
  });

  it("archives a session with a non-destructive POST endpoint", async () => {
    const responseBody = {
      ok: true,
      session_id: "session-1",
      archive_path: "C:\\Users\\Example\\Documents\\Devin_Chat\\ProjectA\\session-1",
      project_name: "ProjectA",
    };
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify(responseBody), { status: 200 }));

    const result = await api.archiveSession("session-1");

    expect(result).toEqual(responseBody);
    expect(mockFetch).toHaveBeenCalledWith("/api/sessions/session-1/archive", {
      method: "POST",
    });
  });

  it("sends the application browse header for roots and path listings", async () => {
    const body = { path: "", parent: "", entries: [], roots: [] };
    mockFetch.mockImplementation(() =>
      Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
    );

    await api.browse();
    expect(mockFetch).toHaveBeenLastCalledWith("/api/browse", {
      headers: { "X-DevinGUI-Browse": "1" },
    });

    await api.browse("C:\\projects");
    expect(mockFetch).toHaveBeenLastCalledWith("/api/browse?path=C%3A%5Cprojects", {
      headers: { "X-DevinGUI-Browse": "1" },
    });
  });

  it.each([false, true])("sends an explicit export overwrite choice: %s", async (overwrite) => {
    const body = {
      output: "C:\\Projects\\Chat.md", overwrite,
      include_reasoning: false, include_tool_calls: false, include_tool_outputs: false,
      include_summaries: false, db_only: false,
    };
    const response = { ok: true, path: body.output, message: "Wrote export" };
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify(response), { status: 200 }));

    expect(await api.exportSession("s-1", body)).toEqual(response);
    expect(mockFetch).toHaveBeenLastCalledWith("/api/sessions/s-1/export", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  });

  it("preserves export conflicts so the caller can request overwrite confirmation", async () => {
    const body = JSON.stringify({ detail: "Destination already exists", error: "export_session" });
    mockFetch.mockResolvedValueOnce(new Response(body, { status: 409, statusText: "Conflict" }));

    await expect(api.exportSession("s-1", {
      output: "C:\\Projects\\Chat.md", include_reasoning: false, include_tool_calls: false,
      include_tool_outputs: false, include_summaries: false, db_only: false,
    })).rejects.toMatchObject({ status: 409, body });
  });

  it("shows actionable structured setup errors instead of only HTTP status", async () => {
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Open Diagnostics and update DevinGUI", error: "list_sessions" }), { status: 500 }));
    await expect(api.getSessions()).rejects.toThrow("Open Diagnostics and update DevinGUI");
  });

  it("rejects malformed diagnostics and uses the explicit retry endpoint", async () => {
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify({ status: "ok" }), { status: 200 }));
    await expect(api.getDiagnostics(true)).rejects.toThrow("Invalid diagnostics response shape");
    expect(mockFetch).toHaveBeenLastCalledWith("/api/diagnostics?refresh=true", undefined);
  });

  it("accepts a complete diagnostics response", async () => {
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify(diagnosticsBody), { status: 200 }));
    expect(await api.getDiagnostics()).toEqual(diagnosticsBody);
    expect(mockFetch).toHaveBeenLastCalledWith("/api/diagnostics", undefined);
  });

  it.each([
    { ...diagnosticsBody, status: ["ready"] },
    { ...diagnosticsBody, cli: { ...diagnosticsBody.cli, status: ["ok"] } },
    { ...diagnosticsBody, checks: [{ ...diagnosticsBody.checks[0], status: ["ok"] }] },
    { ...diagnosticsBody, optional_integrations: { ...diagnosticsBody.optional_integrations, chatgpt: { ...diagnostic, status: ["ok"] } } },
  ])("rejects coerced diagnostics statuses before rendering: %j", async (body) => {
    mockFetch.mockResolvedValueOnce(new Response(JSON.stringify(body), { status: 200 }));
    await expect(api.getDiagnostics()).rejects.toThrow("Invalid diagnostics response shape");
  });

  it("throws ApiError with status and body on HTTP error", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response("Internal Server Error", { status: 500, statusText: "Server Error" })
    );

    try {
      await api.getModels();
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      if (e instanceof ApiError) {
        expect(e.status).toBe(500);
        expect(e.body).toBe("Internal Server Error");
      }
    }
  });

  it("throws ApiError for invalid response shape", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ unexpected: true }), { status: 200 })
    );

    await expect(api.getModels()).rejects.toBeInstanceOf(ApiError);
  });

  it("rejects on network failure", async () => {
    mockFetch.mockRejectedValueOnce(new Error("Network error"));
    await expect(api.getModels()).rejects.toThrow("Network error");
  });

  it("ApiError preserves status and body", () => {
    const err = new ApiError("failed", 404, "not found");
    expect(err.message).toBe("failed");
    expect(err.status).toBe(404);
    expect(err.body).toBe("not found");
  });
});
