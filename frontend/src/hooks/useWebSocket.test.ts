import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useRef } from "react";
import { renderHook, act } from "@testing-library/react";
import { useWebSocket } from "./useWebSocket";
import { useChatSession } from "./useChatSession";
import type { UseChatSessionReturn } from "./useChatSession";
import type { WSOutbound } from "../types";

let instances: MockWebSocket[] = [];

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  sent: string[] = [];
  closed = false;

  constructor(public url: string) {
    instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
    this.closed = true;
    if (this.onclose) {
      this.onclose();
    }
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN;
    if (this.onopen) this.onopen();
  }

  simulateClose() {
    this.readyState = MockWebSocket.CLOSED;
    if (this.onclose) this.onclose();
  }

  simulateMessage(data: string) {
    if (this.onmessage) {
      this.onmessage(new MessageEvent("message", { data }));
    }
  }
}

describe("useWebSocket", () => {
  beforeEach(() => {
    instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
    vi.stubGlobal("location", {
      protocol: "http:",
      host: "localhost:8000",
    });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("connects, sends messages, and delivers inbound messages", () => {
    const onMessage = vi.fn();
    const { result } = renderHook(() => useWebSocket(onMessage));

    expect(instances).toHaveLength(1);
    const ws = instances[0];

    act(() => {
      ws.simulateOpen();
    });
    expect(result.current.connected).toBe(true);

    const outbound: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "hello",
    };
    act(() => {
      result.current.send(outbound);
    });
    expect(ws.sent).toHaveLength(1);
    expect(JSON.parse(ws.sent[0])).toEqual(outbound);

    act(() => {
      ws.simulateMessage(
        JSON.stringify({
          type: "error",
          sessionId: "session-1",
          operation: "prompt",
          message: "oops",
        })
      );
    });
    expect(onMessage).toHaveBeenCalledWith({
      type: "error",
      sessionId: "session-1",
      operation: "prompt",
      message: "oops",
    });
  });

  it("queues messages while disconnected and flushes on connect", () => {
    const { result } = renderHook(() => useWebSocket());
    const ws = instances[0];

    const outbound: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued",
    };
    act(() => {
      result.current.send(outbound);
    });
    expect(ws.sent).toHaveLength(0);

    act(() => {
      ws.simulateOpen();
    });
    expect(ws.sent).toHaveLength(1);
    expect(JSON.parse(ws.sent[0])).toEqual(outbound);
  });

  it("only queues prompt messages while disconnected and reports explicit send results", () => {
    const { result } = renderHook(() => useWebSocket());
    const ws = instances[0];

    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued",
    };
    const cancel: WSOutbound = { type: "cancel", sessionId: "session-1" };

    expect(result.current.send(prompt)).toEqual({ sent: false, queued: true });
    expect(result.current.send(cancel)).toEqual({ sent: false, queued: false });

    act(() => {
      ws.simulateOpen();
    });
    expect(ws.sent).toHaveLength(1);
    expect(JSON.parse(ws.sent[0])).toEqual(prompt);
  });

  it("reconnects with exponential backoff and stops on unmount", () => {
    const { result, unmount } = renderHook(() => useWebSocket());
    const ws1 = instances[0];

    expect(result.current.connected).toBe(false);

    act(() => {
      ws1.simulateClose();
    });

    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(instances).toHaveLength(2);
    const ws2 = instances[1];

    act(() => {
      ws2.simulateClose();
    });

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(instances).toHaveLength(3);

    act(() => {
      ws2.simulateOpen();
    });
    expect(result.current.connected).toBe(true);

    act(() => {
      unmount();
    });
    expect(instances[instances.length - 1].closed).toBe(true);
  });

  it("cancels reconnect after cleanup", () => {
    const { unmount } = renderHook(() => useWebSocket());
    const ws1 = instances[0];

    act(() => {
      ws1.simulateClose();
    });

    act(() => {
      unmount();
    });

    act(() => {
      vi.advanceTimersByTime(5000);
    });

    expect(instances).toHaveLength(1);
  });

  it("sends resume_session on reconnect and blocks the prompt queue until session_resumed", () => {
    const activeSessionId: string | null = "session-1";
    const onMessage = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(onMessage, () => activeSessionId)
    );

    const ws1 = instances[0];
    act(() => {
      ws1.simulateOpen();
    });
    // First connect with an active session also resumes; no queue yet.
    expect(ws1.sent).toHaveLength(1);
    const firstResume = JSON.parse(ws1.sent[0]);
    expect(firstResume.type).toBe("resume_session");
    expect(firstResume.sessionId).toBe("session-1");
    expect(typeof firstResume.requestId).toBe("string");
    expect(result.current.getResumeRequestId()).toBe(firstResume.requestId);

    // Queue a prompt while disconnected.
    act(() => {
      ws1.simulateClose();
    });
    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued-after-reconnect",
    };
    act(() => {
      result.current.send(prompt);
    });

    // Reconnect.
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const ws2 = instances[1];
    act(() => {
      ws2.simulateOpen();
    });

    // resume_session must be sent first; the queued prompt must NOT be flushed yet.
    expect(ws2.sent).toHaveLength(1);
    const secondResume = JSON.parse(ws2.sent[0]);
    expect(secondResume.type).toBe("resume_session");
    expect(secondResume.sessionId).toBe("session-1");
    expect(typeof secondResume.requestId).toBe("string");
    expect(result.current.getResumeRequestId()).toBe(secondResume.requestId);

    // A second prompt sent while one is already queued is rejected by the
    // cap (at most one pending prompt) and does not grow the queue.
    const prompt2: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "during-resume",
    };
    act(() => {
      const result2 = result.current.send(prompt2);
      expect(result2).toEqual({
        sent: false,
        queued: false,
        error: "A prompt is already waiting for the connection.",
      });
    });
    expect(ws2.sent).toHaveLength(1);

    // On session_resumed the single queued prompt flushes.
    act(() => {
      ws2.simulateMessage(JSON.stringify({ type: "session_resumed", sessionId: "session-1", requestId: secondResume.requestId }));
    });
    expect(ws2.sent).toHaveLength(2);
    expect(JSON.parse(ws2.sent[1])).toEqual(prompt);
    expect(onMessage).toHaveBeenCalledWith({
      type: "session_resumed",
      sessionId: "session-1",
      requestId: secondResume.requestId,
    });
  });

  it("drops queued prompts when resume_failed is received on reconnect", () => {
    const onMessage = vi.fn();
    const onQueueDropped = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(onMessage, () => "session-1", undefined, undefined, onQueueDropped)
    );

    const ws1 = instances[0];
    act(() => {
      ws1.simulateOpen();
    });
    act(() => {
      ws1.simulateClose();
    });

    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued",
      requestId: "req-prompt-1",
    };
    act(() => {
      result.current.send(prompt);
    });

    act(() => {
      vi.advanceTimersByTime(500);
    });
    const ws2 = instances[1];
    act(() => {
      ws2.simulateOpen();
    });
    expect(ws2.sent).toHaveLength(1); // resume_session only
    const resumeRequestId = JSON.parse(ws2.sent[0]).requestId;

    act(() => {
      ws2.simulateMessage(
        JSON.stringify({ type: "resume_failed", sessionId: "session-1", message: "boom", requestId: resumeRequestId })
      );
    });
    // Queue is dropped; nothing else is sent.
    expect(ws2.sent).toHaveLength(1);
    expect(onMessage).toHaveBeenCalledWith({
      type: "resume_failed",
      sessionId: "session-1",
      message: "boom",
      requestId: resumeRequestId,
    });
    // The chat hook is notified so the turn can transition to ready.
    expect(onQueueDropped).toHaveBeenCalledWith("req-prompt-1", "resume_failed");
  });

  it("ignores a stale session_resumed for an older resume request id", () => {
    const onMessage = vi.fn();
    const onPromptFlushed = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(onMessage, () => "session-1", undefined, onPromptFlushed)
    );

    const ws1 = instances[0];
    act(() => {
      ws1.simulateOpen();
    });
    const firstResume = JSON.parse(ws1.sent[0]);

    act(() => {
      ws1.simulateClose();
    });
    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued",
      requestId: "req-prompt-1",
    };
    act(() => {
      result.current.send(prompt);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const ws2 = instances[1];
    act(() => {
      ws2.simulateOpen();
    });
    const secondResume = JSON.parse(ws2.sent[0]);
    expect(ws2.sent).toHaveLength(1);
    expect(secondResume.requestId).not.toBe(firstResume.requestId);

    act(() => {
      ws2.simulateMessage(
        JSON.stringify({
          type: "session_resumed",
          sessionId: "session-1",
          requestId: firstResume.requestId,
        })
      );
    });
    expect(ws2.sent).toHaveLength(1);
    expect(onPromptFlushed).not.toHaveBeenCalled();
    expect(onMessage).toHaveBeenCalledWith({
      type: "session_resumed",
      sessionId: "session-1",
      requestId: firstResume.requestId,
    });

    act(() => {
      ws2.simulateMessage(
        JSON.stringify({
          type: "session_resumed",
          sessionId: "session-1",
          requestId: secondResume.requestId,
        })
      );
    });
    expect(ws2.sent).toHaveLength(2);
    expect(JSON.parse(ws2.sent[1])).toEqual(prompt);
    expect(onPromptFlushed).toHaveBeenCalledTimes(1);
    expect(onPromptFlushed).toHaveBeenCalledWith("req-prompt-1");
  });

  it("ignores a session_resumed for a different session even with a matching request id", () => {
    const onMessage = vi.fn();
    const onPromptFlushed = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(onMessage, () => "session-1", undefined, onPromptFlushed)
    );

    const ws1 = instances[0];
    act(() => {
      ws1.simulateOpen();
    });

    act(() => {
      ws1.simulateClose();
    });
    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued",
      requestId: "req-prompt-1",
    };
    act(() => {
      result.current.send(prompt);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const ws2 = instances[1];
    act(() => {
      ws2.simulateOpen();
    });
    const secondResume = JSON.parse(ws2.sent[0]);
    expect(ws2.sent).toHaveLength(1);

    act(() => {
      ws2.simulateMessage(
        JSON.stringify({
          type: "session_resumed",
          sessionId: "session-2",
          requestId: secondResume.requestId,
        })
      );
    });
    expect(ws2.sent).toHaveLength(1);
    expect(onPromptFlushed).not.toHaveBeenCalled();
    expect(onMessage).toHaveBeenCalledWith({
      type: "session_resumed",
      sessionId: "session-2",
      requestId: secondResume.requestId,
    });

    act(() => {
      ws2.simulateMessage(
        JSON.stringify({
          type: "session_resumed",
          sessionId: "session-1",
          requestId: secondResume.requestId,
        })
      );
    });
    expect(ws2.sent).toHaveLength(2);
    expect(JSON.parse(ws2.sent[1])).toEqual(prompt);
    expect(onPromptFlushed).toHaveBeenCalledTimes(1);
    expect(onPromptFlushed).toHaveBeenCalledWith("req-prompt-1");
  });

  it("ignores a stale resume_failed and drops the queue only on the matching failure", () => {
    const onMessage = vi.fn();
    const onQueueDropped = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(onMessage, () => "session-1", undefined, undefined, onQueueDropped)
    );

    const ws1 = instances[0];
    act(() => {
      ws1.simulateOpen();
    });
    const firstResume = JSON.parse(ws1.sent[0]);

    act(() => {
      ws1.simulateClose();
    });
    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued",
      requestId: "req-prompt-1",
    };
    act(() => {
      result.current.send(prompt);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const ws2 = instances[1];
    act(() => {
      ws2.simulateOpen();
    });
    const secondResume = JSON.parse(ws2.sent[0]);
    expect(ws2.sent).toHaveLength(1);

    act(() => {
      ws2.simulateMessage(
        JSON.stringify({
          type: "resume_failed",
          sessionId: "session-1",
          message: "stale failure",
          requestId: firstResume.requestId,
        })
      );
    });
    expect(ws2.sent).toHaveLength(1);
    expect(onQueueDropped).not.toHaveBeenCalled();
    expect(onMessage).toHaveBeenCalledWith({
      type: "resume_failed",
      sessionId: "session-1",
      message: "stale failure",
      requestId: firstResume.requestId,
    });

    act(() => {
      ws2.simulateMessage(
        JSON.stringify({
          type: "resume_failed",
          sessionId: "session-1",
          message: "boom",
          requestId: secondResume.requestId,
        })
      );
    });
    expect(ws2.sent).toHaveLength(1);
    expect(onQueueDropped).toHaveBeenCalledTimes(1);
    expect(onQueueDropped).toHaveBeenCalledWith("req-prompt-1", "resume_failed");
  });

  it("flushes the queue immediately on connect when there is no active session", () => {
    const { result } = renderHook(() => useWebSocket(undefined, () => null));
    const ws = instances[0];

    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued",
    };
    act(() => {
      result.current.send(prompt);
    });

    act(() => {
      ws.simulateOpen();
    });
    expect(ws.sent).toHaveLength(1);
    expect(JSON.parse(ws.sent[0])).toEqual(prompt);
  });

  it("generates a new requestId for each reconnect resume_session", () => {
    const { result } = renderHook(() =>
      useWebSocket(undefined, () => "session-1")
    );

    // First connect: resume_session carries a requestId.
    const ws1 = instances[0];
    act(() => {
      ws1.simulateOpen();
    });
    const firstResume = JSON.parse(ws1.sent[0]);
    expect(firstResume.requestId).toEqual(expect.any(String));
    expect(result.current.getResumeRequestId()).toBe(firstResume.requestId);

    // Disconnect and reconnect: a new requestId is generated.
    act(() => {
      ws1.simulateClose();
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const ws2 = instances[1];
    act(() => {
      ws2.simulateOpen();
    });
    const secondResume = JSON.parse(ws2.sent[0]);
    expect(secondResume.requestId).toEqual(expect.any(String));
    expect(secondResume.requestId).not.toBe(firstResume.requestId);
    expect(result.current.getResumeRequestId()).toBe(secondResume.requestId);
  });

  it("returns null from getResumeRequestId when no resume has been initiated", () => {
    const { result } = renderHook(() => useWebSocket(undefined, () => null));
    expect(result.current.getResumeRequestId()).toBeNull();
  });

  it("notifies onResumeStarted synchronously with the requestId before the wire message is sent", () => {
    const onResumeStarted = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(undefined, () => "session-1", onResumeStarted)
    );

    const ws = instances[0];
    // Intercept send to capture ordering relative to onResumeStarted.
    const sendSpy = vi.spyOn(ws, "send");

    act(() => {
      ws.simulateOpen();
    });

    expect(onResumeStarted).toHaveBeenCalledTimes(1);
    const notifiedRequestId = onResumeStarted.mock.calls[0][0];
    expect(typeof notifiedRequestId).toBe("string");
    // The callback must have been invoked before the resume_session wire
    // message was sent, so useChatSession can dispatch resume_started first.
    expect(sendSpy).toHaveBeenCalledTimes(1);
    const sentPayload = JSON.parse(sendSpy.mock.calls[0][0]);
    expect(sentPayload.type).toBe("resume_session");
    expect(sentPayload.requestId).toBe(notifiedRequestId);
    // The same id is exposed via getResumeRequestId for any other consumers.
    expect(result.current.getResumeRequestId()).toBe(notifiedRequestId);
    // Ordering: callback invoked before send.
    expect(onResumeStarted.mock.invocationCallOrder[0]).toBeLessThan(
      sendSpy.mock.invocationCallOrder[0]
    );
  });

  it("does not call onResumeStarted when there is no active session on open", () => {
    const onResumeStarted = vi.fn();
    renderHook(() => useWebSocket(undefined, () => null, onResumeStarted));

    const ws = instances[0];
    act(() => {
      ws.simulateOpen();
    });

    expect(onResumeStarted).not.toHaveBeenCalled();
  });

  it("calls onResumeStarted with a fresh requestId on each reconnect", () => {
    const onResumeStarted = vi.fn();
    renderHook(() => useWebSocket(undefined, () => "session-1", onResumeStarted));

    const ws1 = instances[0];
    act(() => {
      ws1.simulateOpen();
    });
    const firstId = onResumeStarted.mock.calls[0][0];

    act(() => {
      ws1.simulateClose();
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const ws2 = instances[1];
    act(() => {
      ws2.simulateOpen();
    });

    expect(onResumeStarted).toHaveBeenCalledTimes(2);
    const secondId = onResumeStarted.mock.calls[1][0];
    expect(secondId).not.toBe(firstId);
  });

  it("rejects a second queued prompt with the cap error and does not grow the queue", () => {
    const { result } = renderHook(() => useWebSocket());
    const ws = instances[0];

    const prompt1: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "first",
      requestId: "req-1",
    };
    const prompt2: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "second",
      requestId: "req-2",
    };

    act(() => {
      const r1 = result.current.send(prompt1);
      expect(r1).toEqual({ sent: false, queued: true });
    });
    act(() => {
      const r2 = result.current.send(prompt2);
      expect(r2).toEqual({
        sent: false,
        queued: false,
        error: "A prompt is already waiting for the connection.",
      });
    });

    // Only the first prompt is flushed on connect.
    act(() => {
      ws.simulateOpen();
    });
    expect(ws.sent).toHaveLength(1);
    expect(JSON.parse(ws.sent[0])).toEqual(prompt1);
  });

  it("notifies onPromptFlushed when a queued prompt is sent to the server", () => {
    const onPromptFlushed = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(undefined, () => null, undefined, onPromptFlushed)
    );
    const ws = instances[0];

    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued",
      requestId: "req-flush-1",
    };
    act(() => {
      result.current.send(prompt);
    });
    expect(onPromptFlushed).not.toHaveBeenCalled();

    act(() => {
      ws.simulateOpen();
    });
    expect(onPromptFlushed).toHaveBeenCalledWith("req-flush-1");
  });

  it("drops a queued prompt after the expiration window and notifies onQueueDropped", () => {
    const onQueueDropped = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(undefined, () => null, undefined, undefined, onQueueDropped)
    );
    const ws = instances[0];

    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "will-expire",
      requestId: "req-expire-1",
    };
    act(() => {
      result.current.send(prompt);
    });

    // Advance past the 60s expiration window.
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(onQueueDropped).toHaveBeenCalledWith("req-expire-1", "expired");
    // The expired prompt is not flushed on connect.
    act(() => {
      ws.simulateOpen();
    });
    expect(ws.sent).toHaveLength(0);
  });

  it("cancelQueuedPrompt removes the prompt from the queue and notifies onQueueDropped", () => {
    const onQueueDropped = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(undefined, () => null, undefined, undefined, onQueueDropped)
    );
    const ws = instances[0];

    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "will-cancel",
      requestId: "req-cancel-1",
    };
    act(() => {
      result.current.send(prompt);
    });

    let cancelled = false;
    act(() => {
      cancelled = result.current.cancelQueuedPrompt("req-cancel-1");
    });
    expect(cancelled).toBe(true);
    expect(onQueueDropped).toHaveBeenCalledWith("req-cancel-1", "cancelled");

    // The cancelled prompt is not flushed on connect.
    act(() => {
      ws.simulateOpen();
    });
    expect(ws.sent).toHaveLength(0);
  });

  it("cancelQueuedPrompt returns false for an unknown requestId", () => {
    const { result } = renderHook(() => useWebSocket());

    let cancelled = true;
    act(() => {
      cancelled = result.current.cancelQueuedPrompt("nonexistent");
    });
    expect(cancelled).toBe(false);
  });

  it("notifies onPromptFlushed for each queued prompt after session_resumed", () => {
    const onPromptFlushed = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(undefined, () => "session-1", undefined, onPromptFlushed)
    );

    const ws1 = instances[0];
    act(() => {
      ws1.simulateOpen();
    });
    // First connect sends resume_session; no prompt yet.
    const firstResume = JSON.parse(ws1.sent[0]);

    // Disconnect and queue a prompt.
    act(() => {
      ws1.simulateClose();
    });
    const prompt: WSOutbound = {
      type: "prompt",
      sessionId: "session-1",
      text: "queued-after-reconnect",
      requestId: "req-prompt-flush",
    };
    act(() => {
      result.current.send(prompt);
    });

    // Reconnect.
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const ws2 = instances[1];
    act(() => {
      ws2.simulateOpen();
    });
    const secondResume = JSON.parse(ws2.sent[0]);
    expect(onPromptFlushed).not.toHaveBeenCalled();

    // On session_resumed the queued prompt flushes and onPromptFlushed fires.
    act(() => {
      ws2.simulateMessage(
        JSON.stringify({
          type: "session_resumed",
          sessionId: "session-1",
          requestId: secondResume.requestId,
        })
      );
    });
    expect(onPromptFlushed).toHaveBeenCalledWith("req-prompt-flush");
    // firstResume's session_resumed was never received, so no spurious flush.
    expect(onPromptFlushed).toHaveBeenCalledTimes(1);
    // Sanity: the first resume id differs from the second.
    expect(firstResume.requestId).not.toBe(secondResume.requestId);
  });

  it("notifies onConnectionLost with in-flight permission info on disconnect", () => {
    const onConnectionLost = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(undefined, undefined, undefined, undefined, undefined, onConnectionLost)
    );
    const ws = instances[0];
    act(() => {
      ws.simulateOpen();
    });

    // Send a permission_response on the wire.
    act(() => {
      result.current.send({
        type: "permission_response",
        sessionId: "session-1",
        toolCallId: "tc-1",
        requestId: "corr-1",
        decision: "allow",
      });
    });
    expect(onConnectionLost).not.toHaveBeenCalled();

    // Socket closes before permission_received.
    act(() => {
      ws.simulateClose();
    });
    expect(onConnectionLost).toHaveBeenCalledTimes(1);
    expect(onConnectionLost).toHaveBeenCalledWith({
      permission: { toolCallId: "tc-1", correlationId: "corr-1" },
      cancelRequestId: null,
    });
  });

  it("notifies onConnectionLost with in-flight cancel info on disconnect", () => {
    const onConnectionLost = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(undefined, undefined, undefined, undefined, undefined, onConnectionLost)
    );
    const ws = instances[0];
    act(() => {
      ws.simulateOpen();
    });

    act(() => {
      result.current.send({
        type: "cancel",
        sessionId: "session-1",
        requestId: "req-cancel-1",
      });
    });

    act(() => {
      ws.simulateClose();
    });
    expect(onConnectionLost).toHaveBeenCalledWith({
      permission: null,
      cancelRequestId: "req-cancel-1",
    });
  });

  it("does not notify onConnectionLost when nothing is in flight", () => {
    const onConnectionLost = vi.fn();
    renderHook(() =>
      useWebSocket(undefined, undefined, undefined, undefined, undefined, onConnectionLost)
    );
    const ws = instances[0];
    act(() => {
      ws.simulateOpen();
    });
    act(() => {
      ws.simulateClose();
    });
    expect(onConnectionLost).not.toHaveBeenCalled();
  });

  it("batches consecutive stream chunks before notifying React", () => {
    const onMessage = vi.fn();
    renderHook(() => useWebSocket(onMessage));
    const ws = instances[0];

    act(() => {
      ws.simulateOpen();
      for (let i = 0; i < 19; i += 1) {
        ws.simulateMessage(
          JSON.stringify({
            type: "session_update",
            sessionId: "session-1",
            update: { type: "agent_message_chunk", chunk: String(i % 10) },
          })
        );
      }
    });
    expect(onMessage).not.toHaveBeenCalled();

    act(() => {
      ws.simulateMessage(
        JSON.stringify({
          type: "session_update",
          sessionId: "session-1",
          update: { type: "agent_message_chunk", chunk: "x" },
        })
      );
    });

    expect(onMessage).toHaveBeenCalledTimes(1);
    expect(onMessage.mock.calls[0][0]).toMatchObject({
      type: "session_update",
      sessionId: "session-1",
      update: {
        type: "agent_message_chunk",
        chunk: "0123456789012345678x",
      },
    });
  });

  it("flushes a partial stream batch before a non-stream event", () => {
    const onMessage = vi.fn();
    renderHook(() => useWebSocket(onMessage));
    const ws = instances[0];

    act(() => {
      ws.simulateOpen();
      ws.simulateMessage(
        JSON.stringify({
          type: "session_update",
          sessionId: "session-1",
          update: { type: "agent_message_chunk", chunk: "Hello" },
        })
      );
      ws.simulateMessage(
        JSON.stringify({
          type: "turn_complete",
          sessionId: "session-1",
          stopReason: "end_turn",
          result: {},
          requestId: "req-prompt-1",
        })
      );
    });

    expect(onMessage).toHaveBeenCalledTimes(2);
    expect(onMessage.mock.calls[0][0]).toMatchObject({
      type: "session_update",
      update: { type: "agent_message_chunk", chunk: "Hello" },
    });
    expect(onMessage.mock.calls[1][0]).toMatchObject({ type: "turn_complete" });
  });

  it("flushes a partial stream batch after the latency cap", () => {
    const onMessage = vi.fn();
    renderHook(() => useWebSocket(onMessage));
    const ws = instances[0];

    act(() => {
      ws.simulateOpen();
      ws.simulateMessage(
        JSON.stringify({
          type: "session_update",
          sessionId: "session-1",
          update: { type: "agent_thought_chunk", chunk: "thinking" },
        })
      );
    });
    expect(onMessage).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(50);
    });
    expect(onMessage).toHaveBeenCalledTimes(1);
    expect(onMessage.mock.calls[0][0]).toMatchObject({
      type: "session_update",
      update: { type: "agent_thought_chunk", chunk: "thinking" },
    });
  });

  it("clears in-flight permission tracking on permission_received", () => {
    const onConnectionLost = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(undefined, undefined, undefined, undefined, undefined, onConnectionLost)
    );
    const ws = instances[0];
    act(() => {
      ws.simulateOpen();
    });
    act(() => {
      result.current.send({
        type: "permission_response",
        sessionId: "session-1",
        toolCallId: "tc-1",
        requestId: "corr-1",
        decision: "allow",
      });
    });
    // Backend acknowledges before any disconnect.
    act(() => {
      ws.simulateMessage(
        JSON.stringify({
          type: "permission_received",
          sessionId: "session-1",
          toolCallId: "tc-1",
          requestId: "corr-1",
          decision: "allow",
        })
      );
    });
    // A subsequent disconnect must not report a stale in-flight
    // permission.
    act(() => {
      ws.simulateClose();
    });
    expect(onConnectionLost).not.toHaveBeenCalled();
  });

  it("clears in-flight cancel tracking on turn_cancelled", () => {
    const onConnectionLost = vi.fn();
    const { result } = renderHook(() =>
      useWebSocket(undefined, undefined, undefined, undefined, undefined, onConnectionLost)
    );
    const ws = instances[0];
    act(() => {
      ws.simulateOpen();
    });
    act(() => {
      result.current.send({
        type: "cancel",
        sessionId: "session-1",
        requestId: "req-cancel-1",
      });
    });
    act(() => {
      ws.simulateMessage(
        JSON.stringify({
          type: "turn_cancelled",
          sessionId: "session-1",
          requestId: "req-cancel-1",
        })
      );
    });
    act(() => {
      ws.simulateClose();
    });
    expect(onConnectionLost).not.toHaveBeenCalled();
  });

  describe("in-flight operation ownership", () => {
    function connect() {
      const onMessage = vi.fn();
      const onConnectionLost = vi.fn();
      const { result } = renderHook(() =>
        useWebSocket(onMessage, undefined, undefined, undefined, undefined, onConnectionLost)
      );
      const ws = instances[0];
      act(() => {
        ws.simulateOpen();
      });
      return { result, ws, onMessage, onConnectionLost };
    }

    function sendPermissionResponse(result: ReturnType<typeof connect>["result"]) {
      act(() => {
        result.current.send({
          type: "permission_response",
          sessionId: "session-1",
          toolCallId: "tc-2",
          requestId: "corr-2",
          decision: "allow",
        });
      });
    }

    function sendPromptAndCancel(result: ReturnType<typeof connect>["result"]) {
      act(() => {
        result.current.send({
          type: "prompt",
          sessionId: "session-1",
          text: "run it",
          requestId: "req-prompt-1",
        });
        result.current.send({
          type: "cancel",
          sessionId: "session-1",
          requestId: "req-cancel-1",
        });
      });
    }

    it.each([
      ["wrong toolCallId", { type: "permission_received", sessionId: "session-1", toolCallId: "tc-1", requestId: "corr-2", decision: "allow" }],
      ["wrong correlation id", { type: "permission_received", sessionId: "session-1", toolCallId: "tc-2", requestId: "corr-1", decision: "allow" }],
      ["missing correlation id", { type: "permission_received", sessionId: "session-1", toolCallId: "tc-2", decision: "allow" }],
      ["wrong session", { type: "permission_received", sessionId: "session-2", toolCallId: "tc-2", requestId: "corr-2", decision: "allow" }],
      ["permission_expired wrong toolCallId", { type: "permission_expired", sessionId: "session-1", toolCallId: "tc-9", requestId: 7 }],
      ["permission_expired wrong session", { type: "permission_expired", sessionId: "session-2", toolCallId: "tc-2" }],
      ["error with a stale requestId", { type: "error", sessionId: "session-1", requestId: "corr-9", operation: "permission_response", message: "boom" }],
      ["error with a null requestId", { type: "error", sessionId: "session-1", requestId: null, operation: "permission_response", message: "boom" }],
      ["error without a requestId", { type: "error", sessionId: "session-1", operation: "permission_response", message: "boom" }],
      ["error for another session", { type: "error", sessionId: "session-2", requestId: "corr-2", operation: "permission_response", message: "boom" }],
    ])("keeps permission tracking on a stale %s and still reports it on disconnect", (_name, inbound) => {
      const { result, ws, onMessage, onConnectionLost } = connect();
      sendPermissionResponse(result);

      act(() => {
        ws.simulateMessage(JSON.stringify(inbound));
      });
      expect(onMessage).toHaveBeenCalledWith(inbound);

      act(() => {
        ws.simulateClose();
      });
      expect(onConnectionLost).toHaveBeenCalledTimes(1);
      expect(onConnectionLost).toHaveBeenCalledWith({
        permission: { toolCallId: "tc-2", correlationId: "corr-2" },
        cancelRequestId: null,
      });
    });

    it.each([
      ["permission_received", { type: "permission_received", sessionId: "session-1", toolCallId: "tc-2", requestId: "corr-2", decision: "allow" }],
      ["permission_expired same session", { type: "permission_expired", sessionId: "session-1", toolCallId: "tc-2", requestId: 7 }],
      ["permission_expired without a sessionId", { type: "permission_expired", toolCallId: "tc-2" }],
      ["error with the matching requestId", { type: "error", sessionId: "session-1", requestId: "corr-2", operation: "permission_response", message: "boom" }],
      ["error with the matching requestId and null session", { type: "error", sessionId: null, requestId: "corr-2", operation: "permission_response", message: "boom" }],
    ])("clears permission tracking on a matching %s", (_name, inbound) => {
      const { result, ws, onConnectionLost } = connect();
      sendPermissionResponse(result);

      act(() => {
        ws.simulateMessage(JSON.stringify(inbound));
      });
      act(() => {
        ws.simulateClose();
      });
      expect(onConnectionLost).not.toHaveBeenCalled();
    });

    it.each([
      ["turn_cancelled wrong requestId", { type: "turn_cancelled", sessionId: "session-1", requestId: "req-cancel-9" }],
      ["turn_cancelled wrong session", { type: "turn_cancelled", sessionId: "session-2", requestId: "req-cancel-1" }],
      ["transition_cancelled wrong requestId", { type: "transition_cancelled", sessionId: "session-1", requestId: "req-cancel-9" }],
      ["turn_complete wrong session", { type: "turn_complete", sessionId: "session-2", result: {}, requestId: "req-prompt-1" }],
      ["turn_complete stale prompt id", { type: "turn_complete", sessionId: "session-1", result: {}, requestId: "req-prompt-9" }],
      ["error with a stale requestId", { type: "error", sessionId: "session-1", requestId: "req-cancel-9", operation: "cancel", message: "boom" }],
      ["error with a null requestId", { type: "error", sessionId: "session-1", requestId: null, operation: "cancel", message: "boom" }],
      ["error without a requestId", { type: "error", sessionId: "session-1", operation: "cancel", message: "boom" }],
      ["error for another session", { type: "error", sessionId: "session-2", requestId: "req-cancel-1", operation: "cancel", message: "boom" }],
    ])("keeps cancel tracking on a stale %s and still reports it on disconnect", (_name, inbound) => {
      const { result, ws, onMessage, onConnectionLost } = connect();
      sendPromptAndCancel(result);

      act(() => {
        ws.simulateMessage(JSON.stringify(inbound));
      });
      expect(onMessage).toHaveBeenCalledWith(inbound);

      act(() => {
        ws.simulateClose();
      });
      expect(onConnectionLost).toHaveBeenCalledTimes(1);
      expect(onConnectionLost).toHaveBeenCalledWith({
        permission: null,
        cancelRequestId: "req-cancel-1",
      });
    });

    it.each([
      ["turn_cancelled", { type: "turn_cancelled", sessionId: "session-1", requestId: "req-cancel-1" }],
      ["transition_cancelled", { type: "transition_cancelled", sessionId: "session-1", requestId: "req-cancel-1" }],
      ["turn_complete for the sent prompt", { type: "turn_complete", sessionId: "session-1", result: {}, requestId: "req-prompt-1" }],
      ["error with the matching requestId", { type: "error", sessionId: "session-1", requestId: "req-cancel-1", operation: "cancel", message: "boom" }],
      ["error with the matching requestId and null session", { type: "error", sessionId: null, requestId: "req-cancel-1", operation: "cancel", message: "boom" }],
    ])("clears cancel tracking on a matching %s", (_name, inbound) => {
      const { result, ws, onConnectionLost } = connect();
      sendPromptAndCancel(result);

      act(() => {
        ws.simulateMessage(JSON.stringify(inbound));
      });
      act(() => {
        ws.simulateClose();
      });
      expect(onConnectionLost).not.toHaveBeenCalled();
    });

    it("keeps a flushed prompt's cancel tracked through a stale same-session turn_complete", () => {
      const onConnectionLost = vi.fn();
      const { result } = renderHook(() =>
        useWebSocket(undefined, () => null, undefined, undefined, undefined, onConnectionLost)
      );
      const ws = instances[0];

      act(() => {
        result.current.send({
          type: "prompt",
          sessionId: "session-1",
          text: "queued",
          requestId: "req-flush-1",
        });
      });
      act(() => {
        ws.simulateOpen();
      });
      expect(JSON.parse(ws.sent[0])).toMatchObject({ type: "prompt", requestId: "req-flush-1" });

      act(() => {
        result.current.send({
          type: "cancel",
          sessionId: "session-1",
          requestId: "req-cancel-1",
        });
      });
      act(() => {
        ws.simulateMessage(
          JSON.stringify({
            type: "turn_complete",
            sessionId: "session-1",
            result: {},
            requestId: "req-flush-9",
          })
        );
      });
      act(() => {
        ws.simulateClose();
      });
      expect(onConnectionLost).toHaveBeenCalledWith({
        permission: null,
        cancelRequestId: "req-cancel-1",
      });
    });

    it("clears cancel tracking on a same-session turn_complete when no prompt id is known", () => {
      const { result, ws, onConnectionLost } = connect();
      act(() => {
        result.current.send({
          type: "cancel",
          sessionId: "session-1",
          requestId: "req-cancel-1",
        });
      });
      act(() => {
        ws.simulateMessage(
          JSON.stringify({
            type: "turn_complete",
            sessionId: "session-1",
            result: {},
            requestId: "req-prompt-foreign",
          })
        );
      });
      act(() => {
        ws.simulateClose();
      });
      expect(onConnectionLost).not.toHaveBeenCalled();
    });
  });

  describe("reconnect with an in-flight queued prompt", () => {
    function wireChat() {
      const order: string[] = [];
      const { result } = renderHook(() => {
        const chatRef = useRef<UseChatSessionReturn | null>(null);
        const ws = useWebSocket(
          (msg) => {
            order.push(`server:${msg.type}`);
            chatRef.current?.onServerMessage(msg);
          },
          () => (chatRef.current?.sessionLive ? chatRef.current.activeSessionId : null),
          (requestId) => chatRef.current?.notifyResumeStarted(requestId),
          (requestId) => {
            order.push(`flushed:${requestId}`);
            chatRef.current?.onPromptFlushed(requestId);
          },
          (requestId, reason) => {
            order.push(`dropped:${reason}`);
            chatRef.current?.onQueueDropped(requestId, reason);
          },
          (payload) => chatRef.current?.onConnectionLost(payload)
        );
        const chat = useChatSession(ws.send, undefined, {
          cancelQueuedPrompt: ws.cancelQueuedPrompt,
        });
        chatRef.current = chat;
        return { ws, chat };
      });
      return { result, order };
    }

    function openSessionAndQueuePrompt(result: { current: { chat: UseChatSessionReturn } }) {
      const ws1 = instances[0];
      act(() => {
        ws1.simulateOpen();
      });
      act(() => {
        expect(result.current.chat.newSession("c:\\proj")).toBe(true);
      });
      const created = JSON.parse(ws1.sent[0]);
      expect(created.type).toBe("new_session");
      act(() => {
        ws1.simulateMessage(
          JSON.stringify({
            type: "session_created",
            sessionId: "session-1",
            requestId: created.requestId,
          })
        );
      });
      expect(result.current.chat.activeSessionId).toBe("session-1");
      expect(result.current.chat.status).toBe("ready");

      act(() => {
        ws1.simulateClose();
      });
      act(() => {
        expect(result.current.chat.sendPrompt("queued while down")).toBe(true);
      });
      expect(result.current.chat.status).toBe("queued");
      expect(result.current.chat.queued).toBe(true);

      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(instances).toHaveLength(2);
      const ws2 = instances[1];
      act(() => {
        ws2.simulateOpen();
      });
      expect(ws2.sent).toHaveLength(1);
      const resume = JSON.parse(ws2.sent[0]);
      expect(resume.type).toBe("resume_session");
      expect(resume.sessionId).toBe("session-1");
      return { ws2, resume };
    }

    it("keeps the queued prompt cancellable, then streaming after the flush and resume ack", () => {
      const { result, order } = wireChat();
      const { ws2, resume } = openSessionAndQueuePrompt(result);

      expect(result.current.chat.status).toBe("queued");
      expect(result.current.chat.queued).toBe(true);

      act(() => {
        ws2.simulateMessage(
          JSON.stringify({
            type: "session_resumed",
            sessionId: "session-1",
            requestId: "req-stale",
          })
        );
      });
      expect(ws2.sent).toHaveLength(1);
      expect(result.current.chat.status).toBe("queued");

      order.length = 0;
      act(() => {
        ws2.simulateMessage(
          JSON.stringify({
            type: "session_resumed",
            sessionId: "session-1",
            requestId: resume.requestId,
          })
        );
      });
      expect(ws2.sent).toHaveLength(2);
      const flushed = JSON.parse(ws2.sent[1]);
      expect(flushed).toMatchObject({
        type: "prompt",
        sessionId: "session-1",
        text: "queued while down",
      });
      expect(order).toEqual([`flushed:${flushed.requestId}`, "server:session_resumed"]);
      expect(result.current.chat.status).toBe("streaming");
      expect(result.current.chat.isStreaming).toBe(true);
      expect(result.current.chat.messages.filter((m) => m.role === "user")).toHaveLength(1);
    });

    it("drops the optimistic queued prompt on resume_failed and lands paused_readonly", () => {
      const { result, order } = wireChat();
      const { ws2, resume } = openSessionAndQueuePrompt(result);

      act(() => {
        ws2.simulateMessage(
          JSON.stringify({
            type: "session_resumed",
            sessionId: "session-1",
            requestId: "req-stale",
          })
        );
      });
      expect(ws2.sent).toHaveLength(1);

      order.length = 0;
      act(() => {
        ws2.simulateMessage(
          JSON.stringify({
            type: "resume_failed",
            sessionId: "session-1",
            message: "gone",
            requestId: resume.requestId,
          })
        );
      });
      expect(order).toEqual(["dropped:resume_failed", "server:resume_failed"]);
      expect(ws2.sent).toHaveLength(1);
      expect(result.current.chat.status).toBe("paused_readonly");
      expect(result.current.chat.error).toBe("Could not resume session: gone");
      expect(result.current.chat.messages.filter((m) => m.role === "user")).toHaveLength(0);
      expect(result.current.chat.queued).toBe(false);
      expect(result.current.chat.sessionLive).toBe(false);

      act(() => {
        expect(result.current.chat.startSession()).toBe(true);
      });
      expect(result.current.chat.status).toBe("resuming");
      expect(ws2.sent).toHaveLength(2);
      const restart = JSON.parse(ws2.sent[1]);
      expect(restart).toMatchObject({
        type: "resume_session",
        sessionId: "session-1",
      });

      act(() => {
        ws2.simulateMessage(
          JSON.stringify({
            type: "session_resumed",
            sessionId: "session-1",
            requestId: restart.requestId,
          })
        );
      });
      expect(result.current.chat.status).toBe("ready");
      expect(result.current.chat.sessionLive).toBe(true);

      act(() => {
        expect(result.current.chat.sendPrompt("after failure")).toBe(true);
      });
      expect(ws2.sent).toHaveLength(3);
      expect(JSON.parse(ws2.sent[2])).toMatchObject({
        type: "prompt",
        sessionId: "session-1",
        text: "after failure",
      });
      expect(result.current.chat.status).toBe("streaming");
    });
  });
});
