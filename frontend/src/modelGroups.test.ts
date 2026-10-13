import { describe, expect, it } from "vitest";
import type { ModelInfo } from "./types";
import {
  buildModelGroups,
  compareReasoning,
  defaultFusionId,
  defaultVariantId,
  fusionSelectionOf,
  groupKeyOf,
  groupNameOf,
  reasoningLabel,
  resolveFusion,
  type FusionGroup,
} from "./modelGroups";

function mk(id: string, overrides: Partial<ModelInfo> = {}): ModelInfo {
  return {
    id,
    display_name: id,
    vision: false,
    tags: [],
    description: "",
    is_new: false,
    available: true,
    excluded: false,
    reasoning: null,
    is_fast: false,
    fusion: null,
    ...overrides,
  };
}

function baseVariant(id: string, level: string | null, displayName: string): ModelInfo {
  return mk(id, {
    display_name: displayName,
    family: "claude-fable-5-1",
    family_name: "Claude Fable 5.1",
    reasoning: level,
  });
}

function fusionCombo(
  leadLevel: string,
  sidekickId: string,
  sidekickFamily: string,
  sidekickName: string,
  sidekickLevel: string | null,
  leadFamily = "claude-fable-5-1",
  leadName = "Claude Fable 5.1"
): ModelInfo {
  return mk(`fusion-${leadFamily}-${leadLevel}-sidekick-${sidekickId}`, {
    display_name: `${leadName} + ${sidekickName}`,
    family: "fusion",
    family_name: "Fusion",
    reasoning: leadLevel,
    fusion: {
      lead_family: leadFamily,
      lead_name: leadName,
      sidekick_id: sidekickId,
      sidekick_family: sidekickFamily,
      sidekick_name: sidekickName,
      sidekick_reasoning: sidekickLevel,
    },
  });
}

const FABLE_VARIANTS = [
  baseVariant("claude-fable-5-1-low", "low", "Claude Fable 5.1 Low"),
  baseVariant("claude-fable-5-1-medium", "medium", "Claude Fable 5.1 Medium"),
  baseVariant("claude-fable-5-1-high", "high", "Claude Fable 5.1 High"),
  baseVariant("claude-fable-5-1-xhigh", "xhigh", "Claude Fable 5.1 XHigh"),
  baseVariant("claude-fable-5-1-max", "max", "Claude Fable 5.1 Max"),
];

// lead levels {low,high,max} x sidekicks {swe-2: {medium,high}, glm-5.2: {high}}
const FUSION_COMBOS = [
  ...["low", "high", "max"].flatMap((lead) => [
    fusionCombo(lead, "swe-2-medium", "swe-2", "SWE-2", "medium"),
    fusionCombo(lead, "swe-2-high", "swe-2", "SWE-2", "high"),
    fusionCombo(lead, "glm-5-2", "glm-5.2", "GLM-5.2", "high"),
  ]),
  fusionCombo("max", "glm-5-2", "glm-5.2", "GLM-5.2", "high", "gpt-6-astra", "GPT-6 Astra"),
];

function fableFusionGroup(): FusionGroup {
  const { fusion } = buildModelGroups(FUSION_COMBOS);
  return fusion.find((g) => g.leadFamily === "claude-fable-5-1")!;
}

describe("buildModelGroups base groups", () => {
  it("groups variants by family, ordered by reasoning level with level labels", () => {
    const { base, fusion } = buildModelGroups(FABLE_VARIANTS);
    expect(fusion).toHaveLength(0);
    expect(base).toHaveLength(1);
    const group = base[0];
    expect(group.name).toBe("Claude Fable 5.1");
    expect(group.variants.map((v) => v.id)).toEqual([
      "claude-fable-5-1-low",
      "claude-fable-5-1-medium",
      "claude-fable-5-1-high",
      "claude-fable-5-1-xhigh",
      "claude-fable-5-1-max",
    ]);
    expect(group.variants.map((v) => v.label)).toEqual([
      "Low",
      "Medium",
      "High",
      "XHigh",
      "Max",
    ]);
  });

  it("falls back to stripped display names when reasoning keys are not distinct", () => {
    const opus = (id: string, name: string) =>
      mk(`claude-opus-4-6${id}`, {
        display_name: name,
        family: "claude-opus-4-6",
        family_name: "Claude Opus 4.6",
        reasoning: "high",
      });
    const { base } = buildModelGroups([
      opus("", "Claude Opus 4.6"),
      opus("-1m", "Claude Opus 4.6 1M"),
      opus("-thinking", "Claude Opus 4.6 Thinking"),
      opus("-thinking-1m", "Claude Opus 4.6 Thinking 1M"),
    ]);
    expect(base).toHaveLength(1);
    expect(base[0].variants.map((v) => v.label)).toEqual([
      "Default",
      "1M",
      "Thinking",
      "Thinking 1M",
    ]);
  });

  it("labels a single null-reasoning variant as Default", () => {
    const { base } = buildModelGroups([
      mk("kimi-k2-7", {
        display_name: "Kimi K2.7",
        family: "kimi-k2-7",
        family_name: "Kimi K2.7",
        reasoning: null,
      }),
    ]);
    expect(base[0].variants).toHaveLength(1);
    expect(base[0].variants[0].label).toBe("Default");
    expect(defaultVariantId(base[0], {})).toBe("kimi-k2-7");
  });
});

