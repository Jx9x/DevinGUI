import type {
  UIMessage,
  ReasoningStep,
  MessageNode,
  SessionUpdate,
  WSInbound,
  UIToolCall,
  PermissionRequestData,
  UsageUpdate,
  ErrorOperation,
  ConnectionLostPayload,
  ConversationCostEstimate,
} from "./types";

export type SessionStatus =
  | "idle"
  | "loading_history"
  | "resuming"
  | "ready"
  | "queued"
  | "streaming"
  | "cancelling"
  | "paused_readonly";

export interface PermissionRequest {
  sessionId: string;
  toolCallId?: string;
  data: PermissionRequestData;
  // Set while a permission_response is in flight to the backend. The panel
  // stays visible so the user can see their decision was submitted, and is
  // cleared only after the backend acknowledges via `permission_received`.
  submitting?: boolean;
  submittedDecision?: "allow" | "deny";
  // The frontend-generated correlation id used in the `permission_response`
  // message. The backend echoes it back under `requestId` in
  // `permission_received`, allowing the reducer to correlate the ack with
  // the active panel and ignore delayed acks for previous requests.
  correlationId?: string;
  // Set when the WebSocket disconnected while a `permission_response` was
  // in flight (before `permission_received`). The panel renders a
  // retry/discard control instead of a stuck spinner. The user can retry
  // the decision (which dispatches `permission_submitting` again, clearing
  // this flag) or discard it (which clears the panel). Do not auto-retry
  // silently — the user must explicitly choose.
  interrupted?: boolean;
  interruptError?: string;
}

export interface ChatState {
  activeSessionId: string | null;
  status: SessionStatus;
  messages: UIMessage[];
  reasoningSteps: ReasoningStep[];
  usage: UsageUpdate;
  costEstimate: ConversationCostEstimate | null;
  error: string | null;
  // Connection-wide error surfaced when the server reports an error for a
  // request-based operation (prompt, cancel, new_session, load_history,
  // resume, permission_response) without a `requestId`. Such an error
  // cannot be correlated to a specific in-flight request, so it is kept
  // separate from the per-operation `error` and must not end the active
  // operation.
  connectionError: string | null;
  permissionRequest: PermissionRequest | null;
  // Tracks the most recent requestId initiated for each operation, so the
  // reducer can ignore stale errors that belong to a previous request.
  lastRequestIds: Partial<Record<ErrorOperation, string>>;
  // Monotonic turn counter for the active session. Incremented each time a
  // prompt is dispatched. Used to derive deterministic IDs for the user
  // message, the assistant streaming message, and tool calls of that turn.
  turnIndex: number;
  // Per-turn ID used for assistant chunks that do not carry a `messageId`.
  // Set when a prompt begins and cleared when the turn ends (`turn_complete`
  // or `error`). Replaces the old shared `"streaming"` literal so unrelated
  // assistant turns never merge into a single message.
  currentTurnId: string | null;
  // Timestamp of the user message that the browser actually submitted for
  // the current turn. Devin can persist internal/subagent/compaction nodes
  // with role="user" while a turn is running, so the newest SQLite user row
  // is NOT a reliable boundary for deciding which DB assistant rows belong
  // to the live turn. Keep the browser-issued boundary explicitly instead.
  currentTurnStartedAt: string | null;
  // Only contiguous text chunks share a block. Reasoning/tool arrivals close
  // it without clearing isStreaming, which also protects live text from DB
  // reconciliation until the whole turn completes.
  openTextBlock: { id: string; messageId?: string } | null;
  // Text, thought and tool blocks share one arrival sequence so events that
  // arrive within the same millisecond still render in their causal order.
  streamItemIndex: number;
  // Deterministic sequence index for tool calls within the current turn.
  // Reset on each prompt and incremented whenever a stable tool-call id has
  // to be synthesized (i.e. the backend did not supply `toolCallId`).
  turnToolCallSeq: number;
  // The request id of the currently queued prompt (when `status === "queued"`).
  // Used to correlate `onPromptFlushed` / `onQueueDropped` notifications from
  // `useWebSocket` with the correct turn so only the matching turn transitions.
  queuedRequestId: string | null;
  // The user message id added optimistically for the currently queued prompt.
  // Removed from `messages` when the queue is dropped (expired, resume_failed,
  // or cancelled) since the prompt never reached the server.
  queuedUserMessageId: string | null;
}

export type ChatAction =
  | { type: "select_session"; sessionId: string; requestId?: string }
  | { type: "clear_session" }
  | {
      type: "prompt_optimistic";
      userMessage: UIMessage;
      prevStatus: SessionStatus;
      requestId?: string;
    }
  | { type: "prompt_queued"; userMessage: UIMessage; requestId: string }
  | { type: "prompt_flushed"; requestId: string }
  | { type: "prompt_queue_dropped"; requestId: string; error: string | null }
  | {
      type: "prompt_send_failed";
      userMessage: UIMessage;
      prevStatus: SessionStatus;
      error?: string;
    }
  | { type: "cancel"; requestId?: string }
  | { type: "set_error"; error: string }
  | { type: "permission_submitting"; decision: "allow" | "deny"; requestId?: string }
  | { type: "permission_responded" }
  | { type: "permission_interrupted"; toolCallId?: string; correlationId?: string; error: string }
  | { type: "permission_discard" }
  | { type: "connection_lost"; payload: ConnectionLostPayload }
  | { type: "resume_started"; requestId?: string }
  | { type: "new_session_initiated"; requestId: string }
  | { type: "server_message"; msg: WSInbound };

