import { useState, useRef, useEffect, useMemo } from "react";
import { X, Minus, Plus, Sparkles } from "lucide-react";
import type { ModelInfo } from "../types";
import { groupKeyOf, groupNameOf } from "../modelGroups";
import { ModelBadge } from "./ModelTag";

interface ModelGroup {
  key: string;
  name: string;
  models: ModelInfo[];
}

/** Group models by groupKeyOf (so fusion combos land in "Fusion: <lead>"
 * groups), sorted by group name case-insensitively. */
function groupModels(models: ModelInfo[]): ModelGroup[] {
  const map = new Map<string, ModelGroup>();
  for (const m of models) {
    const key = groupKeyOf(m);
    let group = map.get(key);
    if (!group) {
      group = { key, name: groupNameOf(m), models: [] };
      map.set(key, group);
    }
    group.models.push(m);
  }
  return [...map.values()].sort((a, b) =>
    a.name.localeCompare(b.name, undefined, { sensitivity: "base" })
  );
}

interface ModelManagerProps {
  models: ModelInfo[];
  hasNewModels: boolean;
  onExclude: (modelId: string) => void;
  onInclude: (modelId: string) => void;
  onExcludeMany: (modelIds: string[]) => void;
  onIncludeMany: (modelIds: string[]) => void;
  onAcknowledge: (modelId: string) => void;
  onClose: () => void;
}

