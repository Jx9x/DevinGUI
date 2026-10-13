import { describe, it, expect, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { chatReducer, initialChatState, useChatSession, requestIdMatches } from "./useChatSession";
import type {
  ChatState,
  PermissionRequest,
} from "./useChatSession";
import type {
  ConversationCostEstimate,
  ErrorOperation,
  UIMessage,
  WSInbound,
  WSOutbound,
  SendResult,
  MessageNode,
  SessionUpdate,
} from "../types";
import { assertWSInbound } from "../types";

const userMessage: UIMessage = {
  id: "user-1",
  role: "user",
  content: "hello",
  timestamp: "2026-07-13T00:00:00Z",
  isStreaming: false,
};

function makeHistoryNode(overrides?: Partial<MessageNode>): MessageNode {
  return {
    row_id: 1,
    node_id: overrides?.node_id ?? "node-1",
    parent_node_id: null,
    created_at: "2026-07-13T00:00:00Z",
    role: "user",
    content: "hello",
    message_id: null,
    metrics: null,
    finish_reason: null,
    metadata: null,
    ...overrides,
  } as MessageNode;
}

describe("chatReducer", () => {
  it("preserves main-turn indices and internal-prompt markers from history", () => {
    const selected = chatReducer(initialChatState, {
      type: "select_session", sessionId: "session-1", requestId: "load-1",
    });
    const loaded = chatReducer(selected, { type: "server_message", msg: assertWSInbound({
      type: "history_loaded", sessionId: "session-1", requestId: "load-1", reasoning: [],
      messages: [
        makeHistoryNode({ node_id: "main-prompt", turn_index: 2 }),
        makeHistoryNode({ node_id: "sidekick-prompt", turn_index: 0 }),
      ],
    }) });
    expect(loaded.messages.map((message) => message.turnIndex)).toEqual([2, 0]);
  });

  it("restores image-only history and keeps tool images during live DB updates", () => {
    const nodes = [makeHistoryNode({content: "", images: [{index: 2, mime_type: "image/png", width: 10, height: 20}]})];
    const selected = chatReducer(initialChatState, {type: "select_session", sessionId: "session-1", requestId: "load-1"});
    const loaded = chatReducer(selected, {type: "server_message", msg: assertWSInbound({
      type: "history_loaded", sessionId: "session-1", requestId: "load-1", messages: nodes, reasoning: [],
    })});
    expect(loaded.messages[0].attachments?.[0]).toEqual({src: "/api/message-nodes/1/images/2", mimeType: "image/png", width: 10, height: 20});
    const live = chatReducer({...loaded, status: "streaming"}, {type: "server_message", msg: assertWSInbound({
      type: "db_message_update", sessionId: "session-1", messages: [...nodes, makeHistoryNode({row_id: 2, node_id: "tool-2", role: "tool", content: null, images: [{index: 0, mime_type: "image/jpeg"}]})],
    })});
    expect(live.messages).toHaveLength(2);
    expect(live.messages[1].attachments?.[0].src).toBe("/api/message-nodes/2/images/0");
  });

  it("keeps optimistic pasted images through an early poll then replaces them with stored URLs", () => {
    const local: UIMessage = {...userMessage, content: "", timestamp: "2026-07-13T00:00:00.123Z", attachments: [{data: "cG5n", mimeType: "image/png"}]};
    const early = chatReducer({...initialChatState, activeSessionId: "session-1", status: "streaming", messages: [local]}, {
      type: "server_message", msg: {type: "db_message_update", sessionId: "session-1", messages: []},
    });
    expect(early.messages).toEqual([local]);
    const persisted = chatReducer(early, {type: "server_message", msg: {
      type: "db_message_update", sessionId: "session-1", messages: [makeHistoryNode({content: "[Image 1]", images: [{index: 0, mime_type: "image/png"}]})],
    }});
    expect(persisted.messages).toHaveLength(1);
    expect(persisted.messages[0].attachments?.[0].src).toBe("/api/message-nodes/1/images/0");
  });

  it("replaces a pasted image prompt when Devin persists a filename marker", () => {
    const prompt = "Please check the example image";
    const local: UIMessage = {
      ...userMessage,
      content: prompt,
      timestamp: "2026-09-25T09:50:49.403Z",
      attachments: [{ data: "cG5n", mimeType: "image/png" }],
    };
    const state = {
      ...initialChatState,
      activeSessionId: "session-1",
      status: "streaming" as const,
      messages: [local],
    };
    const stored = makeHistoryNode({
      content: `[Image 1: C:\\Users\\Example\\AppData\\Local\\Temp\\devin-pasted-images\\pasted.png]\n\n${prompt}`,
      created_at: "2026-09-25T09:50:49.402Z",
      images: [{ index: 0, mime_type: "image/png" }],
    });

    const updated = chatReducer(state, {
      type: "server_message",
      msg: { type: "db_message_update", sessionId: "session-1", messages: [stored] },
    });
    expect(updated.messages).toHaveLength(1);
    expect(updated.messages[0].content).toBe(prompt);
    expect(updated.messages[0].attachments?.[0].src).toBe("/api/message-nodes/1/images/0");

    const completed = chatReducer(updated, {
      type: "server_message",
      msg: { type: "turn_complete", sessionId: "session-1", result: {} } as WSInbound,
    });
    expect(completed.messages).toHaveLength(1);

    const selected = chatReducer(initialChatState, {
      type: "select_session", sessionId: "session-1", requestId: "load-1",
    });
    const reopened = chatReducer(selected, {
      type: "server_message",
      msg: {
        type: "history_loaded", sessionId: "session-1", requestId: "load-1",
        messages: [stored], reasoning: [],
      },
    });
    expect(reopened.messages).toHaveLength(1);
    expect(reopened.messages[0].content).toBe(prompt);
  });

  it("switches to a session and loads history", () => {
    const selected = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "req-load-1",
    });
    expect(selected.activeSessionId).toBe("session-1");
    expect(selected.status).toBe("loading_history");
    expect(selected.messages).toHaveLength(0);

    const historyNode: MessageNode = makeHistoryNode({
      node_id: "node-1",
      role: "assistant",
      content: "previous response",
    });
    const loaded = chatReducer(selected, {
      type: "server_message",
      msg: {
        type: "history_loaded",
        sessionId: "session-1",
        messages: [historyNode],
        reasoning: [],
        requestId: "req-load-1",
      } as WSInbound,
    });
    expect(loaded.status).toBe("resuming");
    expect(loaded.messages).toHaveLength(1);
    expect(loaded.messages[0].role).toBe("assistant");

    const resumed = chatReducer(loaded, {
      type: "server_message",
      msg: {
        type: "session_resumed",
        sessionId: "session-1",
        requestId: "req-load-1",
      } as WSInbound,
    });
    expect(resumed.status).toBe("ready");
  });

  it("keeps explicitly viewed history read-only until the session is started", () => {
    const selected = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "req-view-1",
    });
    const loaded = chatReducer(selected, {
      type: "server_message",
      msg: {
        type: "history_loaded",
        sessionId: "session-1",
        messages: [makeHistoryNode()],
        reasoning: [],
        readOnly: true,
        requestId: "req-view-1",
      } as WSInbound,
    });

    expect(loaded.status).toBe("paused_readonly");
    expect(loaded.messages).toHaveLength(1);
  });

  it("streams assistant message chunks", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: {
        type: "session_created",
        sessionId: "session-1",
      } as WSInbound,
    });

    const update: SessionUpdate = {
      type: "agent_message_chunk",
      messageId: "stream-1",
      chunk: "Hello",
    };
    const first = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update,
      } as WSInbound,
    });
    expect(first.status).toBe("streaming");
    expect(first.messages).toHaveLength(1);
    expect(first.messages[0].content).toBe("Hello");
    expect(first.messages[0].isStreaming).toBe(true);

    const next = chatReducer(first, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: { ...update, chunk: " world" },
      } as WSInbound,
    });
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].content).toBe("Hello world");
  });

  it("tracks exact latest-inference context and allows compaction to reduce it", () => {
    const ready = {
      ...initialChatState,
      activeSessionId: "session-1",
      status: "ready" as const,
    };
    const estimate = (used: number) =>
      ({ context: { used, size: 200000 } }) as ConversationCostEstimate;

    const persisted = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "cost_update",
        sessionId: "session-1",
        estimate: estimate(170000),
      },
    });
    expect(persisted.usage.used).toBe(170000);

    const live = chatReducer(persisted, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: {
          type: "usage_update",
          used: 83755,
          size: 200000,
          inputTokens: 83312,
        },
      },
    });
    expect(live.usage.used).toBe(83312);

    const delayedPersistence = chatReducer(live, {
      type: "server_message",
      msg: {
        type: "cost_update",
        sessionId: "session-1",
        estimate: estimate(166000),
      },
    });
    expect(delayedPersistence.usage.used).toBe(83312);

    const compacted = chatReducer(delayedPersistence, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: {
          type: "usage_update",
          used: 30400,
          size: 200000,
          inputTokens: 30000,
        },
      },
    });
    expect(compacted.usage.used).toBe(30000);
  });

  it("transitions to cancelling on cancel and back to ready on turn_complete", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const streaming = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
    });
    const update: SessionUpdate = {
      type: "agent_message_chunk",
      messageId: "stream-1",
      chunk: "typing",
    };
    const withAssistant = chatReducer(streaming, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update,
      } as WSInbound,
    });
    expect(withAssistant.status).toBe("streaming");
    expect(withAssistant.messages[1].isStreaming).toBe(true);

    const cancelling = chatReducer(withAssistant, { type: "cancel" });
    expect(cancelling.status).toBe("cancelling");
    // Streaming messages remain streaming until the backend confirms.
    expect(cancelling.messages[1].isStreaming).toBe(true);

    const completed = chatReducer(cancelling, {
      type: "server_message",
      msg: { type: "turn_complete", sessionId: "session-1", result: {} } as WSInbound,
    });
    expect(completed.status).toBe("ready");
    expect(completed.messages[1].isStreaming).toBe(false);
  });

  it("transitions from cancelling to ready on a correlated prompt error", () => {
    // Set up a streaming prompt with a known requestId so a correlated
    // prompt error can end the turn.
    const prompt = chatReducer(
      { ...initialChatState, activeSessionId: "session-1", status: "ready" },
      { type: "prompt_optimistic", userMessage, prevStatus: "ready", requestId: "req-prompt-1" }
    );
    const cancelling = chatReducer(prompt, { type: "cancel", requestId: "req-cancel-1" });
    expect(cancelling.status).toBe("cancelling");
    const errored = chatReducer(cancelling, {
      type: "server_message",
      msg: {
        type: "error",
        sessionId: "session-1",
        requestId: "req-prompt-1",
        operation: "prompt",
        message: "boom",
      } as WSInbound,
    });
    expect(errored.status).toBe("ready");
    expect(errored.error).toBe("boom");
  });

  it("leaves cancelling on turn_cancelled and clears streaming flags", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const streaming = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
    });
    const withAssistant = chatReducer(streaming, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: {
          type: "agent_message_chunk",
          messageId: "stream-1",
          chunk: "typing",
        },
      } as WSInbound,
    });
    expect(withAssistant.messages[1].isStreaming).toBe(true);

    const cancelling = chatReducer(withAssistant, {
      type: "cancel",
      requestId: "req-cancel-1",
    });
    expect(cancelling.status).toBe("cancelling");

    const cancelled = chatReducer(cancelling, {
      type: "server_message",
      msg: {
        type: "turn_cancelled",
        sessionId: "session-1",
        requestId: "req-cancel-1",
      } as WSInbound,
    });
    expect(cancelled.status).toBe("ready");
    expect(cancelled.messages[1].isStreaming).toBe(false);
    expect(cancelled.error).toBeNull();
    expect(cancelled.currentTurnId).toBeNull();
    expect(cancelled.turnToolCallSeq).toBe(0);
  });

  it("ignores turn_cancelled with a non-matching requestId", () => {
    const cancelling = chatReducer(
      { ...initialChatState, activeSessionId: "session-1", status: "streaming" },
      { type: "cancel", requestId: "req-cancel-current" }
    );
    expect(cancelling.status).toBe("cancelling");

    const stale = chatReducer(cancelling, {
      type: "server_message",
      msg: {
        type: "turn_cancelled",
        sessionId: "session-1",
        requestId: "req-cancel-stale",
      } as WSInbound,
    });
    // Stale acknowledgment must not transition out of cancelling.
    expect(stale).toBe(cancelling);
    expect(stale.status).toBe("cancelling");
  });

  it("rejects turn_cancelled without a requestId when a cancel request is outstanding", () => {
    const cancelling = chatReducer(
      { ...initialChatState, activeSessionId: "session-1", status: "streaming" },
      { type: "cancel", requestId: "req-cancel-1" }
    );
    // Strict correlation: a turn_cancelled without requestId is rejected
    // when the frontend has an outstanding cancel request.
    const rejected = chatReducer(cancelling, {
      type: "server_message",
      msg: {
        type: "turn_cancelled",
        sessionId: "session-1",
      } as WSInbound,
    });
    expect(rejected).toBe(cancelling);
    expect(rejected.status).toBe("cancelling");
  });

  it("accepts an uncorrelated turn_complete when this page issued no prompt", () => {
    // After a mid-turn reload the reducer is rebuilt with no stored prompt
    // id; the turn_complete that belongs to the orphaned turn carries an
    // unknown requestId and must still settle the UI — dropping it would
    // leave the session streaming (and the Stop button visible) forever.
    const streaming = {
      ...initialChatState,
      activeSessionId: "session-1",
      status: "streaming" as const,
      messages: [
        { ...userMessage },
        {
          id: "assistant-1",
          role: "assistant" as const,
          content: "partial",
          timestamp: "2026-07-13T00:00:01Z",
          isStreaming: true,
        },
      ],
    };
    const completed = chatReducer(streaming, {
      type: "server_message",
      msg: {
        type: "turn_complete",
        sessionId: "session-1",
        result: {},
        requestId: "req-from-previous-page",
      } as WSInbound,
    });
    expect(completed.status).toBe("ready");
    expect(completed.messages[1].isStreaming).toBe(false);
  });

  it("still rejects a turn_complete for a different outstanding prompt id", () => {
    const streaming = chatReducer(
      { ...initialChatState, activeSessionId: "session-1", status: "ready" },
      {
        type: "prompt_optimistic",
        userMessage,
        prevStatus: "ready",
        requestId: "req-prompt-2",
      }
    );
    const stale = chatReducer(streaming, {
      type: "server_message",
      msg: {
        type: "turn_complete",
        sessionId: "session-1",
        result: {},
        requestId: "req-prompt-1",
      } as WSInbound,
    });
    expect(stale.status).toBe("streaming");
  });

  it("accepts an uncorrelated turn_cancelled when this page issued no cancel", () => {
    const streaming = {
      ...initialChatState,
      activeSessionId: "session-1",
      status: "streaming" as const,
    };
    const cancelled = chatReducer(streaming, {
      type: "server_message",
      msg: {
        type: "turn_cancelled",
        sessionId: "session-1",
        requestId: "req-other-page",
      } as WSInbound,
    });
    expect(cancelled.status).toBe("ready");
  });

  it("transition_cancelled clears resuming status to paused_readonly without implying a turn was cancelled", () => {
    // Set up a session that is ready, then start a reconnect resume.
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    expect(ready.status).toBe("ready");

    const resuming = chatReducer(ready, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    expect(resuming.status).toBe("resuming");

    // The user cancels the in-flight transition.
    const cancelling = chatReducer(resuming, {
      type: "cancel",
      requestId: "req-cancel-1",
    });
    expect(cancelling.status).toBe("cancelling");

    // The backend reports transition_cancelled (not turn_cancelled).
    const cancelled = chatReducer(cancelling, {
      type: "server_message",
      msg: {
        type: "transition_cancelled",
        sessionId: "session-1",
        requestId: "req-cancel-1",
      } as WSInbound,
    });
    // Status must NOT be "ready" — ACP is not loaded after a transition
    // cancellation. paused_readonly surfaces a non-streaming state so
    // the user is not misled.
    expect(cancelled.status).toBe("paused_readonly");
    expect(cancelled.error).toBeNull();
    expect(cancelled.currentTurnId).toBeNull();
    expect(cancelled.turnToolCallSeq).toBe(0);
  });

  it("transition_cancelled clears loading_history status to paused_readonly", () => {
    // Selecting a session enters loading_history.
    const loading = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "req-lh-1",
    });
    expect(loading.status).toBe("loading_history");

    const cancelling = chatReducer(loading, {
      type: "cancel",
      requestId: "req-cancel-1",
    });
    expect(cancelling.status).toBe("cancelling");

    const cancelled = chatReducer(cancelling, {
      type: "server_message",
      msg: {
        type: "transition_cancelled",
        sessionId: "session-1",
        requestId: "req-cancel-1",
      } as WSInbound,
    });
    expect(cancelled.status).toBe("paused_readonly");
  });

  it("transition_cancelled records cancel as consumed so a stale turn_cancelled is rejected", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const resuming = chatReducer(ready, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    const cancelling = chatReducer(resuming, {
      type: "cancel",
      requestId: "req-cancel-1",
    });

    const cancelled = chatReducer(cancelling, {
      type: "server_message",
      msg: {
        type: "transition_cancelled",
        sessionId: "session-1",
        requestId: "req-cancel-1",
      } as WSInbound,
    });
    expect(cancelled.status).toBe("paused_readonly");

    // A stale turn_cancelled for the same request id must be rejected —
    // the cancel was already consumed by transition_cancelled.
    const staleTurnCancelled = chatReducer(cancelled, {
      type: "server_message",
      msg: {
        type: "turn_cancelled",
        sessionId: "session-1",
        requestId: "req-cancel-1",
      } as WSInbound,
    });
    expect(staleTurnCancelled).toBe(cancelled);
    expect(staleTurnCancelled.status).toBe("paused_readonly");
  });

  it("ignores transition_cancelled with a non-matching requestId", () => {
    const resuming = chatReducer(
      { ...initialChatState, activeSessionId: "session-1", status: "resuming" },
      { type: "cancel", requestId: "req-cancel-current" }
    );
    expect(resuming.status).toBe("cancelling");

    const stale = chatReducer(resuming, {
      type: "server_message",
      msg: {
        type: "transition_cancelled",
        sessionId: "session-1",
        requestId: "req-cancel-stale",
      } as WSInbound,
    });
    // Stale acknowledgment must not transition out of cancelling.
    expect(stale).toBe(resuming);
    expect(stale.status).toBe("cancelling");
  });

  it("ignores errors from a different session", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const errored = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "error",
        sessionId: "session-old",
        operation: "prompt",
        message: "stale boom",
      } as WSInbound,
    });
    // State is unchanged: the error belongs to a previous session.
    expect(errored).toBe(ready);
    expect(errored.error).toBeNull();
  });

  it("ignores errors whose requestId does not match the last prompt", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    // First prompt initiated.
    const firstPrompt = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
      requestId: "req-prompt-1",
    });
    // Second prompt supersedes the first.
    const secondPrompt = chatReducer(firstPrompt, {
      type: "prompt_optimistic",
      userMessage: { ...userMessage, id: "user-2" },
      prevStatus: "streaming",
      requestId: "req-prompt-2",
    });
    // Late error from the first prompt arrives; it must be ignored.
    const errored = chatReducer(secondPrompt, {
      type: "server_message",
      msg: {
        type: "error",
        sessionId: "session-1",
        requestId: "req-prompt-1",
        operation: "prompt",
        message: "late boom",
      } as WSInbound,
    });
    expect(errored).toBe(secondPrompt);
    expect(errored.error).toBeNull();
  });

  it("marks permission as submitting and clears it on permission_received", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    expect(withPermission.permissionRequest).not.toBeNull();
    expect(withPermission.permissionRequest?.submitting).toBeFalsy();

    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
    });
    expect(submitting.permissionRequest?.submitting).toBe(true);
    expect(submitting.permissionRequest?.submittedDecision).toBe("allow");
    // Panel stays visible while waiting for backend ack.
    expect(submitting.permissionRequest).not.toBeNull();

    const acknowledged = chatReducer(submitting, {
      type: "server_message",
      msg: {
        type: "permission_received",
        sessionId: "session-1",
        toolCallId: "tc-1",
        decision: "allow",
      } as WSInbound,
    });
    expect(acknowledged.permissionRequest).toBeNull();
  });

  it("handles session switching and resume failure", () => {
    const s1 = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
    });
    const s2 = chatReducer(s1, {
      type: "select_session",
      sessionId: "session-2",
    });
    expect(s2.activeSessionId).toBe("session-2");
    expect(s2.status).toBe("loading_history");

    const failed = chatReducer(s2, {
      type: "server_message",
      msg: {
        type: "resume_failed",
        sessionId: "session-2",
        message: "not found",
      } as WSInbound,
    });
    expect(failed.status).toBe("paused_readonly");
    expect(failed.error).toContain("not found");
  });

  it("completes a turn and stops streaming", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const streaming = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: {
          type: "agent_message_chunk",
          messageId: "stream-1",
          chunk: "done",
        },
      } as WSInbound,
    });
    const completed = chatReducer(streaming, {
      type: "server_message",
      msg: { type: "turn_complete", sessionId: "session-1", result: {} } as WSInbound,
    });
    expect(completed.status).toBe("ready");
    expect(completed.messages[0].isStreaming).toBe(false);
  });

  it("reverts optimistic prompt when send fails", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const optimistic = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
    });
    expect(optimistic.messages).toHaveLength(1);
    const reverted = chatReducer(optimistic, {
      type: "prompt_send_failed",
      userMessage,
      prevStatus: "ready",
    });
    expect(reverted.messages).toHaveLength(0);
    expect(reverted.status).toBe("ready");
  });

  it("transitions to resuming on reconnect and back to ready on session_resumed", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    expect(ready.status).toBe("ready");

    const resuming = chatReducer(ready, { type: "resume_started" });
    expect(resuming.status).toBe("resuming");
    expect(resuming.activeSessionId).toBe("session-1");
    expect(resuming.error).toBeNull();

    const resumed = chatReducer(resuming, {
      type: "server_message",
      msg: { type: "session_resumed", sessionId: "session-1" } as WSInbound,
    });
    expect(resumed.status).toBe("ready");
  });

  it("synchronous resume_started then immediate session_resumed clears resuming (race fix)", () => {
    // Simulates the fast-backend race: useWebSocket calls onResumeStarted
    // (dispatching resume_started with the exact wire requestId) and the
    // backend's session_resumed arrives immediately after. The final status
    // must be `ready`, not stuck in `resuming`.
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    expect(ready.status).toBe("ready");

    // Synchronous notification from useWebSocket before the wire message.
    const resuming = chatReducer(ready, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    expect(resuming.status).toBe("resuming");
    expect(resuming.lastRequestIds.resume).toBe("req-resume-1");

    // session_resumed with the matching requestId arrives immediately.
    const resumed = chatReducer(resuming, {
      type: "server_message",
      msg: {
        type: "session_resumed",
        sessionId: "session-1",
        requestId: "req-resume-1",
      } as WSInbound,
    });
    expect(resumed.status).toBe("ready");
  });

  it("transitions to paused_readonly when resume fails after reconnect", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const resuming = chatReducer(ready, { type: "resume_started" });
    const failed = chatReducer(resuming, {
      type: "server_message",
      msg: {
        type: "resume_failed",
        sessionId: "session-1",
        message: "gone",
      } as WSInbound,
    });
    expect(failed.status).toBe("paused_readonly");
    expect(failed.error).toContain("gone");
  });

  it("ignores resume_started when there is no active session", () => {
    const state = chatReducer(initialChatState, { type: "resume_started" });
    expect(state).toBe(initialChatState);
  });

  it("keeps a queued prompt queued through resume_started and streaming through session_resumed", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-q1",
    });
    expect(queued.status).toBe("queued");
    expect(queued.queuedRequestId).toBe("req-q1");
    expect(queued.queuedUserMessageId).toBe("user-1");

    const resuming = chatReducer(queued, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    expect(resuming.status).toBe("queued");
    expect(resuming.lastRequestIds.resume).toBe("req-resume-1");
    expect(resuming.queuedRequestId).toBe("req-q1");
    expect(resuming.queuedUserMessageId).toBe("user-1");

    const flushed = chatReducer(resuming, {
      type: "prompt_flushed",
      requestId: "req-q1",
    });
    expect(flushed.status).toBe("streaming");
    expect(flushed.queuedRequestId).toBeNull();
    expect(flushed.queuedUserMessageId).toBeNull();

    const resumed = chatReducer(flushed, {
      type: "server_message",
      msg: {
        type: "session_resumed",
        sessionId: "session-1",
        requestId: "req-resume-1",
      } as WSInbound,
    });
    expect(resumed.status).toBe("streaming");
    expect(resumed.messages.filter((m) => m.id === "user-1")).toHaveLength(1);
  });

  it("drops a queued prompt on resume_failed after reconnect and lands paused_readonly", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-q1",
    });
    const resuming = chatReducer(queued, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    const dropped = chatReducer(resuming, {
      type: "prompt_queue_dropped",
      requestId: "req-q1",
      error: "Connection could not be resumed; your prompt was not sent.",
    });
    expect(dropped.status).toBe("ready");
    expect(dropped.messages).toHaveLength(0);
    expect(dropped.queuedRequestId).toBeNull();
    expect(dropped.queuedUserMessageId).toBeNull();

    const failed = chatReducer(dropped, {
      type: "server_message",
      msg: {
        type: "resume_failed",
        sessionId: "session-1",
        message: "gone",
        requestId: "req-resume-1",
      } as WSInbound,
    });
    expect(failed.status).toBe("paused_readonly");
    expect(failed.error).toBe("Could not resume session: gone");
    expect(failed.messages).toHaveLength(0);
  });

  it("ends a cancelled queued prompt at ready with no user message after the resume ack", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-q1",
    });
    const resuming = chatReducer(queued, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    const dropped = chatReducer(resuming, {
      type: "prompt_queue_dropped",
      requestId: "req-q1",
      error: null,
    });
    expect(dropped.status).toBe("ready");
    expect(dropped.error).toBeNull();
    expect(dropped.messages).toHaveLength(0);

    const resumed = chatReducer(dropped, {
      type: "server_message",
      msg: {
        type: "session_resumed",
        sessionId: "session-1",
        requestId: "req-resume-1",
      } as WSInbound,
    });
    expect(resumed.status).toBe("ready");
    expect(resumed.messages).toHaveLength(0);
  });

  it("ends an expired queued prompt at ready with its timeout error after the resume ack", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-q1",
    });
    const resuming = chatReducer(queued, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    const dropped = chatReducer(resuming, {
      type: "prompt_queue_dropped",
      requestId: "req-q1",
      error: "Your prompt timed out waiting for the connection.",
    });
    expect(dropped.status).toBe("ready");
    expect(dropped.messages).toHaveLength(0);

    const resumed = chatReducer(dropped, {
      type: "server_message",
      msg: {
        type: "session_resumed",
        sessionId: "session-1",
        requestId: "req-resume-1",
      } as WSInbound,
    });
    expect(resumed.status).toBe("ready");
    expect(resumed.error).toBe("Your prompt timed out waiting for the connection.");
    expect(resumed.messages).toHaveLength(0);
  });

  it("ignores prompt_flushed and prompt_queue_dropped for stale request ids across a resume", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-q1",
    });
    const resuming = chatReducer(queued, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    const staleFlush = chatReducer(resuming, {
      type: "prompt_flushed",
      requestId: "req-stale",
    });
    expect(staleFlush).toBe(resuming);
    const staleDrop = chatReducer(resuming, {
      type: "prompt_queue_dropped",
      requestId: "req-stale",
      error: "should not land",
    });
    expect(staleDrop).toBe(resuming);
    expect(resuming.status).toBe("queued");
    expect(resuming.queuedRequestId).toBe("req-q1");
    expect(resuming.queuedUserMessageId).toBe("user-1");
    expect(resuming.messages).toHaveLength(1);
  });

  it("keeps a streamed active turn through resume_started and session_resumed until turn_complete", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const streaming = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
      requestId: "req-p1",
    });
    const streamed = chatReducer(streaming, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: {
          type: "agent_message_chunk",
          messageId: "m1",
          chunk: "hi",
        },
      } as WSInbound,
    });
    expect(streamed.status).toBe("streaming");
    expect(streamed.messages[1]?.isStreaming).toBe(true);
    const turnId = streamed.currentTurnId;
    expect(turnId).not.toBeNull();

    const resuming = chatReducer(streamed, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    expect(resuming.status).toBe("streaming");
    expect(resuming.currentTurnId).toBe(turnId);
    expect(resuming.messages[1]?.isStreaming).toBe(true);

    const resumed = chatReducer(resuming, {
      type: "server_message",
      msg: {
        type: "session_resumed",
        sessionId: "session-1",
        requestId: "req-resume-1",
      } as WSInbound,
    });
    expect(resumed.status).toBe("streaming");
    expect(resumed.currentTurnId).toBe(turnId);
    expect(resumed.messages[1]?.isStreaming).toBe(true);

    const done = chatReducer(resumed, {
      type: "server_message",
      msg: {
        type: "turn_complete",
        sessionId: "session-1",
        result: {},
        requestId: "req-p1",
      } as WSInbound,
    });
    expect(done.status).toBe("ready");
    expect(done.currentTurnId).toBeNull();
    expect(done.messages[1]?.isStreaming).toBe(false);
  });

  it("keeps cancelling status through resume_started and session_resumed until turn_cancelled", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const streaming = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
      requestId: "req-p1",
    });
    const cancelling = chatReducer(streaming, {
      type: "cancel",
      requestId: "req-c1",
    });
    expect(cancelling.status).toBe("cancelling");

    const resuming = chatReducer(cancelling, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    expect(resuming.status).toBe("cancelling");

    const resumed = chatReducer(resuming, {
      type: "server_message",
      msg: {
        type: "session_resumed",
        sessionId: "session-1",
        requestId: "req-resume-1",
      } as WSInbound,
    });
    expect(resumed.status).toBe("cancelling");

    const cancelled = chatReducer(resumed, {
      type: "server_message",
      msg: {
        type: "turn_cancelled",
        sessionId: "session-1",
        requestId: "req-c1",
      } as WSInbound,
    });
    expect(cancelled.status).toBe("ready");
    expect(cancelled.currentTurnId).toBeNull();
  });

  it("gives consecutive prompts distinct assistant streaming messages", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });

    // First prompt (turn 0).
    const prompt0 = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage: { ...userMessage, id: "user-session-1-0-hello" },
      prevStatus: "ready",
    });
    const chunk0 = chatReducer(prompt0, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: { type: "agent_message_chunk", chunk: "first" },
      } as WSInbound,
    });
    expect(chunk0.messages).toHaveLength(2);
    const firstAssistant = chunk0.messages[1];
    expect(firstAssistant.id).toBe("turn-session-1-0");
    expect(firstAssistant.content).toBe("first");

    // Turn completes; per-turn id rotates.
    const done0 = chatReducer(chunk0, {
      type: "server_message",
      msg: { type: "turn_complete", sessionId: "session-1", result: {} } as WSInbound,
    });
    expect(done0.currentTurnId).toBeNull();

    // Second prompt (turn 1) must not append to the first assistant message.
    const prompt1 = chatReducer(done0, {
      type: "prompt_optimistic",
      userMessage: { ...userMessage, id: "user-session-1-1-hello" },
      prevStatus: "ready",
    });
    const chunk1 = chatReducer(prompt1, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: { type: "agent_message_chunk", chunk: "second" },
      } as WSInbound,
    });
    expect(chunk1.messages).toHaveLength(4);
    const secondAssistant = chunk1.messages[3];
    expect(secondAssistant.id).toBe("turn-session-1-1");
    expect(secondAssistant.content).toBe("second");
    // The first assistant message is untouched.
    expect(chunk1.messages[1].content).toBe("first");
  });

  it("produces deterministic tool-call ids without backend toolCallId", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const prompt = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
    });
    const toolCallUpdate = {
      type: "tool_call",
      title: "Read file",
      kind: "read",
      messageId: undefined,
    } as SessionUpdate;
    const withTool = chatReducer(prompt, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: toolCallUpdate,
      } as WSInbound,
    });
    const assistant = withTool.messages.find((m) => m.role === "assistant");
    expect(assistant?.toolCalls?.[0].id).toBe("Read file-read-no-msg-0");

    // Reprocessing the same update from the same initial state yields the
    // same id (no Date.now() drift).
    const replay = chatReducer(prompt, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: toolCallUpdate,
      } as WSInbound,
    });
    const replayAssistant = replay.messages.find((m) => m.role === "assistant");
    expect(replayAssistant?.toolCalls?.[0].id).toBe("Read file-read-no-msg-0");
  });

  it("discards history nodes with an unknown role instead of casting", () => {
    const selected = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "req-load-1",
    });
    const validUser = makeHistoryNode({ node_id: "node-user", role: "user" });
    const bogusRole = makeHistoryNode({
      node_id: "node-bogus",
      role: "developer" as unknown as MessageNode["role"],
      content: "should be dropped",
    });
    const loaded = chatReducer(selected, {
      type: "server_message",
      msg: {
        type: "history_loaded",
        sessionId: "session-1",
        messages: [bogusRole, validUser],
        reasoning: [],
        requestId: "req-load-1",
      } as WSInbound,
    });
    expect(loaded.messages).toHaveLength(1);
    expect(loaded.messages[0].role).toBe("user");
  });

  it("ignores a stale session_created whose requestId does not match the latest new_session request", () => {
    // User starts a new session (request A).
    const initiated = chatReducer(initialChatState, {
      type: "new_session_initiated",
      requestId: "A",
    });
    // While creation is in flight, the user selects an existing session (B).
    const selected = chatReducer(initiated, {
      type: "select_session",
      sessionId: "session-existing",
      requestId: "B",
    });
    expect(selected.activeSessionId).toBe("session-existing");
    expect(selected.status).toBe("loading_history");

    // A late session_created for request A must not overwrite the active
    // session or clear messages.
    const stale = chatReducer(selected, {
      type: "server_message",
      msg: {
        type: "session_created",
        sessionId: "session-new",
        requestId: "A",
      } as WSInbound,
    });
    expect(stale).toBe(selected);
    expect(stale.activeSessionId).toBe("session-existing");
    expect(stale.status).toBe("loading_history");
  });

  it("replaces the active session when session_created requestId matches", () => {
    const initiated = chatReducer(initialChatState, {
      type: "new_session_initiated",
      requestId: "A",
    });
    const created = chatReducer(initiated, {
      type: "server_message",
      msg: {
        type: "session_created",
        sessionId: "session-new",
        requestId: "A",
      } as WSInbound,
    });
    expect(created.activeSessionId).toBe("session-new");
    expect(created.status).toBe("ready");
  });

  it("ignores stale history_loaded whose requestId does not match the latest load_history request", () => {
    // First select (request A).
    const firstSelect = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "A",
    });
    // User switches to another session (request B) before history arrives.
    const secondSelect = chatReducer(firstSelect, {
      type: "select_session",
      sessionId: "session-2",
      requestId: "B",
    });
    expect(secondSelect.activeSessionId).toBe("session-2");

    // Stale history for session-1 (request A) must be ignored.
    const staleHistory = chatReducer(secondSelect, {
      type: "server_message",
      msg: {
        type: "history_loaded",
        sessionId: "session-1",
        messages: [makeHistoryNode({ node_id: "stale", role: "assistant", content: "old" })],
        reasoning: [],
        requestId: "A",
      } as WSInbound,
    });
    expect(staleHistory).toBe(secondSelect);
    expect(staleHistory.activeSessionId).toBe("session-2");
    expect(staleHistory.messages).toHaveLength(0);
  });

  it("ignores stale session_resumed whose requestId does not match", () => {
    // select_session stores requestId in load_history.
    const selected = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "A",
    });
    // A reconnect resume supersedes it with a new request id.
    const resuming = chatReducer(selected, { type: "resume_started", requestId: "B" });
    expect(resuming.lastRequestIds.resume).toBe("B");

    // Stale session_resumed for request A must be ignored.
    const stale = chatReducer(resuming, {
      type: "server_message",
      msg: { type: "session_resumed", sessionId: "session-1", requestId: "A" } as WSInbound,
    });
    expect(stale).toBe(resuming);
  });

  it("ignores stale resume_failed whose requestId does not match", () => {
    const selected = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "A",
    });
    const resuming = chatReducer(selected, { type: "resume_started", requestId: "B" });

    const stale = chatReducer(resuming, {
      type: "server_message",
      msg: {
        type: "resume_failed",
        sessionId: "session-1",
        message: "stale failure",
        requestId: "A",
      } as WSInbound,
    });
    expect(stale).toBe(resuming);
    expect(stale.status).not.toBe("paused_readonly");
  });

  it("accepts session_resumed whose requestId matches the resume request", () => {
    const selected = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "A",
    });
    const resuming = chatReducer(selected, { type: "resume_started", requestId: "B" });
    const resumed = chatReducer(resuming, {
      type: "server_message",
      msg: { type: "session_resumed", sessionId: "session-1", requestId: "B" } as WSInbound,
    });
    expect(resumed.status).toBe("ready");
  });

  it("accepts session_resumed whose requestId matches the load_history request", () => {
    const selected = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "A",
    });
    const resumed = chatReducer(selected, {
      type: "server_message",
      msg: { type: "session_resumed", sessionId: "session-1", requestId: "A" } as WSInbound,
    });
    expect(resumed.status).toBe("ready");
  });

  it("clears the permission panel only when permission_received ids match", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    // Submit with a correlation id.
    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });
    expect(submitting.permissionRequest?.correlationId).toBe("corr-1");

    // Matching tool call id and correlation id clears the panel.
    const acknowledged = chatReducer(submitting, {
      type: "server_message",
      msg: {
        type: "permission_received",
        sessionId: "session-1",
        toolCallId: "tc-1",
        requestId: "corr-1",
        acpRequestId: 42,
        decision: "allow",
      } as WSInbound,
    });
    expect(acknowledged.permissionRequest).toBeNull();
  });

  it("does not clear a newer permission panel on a delayed permission_received", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    // First permission request.
    const withFirst = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "first" },
      } as WSInbound,
    });
    const submittingFirst = chatReducer(withFirst, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });

    // A newer permission request arrives (e.g. a second tool call) before
    // the first ack.
    const withSecond = chatReducer(submittingFirst, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-2",
        data: { toolCallId: "tc-2", message: "second" },
      } as WSInbound,
    });
    expect(withSecond.permissionRequest?.toolCallId).toBe("tc-2");
    expect(withSecond.permissionRequest?.correlationId).toBeUndefined();

    // Delayed ack for the first request must not clear the newer panel.
    const delayedAck = chatReducer(withSecond, {
      type: "server_message",
      msg: {
        type: "permission_received",
        sessionId: "session-1",
        toolCallId: "tc-1",
        requestId: "corr-1",
        decision: "allow",
      } as WSInbound,
    });
    expect(delayedAck).toBe(withSecond);
    expect(delayedAck.permissionRequest?.toolCallId).toBe("tc-2");
  });

  it("clears the panel and sets an error on permission_expired for the active tool call", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    expect(withPermission.permissionRequest).not.toBeNull();

    const expired = chatReducer(withPermission, {
      type: "server_message",
      msg: {
        type: "permission_expired",
        sessionId: "session-1",
        toolCallId: "tc-1",
        requestId: 99,
      } as WSInbound,
    });
    expect(expired.permissionRequest).toBeNull();
    expect(expired.error).toBe("Permission request expired.");
  });

  it("ignores permission_expired for a different tool call", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "active" },
      } as WSInbound,
    });

    const expired = chatReducer(withPermission, {
      type: "server_message",
      msg: {
        type: "permission_expired",
        sessionId: "session-1",
        toolCallId: "tc-other",
        requestId: 99,
      } as WSInbound,
    });
    expect(expired).toBe(withPermission);
    expect(expired.permissionRequest?.toolCallId).toBe("tc-1");
  });

  it("ignores permission_expired carrying another session's reused toolCallId", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-b" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-b",
        toolCallId: "edit_1",
        data: { toolCallId: "edit_1", message: "apply edit" },
      } as WSInbound,
    });
    expect(withPermission.permissionRequest).not.toBeNull();

    const otherSession = chatReducer(withPermission, {
      type: "server_message",
      msg: {
        type: "permission_expired",
        sessionId: "session-a",
        toolCallId: "edit_1",
        requestId: 9,
      } as WSInbound,
    });
    expect(otherSession).toBe(withPermission);

    const noSession = chatReducer(withPermission, {
      type: "server_message",
      msg: {
        type: "permission_expired",
        toolCallId: "edit_1",
        requestId: 9,
      } as WSInbound,
    });
    expect(noSession.permissionRequest).toBeNull();
    expect(noSession.error).toBe("Permission request expired.");

    const expired = chatReducer(withPermission, {
      type: "server_message",
      msg: {
        type: "permission_expired",
        sessionId: "session-b",
        toolCallId: "edit_1",
        requestId: 9,
      } as WSInbound,
    });
    expect(expired.permissionRequest).toBeNull();
    expect(expired.error).toBe("Permission request expired.");
  });

  it("ignores permission_received for an already-expired request", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });

    // The request expires first, clearing the panel.
    const expired = chatReducer(submitting, {
      type: "server_message",
      msg: {
        type: "permission_expired",
        sessionId: "session-1",
        toolCallId: "tc-1",
        requestId: 99,
      } as WSInbound,
    });
    expect(expired.permissionRequest).toBeNull();
    expect(expired.error).toBe("Permission request expired.");

    // A late permission_received for the same ids must not change state
    // (the panel is already gone; the error must persist).
    const lateAck = chatReducer(expired, {
      type: "server_message",
      msg: {
        type: "permission_received",
        sessionId: "session-1",
        toolCallId: "tc-1",
        requestId: "corr-1",
        acpRequestId: 99,
        decision: "allow",
      } as WSInbound,
    });
    expect(lateAck).toBe(expired);
    expect(lateAck.permissionRequest).toBeNull();
    expect(lateAck.error).toBe("Permission request expired.");
  });

  it("returns to streaming (not ready) on a failed cancel error and keeps currentTurnId", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const prompt = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
      requestId: "req-prompt-1",
    });
    const cancelling = chatReducer(prompt, { type: "cancel", requestId: "req-cancel-1" });
    expect(cancelling.status).toBe("cancelling");
    expect(cancelling.currentTurnId).not.toBeNull();

    const errored = chatReducer(cancelling, {
      type: "server_message",
      msg: {
        type: "error",
        sessionId: "session-1",
        requestId: "req-cancel-1",
        operation: "cancel",
        message: "cancel failed",
      } as WSInbound,
    });
    // A failed cancel does not mean the underlying turn stopped; return to
    // streaming with a retryable error and keep the per-turn ids intact.
    expect(errored.status).toBe("streaming");
    expect(errored.error).toBe("cancel failed");
    expect(errored.currentTurnId).toBe(cancelling.currentTurnId);
    expect(errored.turnToolCallSeq).toBe(cancelling.turnToolCallSeq);
  });

  it("resets submitting on a failed permission_response error and keeps the panel", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });
    expect(submitting.permissionRequest?.submitting).toBe(true);
    expect(submitting.permissionRequest?.submittedDecision).toBe("allow");

    const errored = chatReducer(submitting, {
      type: "server_message",
      msg: {
        type: "error",
        sessionId: "session-1",
        requestId: "corr-1",
        operation: "permission_response",
        message: "decision rejected",
      } as WSInbound,
    });
    // The panel stays visible so the user can retry; submitting is reset.
    expect(errored.permissionRequest).not.toBeNull();
    expect(errored.permissionRequest?.submitting).toBe(false);
    expect(errored.permissionRequest?.submittedDecision).toBeUndefined();
    expect(errored.error).toBe("decision rejected");
  });

  it("does not end streaming for an uncorrelated prompt error (no requestId)", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const prompt = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
      requestId: "req-prompt-1",
    });
    const withChunk = chatReducer(prompt, {
      type: "server_message",
      msg: {
        type: "session_update",
        sessionId: "session-1",
        update: { type: "agent_message_chunk", chunk: "typing" },
      } as WSInbound,
    });
    expect(withChunk.status).toBe("streaming");

    const errored = chatReducer(withChunk, {
      type: "server_message",
      msg: {
        type: "error",
        sessionId: "session-1",
        operation: "prompt",
        message: "uncorrelated boom",
      } as WSInbound,
    });
    // The uncorrelated error must not end the active prompt. It is surfaced
    // as a connection-wide failure instead.
    expect(errored.status).toBe("streaming");
    expect(errored.error).toBeNull();
    expect(errored.connectionError).toBe("uncorrelated boom");
  });

  const REQUEST_BASED: readonly ErrorOperation[] = [
    "prompt",
    "cancel",
    "new_session",
    "load_history",
    "resume",
    "permission_response",
  ];

  it.each(REQUEST_BASED)(
    "treats a null requestId on a request-based %s error as uncorrelated",
    (operation) => {
      const streamingAssistant: UIMessage = {
        id: "assistant-1",
        role: "assistant",
        content: "typing",
        timestamp: "2026-07-13T00:00:01Z",
        isStreaming: true,
      };
      const pendingPermission: PermissionRequest = {
        sessionId: "s1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm" },
        submitting: true,
        submittedDecision: "allow",
        correlationId: "req-current",
      };
      const state: ChatState = {
        ...initialChatState,
        activeSessionId: "s1",
        status: "streaming",
        messages: [userMessage, streamingAssistant],
        currentTurnId: "turn-s1-1",
        currentTurnStartedAt: "2026-07-13T00:00:00Z",
        streamItemIndex: 1,
        lastRequestIds: { [operation]: "req-current" },
        permissionRequest:
          operation === "permission_response" ? pendingPermission : null,
      };
      const errored = chatReducer(state, {
        type: "server_message",
        msg: assertWSInbound({
          type: "error",
          sessionId: "s1",
          requestId: null,
          operation,
          message: "null-id boom",
        }),
      });
      expect(errored).toEqual({ ...state, connectionError: "null-id boom" });
    }
  );

  it("keeps streaming when a null requestId prompt error arrives with no stored id", () => {
    const streamingAssistant: UIMessage = {
      id: "assistant-1",
      role: "assistant",
      content: "typing",
      timestamp: "2026-07-13T00:00:01Z",
      isStreaming: true,
    };
    const state: ChatState = {
      ...initialChatState,
      activeSessionId: "s1",
      status: "streaming",
      messages: [userMessage, streamingAssistant],
      currentTurnId: "turn-s1-1",
      currentTurnStartedAt: "2026-07-13T00:00:00Z",
      streamItemIndex: 1,
    };
    const errored = chatReducer(state, {
      type: "server_message",
      msg: assertWSInbound({
        type: "error",
        sessionId: "s1",
        requestId: null,
        operation: "prompt",
        message: "null-id boom",
      }),
    });
    expect(errored).toEqual({ ...state, connectionError: "null-id boom" });
  });

  it("applies a null requestId permission error like an omitted id", () => {
    const pendingPermission: PermissionRequest = {
      sessionId: "s1",
      toolCallId: "tc-1",
      data: { toolCallId: "tc-1", message: "run rm" },
    };
    const state: ChatState = {
      ...initialChatState,
      activeSessionId: "s1",
      status: "streaming",
      lastRequestIds: { permission: "req-perm-1" },
      permissionRequest: pendingPermission,
    };
    const errored = chatReducer(state, {
      type: "server_message",
      msg: assertWSInbound({
        type: "error",
        sessionId: "s1",
        requestId: null,
        operation: "permission",
        message: "perm failed",
      }),
    });
    expect(errored).toEqual({
      ...state,
      permissionRequest: null,
      error: "perm failed",
    });
  });

  it("rejects session_resumed without a requestId when lastRequestIds.resume is set", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const resuming = chatReducer(ready, {
      type: "resume_started",
      requestId: "req-resume-1",
    });
    expect(resuming.status).toBe("resuming");
    expect(resuming.lastRequestIds.resume).toBe("req-resume-1");

    // A session_resumed without requestId must be rejected — the backend
    // echoes IDs for all request-response operations, so a missing ID
    // indicates a stale or malformed message.
    const rejected = chatReducer(resuming, {
      type: "server_message",
      msg: { type: "session_resumed", sessionId: "session-1" } as WSInbound,
    });
    expect(rejected).toBe(resuming);
    expect(rejected.status).toBe("resuming");
  });

  it("rejects history_loaded without a requestId when lastRequestIds.load_history is set", () => {
    const selected = chatReducer(initialChatState, {
      type: "select_session",
      sessionId: "session-1",
      requestId: "req-load-1",
    });
    expect(selected.status).toBe("loading_history");
    expect(selected.lastRequestIds.load_history).toBe("req-load-1");

    // A history_loaded without requestId must be rejected.
    const rejected = chatReducer(selected, {
      type: "server_message",
      msg: {
        type: "history_loaded",
        sessionId: "session-1",
        messages: [makeHistoryNode({ node_id: "stale", role: "assistant", content: "old" })],
        reasoning: [],
      } as unknown as WSInbound,
    });
    expect(rejected).toBe(selected);
    expect(rejected.status).toBe("loading_history");
    expect(rejected.messages).toHaveLength(0);
  });

  it("accepts a permission_request without a requestId (unsolicited)", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    // A request_permission with no requestId is unsolicited and must still
    // be accepted so the permission panel is shown.
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    expect(withPermission.permissionRequest).not.toBeNull();
    expect(withPermission.permissionRequest?.toolCallId).toBe("tc-1");
  });

  it("sets the turn status to queued (not streaming) on prompt_queued", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-queued-1",
    });
    expect(queued.status).toBe("queued");
    expect(queued.messages).toHaveLength(1);
    expect(queued.queuedRequestId).toBe("req-queued-1");
    expect(queued.queuedUserMessageId).toBe(userMessage.id);
    expect(queued.lastRequestIds.prompt).toBe("req-queued-1");
  });

  it("transitions queued -> streaming on prompt_flushed with matching requestId", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-queued-1",
    });
    const flushed = chatReducer(queued, {
      type: "prompt_flushed",
      requestId: "req-queued-1",
    });
    expect(flushed.status).toBe("streaming");
    expect(flushed.queuedRequestId).toBeNull();
    expect(flushed.queuedUserMessageId).toBeNull();
    // The user message remains.
    expect(flushed.messages).toHaveLength(1);
  });

  it("ignores prompt_flushed with a non-matching requestId", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-queued-1",
    });
    const stale = chatReducer(queued, {
      type: "prompt_flushed",
      requestId: "req-other",
    });
    expect(stale).toBe(queued);
    expect(stale.status).toBe("queued");
  });

  it("transitions to ready with an error on prompt_queue_dropped (resume_failed)", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-queued-1",
    });
    const dropped = chatReducer(queued, {
      type: "prompt_queue_dropped",
      requestId: "req-queued-1",
      error: "Connection could not be resumed; your prompt was not sent.",
    });
    expect(dropped.status).toBe("ready");
    expect(dropped.error).toBe("Connection could not be resumed; your prompt was not sent.");
    // The optimistic user message is removed.
    expect(dropped.messages).toHaveLength(0);
    expect(dropped.queuedRequestId).toBeNull();
    expect(dropped.queuedUserMessageId).toBeNull();
  });

  it("transitions to ready with an error on prompt_queue_dropped (expired)", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-queued-1",
    });
    const dropped = chatReducer(queued, {
      type: "prompt_queue_dropped",
      requestId: "req-queued-1",
      error: "Your prompt timed out waiting for the connection.",
    });
    expect(dropped.status).toBe("ready");
    expect(dropped.error).toBe("Your prompt timed out waiting for the connection.");
    expect(dropped.messages).toHaveLength(0);
  });

  it("transitions to ready with no error on prompt_queue_dropped (cancelled)", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-queued-1",
    });
    const dropped = chatReducer(queued, {
      type: "prompt_queue_dropped",
      requestId: "req-queued-1",
      error: null,
    });
    expect(dropped.status).toBe("ready");
    expect(dropped.error).toBeNull();
    expect(dropped.messages).toHaveLength(0);
  });

  it("ignores prompt_queue_dropped with a non-matching requestId", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const queued = chatReducer(ready, {
      type: "prompt_queued",
      userMessage,
      requestId: "req-queued-1",
    });
    const stale = chatReducer(queued, {
      type: "prompt_queue_dropped",
      requestId: "req-other",
      error: "nope",
    });
    expect(stale).toBe(queued);
    expect(stale.status).toBe("queued");
  });

  it("surfaces the cap error on prompt_send_failed when an error is provided", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const failed = chatReducer(ready, {
      type: "prompt_send_failed",
      userMessage,
      prevStatus: "ready",
      error: "A prompt is already waiting for the connection.",
    });
    expect(failed.status).toBe("ready");
    expect(failed.error).toBe("A prompt is already waiting for the connection.");
    expect(failed.messages).toHaveLength(0);
  });

  // -- Task 7: indeterminate permission/cancel on disconnect -------------

  it("transitions a submitting permission panel to interrupted on connection_lost", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });
    expect(submitting.permissionRequest?.submitting).toBe(true);

    // Socket disconnects before permission_received.
    const interrupted = chatReducer(submitting, {
      type: "connection_lost",
      payload: {
        permission: { toolCallId: "tc-1", correlationId: "corr-1" },
        cancelRequestId: null,
      },
    });
    expect(interrupted.permissionRequest).not.toBeNull();
    expect(interrupted.permissionRequest?.submitting).toBe(false);
    expect(interrupted.permissionRequest?.submittedDecision).toBeUndefined();
    expect(interrupted.permissionRequest?.correlationId).toBeUndefined();
    expect(interrupted.permissionRequest?.interrupted).toBe(true);
    expect(interrupted.permissionRequest?.interruptError).toBe(
      "Connection lost before the permission decision was confirmed."
    );
  });

  it("does not mark a permission panel interrupted when the disconnect did not affect it", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    // No permission_response was in flight — connection_lost with no
    // permission payload must not change the panel.
    const unchanged = chatReducer(withPermission, {
      type: "connection_lost",
      payload: { permission: null, cancelRequestId: null },
    });
    expect(unchanged).toBe(withPermission);
    expect(unchanged.permissionRequest?.interrupted).toBeFalsy();
  });

  it("ignores permission_interrupted for a non-matching tool call id", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "active" },
      } as WSInbound,
    });
    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });
    // Disconnect reports a different tool call id — must not interrupt
    // the active panel.
    const unchanged = chatReducer(submitting, {
      type: "connection_lost",
      payload: {
        permission: { toolCallId: "tc-other", correlationId: "corr-1" },
        cancelRequestId: null,
      },
    });
    expect(unchanged.permissionRequest?.submitting).toBe(true);
    expect(unchanged.permissionRequest?.interrupted).toBeFalsy();
  });

  it("retries normally after reconnect within the grace window (permission_received clears interrupted panel)", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });
    const interrupted = chatReducer(submitting, {
      type: "connection_lost",
      payload: {
        permission: { toolCallId: "tc-1", correlationId: "corr-1" },
        cancelRequestId: null,
      },
    });
    expect(interrupted.permissionRequest?.interrupted).toBe(true);

    // The original response went through; the backend confirms after
    // reconnect with a permission_received for the same tool call id.
    // The correlation id no longer matches (it was cleared), but the
    // interrupted-panel fallback should still clear the panel.
    const acknowledged = chatReducer(interrupted, {
      type: "server_message",
      msg: {
        type: "permission_received",
        sessionId: "session-1",
        toolCallId: "tc-1",
        requestId: "corr-1",
        decision: "allow",
      } as WSInbound,
    });
    expect(acknowledged.permissionRequest).toBeNull();
  });

  it("retries after reconnect and surfaces the backend 'no longer pending' error", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });
    const interrupted = chatReducer(submitting, {
      type: "connection_lost",
      payload: {
        permission: { toolCallId: "tc-1", correlationId: "corr-1" },
        cancelRequestId: null,
      },
    });

    // User retries after reconnect — a new permission_submitting clears
    // the interrupted flags and sets a new correlation id.
    const retrying = chatReducer(interrupted, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-2",
    });
    expect(retrying.permissionRequest?.interrupted).toBe(false);
    expect(retrying.permissionRequest?.submitting).toBe(true);
    expect(retrying.permissionRequest?.correlationId).toBe("corr-2");

    // Past the grace window, the backend rejects with "no longer pending".
    const errored = chatReducer(retrying, {
      type: "server_message",
      msg: {
        type: "error",
        sessionId: "session-1",
        requestId: "corr-2",
        operation: "permission_response",
        message: "Permission request is no longer pending.",
      } as WSInbound,
    });
    expect(errored.permissionRequest).not.toBeNull();
    expect(errored.permissionRequest?.submitting).toBe(false);
    expect(errored.permissionRequest?.interrupted).toBe(true);
    expect(errored.permissionRequest?.interruptError).toBe(
      "Permission request is no longer pending."
    );
    expect(errored.error).toBe("Permission request is no longer pending.");
  });

  it("discardPermission clears an interrupted panel and surfaces the interrupt error", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });
    const interrupted = chatReducer(submitting, {
      type: "connection_lost",
      payload: {
        permission: { toolCallId: "tc-1", correlationId: "corr-1" },
        cancelRequestId: null,
      },
    });

    const discarded = chatReducer(interrupted, { type: "permission_discard" });
    expect(discarded.permissionRequest).toBeNull();
    expect(discarded.error).toBe("Connection lost before the permission decision was confirmed.");
  });

  it("permission_discard is a no-op when the panel is not interrupted", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    // Not interrupted — discard must not clear the panel.
    const unchanged = chatReducer(withPermission, { type: "permission_discard" });
    expect(unchanged).toBe(withPermission);
    expect(unchanged.permissionRequest).not.toBeNull();
  });

  it("an in-flight cancel that becomes indeterminate on disconnect returns to streaming", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const prompt = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
      requestId: "req-prompt-1",
    });
    const cancelling = chatReducer(prompt, { type: "cancel", requestId: "req-cancel-1" });
    expect(cancelling.status).toBe("cancelling");

    // Socket disconnects while the cancel is in flight.
    const afterDisconnect = chatReducer(cancelling, {
      type: "connection_lost",
      payload: { permission: null, cancelRequestId: "req-cancel-1" },
    });
    // The cancel is indeterminate — return to streaming so the UI does
    // not assume the turn was cancelled.
    expect(afterDisconnect.status).toBe("streaming");
    // The per-turn ids are preserved so subsequent chunks still append.
    expect(afterDisconnect.currentTurnId).toBe(cancelling.currentTurnId);
  });

  it("connection_lost with a cancelRequestId does not change status when not cancelling", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const streaming = chatReducer(ready, {
      type: "prompt_optimistic",
      userMessage,
      prevStatus: "ready",
      requestId: "req-prompt-1",
    });
    expect(streaming.status).toBe("streaming");
    // A stale cancelRequestId arrives but we are not cancelling — no
    // change.
    const unchanged = chatReducer(streaming, {
      type: "connection_lost",
      payload: { permission: null, cancelRequestId: "req-cancel-stale" },
    });
    expect(unchanged.status).toBe("streaming");
  });

  it("connection_lost handles both an in-flight permission and cancel simultaneously", () => {
    const ready = chatReducer(initialChatState, {
      type: "server_message",
      msg: { type: "session_created", sessionId: "session-1" } as WSInbound,
    });
    const withPermission = chatReducer(ready, {
      type: "server_message",
      msg: {
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound,
    });
    const submitting = chatReducer(withPermission, {
      type: "permission_submitting",
      decision: "allow",
      requestId: "corr-1",
    });
    // While the permission is submitting, the user also cancels (edge
    // case: both in flight). The disconnect interrupts both.
    const cancelling = chatReducer(submitting, { type: "cancel", requestId: "req-cancel-1" });
    expect(cancelling.status).toBe("cancelling");

    const afterDisconnect = chatReducer(cancelling, {
      type: "connection_lost",
      payload: {
        permission: { toolCallId: "tc-1", correlationId: "corr-1" },
        cancelRequestId: "req-cancel-1",
      },
    });
    expect(afterDisconnect.permissionRequest?.interrupted).toBe(true);
    expect(afterDisconnect.permissionRequest?.submitting).toBe(false);
    expect(afterDisconnect.status).toBe("streaming");
  });
});

