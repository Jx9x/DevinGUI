import { useState, useEffect, useCallback, useMemo } from "react";
import { api, ApiError } from "../api/client";
import type { ModelInfo } from "../types";

export interface UseModelsReturn {
  models: ModelInfo[];
  selectableModels: ModelInfo[];
  recentModels: ModelInfo[];
  selectedModel: string | null;
  setSelectedModel: (id: string | null) => void;
  lastUsedVariants: Record<string, string>;
  hasNewModels: boolean;
  cacheUpdatedAt: number | null;
  refresh: () => Promise<void>;
  excludeModel: (id: string) => Promise<void>;
  includeModel: (id: string) => Promise<void>;
  excludeModels: (ids: string[]) => Promise<void>;
  includeModels: (ids: string[]) => Promise<void>;
  acknowledgeModel: (id: string) => Promise<void>;
  error: string | null;
}

export function useModels(): UseModelsReturn {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [rawRecent, setRawRecent] = useState<ModelInfo[]>([]);
  const [lastUsed, setLastUsed] = useState<string | null>(null);
  const [lastUsedVariants, setLastUsedVariants] = useState<Record<string, string>>({});
  const [cacheUpdatedAt, setCacheUpdatedAt] = useState<number | null>(null);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.getModels();
      setModels(data.models);
      setRawRecent(data.recent ?? []);
      setLastUsed(data.last_used ?? null);
      setLastUsedVariants(data.last_used_variants ?? {});
      setCacheUpdatedAt(data.cache_info?.updated_at ?? null);
      setError(null);
    } catch (e) {
      const message =
        e instanceof ApiError
          ? e.message
          : e instanceof Error
            ? e.message
            : "Failed to fetch models";
      console.error(message, e);
      setError(message);
    }
  }, []);

  const selectableModels = useMemo(
    () => models.filter((m) => m.available && !m.excluded),
    [models]
  );

  const recentModels = useMemo(() => {
    const selectableIds = new Set(selectableModels.map((m) => m.id));
    return rawRecent.filter((m) => selectableIds.has(m.id));
  }, [rawRecent, selectableModels]);

  const hasNewModels = useMemo(() => models.some((m) => m.is_new && !m.excluded), [models]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (selectedModel && selectableModels.some((m) => m.id === selectedModel)) {
      return;
    }
    const fallback =
      [lastUsed, recentModels[0]?.id, selectableModels[0]?.id].find(
        (id): id is string =>
          typeof id === "string" && selectableModels.some((m) => m.id === id)
      ) ?? null;
    setSelectedModel(fallback);
  }, [selectableModels, recentModels, lastUsed, selectedModel]);

  const modelAction = useCallback(
    async (id: string, action: "exclude" | "include" | "acknowledge") => {
      try {
        if (action === "exclude") {
          await api.excludeModel(id);
        } else if (action === "include") {
          await api.includeModel(id);
        } else {
          await api.acknowledgeModel(id);
        }
        await refresh();
      } catch (e) {
        const message =
          e instanceof ApiError
            ? e.message
            : e instanceof Error
              ? e.message
              : `Failed to ${action} model`;
        console.error(message, e);
        setError(message);
      }
    },
    [refresh]
  );

  const excludeModel = useCallback((id: string) => modelAction(id, "exclude"), [modelAction]);
  const includeModel = useCallback((id: string) => modelAction(id, "include"), [modelAction]);
  const acknowledgeModel = useCallback(
    (id: string) => modelAction(id, "acknowledge"),
    [modelAction]
  );

  const bulkModelAction = useCallback(
    async (ids: string[], action: "exclude" | "include") => {
      try {
        if (action === "exclude") {
          await api.excludeModels(ids);
        } else {
          await api.includeModels(ids);
        }
        await refresh();
      } catch (e) {
        const message =
          e instanceof ApiError
            ? e.message
            : e instanceof Error
              ? e.message
              : `Failed to ${action} models`;
        console.error(message, e);
        setError(message);
      }
    },
    [refresh]
  );

  const excludeModels = useCallback(
    (ids: string[]) => bulkModelAction(ids, "exclude"),
    [bulkModelAction]
  );
  const includeModels = useCallback(
    (ids: string[]) => bulkModelAction(ids, "include"),
    [bulkModelAction]
  );

  return {
    models,
    selectableModels,
    recentModels,
    selectedModel,
    setSelectedModel,
    lastUsedVariants,
    hasNewModels,
    cacheUpdatedAt,
    refresh,
    excludeModel,
    includeModel,
    excludeModels,
    includeModels,
    acknowledgeModel,
    error,
  };
}
