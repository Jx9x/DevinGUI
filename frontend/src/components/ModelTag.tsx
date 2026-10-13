import { Eye, Sparkles } from "lucide-react";
import type { ModelInfo } from "../types";

export const TAG_COLORS: Record<string, string> = {
  Free: "text-green-400",
  New: "text-blue-400",
  Fast: "text-red-400",
};

export const TAG_BG_COLORS: Record<string, string> = {
  Free: "bg-green-500/10",
  New: "bg-blue-500/10",
  Fast: "bg-red-500/10",
};

/** Cache age (in days) beyond which the "Free" tag may be stale. */
const FREE_STALE_DAYS = 2;
const SECONDS_PER_DAY = 86400;

/**
 * Whether the "Free" tag should be shown as potentially stale, given the
 * model cache's last-updated timestamp (Unix seconds).  Returns false when
 * the cache age is unknown (treated as fresh to avoid false alarms).
 */
export function isFreeTagStale(cacheUpdatedAt: number | null | undefined): boolean {
  if (cacheUpdatedAt == null) return false;
  const ageDays = (Date.now() / 1000 - cacheUpdatedAt) / SECONDS_PER_DAY;
  return ageDays > FREE_STALE_DAYS;
}

/** Effort levels that are already encoded in the model display name. */
const EFFORT_LEVELS = new Set([
  "low",
  "medium",
  "high",
  "max",
  "xhigh",
  "x-high",
]);

/**
 * Normalize a raw tag for compact display.
 * Returns null if the tag should not be shown.
 */
export function normalizeTag(raw: string): string | null {
  const tag = raw.trim();
  if (!tag) return null;

  // Promo is implied by Free.
  if (tag.toLowerCase() === "promo") return null;

  // "Effort: Max" / "Reasoning Effort: Low" → drop entirely (name already has it).
  const effortMatch = /^(?:reasoning\s+)?effort:\s*(.+)$/i.exec(tag);
  if (effortMatch) return null;

  // Bare effort levels (stale registry after partial refresh).
  if (EFFORT_LEVELS.has(tag.toLowerCase())) return null;

  // "200K Context" / "1M Context" → "200K" / "1M"
  const ctxMatch = /^(\d+(?:\.\d+)?)([kKmM])(?:\s*context)?$/i.exec(tag);
  if (ctxMatch) return `${ctxMatch[1]}${ctxMatch[2].toUpperCase()}`;

  return tag;
}

export function tagColor(tag: string): string {
  return TAG_COLORS[tag] || "text-yellow-400";
}

export function tagBgColor(tag: string): string {
  return TAG_BG_COLORS[tag] || "bg-yellow-500/10";
}

/**
 * Format a per-million-token price compactly: integers have no decimal
 * ("5"), other values trim trailing zeros with at most 2 decimals ("0.2",
 * "1.25").  Returns null for non-finite or negative values.
 */
export function formatTokenPrice(value: number | null | undefined): string | null {
  if (value == null || !Number.isFinite(value) || value < 0) return null;
  if (Number.isInteger(value)) return String(value);
  return String(parseFloat(value.toFixed(2)));
}

/**
 * Compact "input/output" price tag (USD per million tokens), e.g. "5/25".
 * Returns null when either price is missing so callers can skip rendering.
 */
export function priceTag(
  inputPrice: number | null | undefined,
  outputPrice: number | null | undefined
): string | null {
  const inFmt = formatTokenPrice(inputPrice);
  const outFmt = formatTokenPrice(outputPrice);
  if (inFmt == null || outFmt == null) return null;
  return `${inFmt}/${outFmt}`;
}

