import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ModelInfo } from "../types";
import { ModelSelector } from "./ModelSelector";

afterEach(cleanup);

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

function fable(level: string): ModelInfo {
  return mk(`claude-fable-5-1-${level}`, {
    display_name: `Claude Fable 5.1 ${level[0].toUpperCase() + level.slice(1)}`,
    family: "claude-fable-5-1",
    family_name: "Claude Fable 5.1",
    reasoning: level,
  });
}

function opus(id: string, name: string): ModelInfo {
  return mk(`claude-opus-4-6${id}`, {
    display_name: name,
    family: "claude-opus-4-6",
    family_name: "Claude Opus 4.6",
    reasoning: "high",
  });
}

function astraCombo(leadLevel: string, sidekickId: string, skFamily: string, skName: string, skLevel: string): ModelInfo {
  return mk(`fusion-gpt-6-astra-${leadLevel}-sidekick-${sidekickId}`, {
    display_name: `GPT-6 Astra + ${skName}`,
    family: "fusion",
    family_name: "Fusion",
    reasoning: leadLevel,
    fusion: {
      lead_family: "gpt-6-astra",
      lead_name: "GPT-6 Astra",
      sidekick_id: sidekickId,
      sidekick_family: skFamily,
      sidekick_name: skName,
      sidekick_reasoning: skLevel,
    },
  });
}

const MODELS: ModelInfo[] = [
  fable("low"),
  fable("medium"),
  fable("high"),
  fable("xhigh"),
  fable("max"),
  // The 2x-cost fast variant must never appear in the picker.
  mk("claude-fable-5-1-high-fast", {
    display_name: "Claude Fable 5.1 High Fast",
    family: "claude-fable-5-1",
    family_name: "Claude Fable 5.1",
    reasoning: "high",
    is_fast: true,
  }),
  // A family whose variants differ by more than effort level.
  opus("", "Claude Opus 4.6"),
  opus("-1m", "Claude Opus 4.6 1M"),
  opus("-thinking", "Claude Opus 4.6 Thinking"),
  // A single-variant model with no effort label.
  mk("kimi-k2-7", {
    display_name: "Kimi K2.7",
    family: "kimi-k2-7",
    family_name: "Kimi K2.7",
  }),
  // Fusion: GPT-6 Astra x {high,max} x {swe-2:{medium,high}, glm-5.2:{high}}
  astraCombo("high", "swe-2-high", "swe-2", "SWE-2", "high"),
  astraCombo("high", "swe-2-medium", "swe-2", "SWE-2", "medium"),
  astraCombo("high", "glm-5-2", "glm-5.2", "GLM-5.2", "high"),
  astraCombo("max", "glm-5-2", "glm-5.2", "GLM-5.2", "high"),
];

function renderSelector(onSelect = vi.fn()) {
  return {
    onSelect,
    ...render(
      <ModelSelector
        models={MODELS}
        selectedModel="claude-fable-5-1-high"
        lastUsedVariants={{}}
        hasNewModels={false}
        onSelect={onSelect}
        onOpenManager={vi.fn()}
      />
    ),
  };
}

describe("ModelSelector", () => {
  it("renders one row per group with an inline reasoning dropdown", () => {
    const { container } = renderSelector();
    fireEvent.click(screen.getByRole("button", { name: /Claude Fable 5.1 High/ }));

    // 3 base groups + 1 fusion group; no per-variant rows.
    expect(container.querySelectorAll("[data-row-button]")).toHaveLength(4);
    expect(screen.getByText("Models")).toBeTruthy();
    expect(screen.getByText("Fusion")).toBeTruthy();
    // Variants are collapsed into the dropdown, not listed as rows.  (The
    // trigger still shows the selected variant's display name.)
    expect(screen.queryByText("Claude Fable 5.1 Low")).toBeNull();
    expect(screen.queryByText("Claude Fable 5.1 High Fast")).toBeNull();

    const select = screen.getByLabelText("Claude Fable 5.1 reasoning") as HTMLSelectElement;
    expect(select.value).toBe("claude-fable-5-1-high");
    // The fast variant is filtered out: 5 options, not 6.
    expect(select.options).toHaveLength(5);
  });

  it("changing the reasoning dropdown selects the variant and keeps the panel open", () => {
    const onSelect = vi.fn();
    renderSelector(onSelect);
    fireEvent.click(screen.getByRole("button", { name: /Claude Fable 5.1 High/ }));

    fireEvent.change(screen.getByLabelText("Claude Fable 5.1 reasoning"), {
      target: { value: "claude-fable-5-1-max" },
    });
    expect(onSelect).toHaveBeenCalledWith("claude-fable-5-1-max");
    expect(screen.getByText("Manage Models")).toBeTruthy();
  });

  it("clicking a fusion row name selects the default combo and closes", () => {
    const onSelect = vi.fn();
    renderSelector(onSelect);
    fireEvent.click(screen.getByRole("button", { name: /Claude Fable 5.1 High/ }));

    fireEvent.click(screen.getByRole("button", { name: "GPT-6 Astra" }));
    // Default: lead "high" + first sidekick (GLM-5.2) at its only level.
    expect(onSelect).toHaveBeenCalledWith("fusion-gpt-6-astra-high-sidekick-glm-5-2");
    expect(screen.queryByText("Manage Models")).toBeNull();
  });

  it("changing the sidekick select resolves to a concrete combo", () => {
    const onSelect = vi.fn();
    renderSelector(onSelect);
    fireEvent.click(screen.getByRole("button", { name: /Claude Fable 5.1 High/ }));

    // The sidekick-level select is hidden for the default (GLM-5.2) sidekick
    // because it only has one level.
    expect(screen.queryByLabelText("GPT-6 Astra sidekick reasoning")).toBeNull();
    const sidekickSelect = screen.getByLabelText("GPT-6 Astra sidekick model") as HTMLSelectElement;
    expect(sidekickSelect.value).toBe("glm-5.2");
    fireEvent.change(sidekickSelect, { target: { value: "swe-2" } });
    expect(onSelect).toHaveBeenCalledWith("fusion-gpt-6-astra-high-sidekick-swe-2-high");
    expect(screen.getByText("Manage Models")).toBeTruthy();
  });

  it("renders a single-variant row without a select and a fusion row with three selects", () => {
    render(
      <ModelSelector
        models={MODELS}
        selectedModel="fusion-gpt-6-astra-high-sidekick-swe-2-high"
        lastUsedVariants={{}}
        hasNewModels={false}
        onSelect={vi.fn()}
        onOpenManager={vi.fn()}
      />
    );
    const trigger = document.querySelector(
      'button[aria-haspopup="true"]'
    ) as HTMLButtonElement;
    fireEvent.click(trigger);

    // Single-variant base group: row present, no reasoning dropdown.
    expect(screen.getByRole("button", { name: "Kimi K2.7" })).toBeTruthy();
    expect(screen.queryByLabelText("Kimi K2.7 reasoning")).toBeNull();

    // Fusion row with the multi-level SWE-2 sidekick selected shows all
    // three dropdowns.
    expect(screen.getByLabelText("GPT-6 Astra reasoning")).toBeTruthy();
    expect(screen.getByLabelText("GPT-6 Astra sidekick model")).toBeTruthy();
    expect(screen.getByLabelText("GPT-6 Astra sidekick reasoning")).toBeTruthy();
  });
});