describe("requestIdMatches", () => {
  it("accepts when there are no expected IDs and the message ID is undefined", () => {
    expect(requestIdMatches(undefined, undefined, undefined)).toBe(true);
    expect(requestIdMatches(undefined)).toBe(true);
  });

  it("rejects when there are no expected IDs but the message has a defined ID", () => {
    expect(requestIdMatches("req-1", undefined, undefined)).toBe(false);
    expect(requestIdMatches("req-1")).toBe(false);
  });

  it("rejects when there are expected IDs and the message ID is undefined", () => {
    expect(requestIdMatches(undefined, "req-1")).toBe(false);
    expect(requestIdMatches(undefined, "req-1", "req-2")).toBe(false);
  });

  it("accepts when there are expected IDs and the message ID matches one of them", () => {
    expect(requestIdMatches("req-1", "req-1")).toBe(true);
    expect(requestIdMatches("req-2", "req-1", "req-2")).toBe(true);
  });

  it("rejects when there are expected IDs and the message ID does not match", () => {
    expect(requestIdMatches("req-3", "req-1")).toBe(false);
    expect(requestIdMatches("req-3", "req-1", "req-2")).toBe(false);
  });

  it("treats empty-string sentinels as no stored ID", () => {
    // Empty strings are filtered out by Boolean(), so they don't count as
    // expected IDs. With no real expected IDs, only undefined is accepted.
    expect(requestIdMatches(undefined, "", "")).toBe(true);
    expect(requestIdMatches("req-1", "", "")).toBe(false);
  });
});

