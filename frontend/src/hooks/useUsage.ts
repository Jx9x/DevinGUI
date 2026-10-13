import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { DEFAULT_INTEGRATIONS, type IntegrationSettings } from "../integrationSettings";
import type { CodexUsageSnapshot, UsageSnapshot } from "../types";

const readDevin = () => api.getUsage();
const readCodex = (force: boolean) => api.getCodexUsage(force);
const recoverDevin = (previous: UsageSnapshot | null): UsageSnapshot => previous ?? {
  daily_used_percent: null, weekly_used_percent: null, weekly_reset_unix: null,
  plan_name: null, fetched_at: Date.now() / 1000, error: "Could not refresh Devin usage. Try refreshing again.",
};
const recoverCodex = (previous: CodexUsageSnapshot | null): CodexUsageSnapshot => ({
  primary: null, secondary: null, plan_name: null, account_email: null, fetched_at: Date.now() / 1000,
  ...previous, error: "Could not refresh ChatGPT usage. Try refreshing again.",
  stale: !!(previous?.primary || previous?.secondary),
});

function useProvider<T>(enabled: boolean, read: (force: boolean) => Promise<T>, recover: (previous: T | null) => T) {
  const [snapshot, setSnapshot] = useState<T | null>(null);
  const [loading, setLoading] = useState(enabled);
  const pending = useRef(false);
  const generation = useRef(0);
  const refresh = useCallback(async (force = true) => {
    if (!enabled || pending.current) return;
    pending.current = true;
    const id = ++generation.current;
    setLoading(true);
    try {
      const data = await read(force);
      if (generation.current === id) setSnapshot(data);
    } catch {
      if (generation.current === id) setSnapshot(recover);
    } finally {
      if (generation.current === id) {
        pending.current = false;
        setLoading(false);
      }
    }
  }, [enabled, read, recover]);
  useEffect(() => {
    if (!enabled) {
      setSnapshot(null);
      setLoading(false);
      return;
    }
    void refresh(false);
    const interval = window.setInterval(() => { if (!document.hidden) void refresh(false); }, 60_000);
    const visible = () => { if (!document.hidden) void refresh(false); };
    document.addEventListener("visibilitychange", visible);
    return () => {
      generation.current += 1;
      pending.current = false;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [enabled, refresh]);
  return { snapshot: enabled ? snapshot : null, loading: enabled && loading, refresh };
}

/** Fetch each provider independently so a slow or unavailable service does not block the other. */
export function useUsage(settings: Pick<IntegrationSettings, "devinUsage" | "codexUsage"> = DEFAULT_INTEGRATIONS) {
  const devin = useProvider(settings.devinUsage, readDevin, recoverDevin);
  const codex = useProvider(settings.codexUsage, readCodex, recoverCodex);
  const { refresh: refreshDevin } = devin;
  const { refresh: refreshCodex } = codex;
  const refresh = useCallback(async (force = true) => {
    await Promise.allSettled([refreshDevin(force), refreshCodex(force)]);
  }, [refreshDevin, refreshCodex]);
  return { snapshot: devin.snapshot, loading: devin.loading, codexSnapshot: codex.snapshot,
    codexLoading: codex.loading, refresh };
}
