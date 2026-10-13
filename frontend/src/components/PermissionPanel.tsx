import { AlertTriangle } from "lucide-react";
import type { PermissionRequest } from "../chatSessionState";
import type { UIMessage } from "../types";

interface PermissionPanelProps {
  request: PermissionRequest | null;
  messages: UIMessage[];
  connected: boolean;
  onRespond: (decision: "allow" | "deny", optionId?: string) => void;
  onDiscard: () => void;
}

export function PermissionPanel({ request, messages, connected, onRespond, onDiscard }: PermissionPanelProps) {
  const permissionData = request?.data as Record<string, unknown> | undefined;
  const permissionRequestToolCall =
    typeof permissionData?.toolCall === "object" && permissionData.toolCall !== null
      ? (permissionData.toolCall as Record<string, unknown>)
      : undefined;
  const permissionToolCall = messages
    .flatMap((message) => message.toolCalls ?? [])
    .find((toolCall) => toolCall.id === request?.toolCallId);
  const permissionTitle =
    permissionToolCall?.title ??
    (typeof permissionRequestToolCall?.title === "string" ? permissionRequestToolCall.title : undefined);
  const permissionDetails =
    (typeof permissionData?.detail === "string" && permissionData.detail) ||
    permissionToolCall?.content
      ?.map((item) => (typeof item === "string" ? item : "text" in item && typeof item.text === "string" ? item.text : ""))
      .filter(Boolean)
      .join("\n");
  const permissionOptions = Array.isArray(permissionData?.options)
    ? permissionData.options.filter(
        (option): option is { optionId: string; name?: string; kind?: string } =>
          typeof option === "object" &&
          option !== null &&
          typeof (option as Record<string, unknown>).optionId === "string"
      )
    : [];

  if (!request) return null;

  return (
    <div className="border-b border-yellow-700 bg-yellow-900/20 px-4 py-3 text-sm text-yellow-100">
      <div className="mx-auto max-w-4xl space-y-2">
        {request.submitting ? (
          <span>Decision ({request.submittedDecision}) submitted — waiting for backend…</span>
        ) : request.interrupted ? (
          <div>
            <AlertTriangle className="mr-1 inline h-4 w-4 text-yellow-400" />
            {request.interruptError ?? "Connection lost before the permission decision was confirmed."}
          </div>
        ) : (
          <>
            <div className="font-medium">Approval required for {permissionTitle ?? "a tool call"}</div>
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-sm bg-black/20 px-2 py-1 font-mono text-xs text-yellow-50">
              {permissionDetails || "Devin did not include the exact command or arguments in this permission request."}
            </pre>
          </>
        )}
        <div className="flex flex-wrap items-center gap-2">
          {request.interrupted && (
            <button onClick={onDiscard} className="rounded-md border border-yellow-600 px-3 py-1 text-xs font-medium text-yellow-200 hover:bg-yellow-800/30">Discard</button>
          )}
          {(permissionOptions.length ? permissionOptions : [
            { optionId: "deny", name: "Deny", kind: "reject_once" },
            { optionId: "allow", name: "Allow", kind: "allow_once" },
          ]).map((option) => {
            const deny = /deny|reject/.test(option.kind ?? option.optionId);
            return (
              <button
                key={option.optionId}
                onClick={() => onRespond(deny ? "deny" : "allow", option.optionId)}
                disabled={!connected || request?.submitting}
                className={deny ? "rounded-md border border-yellow-600 px-3 py-1 text-xs font-medium text-yellow-200 hover:bg-yellow-800/30 disabled:cursor-not-allowed disabled:opacity-50" : "rounded-md bg-accent px-3 py-1 text-xs font-medium text-white hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"}
              >
                {option.name ?? option.optionId}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
