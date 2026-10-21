// Grouping/selection logic for the model picker — one row per base model
// (with a reasoning-level dropdown) plus fusion rows (lead level, sidekick
// model, sidekick level).  Pure functions: no React, so they are unit-tested
// directly. Callers pass selectable models allowed by the visibility policy.

import type { ModelInfo } from "./types";

export const REASONING_ORDER = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const;

export const REASONING_LABELS: Record<string, string> = {
  none: "None",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "XHigh",
  max: "Max",
};

export const DEFAULT_REASONING = "high";

export interface LevelOption {
  key: string; // reasoning key ("" when null)
  label: string;
}

export interface VariantOption {
  id: string;
  label: string;
  model: ModelInfo;
}

export interface BaseGroup {
  kind: "base";
  key: string;
  name: string;
  variants: VariantOption[];
}

export interface SidekickOption {
  family: string;
  name: string;
  levels: LevelOption[];
}

export interface FusionGroup {
  kind: "fusion";
  key: string;
  name: string;
  leadFamily: string;
  leadLevels: LevelOption[];
  sidekicks: SidekickOption[];
  combos: ModelInfo[];
}

export type ModelGroup = BaseGroup | FusionGroup;

/** Keep Fast variants hidden, except standalone SWE-2 Lightning. */
export function isModelVisible(model: ModelInfo): boolean {
  return (
    !model.is_fast ||
    (!model.fusion &&
      model.family === "swe-2" &&
      model.id.startsWith("swe-2-") &&
      model.id.endsWith("-lightning"))
  );
}

/** Group key for any model: `fusion:<lead_family>` for fusion combos, else
 * `family ?? id`. */
export function groupKeyOf(m: ModelInfo): string {
  return m.fusion ? `fusion:${m.fusion.lead_family}` : (m.family ?? m.id);
}

/** Display name for the group: `Fusion: <lead_name>` for fusion (used by the
 * manager), else `family_name ?? display_name`. */
export function groupNameOf(m: ModelInfo): string {
  return m.fusion ? `Fusion: ${m.fusion.lead_name}` : (m.family_name ?? m.display_name);
}

export function reasoningLabel(key: string | null | undefined): string {
  const k = key ?? "";
  return REASONING_LABELS[k] ?? (k ? k.charAt(0).toUpperCase() + k.slice(1) : "Default");
}

/** Sort by REASONING_ORDER index; unknown/null keys sort last. */
export function compareReasoning(
  a: string | null | undefined,
  b: string | null | undefined
): number {
  const index = (k: string | null | undefined): number => {
    if (!k) return REASONING_ORDER.length;
    const i = (REASONING_ORDER as readonly string[]).indexOf(k);
    return i < 0 ? REASONING_ORDER.length : i;
  };
  return index(a) - index(b);
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Distinct level keys (`""` for null) sorted by reasoning order, labeled. */
function distinctLevels(keys: (string | null | undefined)[]): LevelOption[] {
  const unique = [...new Set(keys.map((k) => k ?? ""))];
  unique.sort(compareReasoning);
  return unique.map((k) => ({ key: k, label: reasoningLabel(k) }));
}

/** Variant label when reasoning keys can't distinguish the variants (e.g.
 * Opus 4.6's 1M/Thinking variants all being "high"): the display name with
 * the family-name prefix stripped, or "Default" when nothing remains. */
function variantLabel(m: ModelInfo): string {
  const familyName = m.family_name ?? "";
  const stripped = familyName
    ? m.display_name.replace(new RegExp(`^${escapeRegExp(familyName)}`, "i"), "").trim()
    : m.display_name.trim();
  return stripped || "Default";
}

/** Build groups from models already filtered for availability and visibility. */
export function buildModelGroups(models: ModelInfo[]): {
  base: BaseGroup[];
  fusion: FusionGroup[];
} {
  const baseMap = new Map<string, ModelInfo[]>();
  const fusionMap = new Map<string, ModelInfo[]>();
  for (const m of models) {
    const map = m.fusion ? fusionMap : baseMap;
    const key = groupKeyOf(m);
    const list = map.get(key);
    if (list) {
      list.push(m);
    } else {
      map.set(key, [m]);
    }
  }

  const base: BaseGroup[] = [];
  for (const [key, members] of baseMap) {
    const sorted = [...members].sort(
      (a, b) =>
        compareReasoning(a.reasoning, b.reasoning) || a.display_name.localeCompare(b.display_name)
    );
    // Reasoning labels only work when they distinguish every variant.
    const distinctReasoning =
      sorted.every((m) => m.reasoning != null) &&
      new Set(sorted.map((m) => m.reasoning)).size === sorted.length;
    const variants: VariantOption[] = sorted.map((m) => ({
      id: m.id,
      label: distinctReasoning ? reasoningLabel(m.reasoning) : variantLabel(m),
      model: m,
    }));
    base.push({ kind: "base", key, name: groupNameOf(members[0]), variants });
  }
  base.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));

  const fusion: FusionGroup[] = [];
  for (const [key, members] of fusionMap) {
    const first = members[0].fusion;
    if (!first) continue;
    const bySidekick = new Map<string, { name: string; levels: (string | null)[] }>();
    for (const m of members) {
      const f = m.fusion;
      if (!f) continue;
      let sk = bySidekick.get(f.sidekick_family);
      if (!sk) {
        sk = { name: f.sidekick_name, levels: [] };
        bySidekick.set(f.sidekick_family, sk);
      }
      sk.levels.push(f.sidekick_reasoning);
    }
    const sidekicks: SidekickOption[] = [...bySidekick.entries()]
      .map(([family, s]) => ({ family, name: s.name, levels: distinctLevels(s.levels) }))
      .sort((a, b) => a.name.localeCompare(b.name));
    fusion.push({
      kind: "fusion",
      key,
      name: first.lead_name,
      leadFamily: first.lead_family,
      leadLevels: distinctLevels(members.map((m) => m.reasoning)),
      sidekicks,
      combos: members,
    });
  }
  fusion.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));

  return { base, fusion };
}

