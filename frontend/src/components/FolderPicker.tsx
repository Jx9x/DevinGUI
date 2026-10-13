import { useState, useRef, useEffect, useCallback } from "react";
import { X, Folder, ChevronRight, ArrowUp, Loader2, Home } from "lucide-react";
import type { BrowseResult } from "../types";
import { api, ApiError } from "../api/client";

interface FolderPickerProps {
  /** Optional starting path. When omitted the backend returns drive roots. */
  initialPath?: string | null;
  onConfirm: (path: string) => void;
  onClose: () => void;
}

export function FolderPicker({ initialPath, onConfirm, onClose }: FolderPickerProps) {
  const [path, setPath] = useState(initialPath ?? "");
  const [result, setResult] = useState<BrowseResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [manualPath, setManualPath] = useState(initialPath ?? "");
  const panelRef = useRef<HTMLDivElement>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);

  const load = useCallback(async (target: string) => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.browse(target || undefined);
      setResult(data);
      setPath(data.path || target);
      setManualPath(data.path || target);
    } catch (e) {
      const message = e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Failed to browse";
      setError(message);
      setResult(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load — start from the roots listing (or initialPath if provided).
  useEffect(() => {
    void load(initialPath ?? "");
  }, [initialPath, load]);

  // Escape to close + restore focus on unmount, mirroring ModelManager.
  useEffect(() => {
    lastFocusedRef.current = document.activeElement as HTMLElement;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
      lastFocusedRef.current?.focus();
    };
  }, [onClose]);

  // Simple focus trap on Tab.
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const trap = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const elements = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )
      ).filter((el) => el.tabIndex >= 0 && !el.hasAttribute("disabled"));
      if (elements.length === 0) return;
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => document.removeEventListener("keydown", trap);
  }, [result]);

  const goUp = () => {
    const parent = result?.parent;
    if (parent) void load(parent);
    else void load("");
  };

  const selectEntry = (entryPath: string) => {
    void load(entryPath);
  };

  const confirm = () => {
    const target = manualPath.trim() || path;
    if (!target) return;
    onConfirm(target);
  };

  const submitManual = (e: React.FormEvent) => {
    e.preventDefault();
    const target = manualPath.trim();
    if (!target) return;
    void load(target);
  };

  const entries = result?.entries ?? [];
  const roots = result?.roots;
  const showRoots = roots && roots.length > 0 && entries.length === 0 && !path;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="folder-picker-title"
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 sm:items-center"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        className="my-4 flex max-h-[calc(100vh-2rem)] w-full max-w-[560px] flex-col rounded-xl border border-border bg-bg-secondary shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <Folder className="h-4 w-4 text-gray-400" />
            <h2 id="folder-picker-title" className="text-sm font-semibold text-gray-200">
              Choose project folder
            </h2>
          </div>
          <button
            onClick={onClose}
            aria-label="Close folder picker"
            className="rounded-sm p-1 text-gray-500 hover:bg-bg-hover hover:text-gray-300"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Current path + up button */}
        <div className="flex items-center gap-1 border-b border-border px-3 py-2">
          <button
            type="button"
            onClick={() => void load("")}
            disabled={loading}
            aria-label="Browse roots"
            title="Browse roots"
            className="rounded-sm p-1 text-gray-400 hover:bg-bg-hover hover:text-gray-200 disabled:opacity-50"
          >
            <Home className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={goUp}
            disabled={loading || (!result?.parent && !showRoots)}
            aria-label="Go up"
            title="Go up"
            className="rounded-sm p-1 text-gray-400 hover:bg-bg-hover hover:text-gray-200 disabled:opacity-50"
          >
            <ArrowUp className="h-3.5 w-3.5" />
          </button>
          <div
            className="min-w-0 flex-1 truncate font-mono text-xs text-gray-400"
            title={path || "Browse roots"}
          >
            {path || "Browse roots"}
          </div>
        </div>

        {/* Manual path entry */}
        <form onSubmit={submitManual} className="flex items-center gap-1 border-b border-border px-3 py-2">
          <input
            type="text"
            value={manualPath}
            onChange={(e) => setManualPath(e.target.value)}
            placeholder="Type or paste a path…"
            className="min-w-0 flex-1 rounded-md border border-border bg-bg-tertiary px-2 py-1 text-xs text-gray-200 placeholder-gray-600 focus:border-accent focus:outline-hidden"
          />
          <button
            type="submit"
            disabled={loading || !manualPath.trim()}
            className="rounded-md border border-border px-2 py-1 text-xs text-gray-300 hover:bg-bg-hover disabled:opacity-50"
          >
            Go
          </button>
        </form>

        {/* Listing */}
        <div className="flex-1 overflow-y-auto px-2 py-2">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-8 text-xs text-gray-500">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
            </div>
          ) : error ? (
            <div className="px-3 py-6 text-center text-xs text-red-400">
              {error}
              <div className="mt-2 text-gray-600">
                You can still type a path above and press Go, then Confirm.
              </div>
            </div>
          ) : showRoots ? (
            <div className="space-y-0.5">
              {roots!.map((root) => (
                <button
                  key={root.path}
                  type="button"
                  onClick={() => selectEntry(root.path)}
                  className="flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-sm text-gray-200 hover:bg-bg-hover"
                >
                  <Folder className="h-3.5 w-3.5 text-gray-500" />
                  <span className="font-mono">{root.name}</span>
                </button>
              ))}
            </div>
          ) : entries.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs text-gray-600">
              No subdirectories here. Use Confirm to choose this folder, or go up.
            </div>
          ) : (
            <div className="space-y-0.5">
              {entries.map((entry) => (
                <button
                  key={entry.path}
                  type="button"
                  onClick={() => selectEntry(entry.path)}
                  className="group flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-sm text-gray-200 hover:bg-bg-hover"
                  title={entry.path}
                >
                  <Folder className="h-3.5 w-3.5 shrink-0 text-gray-500" />
                  <span className="min-w-0 flex-1 truncate">{entry.name}</span>
                  <ChevronRight className="h-3.5 w-3.5 shrink-0 text-gray-600 opacity-0 group-hover:opacity-100" />
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
          <div className="min-w-0 truncate font-mono text-[10px] text-gray-600" title={path}>
            {path || "No folder selected"}
          </div>
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-border px-3 py-1.5 text-xs text-gray-300 hover:bg-bg-hover"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={confirm}
              disabled={!path && !manualPath.trim()}
              className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              Confirm
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