export const initialChatState: ChatState = {
  activeSessionId: null,
  status: "idle",
  messages: [],
  reasoningSteps: [],
  usage: {},
  costEstimate: null,
  error: null,
  connectionError: null,
  permissionRequest: null,
  lastRequestIds: {},
  turnIndex: 0,
  currentTurnId: null,
  currentTurnStartedAt: null,
  openTextBlock: null,
  streamItemIndex: 0,
  turnToolCallSeq: 0,
  queuedRequestId: null,
  queuedUserMessageId: null,
};

function stableMessageId(
  node: Pick<MessageNode, "node_id" | "message_id" | "role" | "created_at" | "content">
): string {
  return (
    String(node.node_id ?? "") ||
    node.message_id ||
    `${node.role || "unknown"}-${node.created_at || "unknown"}-${(node.content || "").slice(0, 40)}`
  );
}

// Build the per-turn ID used for assistant chunks without a `messageId`.
function turnIdFor(sessionId: string, turnIndex: number): string {
  return `turn-${sessionId}-${turnIndex}`;
}

function stableToolCallId(
  update: Pick<SessionUpdate, "toolCallId" | "title" | "kind" | "messageId">,
  seq: number
): string {
  return (
    update.toolCallId ||
    `${update.title || "tool"}-${update.kind || "unknown"}-${update.messageId || "no-msg"}-${seq}`
  );
}

const ALLOWED_UI_ROLES: ReadonlySet<"user" | "assistant" | "tool"> = new Set([
  "user",
  "assistant",
  "tool",
]);

// Devin prefixes stored image prompts with lines such as
// "[Image 1: C:\\...\\pasted.png]". The image itself is stored separately,
// so these lines are neither user text nor useful in the rendered message.
function stripStoredImageMarkers(text: string): string {
  return text.replace(
    /^(?:[ \t]*\r?\n)*(?:[ \t]*\[Image \d+(?::[^\r\n]*)?\][ \t]*(?:\r?\n|$)(?:[ \t]*\r?\n)*)+/,
    ""
  );
}

function toUIMessage(node: MessageNode): UIMessage | null {
  if (!node.role || (!node.content && !node.images?.length)) return null;
  // Discard nodes whose role is not part of the UI message model rather than
  // blindly casting, so malformed DB rows cannot crash the reducer/render.
  if (!ALLOWED_UI_ROLES.has(node.role as "user" | "assistant" | "tool")) {
    return null;
  }
  return {
    id: stableMessageId(node),
    role: node.role as "user" | "assistant" | "tool",
    content: node.role === "user" && node.images?.length
      ? stripStoredImageMarkers(node.content || "")
      : node.content || "",
    timestamp: node.created_at,
    isStreaming: false,
    turnIndex: node.turn_index,
    metrics: node.metrics || undefined,
    attachments: node.images?.length ? node.images.map((image) => ({
      src: `/api/message-nodes/${node.row_id}/images/${image.index}`,
      mimeType: image.mime_type,
      width: image.width,
      height: image.height,
    })) : undefined,
  };
}

function convertNodesToMessages(nodes: MessageNode[]): UIMessage[] {
  return nodes.map(toUIMessage).filter((m): m is UIMessage => m !== null);
}

function normalizeDraftText(value: string): string {
  return value
    .normalize("NFKC")
    .replace(/[\u200B-\u200D\uFEFF]/g, "")
    .trim()
    .replace(/\s+/g, " ");
}