describe("useChatSession callback guards", () => {
  // Builds a mock `send` that records every outgoing message and always
  // reports a successful send so the hook proceeds to dispatch its
  // optimistic action.
  function makeSend(): { send: (msg: WSOutbound) => SendResult; sent: WSOutbound[] } {
    const sent: WSOutbound[] = [];
    const send = (msg: WSOutbound): SendResult => {
      sent.push(msg);
      return { sent: true, queued: false };
    };
    return { send, sent };
  }

  function requestIdFor(sent: WSOutbound[], type: WSOutbound["type"], index = 0): string {
    const matches = sent.filter((m) => m.type === type);
    const match = matches[index];
    if (!match || !("requestId" in match) || typeof match.requestId !== "string") {
      throw new Error(`No requestId found for ${type} at index ${index}`);
    }
    return match.requestId;
  }

  it("views existing history without starting Devin and starts it only on request", () => {
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.selectSession("session-existing");
    });
    expect(sent[0]).toMatchObject({
      type: "view_history",
      sessionId: "session-existing",
    });
    const viewRequestId = requestIdFor(sent, "view_history");

    act(() => {
      result.current.onServerMessage({
        type: "history_loaded",
        sessionId: "session-existing",
        messages: [],
        reasoning: [],
        readOnly: true,
        requestId: viewRequestId,
      });
    });
    expect(result.current.status).toBe("paused_readonly");
    expect(result.current.sessionLive).toBe(false);

    act(() => {
      expect(result.current.startSession("gpt-5", "bypass")).toBe(true);
    });
    expect(sent[1]).toMatchObject({
      type: "resume_session",
      sessionId: "session-existing",
      model: "gpt-5",
      permissionMode: "bypass",
    });
    expect(result.current.status).toBe("resuming");
  });

  it("does not invoke onSessionCreated for a stale session_created", () => {
    const onSessionCreated = vi.fn();
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send, { onSessionCreated }));

    // Start a new session (request A).
    act(() => {
      result.current.newSession("cwd");
    });
    const requestA = requestIdFor(sent, "new_session");

    // While creation is in flight, select an existing session (request B),
    // which supersedes the new-session request by clearing
    // lastRequestIds.new_session to the empty sentinel.
    act(() => {
      result.current.selectSession("session-existing");
    });

    // A late session_created for request A must not fire the callback.
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-new",
        requestId: requestA,
      } as WSInbound);
    });
    expect(onSessionCreated).not.toHaveBeenCalled();
    // The reducer also rejected it, so the active session is unchanged.
    expect(result.current.activeSessionId).toBe("session-existing");
  });

  it("invokes onSessionCreated for a current session_created", () => {
    const onSessionCreated = vi.fn();
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send, { onSessionCreated }));

    act(() => {
      result.current.newSession("cwd", "gpt-4");
    });
    const requestA = requestIdFor(sent, "new_session");

    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-new",
        model: "gpt-4",
        requestId: requestA,
      } as WSInbound);
    });
    expect(onSessionCreated).toHaveBeenCalledTimes(1);
    expect(onSessionCreated).toHaveBeenCalledWith("session-new", "gpt-4");
  });

  it("does not invoke onTurnComplete for a stale turn_complete", () => {
    const onTurnComplete = vi.fn();
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send, { onTurnComplete }));

    // Create a session and reach the ready state.
    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    // First prompt (request A).
    act(() => {
      result.current.sendPrompt("hello");
    });
    const promptA = requestIdFor(sent, "prompt", 0);

    // Second prompt supersedes the first (request B).
    act(() => {
      result.current.sendPrompt("world");
    });
    const promptB = requestIdFor(sent, "prompt", 1);
    expect(promptB).not.toBe(promptA);

    // A late turn_complete for request A must not fire the callback.
    act(() => {
      result.current.onServerMessage({
        type: "turn_complete",
        sessionId: "session-1",
        requestId: promptA,
        result: {},
      } as WSInbound);
    });
    expect(onTurnComplete).not.toHaveBeenCalled();
  });

  it("sends the selected model with each prompt", () => {
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd", "glm-5-2");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    // The user switches the dropdown after the session was created; the new
    // selection must ride along with the prompt so the backend can re-apply
    // it to the live ACP session instead of using the creation-time model.
    act(() => {
      result.current.sendPrompt("hello", "claude-opus-5-high");
    });

    const prompt = sent.filter((m) => m.type === "prompt")[0];
    expect(prompt).toMatchObject({ text: "hello", model: "claude-opus-5-high" });
  });

  it("preserves prompt line breaks in the optimistic message and WebSocket payload", () => {
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    const text = "First line\nSecond line\n\nFourth line";
    act(() => {
      result.current.sendPrompt(text);
    });

    const prompt = sent.filter((m) => m.type === "prompt")[0];
    expect(prompt).toMatchObject({ text });
    expect(result.current.messages[result.current.messages.length - 1]?.content).toBe(text);
  });

  it("invokes onTurnComplete for a current turn_complete", () => {
    const onTurnComplete = vi.fn();
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send, { onTurnComplete }));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    act(() => {
      result.current.sendPrompt("hello");
    });
    const promptA = requestIdFor(sent, "prompt", 0);

    act(() => {
      result.current.onServerMessage({
        type: "turn_complete",
        sessionId: "session-1",
        requestId: promptA,
        result: {},
      } as WSInbound);
    });
    expect(onTurnComplete).toHaveBeenCalledTimes(1);
  });
});

