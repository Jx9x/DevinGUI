import {
  assertSessionArray,
  assertProjectArray,
  assertBrowseResult,
  assertModelsResponse,
  type Session,
  type Project,
  type BrowseResult,
  type ModelsResponse,
  type ArchiveResponse,
  type UsageSnapshot,
  type CodexUsageSnapshot,
  type ExportResponse,
  type ExportContentOptions,
  type ExportPreviewResponse,
} from "../types";

import { isDiagnosticsSnapshot, type DiagnosticsSnapshot } from "../diagnostics";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public body?: string
  ) {
    super(message);
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function apiFetch<T>(
  url: string,
  validate: (value: unknown) => value is T,
  label: string,
  options?: RequestInit
): Promise<T> {
  const res = await fetch(url, options);
  const text = await res.text();
  if (!res.ok) {
    let message = `${label} failed: ${res.status} ${res.statusText}`;
    try {
      const body: unknown = JSON.parse(text);
      if (isObject(body) && typeof body.detail === "string") message = body.detail.slice(0, 2000);
    } catch {
      message = `${label} failed: ${res.status} ${res.statusText}`;
    }
    throw new ApiError(message, res.status, text);
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    throw new ApiError(`Invalid JSON in ${label} response`, res.status, text);
  }
  if (!validate(data)) {
    throw new ApiError(`Invalid ${label} response shape`, res.status, text);
  }
  return data;
}

function isSessionArray(value: unknown): value is Session[] {
  try {
    assertSessionArray(value);
    return true;
  } catch {
    return false;
  }
}

function isProjectArray(value: unknown): value is Project[] {
  try {
    assertProjectArray(value);
    return true;
  } catch {
    return false;
  }
}

function isBrowseResultValue(value: unknown): value is BrowseResult {
  try {
    assertBrowseResult(value);
    return true;
  } catch {
    return false;
  }
}

function isModelsResponse(value: unknown): value is ModelsResponse {
  try {
    assertModelsResponse(value);
    return true;
  } catch {
    return false;
  }
}

function isModelActionResponse(value: unknown): value is { ok: true } {
  return isObject(value) && value.ok === true;
}

function isArchiveResponse(value: unknown): value is ArchiveResponse {
  return (
    isObject(value) &&
    value.ok === true &&
    typeof value.session_id === "string" &&
    typeof value.archive_path === "string" &&
    typeof value.project_name === "string"
  );
}

function isUsageSnapshot(value: unknown): value is UsageSnapshot {
  return (
    isObject(value) &&
    typeof value.fetched_at === "number" &&
    (value.error === null || typeof value.error === "string") &&
    (value.daily_used_percent === null || typeof value.daily_used_percent === "number") &&
    (value.weekly_used_percent === null || typeof value.weekly_used_percent === "number") &&
    (value.weekly_reset_unix === null || typeof value.weekly_reset_unix === "number") &&
    (value.plan_name === null || typeof value.plan_name === "string")
  );
}

function isCodexUsageSnapshot(value: unknown): value is CodexUsageSnapshot {
  const nullableNumber = (field: unknown) => field === null || (typeof field === "number" && Number.isFinite(field));
  const window = (field: unknown) => field === null || (
    isObject(field) && nullableNumber(field.used_percent) && nullableNumber(field.window_minutes) && nullableNumber(field.reset_unix)
  );
  return isObject(value) && window(value.primary) && window(value.secondary)
    && (value.plan_name === null || typeof value.plan_name === "string")
    && (value.account_email === null || typeof value.account_email === "string")
    && typeof value.fetched_at === "number" && Number.isFinite(value.fetched_at)
    && (value.error === null || typeof value.error === "string") && typeof value.stale === "boolean";
}

function isExportResponse(value: unknown): value is ExportResponse {
  return (
    isObject(value) &&
    value.ok === true &&
    typeof value.path === "string" &&
    typeof value.message === "string"
  );
}

function isExportPreviewResponse(value: unknown): value is ExportPreviewResponse {
  if (!isObject(value) || typeof value.markdown !== "string" || !isObject(value.counts)) {
    return false;
  }
  const counts = value.counts;
  return ["base", "include_reasoning", "include_tool_calls", "include_tool_outputs", "include_summaries", "db_only", "total"]
    .every((key) => typeof counts[key] === "number" && Number.isFinite(counts[key]) && counts[key] >= 0);
}

export const api = {
  getDiagnostics(refresh = false): Promise<DiagnosticsSnapshot> {
    return apiFetch(`/api/diagnostics${refresh ? "?refresh=true" : ""}`, isDiagnosticsSnapshot, "diagnostics");
  },

  getSessions(cwd?: string): Promise<Session[]> {
    const url = cwd ? `/api/sessions?cwd=${encodeURIComponent(cwd)}` : "/api/sessions";
    return apiFetch(url, isSessionArray, "sessions");
  },

  getProjects(): Promise<Project[]> {
    return apiFetch("/api/projects", isProjectArray, "projects");
  },

  browse(path?: string): Promise<BrowseResult> {
    const url = path ? `/api/browse?path=${encodeURIComponent(path)}` : "/api/browse";
    return apiFetch(url, isBrowseResultValue, "browse", {
      headers: { "X-DevinGUI-Browse": "1" },
    });
  },

  archiveSession(sessionId: string): Promise<ArchiveResponse> {
    return apiFetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/archive`,
      isArchiveResponse,
      "archive session",
      { method: "POST" }
    );
  },

  getModels(): Promise<ModelsResponse> {
    return apiFetch("/api/models", isModelsResponse, "models");
  },

  excludeModel(id: string): Promise<void> {
    return apiFetch(
      `/api/models/${encodeURIComponent(id)}/exclude`,
      isModelActionResponse,
      "exclude model",
      { method: "POST" }
    ).then(() => undefined);
  },

  includeModel(id: string): Promise<void> {
    return apiFetch(
      `/api/models/${encodeURIComponent(id)}/include`,
      isModelActionResponse,
      "include model",
      { method: "POST" }
    ).then(() => undefined);
  },

  excludeModels(ids: string[]): Promise<void> {
    return apiFetch("/api/models/bulk-exclude", isModelActionResponse, "exclude models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_ids: ids }),
    }).then(() => undefined);
  },

  includeModels(ids: string[]): Promise<void> {
    return apiFetch("/api/models/bulk-include", isModelActionResponse, "include models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_ids: ids }),
    }).then(() => undefined);
  },

  acknowledgeModel(id: string): Promise<void> {
    return apiFetch(
      `/api/models/${encodeURIComponent(id)}/acknowledge`,
      isModelActionResponse,
      "acknowledge model",
      { method: "POST" }
    ).then(() => undefined);
  },

  getUsage(): Promise<UsageSnapshot> {
    return apiFetch("/api/usage", isUsageSnapshot, "usage");
  },

  getCodexUsage(refresh = false): Promise<CodexUsageSnapshot> {
    return apiFetch(`/api/usage/codex${refresh ? "?refresh=true" : ""}`, isCodexUsageSnapshot, "ChatGPT usage");
  },

  exportSession(
    sessionId: string,
    body: ExportContentOptions & { output: string; overwrite?: boolean }
  ): Promise<ExportResponse> {
    return apiFetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/export`,
      isExportResponse,
      "export session",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    );
  },

  previewExport(sessionId: string, options: ExportContentOptions): Promise<ExportPreviewResponse> {
    return apiFetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/export/preview`,
      isExportPreviewResponse,
      "preview export",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(options),
      }
    );
  },
};
