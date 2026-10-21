import { useState, useRef, useEffect, useId, useMemo } from "react";
import { ChevronDown, Eye, Settings2, Sparkles } from "lucide-react";
import type { ModelInfo } from "../types";
import {
  buildModelGroups,
  defaultFusionId,
  defaultVariantId,
  fusionSelectionOf,
  isModelVisible,
  resolveFusion,
  type BaseGroup,
  type FusionGroup,
  type FusionSelection,
} from "../modelGroups";
import { ModelBadge } from "./ModelTag";

interface ModelSelectorProps {
  models: ModelInfo[];
  selectedModel: string | null;
  lastUsedVariants: Record<string, string>;
  hasNewModels: boolean;
  cacheUpdatedAt?: number | null;
  onSelect: (modelId: string) => void;
  onOpenManager: () => void;
}

const SELECT_CLASS =
  "h-6 w-full shrink-0 rounded-sm border border-border bg-bg-tertiary px-1 text-xs text-gray-200 focus:border-accent focus:outline-hidden";

const ROW_BASE_CLASS = "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors";
const ROW_SELECTED_CLASS = "border border-accent/30 bg-accent/15";
const ROW_IDLE_CLASS = "border border-transparent hover:bg-bg-hover";

const SECTION_HEADER_CLASS =
  "px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-600";

/**
 * One row per base model family: the name selects the current variant, an
 * inline dropdown switches reasoning level (or other variant label), and the
 * badge reflects the currently-resolved variant.
 */