describe("useChatSession queued-prompt flow", () => {
  // Builds a mock `send` that succeeds for `new_session` but queues prompts
  // (sent: false, queued: true) so the hook enters the `queued` status.
  function makeQueuedSend(): { send: (msg: WSOutbound) => SendResult; sent: WSOutbound[] } {
    const sent: WSOutbound[] = [];
    const send = (msg: WSOutbound): SendResult => {
      sent.push(msg);
      if (msg.type === "new_session") return { sent: true, queued: false };
      if (msg.type === "prompt") return { sent: false, queued: true };
      return { sent: false, queued: false };
    };
    return { send, sent };
  }

  function requestIdFor(sent: WSOutbound[], type: WSOutbound["type"], index = 0): string {
    const matches = sent.filter((m) => m.type === type);
    const match = matches[index];
    if (!match || !("requestId" in match) || typeof match.requestId !== "string") {
      throw new Error(`No requestId found for ${type} at index ${index}`);
    }
    return match.requestId;
  }

  it("sets the turn status to queued when send returns queued", () => {
    const { send, sent } = makeQueuedSend();
    const { result } = renderHook(() => useChatSession(send));

    // Create a session first.
    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });
    expect(result.current.status).toBe("ready");

    // Send a prompt that gets queued.
    act(() => {
      result.current.sendPrompt("hello");
    });
    expect(result.current.status).toBe("queued");
    expect(result.current.queued).toBe(true);
    expect(result.current.isStreaming).toBe(false);
    // The user message is visible.
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].role).toBe("user");
  });

  it("transitions queued -> streaming when onPromptFlushed is called", () => {
    const { send, sent } = makeQueuedSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    act(() => {
      result.current.sendPrompt("hello");
    });
    expect(result.current.status).toBe("queued");

    const promptReq = requestIdFor(sent, "prompt", 0);
    act(() => {
      result.current.onPromptFlushed(promptReq);
    });
    expect(result.current.status).toBe("streaming");
    expect(result.current.queued).toBe(false);
  });

  it("transitions to ready with an error on onQueueDropped (resume_failed)", () => {
    const { send, sent } = makeQueuedSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    act(() => {
      result.current.sendPrompt("hello");
    });
    expect(result.current.status).toBe("queued");

    const promptReq = requestIdFor(sent, "prompt", 0);
    act(() => {
      result.current.onQueueDropped(promptReq, "resume_failed");
    });
    expect(result.current.status).toBe("ready");
    expect(result.current.error).toBe("Connection could not be resumed; your prompt was not sent.");
    // The optimistic user message is removed.
    expect(result.current.messages).toHaveLength(0);
  });

  it("transitions to ready with an error on onQueueDropped (expired)", () => {
    const { send, sent } = makeQueuedSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    act(() => {
      result.current.sendPrompt("hello");
    });
    const promptReq = requestIdFor(sent, "prompt", 0);
    act(() => {
      result.current.onQueueDropped(promptReq, "expired");
    });
    expect(result.current.status).toBe("ready");
    expect(result.current.error).toBe("Your prompt timed out waiting for the connection.");
  });

  it("cancelling a queued prompt via cancelQueuedPrompt returns to ready", () => {
    const { send, sent } = makeQueuedSend();
    const hookResult: { current: ReturnType<typeof useChatSession> } = {
      current: null as unknown as ReturnType<typeof useChatSession>,
    };
    const cancelQueuedPrompt = vi.fn((requestId: string) => {
      // Simulate the WebSocket hook's behaviour: notify onQueueDropped with
      // reason "cancelled".
      hookResult.current.onQueueDropped(requestId, "cancelled");
      return true;
    });
    const { result } = renderHook(() => useChatSession(send, undefined, { cancelQueuedPrompt }));
    hookResult.current = result.current;

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    act(() => {
      result.current.sendPrompt("hello");
    });
    expect(result.current.status).toBe("queued");

    const promptReq = requestIdFor(sent, "prompt", 0);
    act(() => {
      result.current.cancel();
    });
    expect(cancelQueuedPrompt).toHaveBeenCalledWith(promptReq);
    expect(result.current.status).toBe("ready");
    expect(result.current.error).toBeNull();
    expect(result.current.messages).toHaveLength(0);
  });

  it("surfaces the cap error when a prompt is rejected", () => {
    // Use a custom send that succeeds for new_session but rejects prompts
    // with the cap error.
    const sent: WSOutbound[] = [];
    const send = (msg: WSOutbound): SendResult => {
      sent.push(msg);
      if (msg.type === "new_session") return { sent: true, queued: false };
      return {
        sent: false,
        queued: false,
        error: "A prompt is already waiting for the connection.",
      };
    };
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });
    expect(result.current.status).toBe("ready");

    act(() => {
      const ok = result.current.sendPrompt("hello");
      expect(ok).toBe(false);
    });
    expect(result.current.status).toBe("ready");
    expect(result.current.error).toBe("A prompt is already waiting for the connection.");
    // No user message was added.
    expect(result.current.messages).toHaveLength(0);
  });
});

