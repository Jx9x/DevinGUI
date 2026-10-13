import { useEffect, useRef, useCallback, useState } from "react";
import {
  assertWSInbound,
  type WSInbound,
  type WSOutbound,
  type SendResult,
  type QueueDropReason,
  type ConnectionLostPayload,
} from "../types";

const INITIAL_RECONNECT_DELAY = 500;
const MAX_RECONNECT_DELAY = 8000;

// At most one pending prompt may be queued at a time. Additional prompts are
// rejected with a user-visible error so the user knows their input was not
// accepted.
const MAX_QUEUED_PROMPTS = 1;
// Queued prompts expire after this duration. Expired prompts are dropped and
// the chat hook is notified so the turn moves to `ready` with an error.
const QUEUE_EXPIRY_MS = 60_000;

// Coalesce high-frequency text/thought chunks before handing them to React.
// Devin commonly emits one ACP notification per token; rendering the entire
// conversation for every notification is needlessly expensive. Flush after
// roughly 20 chunk frames, but never wait longer than 50ms so the UI still
// feels live. Non-stream events always flush first to preserve ordering.
const STREAM_BATCH_MAX_CHUNKS = 20;
const STREAM_BATCH_MAX_DELAY_MS = 50;

type StreamChunkMessage = Extract<WSInbound, { type: "session_update" }>;

interface StreamBatch {
  sessionId: string;
  updateType: "agent_message_chunk" | "agent_thought_chunk";
  messageId?: string;
  chunks: string[];
}

interface QueuedPrompt {
  msg: WSOutbound;
  requestId: string;
  expireTimer: number | null;
}

// Monotonic counter used to build unique correlation ids for outgoing
// commands. Combined with a timestamp so ids are unique across reloads.
let _requestCounter = 0;
function generateRequestId(): string {
  _requestCounter += 1;
  return `req-${Date.now()}-${_requestCounter}`;
}

export interface UseWebSocketReturn {
  connected: boolean;
  send: (msg: WSOutbound) => SendResult;
  // Returns the request id used for the most recent reconnect
  // `resume_session` message, or null if no resume has been initiated.
  // `useChatSession` reads this so the same id is stored in
  // `lastRequestIds.resume` and echoed back by the backend.
  getResumeRequestId: () => string | null;
  // Removes a queued prompt from the queue (clearing its expiration timer)
  // and notifies `onQueueDropped` with reason `"cancelled"`. Returns true if
  // a matching queued prompt was found and removed.
  cancelQueuedPrompt: (requestId: string) => boolean;
}

