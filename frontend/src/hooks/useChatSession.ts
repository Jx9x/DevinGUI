import { useCallback, useMemo, useReducer, useRef } from "react";
import type {
  UIMessage,
  ImageAttachment,
  ReasoningStep,
  MessageMetrics,
  WSInbound,
  WSOutbound,
  UsageUpdate,
  SendResult,
  QueueDropReason,
  ConnectionLostPayload,
  ConversationCostEstimate,
} from "../types";
import { chatReducer, initialChatState, requestIdMatches } from "../chatSessionState";
import type { PermissionRequest, SessionStatus } from "../chatSessionState";

export { chatReducer, initialChatState, requestIdMatches };
export type { PermissionRequest, SessionStatus };
export type { ChatAction, ChatState } from "../chatSessionState";

export interface ChatSessionCallbacks {
  onSessionCreated?: (sessionId: string, model?: string) => void;
  onTurnComplete?: () => void;
}

export interface UseChatSessionReturn {
  activeSessionId: string | null;
  status: SessionStatus;
  messages: UIMessage[];
  reasoningSteps: ReasoningStep[];
  usage: UsageUpdate;
  costEstimate: ConversationCostEstimate | null;
  error: string | null;
  connectionError: string | null;
  permissionRequest: PermissionRequest | null;
  isStreaming: boolean;
  queued: boolean;
  loadingHistory: boolean;
  resuming: boolean;
  sessionLive: boolean;
  aggregateMetrics: MessageMetrics;
  onServerMessage: (msg: WSInbound) => void;
  // Called synchronously by `useWebSocket` immediately before it sends
  // `resume_session` on the wire. Dispatches `resume_started` with the exact
  // request id that will be sent, so the reducer records
  // `lastRequestIds.resume` and enters `resuming` before a fast
  // `session_resumed` response can arrive.
  notifyResumeStarted: (requestId: string) => void;
  // Called by `useWebSocket` when a queued prompt is finally sent to the
  // server. Dispatches `prompt_flushed` so the matching turn transitions from
  // `queued` to `streaming`.
  onPromptFlushed: (requestId: string) => void;
  // Called by `useWebSocket` when a queued prompt is dropped before reaching
  // the server (resume_failed, expiration, or cancellation). Dispatches
  // `prompt_queue_dropped` so the matching turn transitions to `ready` with
  // an appropriate error (or no error for cancellation).
  onQueueDropped: (requestId: string, reason: QueueDropReason) => void;
  // Called by `useWebSocket` when the socket closes or errors while a
  // permission response or cancel command is in flight. Dispatches
  // `connection_lost` so the reducer marks the permission panel
  // `interrupted`/retryable and moves an indeterminate cancel back to
  // `streaming`.
  onConnectionLost: (payload: ConnectionLostPayload) => void;
  newSession: (cwd: string, model?: string, permissionMode?: "bypass") => boolean;
  selectSession: (sessionId: string) => boolean;
  startSession: (model?: string, permissionMode?: "bypass") => boolean;
  clearSession: () => void;
  // `model` is the model the user has selected in the dropdown. It is sent
  // with every prompt so the backend can re-apply it to the live ACP session
  // (which may have been started with a different model) before the turn runs.
  // `images` may be a legacy model string for backwards-compatible callers.
  sendPrompt: (text: string, images?: ImageAttachment[] | string, model?: string) => boolean;
  cancel: () => void;
  respondPermission: (decision: "allow" | "deny", optionId?: string) => void;
  // Discards the currently interrupted permission panel. Called by the UI
  // when the user explicitly chooses to dismiss the decision rather than
  // retry. Clears the panel and surfaces the interrupt error so the user
  // knows the decision was not delivered.
  discardPermission: () => void;
}

// Monotonic counter used to build unique correlation ids for outgoing
// commands. Combined with a timestamp so ids are unique across reloads.
let _requestCounter = 0;
function generateRequestId(): string {
  _requestCounter += 1;
  return `req-${Date.now()}-${_requestCounter}`;
}

