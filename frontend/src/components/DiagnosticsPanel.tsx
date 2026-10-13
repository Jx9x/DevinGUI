import { useEffect, useRef, useState } from "react";
import type { DiagnosticsSnapshot } from "../diagnostics";
import type { IntegrationSettings } from "../integrationSettings";

const integrations = [
  { key: "devinUsage", diagnostic: "devin_usage", label: "Devin Desktop quota", requirement: "Requires a separate Devin Desktop sign-in." },
  { key: "codexUsage", diagnostic: "codex_usage", label: "Codex / ChatGPT allowance", requirement: "Requires Codex and a suitable ChatGPT login, not an API-key-only account." },
  { key: "chatgpt", diagnostic: "chatgpt", label: "Chat On Steroids conversations", requirement: "Requires the custom companion application, not included in this project." },
] as const;
const button = "rounded-md border border-border px-3 py-1.5 text-xs text-gray-300 hover:bg-bg-hover disabled:opacity-40";

interface Props {
  snapshot: DiagnosticsSnapshot | null;
  loading: boolean;
  error: string | null;
  settings: IntegrationSettings;
  onChange: (settings: IntegrationSettings) => boolean;
  onRetry: () => void;
  onClose: () => void;
}

export function DiagnosticsPanel({ snapshot, loading, error, settings, onChange, onRetry, onClose }: Props) {
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const [saved, setSaved] = useState(true);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    close.current?.focus();
    return () => previous?.focus();
  }, []);
  return (
    <div role="dialog" aria-modal="true" aria-labelledby="diagnostics-title" className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 p-4" onClick={onClose}
      onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); onClose(); }
        if (event.key !== "Tab") return;
        const elements = Array.from(panel.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input") ?? []);
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
      <div ref={panel} className="flex max-h-[90vh] w-full max-w-3xl flex-col rounded-xl border border-border bg-bg-secondary p-5 shadow-2xl" onClick={event => event.stopPropagation()}>
        <div className="flex items-center justify-between gap-3">
          <h2 id="diagnostics-title" className="text-base font-semibold">Diagnostics & optional integrations</h2>
          <button ref={close} onClick={onClose} className={button} aria-label="Close diagnostics">Close</button>
        </div>
        <div className="mt-4 space-y-4 overflow-y-auto">
          <p className="text-sm text-gray-400">This GUI targets the current official stable Devin CLI. Storage-related limitations do not automatically disable working live chat. Checks never read conversation content or expose credentials.</p>
          {error && <p role="alert" className="text-sm text-amber-300">{error}</p>}
          {!snapshot && !error && <p role="status" className="text-sm text-gray-400">Checking local setup…</p>}
          {snapshot && <>
            <p className="text-xs text-gray-400">Tested CLI: <strong>{snapshot.cli.tested_version}</strong> · Installed: <strong>{snapshot.cli.version ?? "Unknown"}</strong></p>
            <ul className="space-y-2" aria-label="Local setup checks">
              {snapshot.checks.map(check => <li key={check.id} className="rounded-lg border border-border bg-bg-primary p-3 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2"><strong className="text-gray-200">{check.label}</strong><span className={check.status === "ok" ? "text-emerald-400" : check.status === "error" ? "text-red-300" : "text-amber-300"}>{check.status.replace(/_/g, " ")}</span></div>
                {check.path && <p className="mt-1 break-all font-mono text-gray-500">{check.path}</p>}
                <p className="mt-1 leading-relaxed text-gray-400">{check.message}</p>
              </li>)}
            </ul>
          </>}
          <section aria-labelledby="optional-integrations-title" className="space-y-3 border-t border-border pt-4">
            <h3 id="optional-integrations-title" className="text-sm font-semibold">Optional integrations</h3>
            <p className="text-xs text-gray-400">All three are off for a fresh browser. These choices are saved in this browser at this address. Disabled integrations do not poll or show unavailable placeholders.</p>
            {integrations.map(item => {
              const check = snapshot?.optional_integrations[item.diagnostic];
              return <div key={item.key} className="rounded-lg border border-border p-3">
                <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={settings[item.key]} onChange={event => setSaved(onChange({ ...settings, [item.key]: event.target.checked }))} />Enable {item.label}</label>
                <p className="mt-1 text-xs text-gray-500">{settings[item.key] ? `Enabled · ${check?.status.replace(/_/g, " ") ?? "Not checked"}` : "Disabled"}</p>
                <p className="mt-1 text-xs leading-relaxed text-gray-400">{check?.message ?? item.requirement}</p>
              </div>;
            })}
            {!saved && <p role="status" className="text-xs text-amber-300">Browser storage is unavailable. Your choices apply only to this tab.</p>}
          </section>
        </div>
        <div className="mt-4 flex justify-end"><button className={button} disabled={loading} onClick={onRetry}>{loading ? "Checking…" : "Retry checks"}</button></div>
      </div>
    </div>
  );
}
