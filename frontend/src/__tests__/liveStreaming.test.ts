/**
 * Regression tests for live token streaming.
 *
 * Every frame below is the exact shape the backend emits after normalizing a
 * real `devin acp` `session/update` notification. The original bug was that
 * the backend forwarded ACP's payload with `content: {type, text}` still
 * attached; `assertWSInbound` requires `content` to be an array, so it threw
 * on all 282 chunks of a turn and the UI showed nothing until the database
 * watcher loaded the finished message.
 */
import { afterEach, describe, it, expect, vi } from "vitest";
import { assertWSInbound } from "../types";
import { chatReducer, initialChatState } from "../hooks/useChatSession";
import { buildChatTimeline } from "../historyTimeline";
import type { WSInbound, ReasoningStep } from "../types";

afterEach(() => vi.useRealTimers());

function ready() {
  return chatReducer(initialChatState, {
    type: "server_message",
    msg: { type: "session_created", sessionId: "s-1" } as WSInbound,
  });
}

function send(state: ReturnType<typeof ready>, msg: unknown) {
  // Route through the real validator so a schema regression fails the test
  // instead of silently dropping frames the way the app does.
  return chatReducer(state, { type: "server_message", msg: assertWSInbound(msg) });
}

const step = (overrides: Partial<ReasoningStep> = {}): ReasoningStep => ({
  step_id: 1,
  reasoning_content: "persisted reasoning",
  timestamp: "2026-07-28T03:00:00Z",
  tool_calls: [],
  message: "",
  metrics: {},
  ...overrides,
});

