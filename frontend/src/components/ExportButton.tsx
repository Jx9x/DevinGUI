import { useState, useMemo, useEffect } from "react";
import { ClipboardCopy, Download, X } from "lucide-react";
import { api, ApiError } from "../api/client";
import type { ExportContentOptions, ExportPreviewResponse } from "../types";

interface ExportButtonProps {
  sessionId: string | null;
  defaultDirectory: string;
  title?: string | null;
}

function safeFilename(name: string): string {
  const cleaned = name.replace(/[\\/:*?"<>|]+/g, "_").replace(/^[ .]+|[ .]+$/g, "");
  return cleaned || "conversation";
}

interface ExportOptions {
  includeReasoning: boolean;
  includeToolCalls: boolean;
  includeToolOutputs: boolean;
  includeSummaries: boolean;
  dbOnly: boolean;
}

const DEFAULT_OPTIONS: ExportOptions = {
  includeReasoning: false,
  includeToolCalls: false,
  includeToolOutputs: false,
  includeSummaries: false,
  dbOnly: false,
};

function requestOptions(options: ExportOptions): ExportContentOptions {
  return {
    include_reasoning: options.includeReasoning,
    include_tool_calls: options.includeToolCalls,
    include_tool_outputs: options.includeToolOutputs,
    include_summaries: options.includeSummaries,
    db_only: options.dbOnly,
  };
}

function tokenLabel(count: number | undefined): string {
  return count === undefined ? "…" : `≈${count.toLocaleString("en-US")} tokens`;
}

export function ExportButton({ sessionId, defaultDirectory, title }: ExportButtonProps) {
  const [isOpen, setIsOpen] = useState(false);
  const defaultOutput = useMemo(() => {
    const name = safeFilename(title || sessionId || "conversation");
    const dir = defaultDirectory.replace(/[\\/]+$/, "");
    return dir ? `${dir}\\${name}.md` : `${name}.md`;
  }, [defaultDirectory, sessionId, title]);
  const [output, setOutput] = useState(defaultOutput);
  const [options, setOptions] = useState<ExportOptions>(DEFAULT_OPTIONS);
  const [status, setStatus] = useState<{ type: "saving" | "copying" | "success" | "error" | "conflict"; text: string; key?: string } | null>(null);
  const [preview, setPreview] = useState<{ key: string; data: ExportPreviewResponse } | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const previewKey = JSON.stringify([sessionId, options]);
  const exportKey = JSON.stringify([sessionId, output.trim(), options]);
  const currentPreview = preview?.key === previewKey ? preview.data : null;
  const busy = status?.type === "saving" || status?.type === "copying";
  const validOutput = output.trim().toLowerCase().endsWith(".md");
  const canOverwrite = status?.type === "conflict" && status.key === exportKey;

  useEffect(() => {
    if (isOpen) {
      setOutput(defaultOutput);
      setOptions(DEFAULT_OPTIONS);
      setStatus(null);
    }
  }, [isOpen, defaultOutput, sessionId]);

  useEffect(() => {
    if (!isOpen || !sessionId) return;
    let active = true;
    setPreview(null);
    setPreviewError(null);
    api.previewExport(sessionId, requestOptions(options))
      .then((data) => {
        if (active) setPreview({ key: previewKey, data });
      })
      .catch((error: unknown) => {
        if (active) setPreviewError(error instanceof Error ? error.message : "Preview failed");
      });
    return () => { active = false; };
  }, [isOpen, sessionId, options, previewKey]);

  const toggle = (key: keyof ExportOptions) => {
    setStatus(null);
    setOptions((prev) => {
      if (key === "includeToolOutputs" && !prev.includeToolOutputs) {
        return { ...prev, includeToolCalls: true, includeToolOutputs: true };
      }
      if (key === "includeToolCalls" && prev.includeToolCalls) {
        return { ...prev, includeToolCalls: false, includeToolOutputs: false };
      }
      return { ...prev, [key]: !prev[key] };
    });
  };

  const handleExport = async (overwrite = false) => {
    if (!sessionId || !validOutput || busy || (overwrite && !canOverwrite)) return;
    const destination = output.trim();
    setStatus({ type: "saving", text: "" });
    try {
      await api.exportSession(sessionId, {
        output: destination,
        overwrite,
        ...requestOptions(options),
      });
      setIsOpen(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setStatus({
          type: "conflict",
          key: exportKey,
          text: `A file already exists at ${destination}. Choose another .md filename or click Overwrite file to replace it.`,
        });
      } else {
        setStatus({ type: "error", text: e instanceof Error ? e.message : "Export failed" });
      }
    }
  };

  const handleCopy = async () => {
    if (!currentPreview) return;
    setStatus({ type: "copying", text: "" });
    try {
      await navigator.clipboard.writeText(currentPreview.markdown);
      setStatus({ type: "success", text: "Markdown copied to clipboard." });
    } catch {
      setStatus({ type: "error", text: "Could not copy to the clipboard." });
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setIsOpen(true)}
        disabled={!sessionId}
        aria-label="Export conversation"
        title="Export conversation"
        className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40"
      >
        <Download className="h-3.5 w-3.5" /> Export
      </button>
      {isOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="export-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 px-4"
          onClick={() => setIsOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-lg border border-border bg-bg-secondary p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 id="export-title" className="text-base font-semibold text-gray-100">
                Export conversation
              </h2>
              <button
                type="button"
                onClick={() => setIsOpen(false)}
                aria-label="Close"
                className="rounded-sm p-1 text-gray-500 hover:bg-bg-hover hover:text-gray-300"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <label className="block text-xs text-gray-400">
              Output file
              <input
                type="text"
                value={output}
                disabled={busy}
                aria-describedby="export-output-hint"
                onChange={(e) => { setOutput(e.target.value); setStatus(null); }}
                className="mt-1 block w-full rounded-md border border-border bg-bg-tertiary px-2 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:border-accent focus:outline-hidden"
              />
            </label>
            <p id="export-output-hint" className="mt-1 text-xs text-gray-500">Exports must be saved as local .md files. Existing files require confirmation.</p>

            <div className="mt-4 flex items-center justify-between text-xs text-gray-400">
              <span>Base conversation</span>
              <span>{tokenLabel(currentPreview?.counts.base)}</span>
            </div>

            <div className="mt-3 space-y-2">
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input
                  type="checkbox"
                  checked={options.includeReasoning}
                  onChange={() => toggle("includeReasoning")}
                  disabled={busy}
                  className="h-4 w-4 rounded-sm"
                />
                <span>Include reasoning</span>
                <span className="ml-auto whitespace-nowrap text-xs text-gray-400">{tokenLabel(currentPreview?.counts.include_reasoning)}</span>
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input
                  type="checkbox"
                  checked={options.includeToolCalls}
                  onChange={() => toggle("includeToolCalls")}
                  disabled={busy}
                  className="h-4 w-4 rounded-sm"
                />
                <span>Include tool calls (inputs)</span>
                <span className="ml-auto whitespace-nowrap text-xs text-gray-400">{tokenLabel(currentPreview?.counts.include_tool_calls)}</span>
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input
                  type="checkbox"
                  checked={options.includeToolOutputs}
                  onChange={() => toggle("includeToolOutputs")}
                  disabled={busy}
                  className="h-4 w-4 rounded-sm"
                />
                <span>Include tool outputs</span>
                <span className="ml-auto whitespace-nowrap text-xs text-gray-400">{tokenLabel(currentPreview?.counts.include_tool_outputs)}</span>
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input
                  type="checkbox"
                  checked={options.includeSummaries}
                  onChange={() => toggle("includeSummaries")}
                  disabled={busy}
                  className="h-4 w-4 rounded-sm"
                />
                <span>Include summaries</span>
                <span className="ml-auto whitespace-nowrap text-xs text-gray-400">{tokenLabel(currentPreview?.counts.include_summaries)}</span>
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input
                  type="checkbox"
                  checked={options.dbOnly}
                  onChange={() => toggle("dbOnly")}
                  disabled={busy}
                  className="h-4 w-4 rounded-sm"
                />
                <span>Use DB only (ignore transcript)</span>
                <span className="ml-auto whitespace-nowrap text-xs text-gray-400">{currentPreview ? `≈${currentPreview.counts.db_only.toLocaleString("en-US")} tokens total` : "…"}</span>
              </label>
            </div>

            <div className="mt-4 flex items-center justify-between border-t border-border pt-3 text-sm font-semibold text-gray-100" aria-live="polite">
              <span>Selected total</span>
              <span>{currentPreview ? tokenLabel(currentPreview.counts.total) : previewError ? "Unavailable" : "Counting…"}</span>
            </div>
            <p className="mt-1 text-xs text-gray-500">Estimated from the rendered Markdown. Option counts show added text; DB only shows its total.</p>
            {previewError && <div className="mt-2 text-xs text-red-400">Token counts unavailable: {previewError}</div>}

            {status?.type === "error" && <div role="alert" className="mt-4 text-xs text-red-400">{status.text}</div>}
            {canOverwrite && <div role="alert" className="mt-4 break-words text-xs text-yellow-400">{status?.text}</div>}
            {status?.type === "success" && <div className="mt-4 text-xs text-green-400">{status.text}</div>}

            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setIsOpen(false)}
                className="rounded-md border border-border px-3 py-1.5 text-sm text-gray-300 hover:bg-bg-hover"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleCopy}
                disabled={!currentPreview || busy}
                className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-gray-300 hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50"
              >
                <ClipboardCopy className="h-4 w-4" /> {status?.type === "copying" ? "Copying…" : "Copy Markdown"}
              </button>
              <button
                type="button"
                onClick={() => handleExport(canOverwrite)}
                disabled={!validOutput || busy}
                className={`rounded-md px-3 py-1.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50 ${canOverwrite ? "bg-red-600 hover:bg-red-500" : "bg-accent hover:bg-accent-hover"}`}
              >
                {status?.type === "saving" ? "Exporting…" : canOverwrite ? "Overwrite file" : "Export"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
