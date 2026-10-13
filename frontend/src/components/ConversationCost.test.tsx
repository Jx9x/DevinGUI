import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

afterEach(cleanup);
import {
  ConversationCost,
  formatPrice,
  formatTokenCount,
  renderModelCostRow,
} from "./ConversationCost";
import type { ConversationCostEstimate } from "../types";

function modelUsage(
  overrides: Partial<ConversationCostEstimate["models"][number]> = {}
): ConversationCostEstimate["models"][number] {
  return {
    model_id: "m",
    display_name: "Model",
    input_tokens: 0,
    cached_input_tokens: 0,
    cache_creation_tokens: 0,
    output_tokens: 0,
    amount: 0,
    input_amount: 0,
    cached_input_amount: 0,
    output_amount: 0,
    hypothetical_amount: 0,
    hypothetical_input_amount: 0,
    hypothetical_cached_input_amount: 0,
    hypothetical_output_amount: 0,
    was_free: false,
    ...overrides,
  };
}

function baseEstimate(overrides: Partial<ConversationCostEstimate> = {}): ConversationCostEstimate {
  return {
    amount: 0,
    hypothetical_amount: 0,
    currency: "USD",
    input_tokens: 0,
    cached_input_tokens: 0,
    output_tokens: 0,
    input_amount: 0,
    cached_input_amount: 0,
    output_amount: 0,
    hypothetical_input_amount: 0,
    hypothetical_cached_input_amount: 0,
    hypothetical_output_amount: 0,
    models: [],
    priced_steps: 0,
    unpriced_steps: 0,
    complete: true,
    estimated: true,
    source: "atif",
    reasons: [],
    ...overrides,
  };
}

describe("formatPrice", () => {
  it("uses exactly three decimal places", () => {
    expect(formatPrice(0.1827)).toBe("$0.183");
    expect(formatPrice(1.0579)).toBe("$1.058");
    expect(formatPrice(0)).toBe("$0.000");
    expect(formatPrice(8.2)).toBe("$8.200");
  });
});

describe("formatTokenCount", () => {
  it("formats thousands with a lowercase k and three decimals", () => {
    expect(formatTokenCount(2423)).toBe("2.423k");
    expect(formatTokenCount(45190)).toBe("45.190k");
    expect(formatTokenCount(133591)).toBe("133.591k");
  });

  it("formats millions with an uppercase M and three decimals", () => {
    expect(formatTokenCount(3_289_210)).toBe("3.289M");
    expect(formatTokenCount(3_156_000)).toBe("3.156M");
  });

  it("leaves small counts raw", () => {
    expect(formatTokenCount(0)).toBe("0");
    expect(formatTokenCount(999)).toBe("999");
  });
});

describe("renderModelCostRow", () => {
  it("renders a paid model with per-category prices and a charged total", () => {
    const row = renderModelCostRow(
      modelUsage({
        display_name: "GPT-5.6 Terra High Thinking",
        input_tokens: 45190,
        cached_input_tokens: 133591,
        output_tokens: 2423,
        input_amount: 0.1134,
        cached_input_amount: 0.0334,
        output_amount: 0.0363,
        amount: 0.1827,
        hypothetical_amount: 0.1827,
      })
    );
    expect(row).toBe(
      "GPT-5.6 Terra High Thinking · In 45.190k ($0.113) · Cache 133.591k ($0.033) · Out 2.423k ($0.036) · $0.183"
    );
  });

  it("renders a free-on-plan model with an API equivalent and no per-category prices", () => {
    const row = renderModelCostRow(
      modelUsage({
        display_name: "GLM-5.2 High",
        input_tokens: 126724,
        cached_input_tokens: 3_156_000,
        output_tokens: 13633,
        amount: 0,
        hypothetical_amount: 1.0579,
        was_free: true,
      })
    );
    expect(row).toBe(
      "GLM-5.2 High · In 126.724k · Cache 3.156M · Out 13.633k · Free · $0.000 (API $1.058)"
    );
  });
});