export function defaultVariantId(group: BaseGroup, lastUsed: Record<string, string>): string {
  const last = lastUsed[group.key];
  if (last && group.variants.some((v) => v.id === last)) return last;
  return (
    group.variants.find((v) => v.model.reasoning === DEFAULT_REASONING)?.id ??
    group.variants[0]?.id ??
    ""
  );
}

/** Preferred level if present in `levels`, else "high", else the first. */
export function pickLevel(levels: LevelOption[], preferred: string | undefined): string {
  if (preferred !== undefined && levels.some((l) => l.key === preferred)) return preferred;
  if (levels.some((l) => l.key === DEFAULT_REASONING)) return DEFAULT_REASONING;
  return levels[0]?.key ?? "";
}

export function defaultFusionId(group: FusionGroup, lastUsed: Record<string, string>): string {
  const last = lastUsed[group.key];
  if (last && group.combos.some((c) => c.id === last)) return last;
  return resolveFusion(
    group,
    {
      leadLevel: pickLevel(group.leadLevels, undefined),
      sidekickFamily: group.sidekicks[0]?.family ?? "",
      sidekickLevel: "", // let resolveFusion pick the sidekick's level
    },
    lastUsed
  );
}

export interface FusionSelection {
  leadLevel: string;
  sidekickFamily: string;
  sidekickLevel: string;
}

export function fusionSelectionOf(m: ModelInfo): FusionSelection {
  return {
    leadLevel: m.reasoning ?? "",
    sidekickFamily: m.fusion?.sidekick_family ?? "",
    sidekickLevel: m.fusion?.sidekick_reasoning ?? "",
  };
}

/** The sidekick reasoning level of a previously used sidekick variant id
 * (from `last_used_variants["sidekick:<family>"]`), or undefined when the id
 * is not among this group's combos. */
function lastUsedSidekickLevel(
  group: FusionGroup,
  sidekickId: string | undefined
): string | undefined {
  if (!sidekickId) return undefined;
  const combo = group.combos.find((c) => c.fusion?.sidekick_id === sidekickId);
  if (!combo) return undefined;
  return combo.fusion?.sidekick_reasoning ?? "";
}

/** Resolve a (possibly partially unavailable) selection to a concrete combo
 * id: exact lead+sidekick+level match, else any combo with that lead level
 * and sidekick, else any combo with that sidekick, else the first combo. */
export function resolveFusion(
  group: FusionGroup,
  sel: FusionSelection,
  lastUsed: Record<string, string>
): string {
  const sk = group.sidekicks.find((s) => s.family === sel.sidekickFamily) ?? group.sidekicks[0];
  if (!sk) return group.combos[0]?.id ?? "";
  const skLevel = sk.levels.some((l) => l.key === sel.sidekickLevel)
    ? sel.sidekickLevel
    : pickLevel(sk.levels, lastUsedSidekickLevel(group, lastUsed[`sidekick:${sk.family}`]));
  const leadLevel = group.leadLevels.some((l) => l.key === sel.leadLevel)
    ? sel.leadLevel
    : pickLevel(group.leadLevels, undefined);

  const exact = group.combos.find(
    (c) =>
      (c.reasoning ?? "") === leadLevel &&
      c.fusion?.sidekick_family === sk.family &&
      (c.fusion?.sidekick_reasoning ?? "") === skLevel
  );
  if (exact) return exact.id;
  const sameLeadAndSidekick = group.combos.find(
    (c) => (c.reasoning ?? "") === leadLevel && c.fusion?.sidekick_family === sk.family
  );
  if (sameLeadAndSidekick) return sameLeadAndSidekick.id;
  const sameSidekick = group.combos.find((c) => c.fusion?.sidekick_family === sk.family);
  if (sameSidekick) return sameSidekick.id;
  return group.combos[0]?.id ?? "";
}