function parseMessageTimestamp(value: string | null | undefined): number {
  if (!value) return Number.NaN;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function isAtOrAfterTurnStart(message: UIMessage, turnStartedAt: string | null): boolean {
  if (!turnStartedAt) return true;
  const messageTime = parseMessageTimestamp(message.timestamp);
  const turnTime = parseMessageTimestamp(turnStartedAt);
  // If either persistence path gave us a timestamp JavaScript cannot parse,
  // prefer suppressing a content-matched live duplicate. The content match
  // is already restrictive, and this fallback is safer than visibly drawing
  // the exact same response twice during a turn.
  if (!Number.isFinite(messageTime) || !Number.isFinite(turnTime)) return true;
  return messageTime >= turnTime;
}

function mergeDbMessages(state: ChatState, dbMessages: MessageNode[]): UIMessage[] {
  const dbMsgs = convertNodesToMessages(dbMessages);
  // A poll can arrive before the CLI writes the pasted prompt. Keep local
  // images until their stored counterpart arrives, then use its lazy URLs.
  const promptText = (text: string) => normalizeDraftText(stripStoredImageMarkers(text));
  for (const local of state.messages) {
    if (local.role !== "user" || !local.attachments?.some((image) => image.data)) continue;
    const localTime = parseMessageTimestamp(local.timestamp);
    const persisted = dbMsgs.find((message) =>
      message.role === "user" &&
      // Some CLI versions persist whole seconds while optimistic times have milliseconds.
      (!Number.isFinite(localTime) || !Number.isFinite(parseMessageTimestamp(message.timestamp)) ||
        parseMessageTimestamp(message.timestamp) >= localTime - 1000) &&
      promptText(message.content) === promptText(local.content)
    );
    if (persisted) {
      if (!persisted.attachments?.length) persisted.attachments = local.attachments;
    } else {
      dbMsgs.push(local);
    }
  }
  const liveMessages = state.messages.filter((m) => m.isStreaming);
  if (liveMessages.length === 0) return dbMsgs;
  const liveIds = new Set(liveMessages.map((m) => m.id));
  const inferredLiveStart = liveMessages.reduce((earliest, message) => {
    const timestamp = parseMessageTimestamp(message.timestamp);
    return Number.isFinite(timestamp) ? Math.min(earliest, timestamp) : earliest;
  }, Number.POSITIVE_INFINITY);
  const liveTurnStartedAt =
    state.currentTurnStartedAt ??
    (Number.isFinite(inferredLiveStart) ? new Date(inferredLiveStart).toISOString() : null);
  // Text streamed so far this turn, joined across the live text blocks (tool
  // calls and reasoning split one reply into several blocks). The DB watcher
  // persists those same in-flight drafts under node ids that never match the
  // live turn ids, so an id-only dedupe renders each draft twice — once from
  // the database and once from the live stream — until the turn completes.
  const liveTextBlocks = liveMessages
    .filter((m) => m.role === "assistant" && !m.toolCalls?.length && m.content.trim())
    .map((m) => normalizeDraftText(m.content));
  const liveText = normalizeDraftText(liveTextBlocks.join(" "));

  const overlapsLiveDraft = (content: string): boolean => {
    const dbText = normalizeDraftText(content);
    if (!dbText || !liveText) return false;
    // SQLite can be either behind or slightly ahead of the ACP stream. Treat
    // containment in either direction as the same in-flight draft. Checking
    // the individual live blocks also handles replies split around tool calls.
    if (liveText.includes(dbText) || dbText.includes(liveText)) return true;
    return liveTextBlocks.some((block) => block.includes(dbText) || dbText.includes(block));
  };

  // IMPORTANT: do not use the newest DB `role="user"` row as the turn
  // boundary. Devin writes internal/subagent/compaction prompts as user-role
  // nodes during a single visible turn. The debug trace that reproduced the
  // duplication showed that DB boundary jumping mid-turn. Instead use the
  // timestamp of the prompt the browser itself submitted.
  const mergedDb = dbMsgs.filter(
    (m) =>
      !liveIds.has(m.id) &&
      !(
        m.role === "assistant" &&
        isAtOrAfterTurnStart(m, liveTurnStartedAt) &&
        overlapsLiveDraft(m.content)
      )
  );
  return [...mergedDb, ...liveMessages];
}

function reasoningContentOverlaps(left: string, right: string): boolean {
  const a = normalizeDraftText(left);
  const b = normalizeDraftText(right);
  if (!a || !b) return false;
  const shorter = Math.min(a.length, b.length);
  return shorter >= 24 && (a.includes(b) || b.includes(a));
}

function nextStreamingStatus(messages: UIMessage[], currentStatus: SessionStatus): SessionStatus {
  if (currentStatus === "streaming" || messages.some((m) => m.isStreaming)) {
    return "streaming";
  }
  return currentStatus;
}

// Demote the transient `isLive` reasoning step when a turn ends. The step is
// kept (so the panel does not blank out before the persisted transcript
// arrives) but is no longer protected from replacement by `reasoning_update`.
function settleLiveReasoning(steps: ReasoningStep[]): ReasoningStep[] {
  return steps.some((s) => s.isLive)
    ? steps.map((s) => (s.isLive ? { ...s, isLive: false } : s))
    : steps;
}

// Build the id for a freshly started assistant text block. The first block
// of a turn reuses the per-turn id so existing correlations keep working;
// later blocks (text resuming after a tool call or a reasoning block) get a
// `-part-N` suffix so each block anchors at its own arrival time and the
// timeline stays chronological.
function textMessageId(state: ChatState, messageId?: string): string {
  const base = messageId || state.currentTurnId || `stream-${state.messages.length}`;
  return state.messages.some((m) => m.id === base) ? `${base}-part-${state.messages.length}` : base;
}

function handleSessionUpdate(state: ChatState, update: SessionUpdate): ChatState {
  let messages = state.messages;
  // Text/tool arrivals end a thought block. Usage/status updates may arrive
  // between its chunks and must leave the thought block open.
  let reasoningSteps =
    update.type === "agent_message_chunk" || update.type === "tool_call"
      ? settleLiveReasoning(state.reasoningSteps)
      : state.reasoningSteps;
  let turnToolCallSeq = state.turnToolCallSeq;
  let openTextBlock = state.openTextBlock;
  let streamItemIndex = state.streamItemIndex;

  if (update.type === "agent_message_chunk") {
    // Append to the open text block only when nothing else (a tool call, a
    // new reasoning block) has happened since the last chunk — i.e. the
    // trailing message is a streaming assistant message with no tool calls.
    // Otherwise start a new block so it anchors at its own arrival time.
    const openBlock = state.openTextBlock;
    const openText = openBlock &&
      (!update.messageId || !openBlock.messageId || update.messageId === openBlock.messageId)
      ? state.messages.find((m) => m.id === openBlock.id && m.isStreaming)
      : undefined;
    const msgId = openText?.id || textMessageId(state, update.messageId);
    if (openText) {
      messages = state.messages.map((m) =>
        m.id === msgId ? { ...m, content: m.content + (update.chunk || "") } : m
      );
    } else {
      messages = [
        ...state.messages,
        {
          id: msgId,
          role: "assistant",
          content: update.chunk || "",
          timestamp: new Date().toISOString(),
          isStreaming: true,
          streamOrder: streamItemIndex++,
        },
      ];
    }
    openTextBlock = { id: msgId, messageId: update.messageId ?? openTextBlock?.messageId };
  } else if (update.type === "agent_thought_chunk") {
    openTextBlock = null;
    // Accumulate live reasoning tokens into a single transient step. The
    // persisted transcript reasoning (sent via `reasoning_update`) will
    // replace this once the turn finishes.
    const liveIndex = reasoningSteps.findIndex((s) => s.isLive);
    if (liveIndex >= 0) {
      reasoningSteps = reasoningSteps.map((s, idx) =>
        idx === liveIndex
          ? { ...s, reasoning_content: s.reasoning_content + (update.chunk || "") }
          : s
      );
    } else {
      reasoningSteps = [
        ...reasoningSteps,
        {
          step_id: null,
          reasoning_content: update.chunk || "",
          timestamp: new Date().toISOString(),
          tool_calls: [],
          message: "",
          metrics: {},
          isLive: true,
          streamOrder: streamItemIndex++,
        },
      ];
    }
  } else if (update.type === "tool_call") {
    openTextBlock = null;
    const synthesized = !update.toolCallId;
    const seq = turnToolCallSeq;
    const toolCall: UIToolCall = {
      id: update.toolCallId || stableToolCallId(update, seq),
      title: update.title || "Tool call",
      kind: update.kind || "unknown",
      status: "pending",
    };
    // The initial `tool_call` frame carries the preview content — the shell
    // command for exec calls, the path+diff for edits. The permission panel
    // relies on this when Devin's `session/request_permission` sends a bare
    // `{"toolCallId": ...}` (confirmed for edit/write calls), so it must be
    // kept here rather than waiting for `tool_call_update` output.
    if (update.content?.length) {
      toolCall.content = update.content;
    }
    if (synthesized) {
      turnToolCallSeq = seq + 1;
    }
    // Each tool call gets its own assistant carrier message anchored at its
    // arrival time, so the timeline renders it where it happened instead of
    // clustering every call at the first one's position. A duplicate
    // `tool_call` frame for the same id merges into the existing carrier.
    const carrierId = `tool-${toolCall.id}`;
    if (state.messages.some((m) => m.id === carrierId)) {
      messages = state.messages.map((m) => {
        if (m.id !== carrierId) return m;
        const calls = m.toolCalls || [];
        const existingIndex = calls.findIndex((tc) => tc.id === toolCall.id);
        if (existingIndex < 0) {
          return { ...m, toolCalls: [...calls, toolCall] };
        }
        // ACP may repeat a tool_call frame for the same stable id. Merge its
        // metadata into the existing call instead of rendering a duplicate.
        return {
          ...m,
          toolCalls: calls.map((tc, index) =>
            index === existingIndex ? { ...tc, ...toolCall } : tc
          ),
        };
      });
    } else {
      messages = [
        ...state.messages,
        {
          id: carrierId,
          role: "assistant",
          content: "",
          timestamp: new Date().toISOString(),
          isStreaming: true,
          toolCalls: [toolCall],
          streamOrder: streamItemIndex++,
        },
      ];
    }
  } else if (update.type === "tool_call_update") {
    messages = state.messages.map((m) => {
      if (!m.toolCalls?.some((tc) => tc.id === update.toolCallId)) return m;
      return {
        ...m,
        toolCalls: m.toolCalls.map((tc) =>
          // ACP sends `tool_call_update` repeatedly as output accumulates and
          // omits `content` on the status-only updates, so an absent
          // `content` must leave the existing output intact rather than
          // clearing it.
          tc.id === update.toolCallId
            ? {
                ...tc,
                status: update.status || tc.status,
                content: update.content ?? tc.content,
              }
            : tc
        ),
      };
    });
  } else if (update.type === "usage_update") {
    return {
      ...state,
      error: null,
      usage: {
        used: update.inputTokens ?? update.used,
        size: update.size,
        cost: update.cost,
        contextSource: update.inputTokens != null ? "acp" : undefined,
      },
      status: nextStreamingStatus(state.messages, state.status),
    };
  }

  return {
    ...state,
    messages,
    reasoningSteps,
    openTextBlock,
    streamItemIndex,
    error: null,
    turnToolCallSeq,
    status: nextStreamingStatus(messages, state.status),
  };
}

type ErrorMessage = Extract<WSInbound, { type: "error" }>;

// Operations that are initiated by a frontend request and therefore require
// a matching `requestId` to correlate an error with the correct in-flight
// request. When the server omits `requestId` for one of these operations,
// the error cannot be safely attributed to the current operation and is
// treated as a connection-wide failure instead.
const REQUEST_BASED_OPERATIONS: ReadonlySet<ErrorOperation> = new Set([
  "prompt",
  "cancel",
  "new_session",
  "load_history",
  "resume",
  "permission_response",
]);

function handleErrorMessage(state: ChatState, msg: ErrorMessage): ChatState {
  // Ignore errors that belong to a different session than the active one.
  // `sessionId` may be `null` (e.g. `acp_unavailable` sent before a session
  // is assigned); treat `null`/`undefined` as "no session" and always accept.
  if (msg.sessionId != null && msg.sessionId !== state.activeSessionId) {
    return state;
  }

  const { operation, message } = msg;

  if (REQUEST_BASED_OPERATIONS.has(operation)) {
    if (msg.requestId != null) {
      // The server provided a requestId; ignore the error when it does not
      // match the most recent request initiated for this operation (e.g. a
      // late error from a previous prompt).
      if (
        state.lastRequestIds[operation] !== undefined &&
        msg.requestId !== state.lastRequestIds[operation]
      ) {
        return state;
      }
    } else {
      // The server omitted a requestId for a request-based operation. The
      // error is uncorrelated and must not be applied to the current
      // operation. Surface it as a connection-wide failure instead so the
      // active operation (e.g. a streaming prompt) is not silently ended.
      return { ...state, connectionError: message };
    }
  } else if (
    msg.requestId != null &&
    state.lastRequestIds[operation] !== undefined &&
    msg.requestId !== state.lastRequestIds[operation]
  ) {
    // Non-request-based operation (e.g. `permission`): keep the existing
    // stale-requestId correlation behavior.
    return state;
  }

  switch (operation) {
    case "cancel":
      // A failed cancel does not mean the underlying prompt stopped. If we
      // were cancelling, return to `streaming` (the turn is likely still
      // running) with a retryable error. Keep the per-turn ids intact so
      // subsequent chunks still append to the current assistant message.
      if (state.status === "cancelling") {
        return { ...state, status: "streaming", error: message };
      }
      return { ...state, error: message };

    case "permission_response":
      // A failed permission submission must re-enable the panel so the user
      // can retry. Keep the permission request visible, clear the
      // submitting/submittedDecision flags, and mark the panel as
      // interrupted/retryable so the UI renders a discard control alongside
      // retry. Do not change `status`.
      if (state.permissionRequest) {
        return {
          ...state,
          permissionRequest: {
            ...state.permissionRequest,
            submitting: false,
            submittedDecision: undefined,
            interrupted: true,
            interruptError: message,
          },
          error: message,
        };
      }
      return { ...state, error: message };

    case "permission":
      // The permission request itself failed; clear it and surface the error.
      return { ...state, permissionRequest: null, error: message };

    default:
      // Other operations (prompt, new_session, load_history, resume): keep
      // the existing status-based behavior. A correlated error ends the
      // active turn when streaming/cancelling.
      if (state.status === "streaming" || state.status === "cancelling") {
        return {
          ...state,
          status: "ready",
          messages: state.messages.map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m)),
          reasoningSteps: settleLiveReasoning(state.reasoningSteps),
          error: message,
          // The turn has ended; clear the per-turn streaming id and
          // tool-call sequence so the next prompt starts fresh.
          currentTurnId: null,
          currentTurnStartedAt: null,
          openTextBlock: null,
          streamItemIndex: 0,
          turnToolCallSeq: 0,
        };
      }
      if (state.status === "loading_history" || state.status === "resuming") {
        return { ...state, status: "paused_readonly", error: message };
      }
      return { ...state, error: message };
  }
}

