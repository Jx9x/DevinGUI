import { createRef } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ChatArea } from "./ChatArea";
import type { ConversationCostEstimate, ReasoningStep, UIMessage } from "../types";

afterEach(cleanup);

const measuredStep: ReasoningStep = {
  step_id: null,
  reasoning_content: "The largest measured reasoning block in this turn.",
  timestamp: "2026-10-03T10:00:01Z",
  tool_calls: [],
  message: "",
  metrics: { completion_tokens: 11912, total_time_ms: 239975, tokens_per_sec: 49.6385 },
  is_throughput_sample: true,
};

const props = {
  messages: [],
  reasoningSteps: [measuredStep],
  status: "ready" as const,
  messagesEndRef: createRef<HTMLDivElement>(),
  costEstimate: null,
};

describe("reasoning throughput", () => {
  it("shows a compact approximate rate on the reasoning block and at the bottom", () => {
    render(<ChatArea {...props} />);

    expect(screen.getByText("· ~50 tok/s")).toBeTruthy();
    expect(screen.getByText("Reasoning · ~50 tok/s")).toBeTruthy();
    expect(screen.queryByText(/estimate/i)).toBeNull();
  });

  it("clears the footer during a new turn and when there is no current sample", () => {
    const { rerender } = render(<ChatArea {...props} />);
    expect(screen.getByText("Reasoning · ~50 tok/s")).toBeTruthy();

    rerender(<ChatArea {...props} status="streaming" />);
    expect(screen.queryByText("Reasoning · ~50 tok/s")).toBeNull();

    rerender(<ChatArea {...props} reasoningSteps={[{ ...measuredStep, is_throughput_sample: false }]} />);
    expect(screen.queryByText("Reasoning · ~50 tok/s")).toBeNull();
    expect(screen.getByText("· ~50 tok/s")).toBeTruthy();
  });

  it("does not infer a rate from the length of live or untimed reasoning", () => {
    render(<ChatArea {...props} status="streaming" reasoningSteps={[
      { ...measuredStep, metrics: {}, isLive: true, is_throughput_sample: false },
    ]} />);

    expect(screen.queryByText(/tok\/s/)).toBeNull();
    expect(screen.getByText("Thinking…")).toBeTruthy();
  });

  it("labels reasoning by characters and preserves a positive rate below one token per second", () => {
    render(<ChatArea {...props} reasoningSteps={[{
      ...measuredStep,
      reasoning_content: "I'm starting to write the code.",
      metrics: { completion_tokens: 1, total_time_ms: 5881, tokens_per_sec: 1 / 5.881 },
    }]} />);

    expect(screen.getByText(/31 chars/)).toBeTruthy();
    expect(screen.queryByText(/1 tokens/)).toBeNull();
    expect(screen.getByText("· ~0.17 tok/s")).toBeTruthy();
    expect(screen.getByText("Reasoning · ~0.17 tok/s")).toBeTruthy();
    expect(screen.queryByText(/~0 tok\/s/)).toBeNull();
  });
});

function turnEstimate(turnIndex: number): ConversationCostEstimate & { turn_index: number } {
  return {
    turn_index: turnIndex,
    amount: turnIndex,
    hypothetical_amount: turnIndex,
    currency: "USD",
    input_tokens: 100,
    cached_input_tokens: 0,
    output_tokens: 10,
    input_amount: turnIndex,
    cached_input_amount: 0,
    output_amount: 0,
    hypothetical_input_amount: turnIndex,
    hypothetical_cached_input_amount: 0,
    hypothetical_output_amount: 0,
    models: [],
    priced_steps: 1,
    unpriced_steps: 0,
    complete: true,
    estimated: true,
    source: "sqlite",
    reasons: [],
  };
}

function prompt(id: string, content: string, turnIndex?: number): UIMessage {
  return { id, content, turnIndex, role: "user", timestamp: null, isStreaming: false };
}

describe("per-turn costs", () => {
  it.each([
    ["persisted main-chain indices", [
      prompt("first", "First prompt", 1),
      prompt("sidekick", "Internal sidekick request", 0),
      prompt("compactor", "Conversation to summarize: earlier context", 0),
      prompt("second", "Second prompt", 2),
      prompt("replay", "Replayed first prompt", 1),
      // Optimistic prompts have no persisted index yet.
      prompt("third", "Third prompt"),
    ]],
    ["legacy history without indices", [
      prompt("first", "First prompt"),
      prompt("compactor", "Conversation to summarize: earlier context"),
      prompt("second", "Second prompt"),
      prompt("third", "Third prompt"),
    ]],
  ] as const)("keeps boxes on the preceding real turn with %s", (_, messages) => {
    render(<ChatArea {...props} reasoningSteps={[]} messages={[...messages]}
      costEstimate={{ ...turnEstimate(8), turns: [turnEstimate(1), turnEstimate(2)] }} />);

    const firstBox = screen.getByText("$1.000").closest("section");
    const secondBox = screen.getByText("$2.000").closest("section");
    expect(firstBox?.parentElement?.textContent).toContain("Second prompt");
    expect(secondBox?.parentElement?.textContent).toContain("Third prompt");
    expect(screen.getAllByTitle("Local cost estimate from token usage and model prices")).toHaveLength(3);
  });
});
