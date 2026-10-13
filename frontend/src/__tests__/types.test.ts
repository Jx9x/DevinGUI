import { describe, it, expect } from "vitest";
import { assertModelsResponse, assertWSInbound } from "../types";

describe("assertWSInbound", () => {
  it("accepts live images and rejects malformed image content", () => {
    const frame = (image: unknown) => ({type: "session_update", sessionId: "s-1", update: {
      type: "tool_call_update", content: [image],
    }});
    expect(() => assertWSInbound(frame({type: "image", mimeType: "image/png", data: "cG5n"}))).not.toThrow();
    expect(() => assertWSInbound(frame({type: "image", mimeType: "image/png", data: 42}))).toThrow();
    expect(() => assertWSInbound(frame({type: "image", mimeType: "text/html", data: "cG5n"}))).toThrow();
  });

  it("rejects non-object values", () => {
    expect(() => assertWSInbound(null)).toThrow(/Invalid WebSocket message/);
    expect(() => assertWSInbound("hello")).toThrow(/Invalid WebSocket message/);
    expect(() => assertWSInbound(42)).toThrow(/Invalid WebSocket message/);
    expect(() => assertWSInbound(undefined)).toThrow(/Invalid WebSocket message/);
  });

  it("rejects values without a string type", () => {
    expect(() => assertWSInbound({})).toThrow(/missing type/);
    expect(() => assertWSInbound({ type: 123 })).toThrow(/missing type/);
    expect(() => assertWSInbound({ type: null })).toThrow(/missing type/);
  });

  it("rejects unknown message types", () => {
    expect(() => assertWSInbound({ type: "not_a_real_type" })).toThrow(
      /Unknown WebSocket message type/
    );
  });

  it("accepts a valid session_created message", () => {
    const msg = { type: "session_created", sessionId: "s-1", requestId: "req-1" };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("accepts session_created with optional model", () => {
    const msg = { type: "session_created", sessionId: "s-1", model: "gpt-4", requestId: "req-1" };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects session_created with a non-string sessionId", () => {
    expect(() => assertWSInbound({ type: "session_created", sessionId: 42 })).toThrow(
      /Invalid session_created message/
    );
  });

  it("rejects session_created with a non-string model", () => {
    expect(() => assertWSInbound({ type: "session_created", sessionId: "s-1", model: 42 })).toThrow(
      /Invalid session_created message/
    );
  });

  it("accepts a valid session_update message", () => {
    const msg = {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "agent_message_chunk", messageId: "m-1", chunk: "hi" },
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("accepts exact input tokens on a usage update", () => {
    const msg = {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "usage_update", used: 12487, size: 262000, inputTokens: 10659 },
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects session_update with a missing update object", () => {
    expect(() => assertWSInbound({ type: "session_update", sessionId: "s-1" })).toThrow(
      /Invalid session_update message/
    );
  });

  it("rejects session_update with an invalid update shape", () => {
    expect(() =>
      assertWSInbound({
        type: "session_update",
        sessionId: "s-1",
        update: { type: 42 },
      })
    ).toThrow(/Invalid session_update message/);
  });

  it("accepts a valid request_permission message", () => {
    const msg = {
      type: "request_permission",
      sessionId: "s-1",
      toolCallId: "tc-1",
      data: { toolCallId: "tc-1", message: "run rm" },
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects request_permission with invalid data", () => {
    expect(() =>
      assertWSInbound({
        type: "request_permission",
        sessionId: "s-1",
        data: { message: 42 },
      })
    ).toThrow(/Invalid request_permission message/);
  });

  it("accepts a valid turn_complete message", () => {
    const msg = {
      type: "turn_complete",
      sessionId: "s-1",
      result: {},
      requestId: "req-1",
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects turn_complete with a non-object result", () => {
    expect(() =>
      assertWSInbound({ type: "turn_complete", sessionId: "s-1", result: null })
    ).toThrow(/Invalid turn_complete message/);
  });

  it("accepts a valid reasoning_update message", () => {
    const msg = { type: "reasoning_update", sessionId: "s-1", steps: [] };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects reasoning_update with non-array steps", () => {
    expect(() =>
      assertWSInbound({
        type: "reasoning_update",
        sessionId: "s-1",
        steps: "nope",
      })
    ).toThrow(/Invalid reasoning_update message/);
  });

  it("accepts a valid history_loaded message", () => {
    const msg = {
      type: "history_loaded",
      sessionId: "s-1",
      messages: [],
      reasoning: [],
      requestId: "req-1",
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects history_loaded missing reasoning array", () => {
    expect(() =>
      assertWSInbound({
        type: "history_loaded",
        sessionId: "s-1",
        messages: [],
      })
    ).toThrow(/Invalid history_loaded message/);
  });

  it("accepts a valid session_resumed message", () => {
    const msg = { type: "session_resumed", sessionId: "s-1", requestId: "req-1" };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects session_resumed with non-string sessionId", () => {
    expect(() => assertWSInbound({ type: "session_resumed", sessionId: 7 })).toThrow(
      /Invalid session_resumed message/
    );
  });

  it("accepts a valid resume_failed message", () => {
    const msg = { type: "resume_failed", sessionId: "s-1", message: "gone", requestId: "req-1" };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects resume_failed missing message", () => {
    expect(() => assertWSInbound({ type: "resume_failed", sessionId: "s-1" })).toThrow(
      /Invalid resume_failed message/
    );
  });

  it("accepts a valid db_message_update message", () => {
    const msg = {
      type: "db_message_update",
      sessionId: "s-1",
      messages: [],
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects db_message_update with non-array messages", () => {
    expect(() =>
      assertWSInbound({
        type: "db_message_update",
        sessionId: "s-1",
        messages: "nope",
      })
    ).toThrow(/Invalid db_message_update message/);
  });

  it("accepts a valid permission_received message with allow", () => {
    const msg = {
      type: "permission_received",
      sessionId: "s-1",
      toolCallId: "tc-1",
      decision: "allow",
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("accepts a valid permission_received message with deny", () => {
    const msg = {
      type: "permission_received",
      sessionId: "s-1",
      toolCallId: "tc-1",
      decision: "deny",
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("accepts permission_received with a string acpRequestId", () => {
    // The Devin CLI uses string UUIDs for inbound `session/request_permission`
    // JSON-RPC ids; the backend echoes them as `acpRequestId`. Rejecting them
    // dropped the ack and left the permission panel stuck on "waiting for
    // backend…".
    const msg = {
      type: "permission_received",
      sessionId: "s-1",
      toolCallId: "tc-1",
      requestId: "req-1",
      acpRequestId: "00000000-0000-4000-8000-000000000001",
      decision: "allow",
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("accepts permission_received with a numeric acpRequestId", () => {
    const msg = {
      type: "permission_received",
      sessionId: "s-1",
      toolCallId: "tc-1",
      requestId: "req-1",
      acpRequestId: 42,
      decision: "allow",
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects permission_received with an invalid decision", () => {
    expect(() =>
      assertWSInbound({
        type: "permission_received",
        sessionId: "s-1",
        decision: "maybe",
      })
    ).toThrow(/Invalid permission_received message/);
  });

  it("rejects permission_received with a non-string sessionId", () => {
    expect(() =>
      assertWSInbound({
        type: "permission_received",
        sessionId: 7,
        decision: "allow",
      })
    ).toThrow(/Invalid permission_received message/);
  });

  it("accepts a valid error message", () => {
    const msg = {
      type: "error",
      sessionId: "s-1",
      operation: "prompt",
      message: "boom",
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("accepts an error message without sessionId", () => {
    const msg = { type: "error", operation: "new_session", message: "boom" };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects an error message with an unknown operation", () => {
    expect(() =>
      assertWSInbound({
        type: "error",
        sessionId: "s-1",
        operation: "bogus",
        message: "boom",
      })
    ).toThrow(/unknown operation/);
  });

  it("rejects an error message with a non-string message", () => {
    expect(() =>
      assertWSInbound({
        type: "error",
        sessionId: "s-1",
        operation: "prompt",
        message: 42,
      })
    ).toThrow(/Invalid error message/);
  });

  // -- session_update: sessionId + discriminator constraints ---------------

  it("rejects session_update with a non-string sessionId", () => {
    expect(() =>
      assertWSInbound({
        type: "session_update",
        sessionId: 7,
        update: { type: "agent_message_chunk", chunk: "hi" },
      })
    ).toThrow(/Invalid session_update message/);
  });

  it("rejects session_update missing sessionId", () => {
    expect(() =>
      assertWSInbound({
        type: "session_update",
        update: { type: "agent_message_chunk", chunk: "hi" },
      })
    ).toThrow(/Invalid session_update message/);
  });

  it("rejects session_update with an unknown update type", () => {
    expect(() =>
      assertWSInbound({
        type: "session_update",
        sessionId: "s-1",
        update: { type: "bogus_update", chunk: "hi" },
      })
    ).toThrow(/Invalid session_update message/);
  });

  it("rejects session_update with an unknown status value", () => {
    expect(() =>
      assertWSInbound({
        type: "session_update",
        sessionId: "s-1",
        update: { type: "tool_call", status: "finished" },
      })
    ).toThrow(/Invalid session_update message/);
  });

  it("accepts session_update with a known status value", () => {
    const msg = {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "tool_call", status: "in_progress" },
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  // -- history_loaded: item validation -------------------------------------

  function makeValidNode(overrides?: Record<string, unknown>) {
    return {
      row_id: 1,
      node_id: "node-1",
      parent_node_id: null,
      created_at: "2026-07-13T00:00:00Z",
      role: "user",
      content: "hi",
      message_id: null,
      metrics: null,
      finish_reason: null,
      metadata: null,
      ...overrides,
    };
  }

  function makeValidStep(overrides?: Record<string, unknown>) {
    return {
      step_id: 1,
      reasoning_content: "thinking",
      timestamp: null,
      tool_calls: [],
      message: "msg",
      metrics: {},
      ...overrides,
    };
  }

  it("accepts history_loaded with valid nodes and reasoning", () => {
    const msg = {
      type: "history_loaded",
      sessionId: "s-1",
      messages: [makeValidNode()],
      reasoning: [makeValidStep()],
      requestId: "req-1",
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("accepts a MessageNode with metrics and metadata", () => {
    const msg = {
      type: "history_loaded",
      sessionId: "s-1",
      messages: [
        makeValidNode({
          metrics: { input_tokens: 10, cache_creation_tokens: null },
          metadata: { num_tokens: 5, metrics: { output_tokens: 3 } },
        }),
      ],
      reasoning: [],
      requestId: "req-1",
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects history_loaded with a malformed MessageNode", () => {
    expect(() =>
      assertWSInbound({
        type: "history_loaded",
        sessionId: "s-1",
        messages: [makeValidNode({ row_id: "not-a-number" })],
        reasoning: [],
      })
    ).toThrow(/Invalid history_loaded message/);
  });

  it("rejects history_loaded with a malformed metrics object", () => {
    expect(() =>
      assertWSInbound({
        type: "history_loaded",
        sessionId: "s-1",
        messages: [makeValidNode({ metrics: { input_tokens: "lots" } })],
        reasoning: [],
      })
    ).toThrow(/Invalid history_loaded message/);
  });

  it("rejects history_loaded with a malformed ReasoningStep", () => {
    expect(() =>
      assertWSInbound({
        type: "history_loaded",
        sessionId: "s-1",
        messages: [],
        reasoning: [makeValidStep({ step_id: "bad" })],
      })
    ).toThrow(/Invalid history_loaded message/);
  });

  it("rejects history_loaded with a malformed tool_calls entry", () => {
    expect(() =>
      assertWSInbound({
        type: "history_loaded",
        sessionId: "s-1",
        messages: [],
        reasoning: [makeValidStep({ tool_calls: [{ function_name: 5 }] })],
      })
    ).toThrow(/Invalid history_loaded message/);
  });

  // -- reasoning_update: item validation -----------------------------------

  it("accepts reasoning_update with valid steps", () => {
    const msg = {
      type: "reasoning_update",
      sessionId: "s-1",
      steps: [makeValidStep()],
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("accepts measured reasoning and validates its throughput fields", () => {
    const frame = (overrides: Record<string, unknown> = {}) => ({
      type: "reasoning_update", sessionId: "s-1",
      steps: [makeValidStep({
        metrics: { completion_tokens: 1200, total_time_ms: 24000, tokens_per_sec: 50 },
        is_throughput_sample: true, ...overrides,
      })],
    });
    expect(() => assertWSInbound(frame())).not.toThrow();
    expect(() => assertWSInbound(frame({ metrics: { tokens_per_sec: "50" } }))).toThrow();
    expect(() => assertWSInbound(frame({ metrics: { total_time_ms: "24000" } }))).toThrow();
    expect(() => assertWSInbound(frame({ is_throughput_sample: "true" }))).toThrow();
  });

  it("rejects reasoning_update with a malformed step", () => {
    expect(() =>
      assertWSInbound({
        type: "reasoning_update",
        sessionId: "s-1",
        steps: [makeValidStep({ reasoning_content: 42 })],
      })
    ).toThrow(/Invalid reasoning_update message/);
  });

  // -- db_message_update: item validation ----------------------------------

  it("accepts db_message_update with valid nodes", () => {
    const msg = {
      type: "db_message_update",
      sessionId: "s-1",
      messages: [makeValidNode()],
    };
    expect(assertWSInbound(msg)).toEqual(msg);
  });

  it("rejects db_message_update with a malformed MessageNode", () => {
    expect(() =>
      assertWSInbound({
        type: "db_message_update",
        sessionId: "s-1",
        messages: [makeValidNode({ content: 7 })],
      })
    ).toThrow(/Invalid db_message_update message/);
  });
});

describe("assertModelsResponse", () => {
  const validModel = {
    id: "m-1",
    display_name: "Model One",
    vision: false,
    tags: [],
    description: "",
    is_new: false,
    available: true,
    excluded: false,
  };

  it("accepts models carrying reasoning/is_fast/fusion and last_used_variants", () => {
    const resp = {
      models: [
        {
          ...validModel,
          reasoning: "high",
          is_fast: false,
          fusion: {
            lead_family: "claude-fable-5-1",
            lead_name: "Claude Fable 5.1",
            sidekick_id: "swe-2-high",
            sidekick_family: "swe-2",
            sidekick_name: "SWE-2",
            sidekick_reasoning: "high",
          },
        },
      ],
      selectable: [],
      recent: [],
      last_used: "m-1",
      last_used_variants: { "claude-fable-5-1": "m-1", "fusion:claude-fable-5-1": "m-1" },
      has_new: false,
    };
    expect(assertModelsResponse(resp)).toEqual(resp);
  });

  it("accepts models with null/absent optional fusion fields", () => {
    const resp = {
      models: [{ ...validModel, reasoning: null, is_fast: true, fusion: null }],
      selectable: [],
    };
    expect(assertModelsResponse(resp)).toEqual(resp);
  });

  it("rejects a non-object fusion field", () => {
    const resp = {
      models: [{ ...validModel, fusion: "bad" }],
      selectable: [],
    };
    expect(() => assertModelsResponse(resp)).toThrow(/Invalid models response/);
  });

  it("rejects last_used_variants with non-string values", () => {
    const resp = {
      models: [],
      selectable: [],
      last_used_variants: { fam: 42 },
    };
    expect(() => assertModelsResponse(resp)).toThrow(/Invalid models response/);
  });
});