// Returns true when a server message should be accepted based on its
// `requestId`. When the frontend has any stored (non-undefined, non-empty)
// expected ID, a message with `requestId === undefined` is rejected — the
// backend echoes IDs for all request-response operations, so a missing ID
// indicates a stale or malformed message. When there is no outstanding
// request (all stored IDs are undefined/empty), permissive behavior is kept
// so unsolicited server messages (e.g. permission requests) still work.
export function requestIdMatches(
  msgRequestId: string | undefined,
  ...storedIds: (string | undefined)[]
): boolean {
  const expected = storedIds.filter((id): id is string => Boolean(id));
  if (expected.length === 0) {
    return msgRequestId === undefined;
  }
  return msgRequestId !== undefined && expected.includes(msgRequestId);
}

function handleServerMessage(state: ChatState, msg: WSInbound): ChatState {
  if (
    msg.type !== "error" &&
    msg.type !== "session_created" &&
    msg.type !== "permission_expired" &&
    "sessionId" in msg &&
    msg.sessionId !== state.activeSessionId
  ) {
    console.warn("Ignoring WebSocket message for inactive session", msg.type, msg.sessionId);
    return state;
  }

  switch (msg.type) {
    case "session_created":
      // Ignore a stale `session_created` whose request id does not match
      // the latest `new_session` request (e.g. the user selected another
      // session while creation was in flight).
      if (!requestIdMatches(msg.requestId, state.lastRequestIds.new_session)) {
        return state;
      }
      return {
        ...state,
        activeSessionId: msg.sessionId,
        status: "ready",
        messages: [],
        reasoningSteps: [],
        usage: {},
        costEstimate: null,
        error: null,
        connectionError: null,
        permissionRequest: null,
        turnIndex: 0,
        currentTurnId: null,
        currentTurnStartedAt: null,
        openTextBlock: null,
        streamItemIndex: 0,
        turnToolCallSeq: 0,
        queuedRequestId: null,
        queuedUserMessageId: null,
      };
    case "session_update":
      return handleSessionUpdate(state, msg.update);
    case "reasoning_update": {
      // Persisted transcript/DB reasoning replaces the historical steps, but
      // the in-flight `isLive` step must survive. This message fires on every
      // DB poll — including several times while the turn is still streaming —
      // so replacing the whole array would repeatedly wipe the only view the
      // user has of what the agent is thinking right now.
      const live = state.reasoningSteps.filter((s) => s.isLive);
      // The persisted watcher can catch up to the *same* reasoning block
      // before ACP finishes streaming it. Do not show both copies. Restrict
      // this suppression to persisted steps from the browser-issued current
      // turn so an identical sentence in older history is never removed.
      const persisted = msg.steps.filter((persistedStep) => {
        if (live.length === 0) return true;
        const stepTime = parseMessageTimestamp(persistedStep.timestamp);
        const turnTime = parseMessageTimestamp(state.currentTurnStartedAt);
        const currentTurn =
          !state.currentTurnStartedAt ||
          !Number.isFinite(stepTime) ||
          !Number.isFinite(turnTime) ||
          stepTime >= turnTime;
        return !(
          currentTurn &&
          live.some((liveStep) =>
            reasoningContentOverlaps(persistedStep.reasoning_content, liveStep.reasoning_content)
          )
        );
      });
      return { ...state, reasoningSteps: [...persisted, ...live] };
    }
    case "turn_complete":
      // Reject a stale `turn_complete` only when it cannot belong to the
      // current turn: a stored prompt id that does not match, or the ""
      // sentinel left by `clear_session` paired with an id'd message.
      // When no prompt id is stored at all (undefined) the page never
      // issued a prompt — e.g. it reloaded mid-turn — so a completion for
      // this session must be accepted: it proves the turn ended, and
      // dropping it would leave the UI streaming (Stop button) forever.
      if (
        state.lastRequestIds.prompt !== undefined &&
        !requestIdMatches(msg.requestId, state.lastRequestIds.prompt)
      ) {
        return state;
      }
      return {
        ...state,
        status: "ready",
        messages: state.messages.map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m)),
        reasoningSteps: settleLiveReasoning(state.reasoningSteps),
        error: null,
        connectionError: null,
        // Turn finished: rotate the per-turn id so the next prompt's
        // assistant chunks do not append to this message.
        currentTurnId: null,
        currentTurnStartedAt: null,
        openTextBlock: null,
        streamItemIndex: 0,
        turnToolCallSeq: 0,
      };
    case "turn_cancelled": {
      // Same correlation policy as `turn_complete`: reject only when a
      // stored cancel id (or the "" consumed sentinel) cannot match, but
      // accept unconditionally when no cancel was ever issued from this
      // page so an orphaned cancellation can still settle the UI.
      if (
        state.lastRequestIds.cancel !== undefined &&
        !requestIdMatches(msg.requestId, state.lastRequestIds.cancel)
      ) {
        return state;
      }
      return {
        ...state,
        status: "ready",
        messages: state.messages.map((m) => (m.isStreaming ? { ...m, isStreaming: false } : m)),
        reasoningSteps: settleLiveReasoning(state.reasoningSteps),
        error: null,
        connectionError: null,
        currentTurnId: null,
        currentTurnStartedAt: null,
        openTextBlock: null,
        streamItemIndex: 0,
        turnToolCallSeq: 0,
      };
    }
    case "transition_cancelled": {
      // A session load/resume transition was cancelled, not a turn. The
      // backend sends this instead of ``turn_cancelled`` so the frontend
      // does not assume ACP is loaded and ready for prompts. Clear the
      // ``resuming`` / ``loading_history`` / ``cancelling`` status and
      // move to ``paused_readonly`` since ACP is no longer guaranteed to
      // be loaded. Record the cancel request id as consumed (empty
      // sentinel) so a stale ``turn_cancelled`` arriving later for the
      // same request is rejected.
      if (!requestIdMatches(msg.requestId, state.lastRequestIds.cancel)) {
        return state;
      }
      return {
        ...state,
        status: "paused_readonly",
        error: null,
        connectionError: null,
        lastRequestIds: { ...state.lastRequestIds, cancel: "" },
        currentTurnId: null,
        currentTurnStartedAt: null,
        openTextBlock: null,
        streamItemIndex: 0,
        turnToolCallSeq: 0,
      };
    }
    case "history_loaded":
      // Ignore stale history that belongs to a previous `load_history`
      // request (e.g. the user selected another session while history was
      // being loaded).
      if (!requestIdMatches(msg.requestId, state.lastRequestIds.load_history)) {
        console.warn(
          "history_loaded ignored: requestId mismatch",
          msg.requestId,
          state.lastRequestIds.load_history
        );
        return state;
      }
      return {
        ...state,
        status: msg.readOnly ? "paused_readonly" : "resuming",
        messages: convertNodesToMessages(msg.messages),
        reasoningSteps: msg.reasoning,
        error: null,
        // Loading a different session's history resets the live turn
        // counters so newly issued prompts get fresh deterministic ids.
        turnIndex: 0,
        currentTurnId: null,
        currentTurnStartedAt: null,
        openTextBlock: null,
        streamItemIndex: 0,
        turnToolCallSeq: 0,
        queuedRequestId: null,
        queuedUserMessageId: null,
      };
    case "session_resumed":
      // `session_resumed` can arrive from either the `load_history` or
      // `resume_session` flow; accept it if its request id matches either.
      if (
        !requestIdMatches(
          msg.requestId,
          state.lastRequestIds.resume,
          state.lastRequestIds.load_history
        )
      ) {
        return state;
      }
      return {
        ...state,
        status:
          state.status === "queued" ||
          state.status === "streaming" ||
          state.status === "cancelling"
            ? state.status
            : "ready",
      };
    case "resume_failed":
      // `resume_failed` can arrive from either flow; same correlation.
      if (
        !requestIdMatches(
          msg.requestId,
          state.lastRequestIds.resume,
          state.lastRequestIds.load_history
        )
      ) {
        return state;
      }
      return {
        ...state,
        status: "paused_readonly",
        error: `Could not resume session: ${msg.message}`,
      };
    case "db_message_update":
      return { ...state, messages: mergeDbMessages(state, msg.messages) };
    case "cost_update":
      if (msg.sessionId !== state.activeSessionId) return state;
      {
        // The cost estimate carries the context-window utilization at the
        // most recent main-agent inference (used = full prompt size for
        // that one inference, size = model's context-window limit). Feed
        // it into `usage` so the StatsBar's "Context: X / Y" indicator
        // renders. ACP's own usage_update is the preferred source when
        // available, so only fill in fields the estimate provides.
        const ctx = msg.estimate.context;
        const usage: UsageUpdate = { ...state.usage };
        if (ctx) {
          if (ctx.used != null && usage.contextSource !== "acp") {
            usage.used = ctx.used;
            usage.contextSource = "persisted";
          }
          if (ctx.size != null && usage.size == null) usage.size = ctx.size;
        }
        return { ...state, costEstimate: msg.estimate, usage };
      }
    case "request_permission":
      return {
        ...state,
        permissionRequest: {
          sessionId: msg.sessionId,
          // Prefer the top-level `toolCallId` (always set by the server)
          // over `data.toolCallId` so the correlation check in
          // `permission_received` reliably matches.
          toolCallId: msg.toolCallId ?? msg.data?.toolCallId,
          data: msg.data,
        },
      };
    case "permission_received":
      // Backend acknowledged that the user's permission decision was
      // received and forwarded to the ACP session. Only clear the panel
      // when the ack matches the active request's tool call id AND
      // correlation id, so a delayed ack for a previous request cannot
      // clear a newer panel.
      if (
        state.permissionRequest &&
        msg.toolCallId === state.permissionRequest.toolCallId &&
        msg.requestId === state.permissionRequest.correlationId
      ) {
        return { ...state, permissionRequest: null };
      }
      // After a disconnect-interruption the correlation id was cleared, so
      // the strict check above cannot match. If the panel is interrupted
      // and the backend confirms the original decision was received (same
      // tool call id), clear the panel — the decision went through after
      // all and the user does not need to retry.
      if (
        state.permissionRequest?.interrupted &&
        msg.toolCallId === state.permissionRequest.toolCallId
      ) {
        return { ...state, permissionRequest: null };
      }
      // Defensive fallback: if the panel is currently submitting and the
      // backend sends `permission_received` for the same tool call id but
      // the correlation id does not match (e.g. due to a subtle race or a
      // correlation id that was lost across a reconnect), clear the panel
      // rather than leaving it stuck on "waiting for backend…". There is at
      // most one permission panel at a time, so a toolCallId match while
      // submitting is sufficient evidence that the ack belongs to this
      // panel.
      if (
        state.permissionRequest?.submitting &&
        msg.toolCallId != null &&
        msg.toolCallId === state.permissionRequest.toolCallId
      ) {
        return { ...state, permissionRequest: null };
      }
      return state;
    case "permission_expired":
      // The backend timed out the ACP permission request and rejected it.
      // Clear the panel for the matching tool call and surface an error so
      // the user knows their decision can no longer be submitted.
      if (msg.sessionId != null && msg.sessionId !== state.activeSessionId) {
        return state;
      }
      if (state.permissionRequest && msg.toolCallId === state.permissionRequest.toolCallId) {
        return {
          ...state,
          permissionRequest: null,
          error: "Permission request expired.",
        };
      }
      return state;
    case "error":
      return handleErrorMessage(state, msg);
    default:
      return state;
  }
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "clear_session":
      return {
        ...initialChatState,
        // Preserve request sentinels so delayed responses from the cleared
        // session cannot repopulate the UI after a project switch/archive.
        lastRequestIds: {
          ...state.lastRequestIds,
          new_session: "",
          load_history: "",
          resume: "",
          prompt: "",
          cancel: "",
        },
      };
    case "select_session":
      return {
        ...state,
        activeSessionId: action.sessionId,
        status: "loading_history",
        messages: [],
        reasoningSteps: [],
        usage: {},
        costEstimate: null,
        error: null,
        connectionError: null,
        permissionRequest: null,
        turnIndex: 0,
        currentTurnId: null,
        currentTurnStartedAt: null,
        openTextBlock: null,
        streamItemIndex: 0,
        turnToolCallSeq: 0,
        queuedRequestId: null,
        queuedUserMessageId: null,
        // Selecting an existing session supersedes any pending new-session
        // creation or reconnect resume. Set their request ids to the empty
        // sentinel so stale `session_created` / `session_resumed` /
        // `resume_failed` messages for those operations are ignored.
        lastRequestIds: {
          ...state.lastRequestIds,
          load_history: action.requestId,
          new_session: "",
          resume: "",
        },
      };
    case "prompt_optimistic": {
      // Start a new turn: assign the per-turn streaming id from the current
      // turn index, then increment so the next prompt gets a fresh id. The
      // tool-call sequence is reset to zero for this turn.
      const turnIndex = state.turnIndex;
      const sessionId = state.activeSessionId;
      const currentTurnId = sessionId ? turnIdFor(sessionId, turnIndex) : null;
      return {
        ...state,
        status: "streaming",
        messages: [...state.messages, action.userMessage],
        error: null,
        connectionError: null,
        turnIndex: turnIndex + 1,
        currentTurnId,
        currentTurnStartedAt: action.userMessage.timestamp,
        openTextBlock: null,
        streamItemIndex: state.messages.length + state.reasoningSteps.length + 1,
        turnToolCallSeq: 0,
        queuedRequestId: null,
        queuedUserMessageId: null,
        lastRequestIds: action.requestId
          ? { ...state.lastRequestIds, prompt: action.requestId }
          : state.lastRequestIds,
      };
    }
    case "prompt_queued": {
      // The prompt was accepted into the WebSocket queue but has not reached
      // the server yet. Enter the `queued` status (not `streaming`) and record
      // the request id / user message id so `prompt_flushed` and
      // `prompt_queue_dropped` can correlate with this turn.
      const turnIndex = state.turnIndex;
      const sessionId = state.activeSessionId;
      const currentTurnId = sessionId ? turnIdFor(sessionId, turnIndex) : null;
      return {
        ...state,
        status: "queued",
        messages: [...state.messages, action.userMessage],
        error: null,
        connectionError: null,
        turnIndex: turnIndex + 1,
        currentTurnId,
        currentTurnStartedAt: action.userMessage.timestamp,
        openTextBlock: null,
        streamItemIndex: state.messages.length + state.reasoningSteps.length + 1,
        turnToolCallSeq: 0,
        queuedRequestId: action.requestId,
        queuedUserMessageId: action.userMessage.id,
        lastRequestIds: { ...state.lastRequestIds, prompt: action.requestId },
      };
    }
    case "prompt_flushed":
      // The queued prompt was sent to the server. Transition the matching
      // turn from `queued` to `streaming`. Ignore if the status is no longer
      // `queued` or the request id does not match (e.g. the user switched
      // sessions or the prompt was already dropped).
      if (state.status !== "queued" || state.queuedRequestId !== action.requestId) {
        return state;
      }
      return {
        ...state,
        status: "streaming",
        queuedRequestId: null,
        queuedUserMessageId: null,
      };
    case "prompt_queue_dropped":
      // The queued prompt was dropped before reaching the server (expired,
      // resume_failed, or cancelled). Transition the matching turn to `ready`,
      // remove the optimistic user message, and surface an error (none for
      // cancellation). Ignore if the status is no longer `queued` or the
      // request id does not match.
      if (state.status !== "queued" || state.queuedRequestId !== action.requestId) {
        return state;
      }
      return {
        ...state,
        status: "ready",
        messages: state.messages.filter((m) => m.id !== state.queuedUserMessageId),
        error: action.error,
        queuedRequestId: null,
        queuedUserMessageId: null,
        currentTurnId: null,
        currentTurnStartedAt: null,
        openTextBlock: null,
        streamItemIndex: 0,
        turnToolCallSeq: 0,
      };
    case "prompt_send_failed":
      return {
        ...state,
        status: action.prevStatus,
        messages: state.messages.filter((m) => m.id !== action.userMessage.id),
        error: action.error ?? "WebSocket not connected. Message could not be sent.",
        currentTurnStartedAt: null,
        openTextBlock: null,
      };
    case "cancel":
      // The cancel command was sent to the backend. Move to `cancelling`
      // so the UI keeps showing the in-flight stream until the backend
      // confirms the turn ended (`turn_complete`) or reports an `error`.
      return {
        ...state,
        status: "cancelling",
        error: null,
        connectionError: null,
        lastRequestIds: action.requestId
          ? { ...state.lastRequestIds, cancel: action.requestId }
          : state.lastRequestIds,
      };
    case "set_error":
      return { ...state, error: action.error };
    case "permission_submitting":
      if (!state.permissionRequest) return state;
      return {
        ...state,
        permissionRequest: {
          ...state.permissionRequest,
          submitting: true,
          submittedDecision: action.decision,
          correlationId: action.requestId,
          // Retrying after an interruption clears the interrupted flags.
          interrupted: false,
          interruptError: undefined,
        },
        error: null,
        lastRequestIds: action.requestId
          ? { ...state.lastRequestIds, permission_response: action.requestId }
          : state.lastRequestIds,
      };
    case "permission_responded":
      return { ...state, permissionRequest: null };
    case "permission_interrupted":
      // The WebSocket disconnected while a permission_response was in
      // flight. Mark the panel as interrupted/retryable so the UI shows a
      // retry/discard control instead of a stuck spinner. Only apply when
      // the panel is still showing and was submitting (i.e. the in-flight
      // response matches the active panel's tool call id / correlation
      // id). A stale notification for a previous panel must not affect a
      // newer one.
      if (
        !state.permissionRequest ||
        !state.permissionRequest.submitting ||
        (action.toolCallId !== undefined &&
          state.permissionRequest.toolCallId !== action.toolCallId) ||
        (action.correlationId !== undefined &&
          state.permissionRequest.correlationId !== action.correlationId)
      ) {
        return state;
      }
      return {
        ...state,
        permissionRequest: {
          ...state.permissionRequest,
          submitting: false,
          submittedDecision: undefined,
          correlationId: undefined,
          interrupted: true,
          interruptError: action.error,
        },
      };
    case "permission_discard":
      // The user explicitly dismissed the interrupted permission panel.
      // Clear it and surface the interrupt error so the user knows the
      // decision was not delivered.
      if (!state.permissionRequest?.interrupted) return state;
      return {
        ...state,
        permissionRequest: null,
        error: state.permissionRequest.interruptError ?? null,
      };
    case "connection_lost": {
      const { permission, cancelRequestId } = action.payload;
      let next = state;
      // Mark an in-flight permission response as interrupted/retryable.
      if (permission) {
        next = chatReducer(next, {
          type: "permission_interrupted",
          toolCallId: permission.toolCallId,
          correlationId: permission.correlationId,
          error: "Connection lost before the permission decision was confirmed.",
        });
      }
      // Mark an in-flight cancel as indeterminate: the cancel may or may
      // not have reached the backend. Return to `streaming` so the UI does
      // not assume the turn was cancelled. If the turn is still running
      // after reconnect (within the session manager grace window),
      // subsequent chunks keep the status at `streaming`; if the turn is
      // gone, `turn_complete` / `turn_cancelled` / `resume_failed` will
      // move the UI to the appropriate terminal state.
      if (cancelRequestId && next.status === "cancelling") {
        next = { ...next, status: "streaming" };
      }
      return next;
    }
    case "resume_started":
      if (!state.activeSessionId) return state;
      return {
        ...state,
        status:
          state.status === "queued" ||
          state.status === "streaming" ||
          state.status === "cancelling"
            ? state.status
            : "resuming",
        error: null,
        connectionError: null,
        // A reconnect resume supersedes any pending new-session creation
        // or history load. Set their request ids to the empty sentinel so
        // stale `session_created` / `history_loaded` messages are ignored.
        lastRequestIds: {
          ...state.lastRequestIds,
          resume: action.requestId,
          new_session: "",
          load_history: "",
        },
      };
    case "new_session_initiated":
      return {
        ...state,
        // Starting a new session supersedes any pending history load or
        // reconnect resume. Set their request ids to the empty sentinel so
        // stale `history_loaded` / `session_resumed` / `resume_failed`
        // messages are ignored.
        lastRequestIds: {
          ...state.lastRequestIds,
          new_session: action.requestId,
          load_history: "",
          resume: "",
        },
      };
    case "server_message":
      return handleServerMessage(state, action.msg);
    default:
      return state;
  }
}
