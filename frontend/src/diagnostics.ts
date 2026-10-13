export type DiagnosticStatus = "ok" | "warning" | "setup_required" | "error";

export interface IntegrationDiagnostic {
  label: string;
  status: DiagnosticStatus;
  message: string;
}

export interface DiagnosticCheck extends IntegrationDiagnostic {
  id: string;
  path: string | null;
}

export interface DiagnosticsSnapshot {
  status: "ready" | "setup_required" | "error";
  history_ready: boolean;
  cli: {
    executable: string | null;
    version: string | null;
    tested_version: string;
    compaction_supported: boolean | null;
    status: DiagnosticStatus;
    message: string;
  };
  checks: DiagnosticCheck[];
  optional_integrations: Record<"devin_usage" | "codex_usage" | "chatgpt", IntegrationDiagnostic>;
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function diagnostic(value: unknown): value is IntegrationDiagnostic {
  return object(value) && typeof value.label === "string" && typeof value.message === "string"
    && typeof value.status === "string" && ["ok", "warning", "setup_required", "error"].includes(value.status);
}

export function isDiagnosticsSnapshot(value: unknown): value is DiagnosticsSnapshot {
  if (!object(value) || typeof value.status !== "string" || !["ready", "setup_required", "error"].includes(value.status)
    || typeof value.history_ready !== "boolean" || !object(value.cli) || !object(value.optional_integrations)) return false;
  const cli = value.cli;
  const optional = value.optional_integrations;
  return nullableString(cli.executable) && nullableString(cli.version)
    && typeof cli.tested_version === "string" && typeof cli.message === "string"
    && typeof cli.status === "string" && ["ok", "warning", "setup_required", "error"].includes(cli.status)
    && (cli.compaction_supported === null || typeof cli.compaction_supported === "boolean")
    && Array.isArray(value.checks) && value.checks.every(check => diagnostic(check)
      && object(check) && typeof check.id === "string" && nullableString(check.path))
    && ["devin_usage", "codex_usage", "chatgpt"].every(key => diagnostic(optional[key]));
}