// Derive a short, deterministic slug from message content so user message
// IDs stay stable when the same prompt is reprocessed.
function contentSlug(text: string): string {
  const slug = text
    .trim()
    .slice(0, 24)
    .replace(/\s+/g, "-")
    .replace(/[^a-zA-Z0-9_-]/g, "");
  return slug || "msg";
}

// Build a deterministic user message ID from the session, turn index, and
// content slug.
function userIdFor(sessionId: string, turnIndex: number, text: string): string {
  return `user-${sessionId}-${turnIndex}-${contentSlug(text)}`;
}

export interface ChatSessionWsControls {
  // Removes a queued prompt from the WebSocket queue and notifies
  // `onQueueDropped` with reason `"cancelled"`. Returns true if a matching
  // queued prompt was found and removed.
  cancelQueuedPrompt: (requestId: string) => boolean;
}

export function useChatSession(
  send: (msg: WSOutbound) => SendResult,
  callbacks?: ChatSessionCallbacks,
  wsControls?: ChatSessionWsControls
): UseChatSessionReturn {
  const sendRef = useRef(send);
  sendRef.current = send;

  const callbacksRef = useRef(callbacks);
  callbacksRef.current = callbacks;

  const wsControlsRef = useRef(wsControls);
  wsControlsRef.current = wsControls;

  const [state, dispatch] = useReducer(chatReducer, initialChatState);

  // Mirror the request ids used by the reducer to accept/reject stale
  // `session_created` and `turn_complete` messages. Reading these refs inside
  // the synchronous `onServerMessage` handler avoids depending on a stale
  // closure of `state` (the callback has no deps), while still reflecting the
  // latest committed state. The reducer processes the same action against the
  // same committed state, so the callback guard mirrors the reducer exactly.
  const latestNewSessionRequestIdRef = useRef(state.lastRequestIds.new_session);
  const latestPromptRequestIdRef = useRef(state.lastRequestIds.prompt);
  latestNewSessionRequestIdRef.current = state.lastRequestIds.new_session;
  latestPromptRequestIdRef.current = state.lastRequestIds.prompt;

  // Called synchronously by `useWebSocket` immediately before it sends
  // `resume_session` on the wire. Dispatching `resume_started` here (instead
  // of inferring it from the `connected` boolean in a React effect) ensures
  // the reducer records `lastRequestIds.resume` and enters `resuming` before
  // a fast `session_resumed` response can arrive, eliminating the race that
  // left the UI stuck in `resuming`.
  const notifyResumeStarted = useCallback((requestId: string) => {
    dispatch({ type: "resume_started", requestId });
  }, []);

  // Called by `useWebSocket` when a queued prompt is finally sent to the
  // server. Dispatches `prompt_flushed` so the matching turn transitions from
  // `queued` to `streaming`.
  const onPromptFlushed = useCallback((requestId: string) => {
    dispatch({ type: "prompt_flushed", requestId });
  }, []);

  // Called by `useWebSocket` when a queued prompt is dropped before reaching
  // the server (resume_failed, expiration, or cancellation). Maps the reason
  // to a user-visible error (none for cancellation) and dispatches
  // `prompt_queue_dropped` so the matching turn transitions to `ready`.
  const onQueueDropped = useCallback((requestId: string, reason: QueueDropReason) => {
    const error =
      reason === "expired"
        ? "Your prompt timed out waiting for the connection."
        : reason === "resume_failed"
          ? "Connection could not be resumed; your prompt was not sent."
          : null;
    dispatch({ type: "prompt_queue_dropped", requestId, error });
  }, []);

  // Called by `useWebSocket` when the socket closes or errors while a
  // permission response or cancel command is in flight. Dispatches
  // `connection_lost` so the reducer marks the permission panel
  // `interrupted`/retryable and moves an indeterminate cancel back to
  // `streaming`.
  const onConnectionLost = useCallback((payload: ConnectionLostPayload) => {
    dispatch({ type: "connection_lost", payload });
  }, []);

  const onServerMessage = useCallback((msg: WSInbound) => {
    dispatch({ type: "server_message", msg });
    if (msg.type === "session_created") {
      // Only fire the side-effect callback when the reducer would also
      // accept the message, so a stale `session_created` (e.g. the user
      // selected another session while creation was in flight) cannot
      // change the selected model or refresh sessions/models.
      if (requestIdMatches(msg.requestId, latestNewSessionRequestIdRef.current)) {
        callbacksRef.current?.onSessionCreated?.(msg.sessionId, msg.model);
      }
    } else if (msg.type === "turn_complete") {
      // Gate the session-list refresh the same way: a stale
      // `turn_complete` for a previous prompt must not trigger it. When no
      // prompt id is stored, mirror the reducer and accept the completion.
      const expectedPromptId = latestPromptRequestIdRef.current;
      if (
        expectedPromptId === undefined ||
        requestIdMatches(msg.requestId, expectedPromptId)
      ) {
        callbacksRef.current?.onTurnComplete?.();
      }
    }
  }, []);

  const newSession = useCallback((cwd: string, model?: string, permissionMode?: "bypass") => {
    const requestId = generateRequestId();
    const result = sendRef.current({ type: "new_session", cwd, model, permissionMode, requestId });
    if (result.sent) {
      dispatch({ type: "new_session_initiated", requestId });
      return true;
    }
    dispatch({
      type: "set_error",
      error: "WebSocket not connected. Could not start a new session.",
    });
    return false;
  }, []);

  const selectSession = useCallback((sessionId: string) => {
    const requestId = generateRequestId();
    const result = sendRef.current({ type: "view_history", sessionId, requestId });
    if (result.sent) {
      dispatch({ type: "select_session", sessionId, requestId });
      return true;
    }
    dispatch({
      type: "set_error",
      error: "WebSocket not connected. Could not load history.",
    });
    return false;
  }, []);

  const startSession = useCallback(
    (model?: string, permissionMode?: "bypass") => {
      if (!state.activeSessionId || state.status !== "paused_readonly") return false;
      const requestId = generateRequestId();
      const result = sendRef.current({
        type: "resume_session",
        sessionId: state.activeSessionId,
        model,
        permissionMode,
        requestId,
      });
      if (result.sent) {
        dispatch({ type: "resume_started", requestId });
        return true;
      }
      dispatch({
        type: "set_error",
        error: "WebSocket not connected. Could not start this session.",
      });
      return false;
    },
    [state.activeSessionId, state.status]
  );

  const clearSession = useCallback(() => {
    dispatch({ type: "clear_session" });
  }, []);

  const sendPrompt = useCallback(
    (text: string, imagesOrModel?: ImageAttachment[] | string, model?: string) => {
      let images: ImageAttachment[] | undefined;
      let selectedModel = model;
      if (typeof imagesOrModel === "string") {
        selectedModel = imagesOrModel;
      } else if (Array.isArray(imagesOrModel)) {
        images = imagesOrModel;
      }
      const hasInput = text.trim() || (images && images.length);
      if (!state.activeSessionId || !hasInput) {
        return false;
      }
      const prevStatus = state.status;
      // Derive a deterministic user message id from the session, the current
      // turn index, and a slug of the content. The reducer increments
      // turnIndex on prompt_optimistic, so reading state.turnIndex here
      // matches the index the reducer will use for the per-turn streaming id.
      const userMessage: UIMessage = {
        id: userIdFor(state.activeSessionId, state.turnIndex, text),
        role: "user",
        content: text,
        timestamp: new Date().toISOString(),
        isStreaming: false,
        attachments: images,
      };
      const requestId = generateRequestId();
      const result = sendRef.current({
        type: "prompt",
        sessionId: state.activeSessionId,
        text,
        images,
        model: selectedModel,
        requestId,
      });
      if (result.sent) {
        dispatch({ type: "prompt_optimistic", userMessage, prevStatus, requestId });
        return true;
      }
      if (result.queued) {
        dispatch({ type: "prompt_queued", userMessage, requestId });
        return true;
      }
      dispatch({
        type: "prompt_send_failed",
        userMessage,
        prevStatus,
        error: result.error,
      });
      return false;
    },
    [state.activeSessionId, state.status, state.turnIndex]
  );

  const cancel = useCallback(() => {
    if (state.status === "resuming") return;
    if (state.status === "cancelling") return;
    // When a prompt is queued (not yet sent to the server), cancel removes it
    // from the WebSocket queue. `cancelQueuedPrompt` notifies
    // `onQueueDropped` with reason `"cancelled"`, which dispatches
    // `prompt_queue_dropped` and transitions the turn back to `ready`.
    if (state.status === "queued") {
      if (state.queuedRequestId) {
        wsControlsRef.current?.cancelQueuedPrompt(state.queuedRequestId);
      }
      return;
    }
    if (state.activeSessionId) {
      const requestId = generateRequestId();
      const result = sendRef.current({
        type: "cancel",
        sessionId: state.activeSessionId,
        requestId,
      });
      if (result.sent || result.queued) {
        dispatch({ type: "cancel", requestId });
      } else {
        // Send failed: keep the UI in `streaming` so the user can retry.
        dispatch({
          type: "set_error",
          error: "WebSocket not connected. Could not cancel the current turn.",
        });
      }
    }
  }, [state.activeSessionId, state.status, state.queuedRequestId]);

  const respondPermission = useCallback(
    (decision: "allow" | "deny", optionId?: string) => {
      if (!state.activeSessionId || !state.permissionRequest) return;
      // Ignore repeated clicks while a response is already in flight.
      if (state.permissionRequest.submitting) return;
      const requestId = generateRequestId();
      const result = sendRef.current({
        type: "permission_response",
        sessionId: state.activeSessionId,
        toolCallId: state.permissionRequest.toolCallId,
        requestId,
        decision,
        optionId,
      });
      if (result.sent || result.queued) {
        // Keep the panel visible (marked as submitting) until the backend
        // acknowledges via `permission_received`.
        dispatch({ type: "permission_submitting", decision, requestId });
      } else {
        // Send failed: keep the permission panel visible so the user can
        // retry their decision.
        dispatch({
          type: "set_error",
          error: "WebSocket not connected. Your decision could not be submitted.",
        });
      }
    },
    [state.activeSessionId, state.permissionRequest]
  );

  const discardPermission = useCallback(() => {
    dispatch({ type: "permission_discard" });
  }, []);

  const aggregateMetrics = useMemo<MessageMetrics>(
    () =>
      state.messages.reduce((acc, m) => {
        if (m.metrics) {
          acc.input_tokens = (acc.input_tokens || 0) + (m.metrics.input_tokens || 0);
          acc.output_tokens = (acc.output_tokens || 0) + (m.metrics.output_tokens || 0);
          acc.cache_read_tokens = (acc.cache_read_tokens || 0) + (m.metrics.cache_read_tokens || 0);
        }
        return acc;
      }, {} as MessageMetrics),
    [state.messages]
  );

  return {
    activeSessionId: state.activeSessionId,
    status: state.status,
    messages: state.messages,
    reasoningSteps: state.reasoningSteps,
    usage: state.usage,
    costEstimate: state.costEstimate,
    error: state.error,
    connectionError: state.connectionError,
    permissionRequest: state.permissionRequest,
    isStreaming: state.status === "streaming" || state.status === "cancelling",
    queued: state.status === "queued",
    loadingHistory: state.status === "loading_history",
    resuming: state.status === "resuming",
    sessionLive:
      state.status === "ready" ||
      state.status === "streaming" ||
      state.status === "cancelling" ||
      state.status === "queued",
    aggregateMetrics,
    onServerMessage,
    notifyResumeStarted,
    onPromptFlushed,
    onQueueDropped,
    onConnectionLost,
    newSession,
    selectSession,
    startSession,
    clearSession,
    sendPrompt,
    cancel,
    respondPermission,
    discardPermission,
  };
}
