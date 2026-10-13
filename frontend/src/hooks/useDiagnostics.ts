import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { DiagnosticsSnapshot } from "../diagnostics";

export function useDiagnostics() {
  const [snapshot, setSnapshot] = useState<DiagnosticsSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(0);
  const refresh = useCallback(async (force = false) => {
    const id = ++request.current;
    setLoading(true);
    try {
      const data = await api.getDiagnostics(force);
      if (request.current !== id) return;
      setSnapshot(data);
      setError(null);
    } catch (failure) {
      if (request.current === id) setError(failure instanceof Error ? failure.message : "Could not check local setup. Retry checks.");
    } finally {
      if (request.current === id) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void refresh();
    return () => { request.current += 1; };
  }, [refresh]);
  return { snapshot, loading, error, refresh };
}
