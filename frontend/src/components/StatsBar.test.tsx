import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

afterEach(cleanup);
import { StatsBar } from "./StatsBar";
import type { ConversationCostEstimate, MessageMetrics } from "../types";

function baseMetrics(overrides: Partial<MessageMetrics> = {}): MessageMetrics {
  return { input_tokens: 100, output_tokens: 50, ...overrides };
}

function baseEstimate(overrides: Partial<ConversationCostEstimate> = {}): ConversationCostEstimate {
  return {
    amount: 0.0012,
    hypothetical_amount: 0.0024,
    currency: "USD",
    input_tokens: 100,
    cached_input_tokens: 0,
    output_tokens: 50,
    input_amount: 0.0008,
    cached_input_amount: 0,
    output_amount: 0.0004,
    hypothetical_input_amount: 0.0016,
    hypothetical_cached_input_amount: 0,
    hypothetical_output_amount: 0.0008,
    models: [],
    priced_steps: 1,
    unpriced_steps: 0,
    complete: true,
    estimated: true,
    source: "sqlite",
    reasons: [],
    ...overrides,
  };
}

describe("StatsBar cost source indicator", () => {
  it("shows the official badge when usage.cost is present (ACP source)", () => {
    render(
      <StatsBar
        usage={{ cost: { amount: 0.0034, currency: "USD" } }}
        metrics={baseMetrics()}
        costEstimate={null}
      />
    );
    expect(screen.getByText("$0.0034 USD")).toBeTruthy();
    expect(screen.getByLabelText("official")).toBeTruthy();
  });

  it("does not show the official badge when only the local estimate is present", () => {
    render(
      <StatsBar
        usage={{}}
        metrics={baseMetrics()}
        costEstimate={baseEstimate()}
      />
    );
    expect(screen.getByText("Est. $0.0012")).toBeTruthy();
    expect(screen.queryByLabelText("official")).toBeNull();
  });

  it("shows both costs with the official badge only on the ACP cost", () => {
    render(
      <StatsBar
        usage={{ cost: { amount: 0.0034, currency: "USD" } }}
        metrics={baseMetrics()}
        costEstimate={baseEstimate()}
      />
    );
    expect(screen.getByText("Est. $0.0012")).toBeTruthy();
    expect(screen.getByText("$0.0034 USD")).toBeTruthy();
    expect(screen.getByLabelText("official")).toBeTruthy();
  });

  it("shows neither cost nor badge when no cost data is available", () => {
    render(
      <StatsBar
        usage={{}}
        metrics={baseMetrics()}
        costEstimate={null}
      />
    );
    expect(screen.queryByLabelText("official")).toBeNull();
    expect(screen.queryByText(/Est\./)).toBeNull();
  });
});