function BaseModelRow({
  group,
  selectedModel,
  lastUsedVariants,
  cacheUpdatedAt,
  onSelect,
  onClose,
}: {
  group: BaseGroup;
  selectedModel: string | null;
  lastUsedVariants: Record<string, string>;
  cacheUpdatedAt?: number | null;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const isSelected = group.variants.some((v) => v.id === selectedModel);
  const currentVariant = isSelected
    ? group.variants.find((v) => v.id === selectedModel)!
    : (group.variants.find((v) => v.id === defaultVariantId(group, lastUsedVariants)) ??
      group.variants[0]);
  return (
    <div className={`${ROW_BASE_CLASS} ${isSelected ? ROW_SELECTED_CLASS : ROW_IDLE_CLASS}`}>
      <button
        data-row-button
        title={group.name}
        className="min-w-0 flex-1 truncate text-left font-medium text-gray-200"
        onClick={() => {
          onSelect(currentVariant.id);
          onClose();
        }}
      >
        {group.name}
      </button>
      {group.variants.length > 1 ? (
        <span className="w-36 shrink-0">
          <select
            aria-label={`${group.name} reasoning`}
            className={SELECT_CLASS}
            value={currentVariant.id}
            onChange={(e) => onSelect(e.target.value)}
          >
            {group.variants.map((v) => (
              <option key={v.id} value={v.id}>
                {v.label}
              </option>
            ))}
          </select>
        </span>
      ) : (
        <span className="w-24 shrink-0" aria-hidden="true" />
      )}
      <span className="flex w-36 shrink-0 items-center justify-end">
        <ModelBadge model={currentVariant.model} variant="text" cacheUpdatedAt={cacheUpdatedAt} />
      </span>
    </div>
  );
}

/**
 * One row per fusion lead model: name + lead-level, sidekick-model and
 * sidekick-level dropdowns.  Each change resolves to a concrete combo id.
 */
function FusionModelRow({
  group,
  selectedModel,
  lastUsedVariants,
  cacheUpdatedAt,
  onSelect,
  onClose,
}: {
  group: FusionGroup;
  selectedModel: string | null;
  lastUsedVariants: Record<string, string>;
  cacheUpdatedAt?: number | null;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const isSelected = group.combos.some((c) => c.id === selectedModel);
  const currentComboId = isSelected ? selectedModel! : defaultFusionId(group, lastUsedVariants);
  const currentCombo = group.combos.find((c) => c.id === currentComboId) ?? group.combos[0];
  const sel = fusionSelectionOf(currentCombo);
  const sidekick =
    group.sidekicks.find((s) => s.family === sel.sidekickFamily) ?? group.sidekicks[0];

  const apply = (patch: Partial<FusionSelection>) => {
    onSelect(resolveFusion(group, { ...sel, ...patch }, lastUsedVariants));
  };

  return (
    <div className={`${ROW_BASE_CLASS} ${isSelected ? ROW_SELECTED_CLASS : ROW_IDLE_CLASS}`}>
      <button
        data-row-button
        title={group.name}
        className="min-w-0 flex-1 truncate text-left font-medium text-gray-200"
        onClick={() => {
          onSelect(currentCombo.id);
          onClose();
        }}
      >
        {group.name}
      </button>
      {group.leadLevels.length > 0 ? (
        <span className="w-24 shrink-0">
          <select
            aria-label={`${group.name} reasoning`}
            className={SELECT_CLASS}
            value={sel.leadLevel}
            onChange={(e) => apply({ leadLevel: e.target.value })}
          >
            {group.leadLevels.map((l) => (
              <option key={l.key} value={l.key}>
                {l.label}
              </option>
            ))}
          </select>
        </span>
      ) : (
        <span className="w-24 shrink-0" aria-hidden="true" />
      )}
      {group.sidekicks.length > 1 && sidekick ? (
        <span className="w-36 shrink-0">
          <select
            aria-label={`${group.name} sidekick model`}
            className={SELECT_CLASS}
            value={sidekick.family}
            onChange={(e) => apply({ sidekickFamily: e.target.value, sidekickLevel: "" })}
          >
            {group.sidekicks.map((s) => (
              <option key={s.family} value={s.family}>
                {s.name}
              </option>
            ))}
          </select>
        </span>
      ) : (
        <span className="w-36 shrink-0" aria-hidden="true" />
      )}
      {sidekick && sidekick.levels.length > 1 ? (
        <span className="w-24 shrink-0">
          <select
            aria-label={`${group.name} sidekick reasoning`}
            className={SELECT_CLASS}
            value={sel.sidekickLevel}
            onChange={(e) => apply({ sidekickLevel: e.target.value })}
          >
            {sidekick.levels.map((l) => (
              <option key={l.key} value={l.key}>
                {l.label}
              </option>
            ))}
          </select>
        </span>
      ) : (
        <span className="w-24 shrink-0" aria-hidden="true" />
      )}
      <span className="flex w-36 shrink-0 items-center justify-end">
        <ModelBadge model={currentCombo} variant="text" cacheUpdatedAt={cacheUpdatedAt} />
      </span>
    </div>
  );
}

export function ModelSelector({
  models,
  selectedModel,
  lastUsedVariants,
  hasNewModels,
  cacheUpdatedAt,
  onSelect,
  onOpenManager,
}: ModelSelectorProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const focusFirstRowRef = useRef(false);
  const triggerId = useId();

  // Look up the selected model in the unfiltered list so a fast/legacy
  // selection still displays its name on the trigger.
  const selected = models.find((m) => m.id === selectedModel);

  const { base, fusion } = useMemo(() => buildModelGroups(models.filter(isModelVisible)), [models]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  // Keyboard-opened panels focus the first row button once rendered.
  useEffect(() => {
    if (open && focusFirstRowRef.current) {
      focusFirstRowRef.current = false;
      ref.current?.querySelector<HTMLElement>("[data-row-button]")?.focus();
    }
  }, [open]);

  const focusFirstRow = () => {
    ref.current?.querySelector<HTMLElement>("[data-row-button]")?.focus();
  };

  const handleTriggerKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!open) {
        focusFirstRowRef.current = true;
        setOpen(true);
      } else {
        focusFirstRow();
      }
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  const handlePanelKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    // Only move between row buttons — never hijack arrows inside selects.
    const target = e.target as HTMLElement;
    if (!target.hasAttribute("data-row-button")) return;
    const buttons = Array.from(
      (e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>("[data-row-button]")
    );
    const index = buttons.indexOf(target);
    if (index < 0) return;
    e.preventDefault();
    const next =
      e.key === "ArrowDown"
        ? (index + 1) % buttons.length
        : (index - 1 + buttons.length) % buttons.length;
    buttons[next]?.focus();
  };

  const close = () => setOpen(false);

  return (
    <div ref={ref} className="relative">
      <button
        ref={triggerRef}
        id={triggerId}
        onClick={() => setOpen(!open)}
        onKeyDown={handleTriggerKeyDown}
        aria-haspopup="true"
        aria-expanded={open}
        className="flex items-center gap-2 rounded-md border border-border bg-bg-tertiary px-3 py-1.5 text-xs font-medium text-gray-200 hover:bg-bg-hover"
      >
        <span
          className="max-w-[260px] truncate"
          title={selected?.display_name || selectedModel || undefined}
        >
          {selected?.display_name || selectedModel || "Select model"}
        </span>
        {selected?.vision && <Eye className="h-3 w-3 text-blue-400" />}
        {hasNewModels && <Sparkles className="h-3 w-3 text-blue-400" />}
        <ChevronDown className="h-3 w-3 text-gray-500" />
      </button>

      {open && (
        <div
          aria-labelledby={triggerId}
          onKeyDown={handlePanelKeyDown}
          className="absolute right-0 top-full z-50 mt-1 max-h-96 w-[40rem] overflow-y-auto rounded-lg border border-border bg-bg-secondary shadow-xl"
        >
          {/* Base models */}
          <div>
            <div className={SECTION_HEADER_CLASS}>Models</div>
            <div className="space-y-0.5 px-1.5 pb-1.5">
              {base.length === 0 && fusion.length === 0 && (
                <div className="px-3 py-4 text-center text-xs text-gray-600">
                  No models available
                </div>
              )}
              {base.map((group) => (
                <BaseModelRow
                  key={group.key}
                  group={group}
                  selectedModel={selectedModel}
                  lastUsedVariants={lastUsedVariants}
                  cacheUpdatedAt={cacheUpdatedAt}
                  onSelect={onSelect}
                  onClose={close}
                />
              ))}
            </div>
          </div>

          {/* Fusion combos */}
          {fusion.length > 0 && (
            <div className="border-t border-border">
              <div className={SECTION_HEADER_CLASS}>Fusion</div>
              <div className="space-y-0.5 px-1.5 pb-1.5">
                {fusion.map((group) => (
                  <FusionModelRow
                    key={group.key}
                    group={group}
                    selectedModel={selectedModel}
                    lastUsedVariants={lastUsedVariants}
                    cacheUpdatedAt={cacheUpdatedAt}
                    onSelect={onSelect}
                    onClose={close}
                  />
                ))}
              </div>
            </div>
          )}

          {/* Footer with manage button */}
          <div className="border-t border-border px-1.5 py-1.5">
            <button
              onClick={() => {
                onOpenManager();
                setOpen(false);
              }}
              className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-xs text-gray-400 hover:bg-bg-hover hover:text-gray-200"
            >
              <Settings2 className="h-3.5 w-3.5" />
              Manage Models
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