describe("ConversationCost", () => {
  it("shows mixed free and paid usage with an approximate API comparison", () => {
    const model = modelUsage({
      amount: 2,
      hypothetical_amount: 5,
      paid_steps: 1,
      free_steps: 399,
      cached_input_tokens: 39_151_989,
      hypothetical_estimated: true,
    });
    render(<ConversationCost estimate={baseEstimate({ models: [model] })} />);
    expect(screen.getByText("399 free · 1 paid")).toBeTruthy();
    expect(screen.getByText("39.152M")).toBeTruthy();
    expect(screen.getByText("$2.000")).toBeTruthy();
    expect(screen.getByText("$5.000")).toBeTruthy();
    expect(screen.getByText(/API est\./)).toBeTruthy();
    expect(renderModelCostRow(model)).toContain("399 free · 1 paid · $2.000 (API est. $5.000)");
  });

  it("shows unpriced model usage as unknown and labels a partial estimate", () => {
    render(
      <ConversationCost
        estimate={baseEstimate({
          complete: false,
          unpriced_steps: 3,
          models: [
            modelUsage({ display_name: "compactor", input_tokens: 184165, unpriced_steps: 3 }),
          ],
        })}
      />
    );
    expect(screen.getByText("compactor")).toBeTruthy();
    expect(screen.getByText("184.165k")).toBeTruthy();
    expect(screen.getByText("Unknown")).toBeTruthy();
    expect(screen.queryByText("$0.000")).toBeNull();
    expect(screen.getByText(/3 inference steps have no available model price/)).toBeTruthy();
  });

  it("shows known costs as partial when the same model also has unpriced steps", () => {
    render(
      <ConversationCost
        estimate={baseEstimate({
          complete: false,
          unpriced_steps: 1,
          priced_steps: 1,
          models: [modelUsage({ amount: 2, paid_steps: 1, unpriced_steps: 1 })],
        })}
      />
    );
    expect(screen.getByText("1 paid · 1 unpriced")).toBeTruthy();
    expect(screen.getByText("Partial $2.000")).toBeTruthy();
  });

  it("renders a single paid-model prompt as one row with no header, footer, or disclaimer", () => {
    render(
      <ConversationCost
        estimate={baseEstimate({
          amount: 0.1827,
          hypothetical_amount: 0.1827,
          input_tokens: 45190,
          cached_input_tokens: 133591,
          output_tokens: 2423,
          input_amount: 0.1134,
          cached_input_amount: 0.0334,
          output_amount: 0.0363,
          hypothetical_input_amount: 0.1134,
          hypothetical_cached_input_amount: 0.0334,
          hypothetical_output_amount: 0.0363,
          priced_steps: 1,
          reasons: ["cache_write_input_rate_fallback"],
          models: [
            modelUsage({
              model_id: "gpt-5.6",
              display_name: "GPT-5.6 Terra High Thinking",
              input_tokens: 45190,
              cached_input_tokens: 133591,
              output_tokens: 2423,
              input_amount: 0.1134,
              cached_input_amount: 0.0334,
              output_amount: 0.0363,
              amount: 0.1827,
              hypothetical_amount: 0.1827,
            }),
          ],
        })}
      />
    );

    // No title row.
    expect(screen.queryByText(/Estimated conversation cost/i)).toBeNull();
    expect(screen.queryByText(/Prompt \d+ cost/i)).toBeNull();
    // No disclaimer.
    expect(screen.queryByText(/Cache creation is estimated/i)).toBeNull();
    // No total row for a single-model box.
    expect(screen.queryByText(/Estimated total/i)).toBeNull();

    expect(screen.getByText("GPT-5.6 Terra High Thinking")).toBeTruthy();
    expect(screen.getByText("45.190k")).toBeTruthy();
    expect(screen.getByText("133.591k")).toBeTruthy();
    expect(screen.getByText("2.423k")).toBeTruthy();
    expect(screen.getByText("$0.183")).toBeTruthy();
  });

  it("renders a free-only conversation with an API equivalent and $0.000 charged", () => {
    render(
      <ConversationCost
        estimate={baseEstimate({
          amount: 0,
          hypothetical_amount: 1.0579,
          input_tokens: 126724,
          cached_input_tokens: 3_156_000,
          output_tokens: 13633,
          hypothetical_input_amount: 1.0,
          hypothetical_cached_input_amount: 0.02,
          hypothetical_output_amount: 0.0379,
          models: [
            modelUsage({
              model_id: "glm-5.2",
              display_name: "GLM-5.2 High",
              input_tokens: 126724,
              cached_input_tokens: 3_156_000,
              output_tokens: 13633,
              amount: 0,
              hypothetical_amount: 1.0579,
              was_free: true,
            }),
          ],
        })}
      />
    );

    expect(screen.getByText("GLM-5.2 High")).toBeTruthy();
    expect(screen.getByText("126.724k")).toBeTruthy();
    expect(screen.getByText("3.156M")).toBeTruthy();
    expect(screen.getByText("13.633k")).toBeTruthy();
    expect(screen.getByText("$0.000")).toBeTruthy();
    expect(screen.getByText("$1.058")).toBeTruthy();
    // Single model -> no total row.
    expect(screen.queryByText(/Estimated total/i)).toBeNull();
  });

  it("renders a mixed-model conversation with one row per model and a total row", () => {
    render(
      <ConversationCost
        estimate={baseEstimate({
          amount: 0.1827,
          hypothetical_amount: 1.2406,
          input_tokens: 171914,
          cached_input_tokens: 3_289_591,
          output_tokens: 16056,
          input_amount: 0.1134,
          cached_input_amount: 0.0334,
          output_amount: 0.0363,
          hypothetical_input_amount: 1.1134,
          hypothetical_cached_input_amount: 0.0534,
          hypothetical_output_amount: 0.0742,
          models: [
            modelUsage({
              model_id: "gpt-5.6",
              display_name: "GPT-5.6 Terra High Thinking",
              input_tokens: 45190,
              cached_input_tokens: 133591,
              output_tokens: 2423,
              input_amount: 0.1134,
              cached_input_amount: 0.0334,
              output_amount: 0.0363,
              amount: 0.1827,
              hypothetical_amount: 0.1827,
            }),
            modelUsage({
              model_id: "glm-5.2",
              display_name: "GLM-5.2 High",
              input_tokens: 126724,
              cached_input_tokens: 3_156_000,
              output_tokens: 13633,
              amount: 0,
              hypothetical_amount: 1.0579,
              was_free: true,
            }),
          ],
        })}
      />
    );

    // Two distinct model rows.
    expect(screen.getByText("GPT-5.6 Terra High Thinking")).toBeTruthy();
    expect(screen.getByText("GLM-5.2 High")).toBeTruthy();

    // Each model row contains only its own token totals.
    expect(screen.getByText("45.190k")).toBeTruthy();
    expect(screen.getByText("133.591k")).toBeTruthy();
    expect(screen.getByText("2.423k")).toBeTruthy();
    expect(screen.getByText("126.724k")).toBeTruthy();
    expect(screen.getByText("3.156M")).toBeTruthy();
    expect(screen.getByText("13.633k")).toBeTruthy();

    // Total row sums charged and API-equivalent. $0.183 appears both on the
    // GPT model row and the total row, so use getAllByText for that value.
    expect(screen.getByText(/Estimated total/i)).toBeTruthy();
    expect(screen.getAllByText("$0.183").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/API equivalent/i)).toBeTruthy();
    expect(screen.getByText("$1.241")).toBeTruthy();

    // No disclaimer.
    expect(screen.queryByText(/Cache creation is estimated/i)).toBeNull();
  });

  it("still surfaces a partial-pricing warning without the cache disclaimer", () => {
    render(
      <ConversationCost
        estimate={baseEstimate({
          amount: 0.1827,
          hypothetical_amount: 0.1827,
          unpriced_steps: 1,
          complete: false,
          reasons: ["cache_write_input_rate_fallback", "unknown_model"],
          models: [
            modelUsage({
              model_id: "gpt-5.6",
              display_name: "GPT-5.6 Terra High Thinking",
              input_tokens: 45190,
              cached_input_tokens: 133591,
              output_tokens: 2423,
              input_amount: 0.1134,
              cached_input_amount: 0.0334,
              output_amount: 0.0363,
              amount: 0.1827,
              hypothetical_amount: 0.1827,
            }),
          ],
        })}
      />
    );

    expect(screen.getByText(/Partial estimate: 1 inference step/)).toBeTruthy();
    expect(screen.queryByText(/Cache creation is estimated/i)).toBeNull();
  });
});
