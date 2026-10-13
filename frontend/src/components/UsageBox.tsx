import { useEffect, useState } from "react";
import { ExternalLink, RefreshCw, Gauge } from "lucide-react";
import type { CodexUsageSnapshot, CodexUsageWindow, UsageSnapshot } from "../types";

interface UsageBoxProps {
  snapshot: UsageSnapshot | null;
  loading: boolean;
  codexSnapshot?: CodexUsageSnapshot | null;
  codexLoading?: boolean;
  devinEnabled?: boolean;
  codexEnabled?: boolean;
  onRefresh: () => void;
}

/** Color a used-percent value green/yellow/red based on thresholds. */
export function usageColorClass(percent: number | null | undefined): string {
  if (percent == null) return "text-gray-500";
  if (percent >= 90) return "text-red-400";
  if (percent >= 70) return "text-yellow-300";
  return "text-green-400";
}

/**
 * Format the time until a Unix-seconds reset as a compact string.
 * Returns "35hr until reset" for sub-48h windows, "2d 10h until reset" for
 * longer ones, "Expired" when the reset is in the past, and "" when unknown.
 */
export function formatResetIn(resetUnix: number | null | undefined, nowMs: number = Date.now()): string {
  if (resetUnix == null) return "";
  const diffMs = resetUnix * 1000 - nowMs;
  if (diffMs <= 0) return "Expired";
  const totalMinutes = Math.floor(diffMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours >= 48) {
    const days = Math.floor(hours / 24);
    const remHours = hours % 24;
    return `${days}d ${remHours}h until reset`;
  }
  if (hours >= 1) return `${hours}hr until reset`;
  return `${minutes}m until reset`;
}

export function formatCompactReset(resetUnix: number | null | undefined, nowMs = Date.now()): string {
  if (resetUnix == null) return "";
  const minutes = Math.ceil((resetUnix * 1000 - nowMs) / 60_000);
  if (minutes <= 0) return "Reset pending";
  const hours = Math.floor(minutes / 60);
  if (hours >= 48) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  return `${minutes}m`;
}

export function quotaWindowLabel(minutes: number | null | undefined, fallback: string): string {
  if (minutes == null) return fallback;
  if (minutes === 10_080) return "Weekly";
  if (minutes === 1_440) return "Daily";
  if (minutes % 60 === 0) return `${minutes / 60}-hour`;
  return `${minutes}-min`;
}

function CodexQuotaRow({ window, fallback, now, stale }: {
  window: CodexUsageWindow | null; fallback: string; now: number; stale: boolean;
}) {
  const label = quotaWindowLabel(window?.window_minutes, fallback);
  const used = window?.used_percent;
  const reset = formatCompactReset(window?.reset_unix, now);
  return (
    <div className="font-mono leading-tight">
      <div className="flex items-baseline justify-between gap-2" aria-label={`${label} usage`}>
        <span className={stale ? "text-gray-500" : usageColorClass(used)}>{used != null ? `${Math.round(used)}%` : "—"}</span>
        <span className="text-gray-400">{label}</span>
      </div>
      {reset && (
        <div className="text-[10px] text-gray-500" title={`Resets ${new Date(window!.reset_unix! * 1000).toLocaleString()}`}>
          {reset === "Reset pending" ? reset : `Reset in ${reset}`}
        </div>
      )}
    </div>
  );
}