interface ModelBadgeProps {
  model: Pick<
    ModelInfo,
    | "vision"
    | "is_new"
    | "tags"
    | "label_tags"
    | "family"
    | "family_name"
    | "input_price"
    | "output_price"
  >;
  onAcknowledge?: () => void;
  showNew?: boolean;
  variant?: "text" | "badge";
  /** Unix timestamp (seconds) the model cache was last updated.  When the
   * cache is older than 2 days, the "Free" tag is rendered with a stale
   * indicator so users know the promo status may be out of date. */
  cacheUpdatedAt?: number | null;
}

export function ModelBadge({
  model,
  onAcknowledge,
  showNew = model.is_new,
  variant = "badge",
  cacheUpdatedAt,
}: ModelBadgeProps) {
  const isText = variant === "text";
  const freeStale = isFreeTagStale(cacheUpdatedAt);

  const statusTags = model.tags
    .map(normalizeTag)
    .filter((t): t is string => t !== null);
  const metaTags = (model.label_tags ?? [])
    .map(normalizeTag)
    .filter((t): t is string => t !== null)
    // Avoid duplicating a status tag that also appears as a label tag.
    .filter((t) => !statusTags.includes(t));

  // Show a compact "input/output" price tag in the slot the "Free" tag
  // would occupy, but only when the model is not free.  This keeps the tag
  // row aligned across free and paid models (free models show "Free",
  // paid models show "5/25") and surfaces pricing at a glance.
  const hasFreeTag = statusTags.includes("Free");
  const price = hasFreeTag ? null : priceTag(model.input_price, model.output_price);

  return (
    <span className="inline-flex shrink-0 items-center gap-1.5">
      {model.vision && (
        <span title="Vision capable" className="shrink-0">
          <Eye className="h-3.5 w-3.5 text-blue-400" />
        </span>
      )}
      {showNew && onAcknowledge ? (
        <button
          onClick={onAcknowledge}
          title="Click to acknowledge"
          className="flex shrink-0 items-center gap-1 rounded-sm bg-blue-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-blue-400 hover:bg-blue-500/30"
        >
          <Sparkles className="h-2.5 w-2.5" /> NEW
        </button>
      ) : showNew ? (
        <span className="shrink-0 rounded-sm bg-blue-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-blue-400">
          NEW
        </span>
      ) : null}
      {price && (
        <span
          title="USD per million tokens (input / output)"
          className={`shrink-0 text-[10px] font-medium tabular-nums text-gray-400 ${
            isText ? "" : "rounded-sm bg-bg-hover px-1.5 py-0.5"
          }`}
        >
          {price}
        </span>
      )}
      {statusTags.map((tag) => {
        const isStaleFree = tag === "Free" && freeStale;
        const staleTitle =
          isStaleFree && cacheUpdatedAt != null
            ? `Free status may be stale — cache last updated ${new Date(cacheUpdatedAt * 1000).toLocaleDateString()}`
            : undefined;
        return (
          <span
            key={tag}
            title={staleTitle}
            className={`shrink-0 text-[10px] font-semibold ${
              isStaleFree ? "text-yellow-400" : tagColor(tag)
            } ${
              isText
                ? ""
                : `rounded-sm px-1.5 py-0.5 ${
                    isStaleFree ? "bg-yellow-500/10" : tagBgColor(tag)
                  }`
            }`}
          >
            {tag}
            {isStaleFree ? "?" : ""}
          </span>
        );
      })}
      {metaTags.map((tag) => {
        const hasColor = tag in TAG_COLORS;
        const isContext = /^\d+(?:\.\d+)?[KM]$/.test(tag);
        return (
          <span
            key={tag}
            className={`shrink-0 text-[10px] tabular-nums ${
              hasColor ? "font-semibold" : "font-medium"
            } ${hasColor ? tagColor(tag) : "text-gray-500"} ${
              isText
                ? isContext
                  ? "w-8 text-right"
                  : ""
                : `rounded-sm px-1.5 py-0.5 ${hasColor ? tagBgColor(tag) : "bg-bg-hover"}`
            }`}
          >
            {tag}
          </span>
        );
      })}
    </span>
  );
}