describe("live streaming", () => {
  it("accepts a normalized agent_message_chunk frame", () => {
    expect(() =>
      assertWSInbound({
        type: "session_update",
        sessionId: "s-1",
        update: { type: "agent_message_chunk", chunk: "Hello" },
      })
    ).not.toThrow();
  });

  it("rejects a frame that still carries ACP's nested content object", () => {
    // This is what the backend used to send. Documents the root cause.
    expect(() =>
      assertWSInbound({
        type: "session_update",
        sessionId: "s-1",
        update: {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text: "Hello" },
          type: "agent_message_chunk",
          chunk: "Hello",
        },
      })
    ).toThrow(/Invalid session_update message/);
  });

  it("accumulates streamed assistant text as it arrives", () => {
    let state = ready();
    for (const chunk of ["The sky ", "appears ", "blue."]) {
      state = send(state, {
        type: "session_update",
        sessionId: "s-1",
        update: { type: "agent_message_chunk", chunk },
      });
    }
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].content).toBe("The sky appears blue.");
    expect(state.messages[0].isStreaming).toBe(true);
    expect(state.status).toBe("streaming");
  });

  it("accumulates streamed reasoning into a single live step", () => {
    let state = ready();
    for (const chunk of ["The", " user", " asks"]) {
      state = send(state, {
        type: "session_update",
        sessionId: "s-1",
        update: { type: "agent_thought_chunk", chunk },
      });
    }
    expect(state.reasoningSteps).toHaveLength(1);
    expect(state.reasoningSteps[0].reasoning_content).toBe("The user asks");
    expect(state.reasoningSteps[0].isLive).toBe(true);
  });

  it("keeps the live reasoning step when reasoning_update arrives mid-turn", () => {
    // The DB watcher fires reasoning_update repeatedly during a turn, often
    // with an empty list. Replacing the array wiped the only live view of
    // what the agent was thinking.
    let state = ready();
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "agent_thought_chunk", chunk: "thinking hard" },
    });
    state = send(state, { type: "reasoning_update", sessionId: "s-1", steps: [] });

    expect(state.reasoningSteps).toHaveLength(1);
    expect(state.reasoningSteps[0].reasoning_content).toBe("thinking hard");

    // Persisted steps are merged in ahead of the still-live one.
    state = send(state, { type: "reasoning_update", sessionId: "s-1", steps: [step()] });
    expect(state.reasoningSteps.map((s) => s.reasoning_content)).toEqual([
      "persisted reasoning",
      "thinking hard",
    ]);
  });

  it("lets persisted reasoning replace the live step once the turn ends", () => {
    let state = ready();
    state = chatReducer(state, {
      type: "prompt_optimistic",
      userMessage: {
        id: "u-1",
        role: "user",
        content: "hi",
        timestamp: "2026-07-28T03:00:00Z",
        isStreaming: false,
      },
      prevStatus: "ready",
      requestId: "req-1",
    });
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "agent_thought_chunk", chunk: "thinking hard" },
    });
    state = send(state, {
      type: "turn_complete",
      sessionId: "s-1",
      stopReason: "end_turn",
      result: {},
      requestId: "req-1",
    });
    // Kept so the panel does not blank out before the transcript lands...
    expect(state.reasoningSteps).toHaveLength(1);
    expect(state.reasoningSteps[0].isLive).toBe(false);
    // ...but no longer protected, so the persisted version replaces it.
    state = send(state, { type: "reasoning_update", sessionId: "s-1", steps: [step()] });
    expect(state.reasoningSteps).toHaveLength(1);
    expect(state.reasoningSteps[0].reasoning_content).toBe("persisted reasoning");
  });

  it("starts a new live reasoning step after a tool call", () => {
    let state = ready();
    for (const chunk of ["first", " reasoning"]) {
      state = send(state, {
        type: "session_update",
        sessionId: "s-1",
        update: { type: "agent_thought_chunk", chunk },
      });
    }
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: {
        type: "tool_call",
        toolCallId: "t-1",
        title: "Run command",
        kind: "execute",
      },
    });
    for (const chunk of ["second", " reasoning"]) {
      state = send(state, {
        type: "session_update",
        sessionId: "s-1",
        update: { type: "agent_thought_chunk", chunk },
      });
    }
    expect(state.reasoningSteps).toHaveLength(2);
    expect(state.reasoningSteps[0].reasoning_content).toBe("first reasoning");
    expect(state.reasoningSteps[0].isLive).toBe(false);
    expect(state.reasoningSteps[1].reasoning_content).toBe("second reasoning");
    expect(state.reasoningSteps[1].isLive).toBe(true);
  });

  it("settles live reasoning when the assistant starts answering", () => {
    let state = ready();
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "agent_thought_chunk", chunk: "thinking" },
    });
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "agent_message_chunk", chunk: "The answer" },
    });
    expect(state.reasoningSteps).toHaveLength(1);
    expect(state.reasoningSteps[0].reasoning_content).toBe("thinking");
    expect(state.reasoningSteps[0].isLive).toBe(false);
    expect(state.messages[0].content).toBe("The answer");
  });

  it("places each tool call and text block at its own arrival position", () => {
    // Regression: every tool call and text chunk used to accumulate onto a
    // single assistant message anchored at the FIRST tool call's arrival
    // time, so the timeline rendered newer calls and text at that old spot
    // while newer reasoning panels appeared below them.
    let state = ready();
    const update = (u: unknown) => {
      state = send(state, { type: "session_update", sessionId: "s-1", update: u });
    };

    update({ type: "agent_thought_chunk", chunk: "first reasoning" });
    update({ type: "tool_call", toolCallId: "t-1", title: "Read file", kind: "read" });
    update({ type: "agent_thought_chunk", chunk: "second reasoning" });
    update({ type: "tool_call", toolCallId: "t-2", title: "Run command", kind: "execute" });
    update({ type: "agent_message_chunk", chunk: "The " });
    update({ type: "agent_message_chunk", chunk: "answer" });

    // Each tool call anchors its own message instead of clustering.
    expect(state.messages).toHaveLength(3);
    expect(state.messages.map((m) => m.toolCalls?.[0]?.id)).toEqual(["t-1", "t-2", undefined]);
    expect(state.messages[2].content).toBe("The answer");

    // The rendered timeline is chronological: reasoning, call, reasoning,
    // call, text.
    const timeline = buildChatTimeline(state.messages, state.reasoningSteps);
    expect(timeline.map((item) => item.kind)).toEqual([
      "reasoning",
      "message",
      "reasoning",
      "message",
      "message",
    ]);
    const carriers = timeline.filter((item) => item.kind === "message");
    expect(carriers[0].kind === "message" && carriers[0].message.toolCalls?.[0]?.id).toBe("t-1");
    expect(carriers[1].kind === "message" && carriers[1].message.toolCalls?.[0]?.id).toBe("t-2");
    expect(carriers[2].kind === "message" && carriers[2].message.content).toBe("The answer");
  });

  it("starts a new text block when text resumes after a tool call", () => {
    let state = ready();
    const update = (u: unknown) => {
      state = send(state, { type: "session_update", sessionId: "s-1", update: u });
    };

    update({ type: "agent_message_chunk", chunk: "before" });
    update({ type: "tool_call", toolCallId: "t-1", title: "Read file", kind: "read" });
    update({ type: "agent_message_chunk", chunk: "after" });

    expect(state.messages.map((m) => m.content)).toEqual(["before", "", "after"]);
    expect(state.messages[2].isStreaming).toBe(true);
  });

  it.each([undefined, "generation-1"])(
    "keeps a final answer after intervening reasoning, even with a reused message id (%s)",
    (messageId) => {
      // Force every block to share a timestamp: wall-clock ties must not
      // reorder text and reasoning that arrived in a known sequence.
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-10-07T23:51:33Z"));
      let state = ready();
      const update = (u: unknown) => {
        state = send(state, { type: "session_update", sessionId: "s-1", update: u });
      };

      update({ type: "agent_message_chunk", messageId, chunk: "Progress report." });
      update({ type: "agent_thought_chunk", chunk: "Preparing the final summary." });
      // Usage updates and persistence polls must not erase the text boundary.
      update({ type: "usage_update", used: 100, size: 1000 });
      state = send(state, { type: "reasoning_update", sessionId: "s-1", steps: [] });
      update({ type: "agent_message_chunk", messageId, chunk: "Final " });
      update({ type: "agent_message_chunk", messageId, chunk: "summary." });

      expect(state.messages.map((message) => message.content)).toEqual([
        "Progress report.", "Final summary.",
      ]);
      expect(state.messages.every((message) => message.isStreaming)).toBe(true);
      const timeline = buildChatTimeline(state.messages, state.reasoningSteps);
      expect(timeline.map((item) => item.kind === "message"
        ? item.message.content : item.step.reasoning_content)).toEqual([
        "Progress report.", "Preparing the final summary.", "Final summary.",
      ]);
      state = send(state, {
        type: "turn_complete", sessionId: "s-1", requestId: "p-final",
        stopReason: "end_turn", result: { stopReason: "end_turn" },
      });
      expect(state.status).toBe("ready");
      expect(state.messages.every((message) => !message.isStreaming)).toBe(true);
      expect(state.reasoningSteps.every((step) => !step.isLive)).toBe(true);
    }
  );

  it("merges a repeated tool_call frame instead of duplicating the tool card", () => {
    let state = ready();
    const first = {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "tool_call", toolCallId: "t-1", title: "Read file", kind: "read" },
    };
    state = send(state, first);
    state = send(state, {
      ...first,
      update: { type: "tool_call", toolCallId: "t-1", title: "Read file again", kind: "read" },
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].toolCalls).toHaveLength(1);
    expect(state.messages[0].toolCalls?.[0]).toMatchObject({
      id: "t-1",
      title: "Read file again",
      kind: "read",
    });
  });

  it("keeps tool output when a status-only tool_call_update arrives", () => {
    let state = ready();
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "tool_call", toolCallId: "t-1", title: "Ran command", kind: "execute" },
    });
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: {
        type: "tool_call_update",
        toolCallId: "t-1",
        status: "in_progress",
        content: [{ text: "Name\n----" }],
      },
    });
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "tool_call_update", toolCallId: "t-1", status: "completed" },
    });

    const toolCall = state.messages.flatMap((m) => m.toolCalls ?? [])[0];
    expect(toolCall.status).toBe("completed");
    expect(toolCall.content).toEqual([{ text: "Name\n----" }]);
  });

  const dbNode = (overrides: Record<string, unknown>) => ({
    row_id: 1,
    node_id: "node-1",
    parent_node_id: null,
    created_at: "2026-08-05T02:13:00Z",
    role: "user",
    content: "hi",
    message_id: null,
    metrics: null,
    finish_reason: null,
    metadata: null,
    ...overrides,
  });

  const beginDbReconcileTurn = (state: ReturnType<typeof ready>, timestamp = "2026-08-05T02:13:00Z") =>
    chatReducer(state, {
      type: "prompt_optimistic",
      userMessage: {
        id: "visible-user",
        role: "user",
        content: "visible prompt",
        timestamp,
        isStreaming: false,
      },
      prevStatus: "ready",
      requestId: "req-db-reconcile",
    });

  it("does not render the DB-persisted in-flight draft alongside the live stream", () => {
    // Regression: the DB watcher persists the growing assistant draft mid-turn
    // and pushes it via db_message_update. The draft's node id never matches
    // the live turn ids, so an id-only dedupe rendered every text block twice
    // (once from the DB, once from the live stream) until the turn completed.
    let state = beginDbReconcileTurn(ready());
    const update = (u: unknown) => {
      state = send(state, { type: "session_update", sessionId: "s-1", update: u });
    };

    update({ type: "agent_message_chunk", chunk: "First part. " });
    update({ type: "tool_call", toolCallId: "t-1", title: "Read file", kind: "read" });
    update({ type: "agent_message_chunk", chunk: "Second part." });

    state = send(state, {
      type: "db_message_update",
      sessionId: "s-1",
      messages: [
        dbNode({ row_id: 1, node_id: "user-1" }),
        dbNode({ row_id: 2, node_id: "draft-1", role: "assistant", content: "First part. ", created_at: "2026-08-05T02:13:01Z" }),
        dbNode({ row_id: 3, node_id: "draft-2", role: "assistant", content: "Second part.", created_at: "2026-08-05T02:13:02Z" }),
      ],
    });

    // The persisted drafts are dropped in favor of the live stream: one user
    // message, two live text blocks, and the tool-call carrier.
    expect(state.messages).toHaveLength(4);
    const texts = state.messages.filter((m) => m.role === "assistant" && m.content);
    expect(texts.map((m) => m.content)).toEqual(["First part. ", "Second part."]);

    // The rendered timeline shows each text block exactly once.
    const timeline = buildChatTimeline(state.messages, state.reasoningSteps);
    const rendered = timeline.map((item) =>
      item.kind === "message" ? item.message.content : ""
    );
    expect(rendered.filter((c) => c === "First part. ")).toHaveLength(1);
    expect(rendered.filter((c) => c === "Second part.")).toHaveLength(1);
  });

  it("dedupes an in-flight DB draft that is slightly ahead of the live stream", () => {
    // The DB and ACP stream are independent. SQLite can occasionally persist
    // a few characters before the corresponding WebSocket chunks arrive.
    // Treat that longer DB row as the same draft rather than rendering both.
    let state = beginDbReconcileTurn(ready());
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "agent_message_chunk", chunk: "Hello wor" },
    });

    state = send(state, {
      type: "db_message_update",
      sessionId: "s-1",
      messages: [
        dbNode({ row_id: 1, node_id: "user-1" }),
        dbNode({ row_id: 2, node_id: "draft-1", role: "assistant", content: "Hello world", created_at: "2026-08-05T02:13:01Z" }),
      ],
    });

    expect(state.messages.map((m) => [m.role, m.content])).toEqual([
      ["user", "hi"],
      ["assistant", "Hello wor"],
    ]);
  });

  it("keeps DB history when old text coincidentally reappears in the live stream", () => {
    // The draft dedupe must only apply to the in-flight region (after the
    // last user message). A previous turn's assistant row whose text happens
    // to be a substring of the current stream is history, not a duplicate.
    let state = beginDbReconcileTurn(ready(), "2026-08-05T02:13:10Z");
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "agent_message_chunk", chunk: "Done. Again." },
    });

    state = send(state, {
      type: "db_message_update",
      sessionId: "s-1",
      messages: [
        dbNode({ row_id: 1, node_id: "user-old", content: "first question", created_at: "2026-08-05T02:13:00Z" }),
        dbNode({ row_id: 2, node_id: "asst-old", role: "assistant", content: "Done.", created_at: "2026-08-05T02:13:01Z" }),
        dbNode({ row_id: 3, node_id: "user-new", content: "second question", created_at: "2026-08-05T02:13:10Z" }),
      ],
    });

    expect(state.messages.map((m) => [m.role, m.content])).toEqual([
      ["user", "first question"],
      ["assistant", "Done."],
      ["user", "second question"],
      ["assistant", "Done. Again."],
    ]);
  });

  it("ignores internal DB user nodes when deduping the current live turn", () => {
    // The supplied debug trace shows Devin inserting internal/subagent user
    // rows while a single browser-issued turn is still active. The old logic
    // used the *last DB user row* as its boundary, so a persisted copy of live
    // assistant output that appeared before that internal row was rendered a
    // second time. The browser's prompt timestamp is the stable boundary.
    let state = beginDbReconcileTurn(ready());
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: {
        type: "agent_message_chunk",
        chunk: "Excellent research. Now I need to handle timestamps.",
      },
    });

    state = send(state, {
      type: "db_message_update",
      sessionId: "s-1",
      messages: [
        dbNode({ row_id: 1, node_id: "visible-user", content: "visible prompt" }),
        dbNode({
          row_id: 2,
          node_id: "persisted-live-copy",
          role: "assistant",
          content: "Excellent research. Now I need to handle timestamps.",
          created_at: "2026-08-05T02:13:01Z",
        }),
        dbNode({
          row_id: 3,
          node_id: "internal-user",
          role: "user",
          content: "Internal subagent/compaction prompt",
          created_at: "2026-08-05T02:13:02Z",
        }),
      ],
    });

    const copies = state.messages.filter(
      (message) => message.content === "Excellent research. Now I need to handle timestamps."
    );
    expect(copies).toHaveLength(1);
    expect(copies[0].isStreaming).toBe(true);
  });

  it("does not duplicate a live reasoning block when persistence catches up mid-turn", () => {
    let state = beginDbReconcileTurn(ready());
    state = send(state, {
      type: "session_update",
      sessionId: "s-1",
      update: { type: "agent_thought_chunk", chunk: "I need to compare the timestamps carefully." },
    });
    state = send(state, {
      type: "reasoning_update",
      sessionId: "s-1",
      steps: [
        step({
          reasoning_content: "I need to compare the timestamps carefully.",
          timestamp: "2026-08-05T02:13:01Z",
        }),
      ],
    });

    expect(state.reasoningSteps).toHaveLength(1);
    expect(state.reasoningSteps[0].isLive).toBe(true);
  });
});