export function ModelManager({
  models,
  hasNewModels,
  onExclude,
  onInclude,
  onExcludeMany,
  onIncludeMany,
  onAcknowledge,
  onClose,
}: ModelManagerProps) {
  const [filter, setFilter] = useState("");
  const panelRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);

  // Fast (2x cost) variants are never shown.
  const visibleModels = useMemo(() => models.filter((m) => !m.is_fast), [models]);

  const filtered = visibleModels.filter(
    (m) =>
      m.display_name.toLowerCase().includes(filter.toLowerCase()) ||
      m.id.toLowerCase().includes(filter.toLowerCase())
  );

  const activeModels = filtered.filter((m) => !m.excluded);
  const excludedModels = filtered.filter((m) => m.excluded);
  const activeGroups = useMemo(() => groupModels(activeModels), [activeModels]);
  const excludedGroups = useMemo(() => groupModels(excludedModels), [excludedModels]);

  useEffect(() => {
    lastFocusedRef.current = document.activeElement as HTMLElement;
    searchInputRef.current?.focus();

    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;

    const focusable = () =>
      Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )
      ).filter((el) => el.tabIndex >= 0 && !el.hasAttribute("disabled"));

    const trap = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) return;
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (e.shiftKey) {
        if (document.activeElement === first) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", trap);
    return () => document.removeEventListener("keydown", trap);
  }, [filtered]);

  useEffect(() => {
    return () => {
      lastFocusedRef.current?.focus();
    };
  }, []);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="model-manager-title"
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 sm:items-center"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        className="my-4 flex max-h-[calc(100vh-2rem)] w-full max-w-[600px] flex-col rounded-xl border border-border bg-bg-secondary shadow-2xl sm:max-h-[80vh]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <h2 id="model-manager-title" className="text-sm font-semibold text-gray-200">
              Model Manager
            </h2>
            {hasNewModels && (
              <span className="flex items-center gap-1 rounded-sm bg-blue-500/20 px-2 py-0.5 text-[10px] font-semibold text-blue-400">
                <Sparkles className="h-3 w-3" /> New models available
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Close model manager"
            className="rounded-sm p-1 text-gray-500 hover:bg-bg-hover hover:text-gray-300"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Search */}
        <div className="border-b border-border px-5 py-2">
          <input
            ref={searchInputRef}
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search models..."
            className="w-full rounded-md border border-border bg-bg-tertiary px-3 py-1.5 text-xs text-gray-200 placeholder-gray-600 focus:border-accent focus:outline-hidden"
          />
        </div>

        {/* Model list */}
        <div className="flex-1 overflow-y-auto px-3 py-3">
          {/* Active models */}
          {activeModels.length > 0 && (
            <div className="mb-4">
              <div className="mb-2 px-2 text-[10px] font-semibold uppercase tracking-wide text-gray-600">
                Active Models ({activeModels.length})
              </div>
              <div className="space-y-1">
                {activeGroups.map((group) =>
                  group.models.length === 1 ? (
                    <ModelManagerRow
                      key={group.models[0].id}
                      model={group.models[0]}
                      onExclude={() => onExclude(group.models[0].id)}
                      onAcknowledge={() => onAcknowledge(group.models[0].id)}
                    />
                  ) : (
                    <div key={group.key} className="space-y-1">
                      <div className="flex items-center justify-between px-2 pt-2 text-[11px] font-semibold text-gray-400">
                        <span>{group.name}</span>
                        <button
                          onClick={() => onExcludeMany(group.models.map((m) => m.id))}
                          title="Exclude all variants from the model selector"
                          className="flex items-center gap-1 rounded-md bg-red-600/20 px-2 py-0.5 text-[10px] font-medium text-red-400 hover:bg-red-600/30"
                        >
                          <Minus className="h-3 w-3" /> Exclude all
                        </button>
                      </div>
                      {group.models.map((m) => (
                        <ModelManagerRow
                          key={m.id}
                          model={m}
                          onExclude={() => onExclude(m.id)}
                          onAcknowledge={() => onAcknowledge(m.id)}
                        />
                      ))}
                    </div>
                  )
                )}
              </div>
            </div>
          )}

          {/* Excluded models */}
          {excludedModels.length > 0 && (
            <div>
              <div className="mb-2 px-2 text-[10px] font-semibold uppercase tracking-wide text-gray-600">
                Excluded Models ({excludedModels.length})
              </div>
              <div className="space-y-1">
                {excludedGroups.map((group) =>
                  group.models.length === 1 ? (
                    <ModelManagerRow
                      key={group.models[0].id}
                      model={group.models[0]}
                      onInclude={() => onInclude(group.models[0].id)}
                    />
                  ) : (
                    <div key={group.key} className="space-y-1">
                      <div className="flex items-center justify-between px-2 pt-2 text-[11px] font-semibold text-gray-400">
                        <span>{group.name}</span>
                        <button
                          onClick={() => onIncludeMany(group.models.map((m) => m.id))}
                          title="Include all variants in the model selector"
                          className="flex items-center gap-1 rounded-md bg-green-600/20 px-2 py-0.5 text-[10px] font-medium text-green-400 hover:bg-green-600/30"
                        >
                          <Plus className="h-3 w-3" /> Include all
                        </button>
                      </div>
                      {group.models.map((m) => (
                        <ModelManagerRow
                          key={m.id}
                          model={m}
                          onInclude={() => onInclude(m.id)}
                        />
                      ))}
                    </div>
                  )
                )}
              </div>
            </div>
          )}

          {filtered.length === 0 && (
            <div className="py-8 text-center text-xs text-gray-600">
              No models match &quot;{filter}&quot;
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="border-t border-border px-5 py-2.5 text-xs text-gray-600">
          Click the red dash to exclude a model from the selector. Excluded models won&apos;t appear
          in the dropdown but remain available here. Fast (2x cost) variants are hidden.
        </div>
      </div>
    </div>
  );
}

function ModelManagerRow({
  model,
  onExclude,
  onInclude,
  onAcknowledge,
}: {
  model: ModelInfo;
  onExclude?: () => void;
  onInclude?: () => void;
  onAcknowledge?: () => void;
}) {
  return (
    <div
      className={`flex items-center gap-3 rounded-md border border-transparent px-3 py-2.5 ${
        model.excluded ? "bg-bg-tertiary/50 opacity-60" : "bg-bg-tertiary"
      }`}
    >
      {/* Model info */}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-200">{model.display_name}</span>
          <ModelBadge
            model={model}
            onAcknowledge={onAcknowledge}
            showNew={model.is_new && !model.excluded}
            variant="badge"
          />
        </div>
        <div className="mt-0.5 truncate font-mono text-[11px] text-gray-600">{model.id}</div>
        {model.description && (
          <div className="mt-0.5 truncate text-[11px] text-gray-500">{model.description}</div>
        )}
      </div>

      {/* Action button */}
      <div className="shrink-0">
        {model.excluded ? (
          <button
            onClick={onInclude}
            title="Include in model selector"
            className="flex items-center gap-1 rounded-md bg-green-600/20 px-2.5 py-1.5 text-xs font-medium text-green-400 hover:bg-green-600/30"
          >
            <Plus className="h-3.5 w-3.5" /> Include
          </button>
        ) : (
          <button
            onClick={onExclude}
            title="Exclude from model selector"
            className="flex items-center gap-1 rounded-md bg-red-600/20 px-2.5 py-1.5 text-xs font-medium text-red-400 hover:bg-red-600/30"
          >
            <Minus className="h-3.5 w-3.5" /> Exclude
          </button>
        )}
      </div>
    </div>
  );
}