export function UsageBox({ snapshot, loading, codexSnapshot = null, codexLoading = false, devinEnabled = true, codexEnabled = true, onRefresh }: UsageBoxProps) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!devinEnabled && !codexEnabled) return;
    const interval = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(interval);
  }, [devinEnabled, codexEnabled]);
  if (!devinEnabled && !codexEnabled) return null;
  const busy = (devinEnabled && loading) || (codexEnabled && codexLoading);
  const body = (() => {
    if (loading && !snapshot) {
      return (
        <div className="flex items-center gap-1.5 text-gray-600">
          <RefreshCw className="h-3 w-3 animate-spin" />
          <span>Loading…</span>
        </div>
      );
    }
    if (!snapshot) return <span className="text-gray-700">Usage unavailable</span>;
    if (snapshot.error) {
      return (
        <span className="text-gray-600" title={snapshot.error}>
          Usage unavailable
        </span>
      );
    }
    const daily = snapshot.daily_used_percent;
    const weekly = snapshot.weekly_used_percent;
    const reset = formatResetIn(snapshot.weekly_reset_unix, now);
    return (
      <div className="flex flex-col gap-0.5 font-mono leading-tight">
        <div className="flex items-baseline justify-between gap-2">
          <span className={usageColorClass(daily)}>{daily != null ? `${Math.round(daily)}%` : "—"}</span>
          <span className="text-gray-400">Daily</span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className={usageColorClass(weekly)}>{weekly != null ? `${Math.round(weekly)}%` : "—"}</span>
          <span className="text-gray-400">Weekly</span>
        </div>
        {reset && <span className="text-[10px] text-gray-500">{reset}</span>}
      </div>
    );
  })();

  return (
    <div className={`ml-auto hidden shrink-0 gap-2.5 rounded-lg border border-border bg-bg-tertiary px-2.5 py-2 text-xs sm:grid ${devinEnabled && codexEnabled ? "w-[276px] grid-cols-2" : "w-[150px] grid-cols-1"}`} aria-label="Account usage">
      {devinEnabled && <section aria-label="Devin usage" className={`flex min-w-0 flex-col gap-1 ${codexEnabled ? "border-r border-border pr-2.5" : ""}`}>
        <div className="flex items-center gap-1 text-gray-400">
          <Gauge className="h-3 w-3 shrink-0" />
          <span>Devin</span>
          {snapshot?.plan_name && !snapshot.error && <span className="truncate text-[10px] text-gray-500" title={snapshot.plan_name}>{snapshot.plan_name}</span>}
          {!codexEnabled && <button onClick={onRefresh} aria-label="Refresh usage" title="Refresh Devin usage" disabled={busy} className="ml-auto rounded-sm p-0.5 text-gray-500 hover:text-gray-300 disabled:opacity-40"><RefreshCw className={`h-3 w-3 ${busy ? "animate-spin" : ""}`} /></button>}
        </div>
        {body}
      </section>}
      {codexEnabled && <section aria-label="Codex / ChatGPT usage" className="flex min-w-0 flex-col gap-1">
        <div className="flex items-center justify-between gap-1">
          <span className="whitespace-nowrap text-[11px] text-gray-400" title={[
            "Account-wide ChatGPT allowance shared with Codex and connected apps.",
            codexSnapshot?.account_email, codexSnapshot?.plan_name,
          ].filter(Boolean).join(" · ")}>Codex / ChatGPT</span>
          <button
            onClick={onRefresh}
            aria-label="Refresh usage"
            title="Refresh Devin and ChatGPT usage"
            disabled={busy}
            className="shrink-0 rounded-sm p-0.5 text-gray-500 hover:bg-bg-hover hover:text-gray-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <RefreshCw className={`h-3 w-3 ${busy ? "animate-spin" : ""}`} />
          </button>
        </div>
        {codexLoading && !codexSnapshot ? (
          <span className="text-gray-500">Loading…</span>
        ) : !codexSnapshot || (codexSnapshot.error && !codexSnapshot.stale) ? (
          <span className="text-gray-500" title={codexSnapshot?.error ?? "ChatGPT usage unavailable"}>Usage unavailable</span>
        ) : (
          <>
            <CodexQuotaRow window={codexSnapshot.primary} fallback="5-hour" now={now} stale={codexSnapshot.stale} />
            <CodexQuotaRow window={codexSnapshot.secondary} fallback="Weekly" now={now} stale={codexSnapshot.stale} />
            {codexSnapshot.stale && <span className="text-[10px] text-yellow-300" title={`${codexSnapshot.error} Last updated ${new Date(codexSnapshot.fetched_at * 1000).toLocaleString()}`}>Last known usage</span>}
          </>
        )}
        <a href="https://chatgpt.com/settings/usage" target="_blank" rel="noopener noreferrer"
          title="Manage Devin's access to your ChatGPT allowance" className="mt-auto flex items-center gap-1 pt-0.5 text-[10px] text-gray-500 hover:text-gray-300">
          Manage sharing <ExternalLink className="h-2.5 w-2.5" />
        </a>
      </section>}
    </div>
  );
}