describe("defaultVariantId", () => {
  const group = () => buildModelGroups(FABLE_VARIANTS).base[0];

  it("defaults to the high variant", () => {
    expect(defaultVariantId(group(), {})).toBe("claude-fable-5-1-high");
  });

  it("honours a last-used variant id", () => {
    expect(defaultVariantId(group(), { "claude-fable-5-1": "claude-fable-5-1-max" })).toBe(
      "claude-fable-5-1-max"
    );
  });

  it("falls back to high when the last-used id is stale", () => {
    expect(defaultVariantId(group(), { "claude-fable-5-1": "gone" })).toBe(
      "claude-fable-5-1-high"
    );
  });
});

describe("buildModelGroups fusion groups", () => {
  it("groups combos by lead family with levels and sidekicks", () => {
    const { base, fusion } = buildModelGroups(FUSION_COMBOS);
    expect(base).toHaveLength(0);
    expect(fusion.map((g) => g.name)).toEqual(["Claude Fable 5.1", "GPT-6 Astra"]);
    const fable = fusion[0];
    expect(fable.leadLevels.map((l) => l.key)).toEqual(["low", "high", "max"]);
    expect(fable.sidekicks.map((s) => s.name)).toEqual(["GLM-5.2", "SWE-2"]);
    const swe2 = fable.sidekicks.find((s) => s.family === "swe-2")!;
    expect(swe2.levels.map((l) => l.key)).toEqual(["medium", "high"]);
    const glm = fable.sidekicks.find((s) => s.family === "glm-5.2")!;
    expect(glm.levels.map((l) => l.key)).toEqual(["high"]);
    expect(fable.combos).toHaveLength(9);
  });
});

describe("defaultFusionId", () => {
  it("defaults to lead high + first sidekick + its level", () => {
    expect(defaultFusionId(fableFusionGroup(), {})).toBe(
      "fusion-claude-fable-5-1-high-sidekick-glm-5-2"
    );
  });

  it("honours a last-used fusion id", () => {
    const id = "fusion-claude-fable-5-1-max-sidekick-swe-2-medium";
    expect(defaultFusionId(fableFusionGroup(), { "fusion:claude-fable-5-1": id })).toBe(id);
  });
});

describe("resolveFusion", () => {
  const group = () => fableFusionGroup();

  it("resolves an exact selection", () => {
    expect(
      resolveFusion(
        group(),
        { leadLevel: "high", sidekickFamily: "swe-2", sidekickLevel: "medium" },
        {}
      )
    ).toBe("fusion-claude-fable-5-1-high-sidekick-swe-2-medium");
  });

  it("falls back to the new sidekick's available level", () => {
    expect(
      resolveFusion(
        group(),
        { leadLevel: "high", sidekickFamily: "glm-5.2", sidekickLevel: "medium" },
        {}
      )
    ).toBe("fusion-claude-fable-5-1-high-sidekick-glm-5-2");
  });

  it("prefers the last-used level for a newly chosen sidekick", () => {
    // Switching sidekick family clears the level selection (""), so the
    // sidekick's last-used variant decides the level.
    expect(
      resolveFusion(
        group(),
        { leadLevel: "high", sidekickFamily: "swe-2", sidekickLevel: "" },
        { "sidekick:swe-2": "swe-2-medium" }
      )
    ).toBe("fusion-claude-fable-5-1-high-sidekick-swe-2-medium");
  });

  it("defaults a new sidekick to high without last-used data", () => {
    expect(
      resolveFusion(
        group(),
        { leadLevel: "high", sidekickFamily: "swe-2", sidekickLevel: "" },
        {}
      )
    ).toBe("fusion-claude-fable-5-1-high-sidekick-swe-2-high");
  });

  it("falls back an unavailable lead level to high", () => {
    expect(
      resolveFusion(
        group(),
        { leadLevel: "xhigh", sidekickFamily: "glm-5.2", sidekickLevel: "high" },
        {}
      )
    ).toBe("fusion-claude-fable-5-1-high-sidekick-glm-5-2");
  });
});

describe("group keys and names", () => {
  it("uses fusion:<lead_family> and 'Fusion: <lead_name>' for fusion models", () => {
    const combo = FUSION_COMBOS[0];
    expect(groupKeyOf(combo)).toBe("fusion:claude-fable-5-1");
    expect(groupNameOf(combo)).toBe("Fusion: Claude Fable 5.1");
  });

  it("uses family / family_name for base models", () => {
    expect(groupKeyOf(FABLE_VARIANTS[0])).toBe("claude-fable-5-1");
    expect(groupNameOf(FABLE_VARIANTS[0])).toBe("Claude Fable 5.1");
  });
});

describe("reasoningLabel / compareReasoning / fusionSelectionOf", () => {
  it("labels known keys and falls back for unknown/empty", () => {
    expect(reasoningLabel("high")).toBe("High");
    expect(reasoningLabel("xhigh")).toBe("XHigh");
    expect(reasoningLabel(null)).toBe("Default");
    expect(reasoningLabel("")).toBe("Default");
    expect(reasoningLabel("turbo")).toBe("Turbo");
  });

  it("orders by REASONING_ORDER with unknowns last", () => {
    expect(compareReasoning("low", "high")).toBeLessThan(0);
    expect(compareReasoning("max", "xhigh")).toBeGreaterThan(0);
    expect(compareReasoning(null, "none")).toBeGreaterThan(0);
    expect(compareReasoning("bogus", null)).toBe(0);
  });

  it("derives the current selection from a combo", () => {
    const combo = FUSION_COMBOS.find((c) => c.id === "fusion-claude-fable-5-1-high-sidekick-swe-2-medium")!;
    expect(fusionSelectionOf(combo)).toEqual({
      leadLevel: "high",
      sidekickFamily: "swe-2",
      sidekickLevel: "medium",
    });
  });
});