export function useWebSocket(
  onMessage?: (msg: WSInbound) => void,
  getActiveSessionId?: () => string | null,
  onResumeStarted?: (requestId: string) => void,
  onPromptFlushed?: (requestId: string) => void,
  onQueueDropped?: (requestId: string, reason: QueueDropReason) => void,
  onConnectionLost?: (payload: ConnectionLostPayload) => void,
  getSelectedModel?: () => string | null,
  getBypassMode?: () => boolean
): UseWebSocketReturn {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectDelayRef = useRef(INITIAL_RECONNECT_DELAY);
  const closedIntentionallyRef = useRef(false);
  const mountedRef = useRef(false);
  const queueRef = useRef<QueuedPrompt[]>([]);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;
  const getActiveSessionIdRef = useRef(getActiveSessionId);
  getActiveSessionIdRef.current = getActiveSessionId;
  // The currently-selected model from the frontend dropdown. Sent with
  // `resume_session` on reconnect so the server relaunches ACP with the
  // user's choice instead of the model persisted in the sessions DB
  // (which may differ if the session was originally created on a
  // different model).
  const getSelectedModelRef = useRef(getSelectedModel);
  getSelectedModelRef.current = getSelectedModel;
  // The current "Bypass approvals" toggle state from the frontend. Sent with
  // `resume_session` on reconnect so the server relaunches ACP with the same
  // permission mode the user had selected, instead of silently dropping it
  // (which would cause unexpected approval prompts mid-conversation).
  const getBypassModeRef = useRef(getBypassMode);
  getBypassModeRef.current = getBypassMode;
  // Notified synchronously *before* the `resume_session` message is sent on
  // the wire so `useChatSession` can dispatch `resume_started` and record the
  // request id before a fast `session_resumed` response arrives.
  const onResumeStartedRef = useRef(onResumeStarted);
  onResumeStartedRef.current = onResumeStarted;
  // Notified when a queued prompt is finally sent to the server so
  // `useChatSession` can transition the corresponding turn from `queued` to
  // `streaming`.
  const onPromptFlushedRef = useRef(onPromptFlushed);
  onPromptFlushedRef.current = onPromptFlushed;
  // Notified when a queued prompt is dropped before reaching the server
  // (resume_failed, expiration, or cancellation) so `useChatSession` can
  // transition the turn to `ready` with an appropriate error.
  const onQueueDroppedRef = useRef(onQueueDropped);
  onQueueDroppedRef.current = onQueueDropped;
  // Notified when the socket closes or errors while a permission response
  // or cancel command is in flight. `useChatSession` uses this to surface
  // an `interrupted`/retryable state on the permission panel and to move
  // an indeterminate cancel back to `streaming` so the UI is not stuck.
  const onConnectionLostRef = useRef(onConnectionLost);
  onConnectionLostRef.current = onConnectionLost;
  // Tracks the most recent permission_response that was actually sent on
  // the wire (toolCallId + browser correlation id). Cleared when the
  // backend acknowledges via `permission_received`, rejects via `error`,
  // or expires via `permission_expired`. On disconnect, this is passed to
  // `onConnectionLost` so the chat hook can mark the panel interrupted.
  const inFlightPermissionRef = useRef<
    { sessionId: string; toolCallId?: string; correlationId?: string } | null
  >(null);
  // Tracks the most recent cancel request id that was sent on the wire.
  // Cleared when the backend acknowledges via `turn_cancelled` or the
  // turn ends via `turn_complete`/`error`. On disconnect, this is passed
  // to `onConnectionLost` so the chat hook can mark the cancel
  // indeterminate and return to `streaming`.
  const inFlightCancelRef = useRef<{ sessionId: string; requestId: string } | null>(null);
  const lastSentPromptRef = useRef<{ sessionId: string; requestId?: string } | null>(null);
  // True when we have sent `resume_session` on open and are waiting for the
  // backend to confirm with `session_resumed` before flushing queued prompts.
  const resumePendingRef = useRef(false);
  // The request id used for the most recent reconnect `resume_session`
  // message. Exposed via `getResumeRequestId` so `useChatSession` can store
  // the same id in `lastRequestIds.resume` and correlate the backend's
  // `session_resumed` / `resume_failed` responses.
  const resumeRequestIdRef = useRef<string | null>(null);
  const resumeSessionIdRef = useRef<string | null>(null);
  const streamBatchRef = useRef<StreamBatch | null>(null);
  const streamFlushTimerRef = useRef<number | null>(null);

  const clearStreamFlushTimer = useCallback(() => {
    if (streamFlushTimerRef.current !== null) {
      window.clearTimeout(streamFlushTimerRef.current);
      streamFlushTimerRef.current = null;
    }
  }, []);

  const flushStreamBatch = useCallback(() => {
    clearStreamFlushTimer();
    const batch = streamBatchRef.current;
    streamBatchRef.current = null;
    if (!batch || batch.chunks.length === 0) return;
    onMessageRef.current?.({
      type: "session_update",
      sessionId: batch.sessionId,
      update: {
        type: batch.updateType,
        messageId: batch.messageId,
        chunk: batch.chunks.join(""),
      },
    });
  }, [clearStreamFlushTimer]);

  const enqueueStreamChunk = useCallback(
    (msg: StreamChunkMessage): boolean => {
      const update = msg.update;
      if (update.type !== "agent_message_chunk" && update.type !== "agent_thought_chunk") {
        return false;
      }

      const current = streamBatchRef.current;
      const compatible =
        current &&
        current.sessionId === msg.sessionId &&
        current.updateType === update.type &&
        current.messageId === update.messageId;

      if (!compatible && current) {
        flushStreamBatch();
      }

      if (!streamBatchRef.current) {
        streamBatchRef.current = {
          sessionId: msg.sessionId,
          updateType: update.type,
          messageId: update.messageId,
          chunks: [],
        };
      }
      streamBatchRef.current.chunks.push(update.chunk || "");

      if (streamBatchRef.current.chunks.length >= STREAM_BATCH_MAX_CHUNKS) {
        flushStreamBatch();
      } else if (streamFlushTimerRef.current === null) {
        streamFlushTimerRef.current = window.setTimeout(
          flushStreamBatch,
          STREAM_BATCH_MAX_DELAY_MS
        );
      }
      return true;
    },
    [flushStreamBatch]
  );

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const flushQueue = useCallback(() => {
    while (queueRef.current.length > 0 && wsRef.current?.readyState === WebSocket.OPEN) {
      const item = queueRef.current.shift();
      if (item) {
        if (item.expireTimer !== null) {
          window.clearTimeout(item.expireTimer);
        }
        wsRef.current.send(JSON.stringify(item.msg));
        if (item.msg.type === "prompt") {
          lastSentPromptRef.current = {
            sessionId: item.msg.sessionId,
            requestId: item.msg.requestId,
          };
        }
        // Notify the chat hook so the turn transitions from `queued` to
        // `streaming` for this specific request.
        onPromptFlushedRef.current?.(item.requestId);
      }
    }
  }, []);

  const closeCurrentSocket = useCallback(() => {
    closedIntentionallyRef.current = true;
    for (const item of queueRef.current) {
      if (item.expireTimer !== null) {
        window.clearTimeout(item.expireTimer);
      }
    }
    queueRef.current = [];
    resumePendingRef.current = false;
    resumeRequestIdRef.current = null;
    resumeSessionIdRef.current = null;
    inFlightPermissionRef.current = null;
    inFlightCancelRef.current = null;
    lastSentPromptRef.current = null;
    clearStreamFlushTimer();
    streamBatchRef.current = null;
    const ws = wsRef.current;
    if (!ws) return;
    wsRef.current = null;
    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      ws.onopen = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      ws.close();
    }
  }, [clearStreamFlushTimer]);

  const connect = useCallback(() => {
    if (
      wsRef.current?.readyState === WebSocket.OPEN ||
      wsRef.current?.readyState === WebSocket.CONNECTING
    ) {
      return;
    }

    clearReconnectTimer();

    if (!mountedRef.current || closedIntentionallyRef.current) {
      return;
    }

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      reconnectDelayRef.current = INITIAL_RECONNECT_DELAY;
      const activeSessionId = getActiveSessionIdRef.current?.();
      if (activeSessionId && ws.readyState === WebSocket.OPEN) {
        // Resume the active session before sending any queued prompts. The
        // queue is flushed only once the backend confirms via
        // `session_resumed` (handled in onmessage). A correlation id is
        // generated here and exposed via `getResumeRequestId` so
        // `useChatSession` can store the same id in `lastRequestIds.resume`.
        const requestId = generateRequestId();
        resumeRequestIdRef.current = requestId;
        resumeSessionIdRef.current = activeSessionId;
        resumePendingRef.current = true;
        // Notify `useChatSession` *before* the message goes on the wire so
        // the reducer records `lastRequestIds.resume` and enters `resuming`
        // before a fast `session_resumed` response can arrive. Without this
        // ordering the response can race ahead of the React effect that used
        // to dispatch `resume_started`, leaving the UI stuck in `resuming`.
        onResumeStartedRef.current?.(requestId);
        const selectedModel = getSelectedModelRef.current?.() ?? undefined;
        const bypassMode = getBypassModeRef.current?.() ?? false;
        ws.send(
          JSON.stringify({
            type: "resume_session",
            sessionId: activeSessionId,
            model: selectedModel,
            permissionMode: bypassMode ? "bypass" : undefined,
            requestId,
          })
        );
      } else {
        flushQueue();
      }
    };

    ws.onclose = () => {
      // Deliver any chunks already received before reporting connection loss.
      // This keeps the reducer state consistent without replaying raw chunks
      // from the backend on reconnect.
      if (mountedRef.current && !closedIntentionallyRef.current) {
        flushStreamBatch();
      } else {
        clearStreamFlushTimer();
        streamBatchRef.current = null;
      }
      setConnected(false);
      wsRef.current = null;
      resumePendingRef.current = false;
      resumeRequestIdRef.current = null;
      resumeSessionIdRef.current = null;

      // Notify the chat hook about any in-flight permission response or
      // cancel command before clearing the refs. The chat hook surfaces an
      // `interrupted`/retryable state on the permission panel and moves an
      // indeterminate cancel back to `streaming` so the UI is not stuck.
      // Only notify when the disconnect was not intentional (unmount or
      // explicit close) so a clean teardown does not spuriously mark
      // in-flight operations as interrupted.
      if (!closedIntentionallyRef.current && mountedRef.current) {
        const inFlightPermission = inFlightPermissionRef.current;
        const payload: ConnectionLostPayload = {
          permission: inFlightPermission
            ? {
                toolCallId: inFlightPermission.toolCallId,
                correlationId: inFlightPermission.correlationId,
              }
            : null,
          cancelRequestId: inFlightCancelRef.current?.requestId ?? null,
        };
        if (payload.permission || payload.cancelRequestId) {
          onConnectionLostRef.current?.(payload);
        }
      }
      inFlightPermissionRef.current = null;
      inFlightCancelRef.current = null;

      if (closedIntentionallyRef.current || !mountedRef.current) {
        return;
      }

      clearReconnectTimer();
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectDelayRef.current = Math.min(reconnectDelayRef.current * 2, MAX_RECONNECT_DELAY);
        connect();
      }, reconnectDelayRef.current);
    };

    ws.onerror = () => {
      console.error("WebSocket error");
    };

    ws.onmessage = (event) => {
      try {
        const msg = assertWSInbound(JSON.parse(event.data));
        if (msg.type === "session_update" && enqueueStreamChunk(msg)) {
          return;
        }
        // Preserve wire ordering: a tool call, completion, error, etc. must
        // always be processed after every text/thought chunk that preceded it.
        flushStreamBatch();
        // Clear in-flight tracking refs when the backend acknowledges or
        // rejects the corresponding operation, so a subsequent disconnect
        // does not spuriously report a stale in-flight command.
        if (msg.type === "permission_received") {
          const permission = inFlightPermissionRef.current;
          if (
            permission &&
            msg.sessionId === permission.sessionId &&
            msg.toolCallId === permission.toolCallId &&
            msg.requestId === permission.correlationId
          ) {
            inFlightPermissionRef.current = null;
          }
        } else if (msg.type === "permission_expired") {
          const permission = inFlightPermissionRef.current;
          if (
            permission &&
            msg.toolCallId === permission.toolCallId &&
            (msg.sessionId == null || msg.sessionId === permission.sessionId)
          ) {
            inFlightPermissionRef.current = null;
          }
        } else if (msg.type === "turn_cancelled" || msg.type === "transition_cancelled") {
          const cancel = inFlightCancelRef.current;
          if (
            cancel &&
            msg.sessionId === cancel.sessionId &&
            msg.requestId === cancel.requestId
          ) {
            inFlightCancelRef.current = null;
            if (lastSentPromptRef.current?.sessionId === msg.sessionId) {
              lastSentPromptRef.current = null;
            }
          }
        } else if (msg.type === "turn_complete") {
          const prompt = lastSentPromptRef.current;
          const promptForSession = prompt !== null && prompt.sessionId === msg.sessionId;
          const promptIdKnown = promptForSession && prompt.requestId !== undefined;
          const completionAccepted =
            !promptIdKnown || prompt?.requestId === msg.requestId;
          const cancel = inFlightCancelRef.current;
          if (cancel && msg.sessionId === cancel.sessionId && completionAccepted) {
            inFlightCancelRef.current = null;
          }
          if (promptForSession && completionAccepted) {
            lastSentPromptRef.current = null;
          }
        } else if (msg.type === "error") {
          if (msg.operation === "permission_response") {
            const permission = inFlightPermissionRef.current;
            if (
              permission &&
              msg.requestId != null &&
              msg.requestId === permission.correlationId &&
              (msg.sessionId == null || msg.sessionId === permission.sessionId)
            ) {
              inFlightPermissionRef.current = null;
            }
          } else if (msg.operation === "cancel") {
            const cancel = inFlightCancelRef.current;
            if (
              cancel &&
              msg.requestId != null &&
              msg.requestId === cancel.requestId &&
              (msg.sessionId == null || msg.sessionId === cancel.sessionId)
            ) {
              inFlightCancelRef.current = null;
            }
          }
        }
        if (
          resumePendingRef.current &&
          (msg.type === "session_resumed" || msg.type === "resume_failed") &&
          msg.requestId === resumeRequestIdRef.current &&
          msg.sessionId === resumeSessionIdRef.current
        ) {
          if (msg.type === "session_resumed") {
            resumePendingRef.current = false;
            flushQueue();
          } else if (msg.type === "resume_failed") {
            // The session could not be resumed; drop queued prompts since the
            // session is now read-only. Notify the chat hook for each dropped
            // prompt so the corresponding turn transitions to `ready` with an
            // error.
            resumePendingRef.current = false;
            const dropped = queueRef.current;
            queueRef.current = [];
            for (const item of dropped) {
              if (item.expireTimer !== null) {
                window.clearTimeout(item.expireTimer);
              }
              onQueueDroppedRef.current?.(item.requestId, "resume_failed");
            }
          }
        }
        onMessageRef.current?.(msg);
      } catch (e) {
        console.error("Failed to parse WS message:", e);
      }
    };
  }, [clearReconnectTimer, flushQueue, flushStreamBatch, clearStreamFlushTimer, enqueueStreamChunk]);

  useEffect(() => {
    mountedRef.current = true;
    closedIntentionallyRef.current = false;
    connect();

    return () => {
      mountedRef.current = false;
      closedIntentionallyRef.current = true;
      clearReconnectTimer();
      closeCurrentSocket();
    };
  }, [connect, clearReconnectTimer, closeCurrentSocket]);

  // Adds a prompt to the queue with an expiration timer. When the timer
  // fires the prompt is removed and `onQueueDropped` is notified with reason
  // `"expired"` so the chat hook can surface an error.
  const enqueuePrompt = useCallback((msg: WSOutbound): void => {
    const requestId =
      "requestId" in msg && typeof msg.requestId === "string" ? msg.requestId : "";
    const expireTimer = window.setTimeout(() => {
      const idx = queueRef.current.findIndex((item) => item.requestId === requestId);
      if (idx !== -1) {
        const [removed] = queueRef.current.splice(idx, 1);
        if (removed?.expireTimer !== null) {
          window.clearTimeout(removed.expireTimer);
        }
        onQueueDroppedRef.current?.(requestId, "expired");
      }
    }, QUEUE_EXPIRY_MS);
    queueRef.current.push({ msg, requestId, expireTimer });
  }, []);

  const send = useCallback(
    (msg: WSOutbound): SendResult => {
      // While waiting for the backend to confirm a session resume, prompts must
      // be queued so they are only delivered after `session_resumed`.
      if (resumePendingRef.current && msg.type === "prompt") {
        if (!mountedRef.current || closedIntentionallyRef.current) {
          return { sent: false, queued: false };
        }
        if (queueRef.current.length >= MAX_QUEUED_PROMPTS) {
          return {
            sent: false,
            queued: false,
            error: "A prompt is already waiting for the connection.",
          };
        }
        enqueuePrompt(msg);
        return { sent: false, queued: true };
      }

      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify(msg));
        // Track in-flight permission responses and cancel commands so a
        // subsequent disconnect can be reported to the chat hook via
        // `onConnectionLost`. Only tracked when actually sent on the wire
        // (queued prompts are tracked separately via the queue).
        if (msg.type === "permission_response") {
          inFlightPermissionRef.current = {
            sessionId: msg.sessionId,
            toolCallId: msg.toolCallId,
            correlationId:
              "requestId" in msg && typeof msg.requestId === "string"
                ? msg.requestId
                : undefined,
          };
        } else if (msg.type === "cancel") {
          inFlightCancelRef.current =
            "requestId" in msg && typeof msg.requestId === "string"
              ? { sessionId: msg.sessionId, requestId: msg.requestId }
              : null;
        }
        if (msg.type === "prompt") {
          lastSentPromptRef.current = {
            sessionId: msg.sessionId,
            requestId: msg.requestId,
          };
        } else if (
          msg.type === "new_session" ||
          msg.type === "view_history" ||
          msg.type === "load_history"
        ) {
          lastSentPromptRef.current = null;
        }
        return { sent: true, queued: false };
      }

      if (!mountedRef.current || closedIntentionallyRef.current) {
        return { sent: false, queued: false };
      }

      if (msg.type === "prompt") {
        if (queueRef.current.length >= MAX_QUEUED_PROMPTS) {
          return {
            sent: false,
            queued: false,
            error: "A prompt is already waiting for the connection.",
          };
        }
        enqueuePrompt(msg);
        return { sent: false, queued: true };
      }

      return { sent: false, queued: false };
    },
    [enqueuePrompt]
  );

  const cancelQueuedPrompt = useCallback((requestId: string): boolean => {
    const idx = queueRef.current.findIndex((item) => item.requestId === requestId);
    if (idx === -1) return false;
    const [removed] = queueRef.current.splice(idx, 1);
    if (removed?.expireTimer !== null) {
      window.clearTimeout(removed.expireTimer);
    }
    onQueueDroppedRef.current?.(requestId, "cancelled");
    return true;
  }, []);

  const getResumeRequestId = useCallback(() => resumeRequestIdRef.current, []);

  return { send, connected, getResumeRequestId, cancelQueuedPrompt };
}