describe("useChatSession connection-lost flow", () => {
  function makeSend(): { send: (msg: WSOutbound) => SendResult; sent: WSOutbound[] } {
    const sent: WSOutbound[] = [];
    const send = (msg: WSOutbound): SendResult => {
      sent.push(msg);
      return { sent: true, queued: false };
    };
    return { send, sent };
  }

  function requestIdFor(sent: WSOutbound[], type: WSOutbound["type"], index = 0): string {
    const matches = sent.filter((m) => m.type === type);
    const match = matches[index];
    if (!match || !("requestId" in match) || typeof match.requestId !== "string") {
      throw new Error(`No requestId found for ${type} at index ${index}`);
    }
    return match.requestId;
  }

  it("marks the permission panel interrupted when onConnectionLost fires", () => {
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    act(() => {
      result.current.onServerMessage({
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound);
    });

    act(() => {
      result.current.respondPermission("allow");
    });
    expect(result.current.permissionRequest?.submitting).toBe(true);

    const corrId = requestIdFor(sent, "permission_response", 0);

    // Simulate a disconnect while the permission_response is in flight.
    act(() => {
      result.current.onConnectionLost({
        permission: { toolCallId: "tc-1", correlationId: corrId },
        cancelRequestId: null,
      });
    });
    expect(result.current.permissionRequest).not.toBeNull();
    expect(result.current.permissionRequest?.submitting).toBe(false);
    expect(result.current.permissionRequest?.interrupted).toBe(true);
    expect(result.current.permissionRequest?.interruptError).toBe(
      "Connection lost before the permission decision was confirmed."
    );
  });

  it("returns to streaming when onConnectionLost fires during cancelling", () => {
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    act(() => {
      result.current.sendPrompt("hello");
    });
    expect(result.current.status).toBe("streaming");

    act(() => {
      result.current.cancel();
    });
    expect(result.current.status).toBe("cancelling");

    const cancelReq = requestIdFor(sent, "cancel", 0);

    // Simulate a disconnect while the cancel is in flight.
    act(() => {
      result.current.onConnectionLost({
        permission: null,
        cancelRequestId: cancelReq,
      });
    });
    expect(result.current.status).toBe("streaming");
  });

  it("discardPermission clears the interrupted panel", () => {
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    act(() => {
      result.current.onServerMessage({
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound);
    });
    act(() => {
      result.current.respondPermission("allow");
    });
    const corrId = requestIdFor(sent, "permission_response", 0);
    act(() => {
      result.current.onConnectionLost({
        permission: { toolCallId: "tc-1", correlationId: corrId },
        cancelRequestId: null,
      });
    });
    expect(result.current.permissionRequest?.interrupted).toBe(true);

    act(() => {
      result.current.discardPermission();
    });
    expect(result.current.permissionRequest).toBeNull();
    expect(result.current.error).toBe(
      "Connection lost before the permission decision was confirmed."
    );
  });

  it("retrying after interruption sends a new permission_response and clears interrupted", () => {
    const { send, sent } = makeSend();
    const { result } = renderHook(() => useChatSession(send));

    act(() => {
      result.current.newSession("cwd");
    });
    const newSessionReq = requestIdFor(sent, "new_session");
    act(() => {
      result.current.onServerMessage({
        type: "session_created",
        sessionId: "session-1",
        requestId: newSessionReq,
      } as WSInbound);
    });

    act(() => {
      result.current.onServerMessage({
        type: "request_permission",
        sessionId: "session-1",
        toolCallId: "tc-1",
        data: { toolCallId: "tc-1", message: "run rm -rf" },
      } as WSInbound);
    });
    act(() => {
      result.current.respondPermission("allow");
    });
    const corrId1 = requestIdFor(sent, "permission_response", 0);
    act(() => {
      result.current.onConnectionLost({
        permission: { toolCallId: "tc-1", correlationId: corrId1 },
        cancelRequestId: null,
      });
    });
    expect(result.current.permissionRequest?.interrupted).toBe(true);

    // Retry — a new permission_response is sent.
    act(() => {
      result.current.respondPermission("allow");
    });
    expect(result.current.permissionRequest?.interrupted).toBe(false);
    expect(result.current.permissionRequest?.submitting).toBe(true);
    const corrId2 = requestIdFor(sent, "permission_response", 1);
    expect(corrId2).not.toBe(corrId1);

    // Backend acknowledges the retry.
    act(() => {
      result.current.onServerMessage({
        type: "permission_received",
        sessionId: "session-1",
        toolCallId: "tc-1",
        requestId: corrId2,
        decision: "allow",
      } as WSInbound);
    });
    expect(result.current.permissionRequest).toBeNull();
  });
});
