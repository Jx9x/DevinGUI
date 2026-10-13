// Shared types for the Devin GUI frontend

export interface Session {
  id: string;
  title: string | null;
  working_directory: string | null;
  model: string | null;
  agent_mode: string | null;
  created_at: string | null;
  last_activity_at: string | null;
  hidden: number | null;
}

export interface Project {
  path: string;
  name: string;
  session_count: number;
  last_activity_at: string | null;
}

export interface BrowseEntry {
  name: string;
  path: string;
  is_dir: boolean;
}

export interface BrowseResult {
  /** The directory this result describes (empty string for the roots listing). */
  path: string;
  /** Parent directory for "go up" navigation, or empty string at the top. */
  parent: string;
  entries: BrowseEntry[];
  /** Top-level browse targets (drive roots on Windows); only present for the
   * initial roots listing when ``path`` is empty. */
  roots?: BrowseEntry[];
  /** Set by the backend when a directory could not be listed. */
  error?: string;
}

export interface ArchiveResponse {
  ok: true;
  session_id: string;
  archive_path: string;
  project_name: string;
}

export interface ExportResponse {
  ok: true;
  path: string;
  message: string;
}

export interface ExportContentOptions {
  include_reasoning: boolean;
  include_tool_calls: boolean;
  include_tool_outputs: boolean;
  include_summaries: boolean;
  db_only: boolean;
}

export interface ExportPreviewResponse {
  markdown: string;
  counts: {
    base: number;
    include_reasoning: number;
    include_tool_calls: number;
    include_tool_outputs: number;
    include_summaries: number;
    db_only: number;
    total: number;
  };
}

export interface MessageMetrics {
  ttft_ms?: number;
  total_time_ms?: number;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_creation_tokens?: number | null;
  /** Output tokens / full inference time, including time to first token. */
  tokens_per_sec?: number;
}

export interface MessageNodeMetadata {
  num_tokens?: number;
  metrics?: MessageMetrics;
  finish_reason?: string;
  created_at?: string;
}

export interface ChatMessage {
  message_id?: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  metadata?: MessageNodeMetadata;
}

export interface MessageNode {
  row_id: number;
  node_id: number | string | null;
  parent_node_id: number | string | null;
  created_at: string | null;
  role: string | null;
  content: string | null;
  message_id: string | null;
  metrics: MessageMetrics | null;
  finish_reason: string | null;
  metadata: MessageNodeMetadata | null;
  images?: MessageImage[];
  /** Main user-prompt index; zero for internal or unidentified user messages. */
  turn_index?: number;
}

export interface MessageImage {
  index: number;
  mime_type: string;
  width?: number;
  height?: number;
}

export interface ToolCallArgument {
  tool_call_id?: string;
  function_name: string;
  arguments?: Record<string, unknown> | null;
}

export interface ReasoningStepMetric {
  prompt_tokens?: number;
  completion_tokens?: number;
  cached_tokens?: number;
  total_time_ms?: number;
  /** Rate of the inference that produced this block, including all output. */
  tokens_per_sec?: number;
}

export interface ReasoningStep {
  step_id: number | null;
  reasoning_content: string;
  timestamp: string | null;
  tool_calls: ToolCallArgument[];
  message: string;
  metrics: ReasoningStepMetric;
  /** Largest measured reasoning block in the latest main-agent turn. */
  is_throughput_sample?: boolean;
  /** True while this step is being built from live ACP thought chunks. */
  isLive?: boolean;
  /** Browser arrival order for live blocks sharing the same timestamp. */
  streamOrder?: number;
}

export interface UsageUpdate {
  used?: number;
  size?: number;
  cost?: { amount: number; currency: string };
  contextSource?: "acp" | "persisted";
}

export interface FusionInfo {
  lead_family: string;
  lead_name: string;
  sidekick_id: string;
  sidekick_family: string;
  sidekick_name: string;
  sidekick_reasoning: string | null;
}

export interface ModelInfo {
  id: string;
  display_name: string;
  family?: string;
  family_name?: string;
  vision: boolean;
  tags: string[];
  label_tags?: string[];
  description: string;
  is_new: boolean;
  available: boolean;
  excluded: boolean;
  /** Canonical reasoning-effort key ("none"|"minimal"|"low"|"medium"|
   * "high"|"xhigh"|"max"), or null when the variant has no effort label.
   * On fusion combos this is the LEAD's level. */
  reasoning?: string | null;
  /** True on the ~2x-cost fast variants ("Fast Mode: 1" label). Hidden in the UI. */
  is_fast?: boolean;
  /** Present on fusion combo variants; null on regular models. */
  fusion?: FusionInfo | null;
  /** Price per million input tokens (USD), or null when free/unknown. */
  input_price?: number | null;
  /** Price per million cached-input tokens (USD), or null when free/unknown.
   * Not shown in the selector — used for conversation cost estimation. */
  cached_input_price?: number | null;
  /** Price per million output tokens (USD), or null when free/unknown. */
  output_price?: number | null;
}

export interface ConversationCostEstimate {
  amount: number;
  hypothetical_amount: number;
  currency: "USD";
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  input_amount: number;
  cached_input_amount: number;
  output_amount: number;
  hypothetical_input_amount: number;
  hypothetical_cached_input_amount: number;
  hypothetical_output_amount: number;
  models: Array<{
    model_id: string;
    display_name: string;
    input_tokens: number;
    cached_input_tokens: number;
    cache_creation_tokens: number;
    output_tokens: number;
    amount: number;
    input_amount: number;
    cached_input_amount: number;
    output_amount: number;
    hypothetical_amount: number;
    hypothetical_input_amount: number;
    hypothetical_cached_input_amount: number;
    hypothetical_output_amount: number;
    was_free: boolean;
    /** Counts cover every inference, including steps excluded from dollar totals. */
    paid_steps?: number;
    free_steps?: number;
    unpriced_steps?: number;
    /** True when the API comparison uses assumed prices for free models. */
    hypothetical_estimated?: boolean;
  }>;
  priced_steps: number;
  unpriced_steps: number;
  complete: boolean;
  estimated: boolean;
  source: "atif" | "sqlite";
  reasons: string[];
  paid_steps?: number;
  free_steps?: number;
  hypothetical_estimated?: boolean;
  turns?: Array<ConversationCostEstimate & { turn_index: number }>;
  steps?: Array<ConversationCostEstimate & { id: string; turn_index: number }>;
  /**
   * Context-window utilization at the most recent main-agent inference.
   * `used` is the full prompt size (uncached + cached prefix) for that one
   * inference — not cumulative across the conversation. `size` is the
   * model's context-window limit. Both may be null when no inference has
   * run yet or the model's window is unknown.
   */
  context?: { used: number | null; size: number | null };
}

export interface CacheInfo {
  path: string | null;
  updated_at: number | null;
}

export interface ModelsResponse {
  models: ModelInfo[];
  selectable: ModelInfo[];
  recent: ModelInfo[];
  last_used: string | null;
  /** Per-family last-used variant ids: `<family>` → variant id,
   * `fusion:<lead_family>` → fusion id, `sidekick:<sidekick_family>` →
   * sidekick variant id. */
  last_used_variants?: Record<string, string>;
  has_new: boolean;
  cache_info?: CacheInfo;
}

/** Live Windsurf/Devin quota snapshot from the GetPlanStatus API. */
export interface UsageSnapshot {
  /** Used daily quota as a percentage (0-100), or null when unavailable. */
  daily_used_percent: number | null;
  /** Used weekly quota as a percentage (0-100), or null when unavailable. */
  weekly_used_percent: number | null;
  /** Unix seconds when the weekly quota resets, or null. */
  weekly_reset_unix: number | null;
  /** Plan name (e.g. "Pro"), or null. */
  plan_name: string | null;
  /** Unix seconds when the snapshot was fetched. */
  fetched_at: number;
  /** Human-readable error when the fetch failed; null on success. */
  error: string | null;
}

/** Account-wide ChatGPT plan allowance from Codex's account/rateLimits/read. */
export interface CodexUsageWindow {
  used_percent: number | null;
  window_minutes: number | null;
  reset_unix: number | null;
}

export interface CodexUsageSnapshot {
  primary: CodexUsageWindow | null;
  secondary: CodexUsageWindow | null;
  plan_name: string | null;
  account_email: string | null;
  fetched_at: number;
  error: string | null;
  /** Cached quotas retained after an unsuccessful refresh. */
  stale: boolean;
}

export interface PermissionRequestData {
  toolCallId?: string;
  message?: string;
  sessionId?: string;
  method?: string;
  /** Backend-extracted human-readable detail (shell command, file path +
   *  compact diff) describing exactly what approval would permit. */
  detail?: string;
  [key: string]: unknown;
}

export interface TurnResult {
  stopReason?: string;
  [key: string]: unknown;
}

// Operation that produced a backend error. Used by the frontend reducer to
// correlate errors with the request that initiated them.
export type ErrorOperation =
  | "prompt"
  | "resume"
  | "new_session"
  | "load_history"
  | "permission"
  | "permission_response"
  | "cancel"
  | "acp_unavailable";

// WebSocket message types (backend → frontend)
export type WSInbound =
  | { type: "session_created"; sessionId: string; model?: string; requestId: string }
  | { type: "session_update"; sessionId: string; update: SessionUpdate }
  | {
      type: "request_permission";
      sessionId: string;
      requestId?: string | number | null;
      toolCallId?: string;
      data: PermissionRequestData;
    }
  | {
      type: "turn_complete";
      sessionId: string;
      stopReason?: string;
      result: TurnResult;
      requestId: string;
    }
  | { type: "turn_cancelled"; sessionId: string; requestId: string }
  | { type: "transition_cancelled"; sessionId: string; requestId: string }
  | { type: "reasoning_update"; sessionId: string; steps: ReasoningStep[] }
  | {
      type: "history_loaded";
      sessionId: string;
      messages: MessageNode[];
      reasoning: ReasoningStep[];
      readOnly?: boolean;
      requestId: string;
    }
  | { type: "session_resumed"; sessionId: string; requestId: string }
  | { type: "resume_failed"; sessionId: string; message: string; requestId: string }
  | { type: "db_message_update"; sessionId: string; messages: MessageNode[] }
  | { type: "cost_update"; sessionId: string; estimate: ConversationCostEstimate }
  | {
      type: "permission_received";
      sessionId: string;
      toolCallId?: string;
      requestId?: string;
      // The echoed ACP JSON-RPC id. The Devin CLI uses string UUIDs for
      // inbound `session/request_permission` ids, but JSON-RPC permits
      // numbers too, so both are accepted.
      acpRequestId?: string | number;
      decision: "allow" | "deny";
    }
  | {
      type: "permission_expired";
      sessionId?: string;
      toolCallId?: string;
      requestId?: string | number;
    }
  | {
      type: "error";
      sessionId?: string | null;
      requestId?: string | null;
      operation: ErrorOperation;
      message: string;
    };

export interface SessionUpdate {
  type:
    | "agent_message_chunk"
    | "agent_thought_chunk"
    | "tool_call"
    | "tool_call_update"
    | "usage_update";
  messageId?: string;
  chunk?: string;
  toolCallId?: string;
  title?: string;
  kind?: string;
  status?: "pending" | "in_progress" | "completed" | "failed";
  content?: ToolCallContent[];
  used?: number;
  size?: number;
  inputTokens?: number;
  cost?: { amount: number; currency: string };
}

// Result of useWebSocket.send: only one of sent or queued can be true.
// The `error` field is populated when a prompt is rejected (e.g. the queue
// is already at capacity) so the caller can surface a user-visible message.
export type SendResult =
  | { sent: true; queued: false }
  | { sent: false; queued: true }
  | { sent: false; queued: false; error?: string };

// Why a queued prompt was dropped before it reached the server. Used by the
// `onQueueDropped` callback from `useWebSocket` to `useChatSession` so the
// chat reducer can transition the corresponding turn to `ready` with an
// appropriate error message (or no error, in the `cancelled` case).
export type QueueDropReason = "cancelled" | "expired" | "resume_failed";

// Status of the permission panel, used by the UI to render the
// appropriate control (spinner while submitting, retry/discard while
// interrupted). Exported so the component layer can branch on it
// without duplicating the hook's internal flags.
export type PermissionPanelStatus = "idle" | "submitting" | "interrupted";

// Payload delivered to `useChatSession` by `useWebSocket` when the
// socket closes or errors while a permission response or cancel command
// is in flight. `permission` describes the in-flight permission response
// (if any); `cancelRequestId` is the in-flight cancel request id (if
// any). Both are `null` when nothing relevant was in flight, so the chat
// hook can cheaply skip work when the disconnect did not affect either
// operation.
export interface ConnectionLostPayload {
  permission: { toolCallId?: string; correlationId?: string } | null;
  cancelRequestId: string | null;
}

// WebSocket message types (frontend → backend)
export type WSOutbound =
  | {
      type: "new_session";
      cwd: string;
      model?: string;
      permissionMode?: "bypass";
      requestId?: string;
    }
  // `model` is the model currently selected in the dropdown. The backend
  // re-applies it to the live ACP session before running the turn and
  // refuses the prompt if the CLI does not confirm it, so a stale selection
  // can never silently burn quota on the wrong model.
  | {
      type: "prompt";
      sessionId: string;
      text: string;
      images?: ImageAttachment[];
      model?: string;
      requestId?: string;
    }
  | { type: "cancel"; sessionId: string; requestId?: string }
  | { type: "view_history"; sessionId: string; requestId?: string }
  | {
      type: "load_history";
      sessionId: string;
      model?: string;
      permissionMode?: "bypass";
      requestId?: string;
    }
  | {
      type: "resume_session";
      sessionId: string;
      model?: string;
      permissionMode?: "bypass";
      requestId?: string;
    }
  | {
      type: "permission_response";
      sessionId: string;
      toolCallId?: string;
      requestId?: string | number;
      decision: "allow" | "deny";
      optionId?: string;
    };

// UI-side message model (accumulated from live chunks + history)
export interface ImageAttachment {
  mimeType: string;
  data: string;
}

export type DisplayImageAttachment = (ImageAttachment & { src?: never }) | {
  src: string;
  mimeType: string;
  data?: never;
  width?: number;
  height?: number;
};

export interface UIMessage {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  timestamp: string | null;
  isStreaming: boolean;
  /** Persisted main user-prompt index; live drafts use local prompt order. */
  turnIndex?: number;
  /** Browser arrival order for live blocks sharing the same timestamp. */
  streamOrder?: number;
  toolCalls?: UIToolCall[];
  metrics?: MessageMetrics;
  reasoningSteps?: ReasoningStep[];
  attachments?: DisplayImageAttachment[];
}

export interface ToolCallResult {
  text?: string;
  error?: string;
  [key: string]: unknown;
}

export interface ToolCallText {
  text: string;
}

export interface ToolCallImage extends ImageAttachment {
  type: "image";
}

export type ToolCallContent = string | ToolCallResult | ToolCallText | ToolCallImage;

export function isToolCallImage(value: unknown): value is ToolCallImage {
  return isObject(value) && value.type === "image" &&
    isString(value.data) && value.data.length > 0 && isString(value.mimeType) &&
    /^image\/(png|jpeg|gif|webp|bmp|avif)$/.test(value.mimeType);
}

export interface UIToolCall {
  id: string;
  title: string;
  kind: string;
  status: "pending" | "in_progress" | "completed" | "failed";
  content?: ToolCallContent[];
}

// -- Runtime validation helpers ---------------------------------------------

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isNumber(value: unknown): value is number {
  return typeof value === "number";
}

function isBoolean(value: unknown): value is boolean {
  return typeof value === "boolean";
}

function isStringOrNull(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function isNumberOrStringOrNull(value: unknown): value is number | string | null {
  return typeof value === "number" || typeof value === "string" || value === null;
}

function isNumberOrNull(value: unknown): value is number | null {
  return typeof value === "number" || value === null;
}

function isOptionalString(value: unknown): value is string | null | undefined {
  return value === undefined || value === null || isString(value);
}

function isOptionalNumber(value: unknown): value is number | null | undefined {
  return value === undefined || value === null || isNumber(value);
}

function isOptionalStringOrNumber(value: unknown): value is string | number | null | undefined {
  return value === undefined || value === null || isString(value) || isNumber(value);
}

function isToolCallContent(value: unknown): value is ToolCallContent {
  if (isObject(value) && value.type === "image") return isToolCallImage(value);
  return typeof value === "string" || isObject(value);
}

function isToolCallContentArray(value: unknown): value is ToolCallContent[] {
  return Array.isArray(value) && value.every(isToolCallContent);
}

function isToolCallArgument(value: unknown): value is ToolCallArgument {
  return (
    isObject(value) &&
    isOptionalString(value.tool_call_id) &&
    isString(value.function_name) &&
    (value.arguments === undefined || value.arguments === null || isObject(value.arguments))
  );
}

function isToolCallArgumentArray(value: unknown): value is ToolCallArgument[] {
  return Array.isArray(value) && value.every(isToolCallArgument);
}

function isMessageMetrics(value: unknown): value is MessageMetrics {
  return (
    isObject(value) &&
    isOptionalNumber(value.ttft_ms) &&
    isOptionalNumber(value.total_time_ms) &&
    isOptionalNumber(value.input_tokens) &&
    isOptionalNumber(value.output_tokens) &&
    isOptionalNumber(value.cache_read_tokens) &&
    (value.cache_creation_tokens === undefined ||
      value.cache_creation_tokens === null ||
      isNumber(value.cache_creation_tokens)) &&
    isOptionalNumber(value.tokens_per_sec)
  );
}

function isOptionalMessageMetrics(value: unknown): value is MessageMetrics | null | undefined {
  return value === undefined || value === null || isMessageMetrics(value);
}

function isMessageNodeMetadata(value: unknown): value is MessageNodeMetadata {
  return (
    isObject(value) &&
    isOptionalNumber(value.num_tokens) &&
    isOptionalMessageMetrics(value.metrics) &&
    isOptionalString(value.finish_reason) &&
    isOptionalString(value.created_at)
  );
}

function isOptionalMessageNodeMetadata(
  value: unknown
): value is MessageNodeMetadata | null | undefined {
  return value === undefined || value === null || isMessageNodeMetadata(value);
}

function isReasoningStepMetric(value: unknown): value is ReasoningStepMetric {
  return (
    isObject(value) &&
    isOptionalNumber(value.prompt_tokens) &&
    isOptionalNumber(value.completion_tokens) &&
    isOptionalNumber(value.cached_tokens) &&
    isOptionalNumber(value.total_time_ms) &&
    isOptionalNumber(value.tokens_per_sec)
  );
}

function isReasoningStep(value: unknown): value is ReasoningStep {
  return (
    isObject(value) &&
    isNumberOrNull(value.step_id) &&
    isString(value.reasoning_content) &&
    isStringOrNull(value.timestamp) &&
    isToolCallArgumentArray(value.tool_calls) &&
    isString(value.message) &&
    isReasoningStepMetric(value.metrics) &&
    (value.is_throughput_sample === undefined || typeof value.is_throughput_sample === "boolean")
  );
}

function isReasoningStepArray(value: unknown): value is ReasoningStep[] {
  return Array.isArray(value) && value.every(isReasoningStep);
}

function isMessageNode(value: unknown): value is MessageNode {
  return (
    isObject(value) &&
    isNumber(value.row_id) &&
    isNumberOrStringOrNull(value.node_id) &&
    isNumberOrStringOrNull(value.parent_node_id) &&
    isStringOrNull(value.created_at) &&
    isStringOrNull(value.role) &&
    isStringOrNull(value.content) &&
    isStringOrNull(value.message_id) &&
    isOptionalNumber(value.turn_index) &&
    (value.metrics === null || isOptionalMessageMetrics(value.metrics)) &&
    isStringOrNull(value.finish_reason) &&
    (value.metadata === null || isOptionalMessageNodeMetadata(value.metadata)) &&
    (value.images === undefined || (Array.isArray(value.images) && value.images.every(isMessageImage)))
  );
}

function isMessageImage(value: unknown): value is MessageImage {
  return isObject(value) && Number.isInteger(value.index) && (value.index as number) >= 0 &&
    isString(value.mime_type) &&
    (value.width === undefined || (Number.isInteger(value.width) && (value.width as number) > 0)) &&
    (value.height === undefined || (Number.isInteger(value.height) && (value.height as number) > 0));
}

function isMessageNodeArray(value: unknown): value is MessageNode[] {
  return Array.isArray(value) && value.every(isMessageNode);
}

function isCost(value: unknown): value is { amount: number; currency: string } {
  return isObject(value) && isNumber(value.amount) && isString(value.currency);
}

function isOptionalCost(value: unknown): value is { amount: number; currency: string } | undefined {
  return value === undefined || isCost(value);
}

function isOptionalToolCallContentArray(value: unknown): value is ToolCallContent[] | undefined {
  return value === undefined || isToolCallContentArray(value);
}

function isPermissionRequestData(value: unknown): value is PermissionRequestData {
  return (
    isObject(value) &&
    isOptionalString(value.toolCallId) &&
    isOptionalString(value.message) &&
    isOptionalString(value.sessionId) &&
    isOptionalString(value.method) &&
    isOptionalString(value.detail)
  );
}

const ERROR_OPERATIONS: ReadonlySet<string> = new Set<ErrorOperation>([
  "prompt",
  "resume",
  "new_session",
  "load_history",
  "permission",
  "permission_response",
  "cancel",
  "acp_unavailable",
]);

function isErrorOperation(value: unknown): value is ErrorOperation {
  return isString(value) && ERROR_OPERATIONS.has(value);
}

function isFusionInfo(value: unknown): value is FusionInfo {
  return (
    isObject(value) &&
    isString(value.lead_family) &&
    isString(value.lead_name) &&
    isString(value.sidekick_id) &&
    isString(value.sidekick_family) &&
    isString(value.sidekick_name) &&
    isStringOrNull(value.sidekick_reasoning)
  );
}

function isModelInfo(value: unknown): value is ModelInfo {
  return (
    isObject(value) &&
    isString(value.id) &&
    isString(value.display_name) &&
    (value.family === undefined || isString(value.family)) &&
    (value.family_name === undefined || isString(value.family_name)) &&
    isBoolean(value.vision) &&
    Array.isArray(value.tags) &&
    value.tags.every(isString) &&
    isString(value.description) &&
    isBoolean(value.is_new) &&
    isBoolean(value.available) &&
    isBoolean(value.excluded) &&
    (value.reasoning === undefined || value.reasoning === null || isString(value.reasoning)) &&
    (value.is_fast === undefined || isBoolean(value.is_fast)) &&
    (value.fusion === undefined || value.fusion === null || isFusionInfo(value.fusion))
  );
}

function isModelsResponse(value: unknown): value is ModelsResponse {
  return (
    isObject(value) &&
    Array.isArray(value.models) &&
    value.models.every(isModelInfo) &&
    Array.isArray(value.selectable) &&
    value.selectable.every(isModelInfo) &&
    (value.recent === undefined ||
      (Array.isArray(value.recent) && value.recent.every(isModelInfo))) &&
    (value.last_used === undefined || value.last_used === null || isString(value.last_used)) &&
    (value.last_used_variants === undefined ||
      (isObject(value.last_used_variants) &&
        Object.values(value.last_used_variants).every(isString))) &&
    (value.has_new === undefined || isBoolean(value.has_new)) &&
    (value.cache_info === undefined ||
      (isObject(value.cache_info) &&
        (value.cache_info.path === null || isString(value.cache_info.path)) &&
        (value.cache_info.updated_at === null || typeof value.cache_info.updated_at === "number")))
  );
}

function isSession(value: unknown): value is Session {
  return (
    isObject(value) &&
    isString(value.id) &&
    isStringOrNull(value.title) &&
    isStringOrNull(value.working_directory) &&
    isStringOrNull(value.model) &&
    isStringOrNull(value.agent_mode) &&
    isStringOrNull(value.created_at) &&
    isStringOrNull(value.last_activity_at) &&
    isNumberOrNull(value.hidden)
  );
}

function isProject(value: unknown): value is Project {
  return (
    isObject(value) &&
    isString(value.path) &&
    isString(value.name) &&
    isNumber(value.session_count) &&
    isStringOrNull(value.last_activity_at)
  );
}

function isBrowseEntry(value: unknown): value is BrowseEntry {
  return (
    isObject(value) &&
    isString(value.name) &&
    isString(value.path) &&
    typeof value.is_dir === "boolean"
  );
}

function isBrowseResult(value: unknown): value is BrowseResult {
  return (
    isObject(value) &&
    isString(value.path) &&
    isString(value.parent) &&
    Array.isArray(value.entries) &&
    value.entries.every(isBrowseEntry) &&
    (value.roots === undefined ||
      (Array.isArray(value.roots) && value.roots.every(isBrowseEntry))) &&
    (value.error === undefined || isString(value.error))
  );
}

const SESSION_UPDATE_TYPES: ReadonlySet<string> = new Set([
  "agent_message_chunk",
  "agent_thought_chunk",
  "tool_call",
  "tool_call_update",
  "usage_update",
]);

const SESSION_UPDATE_STATUSES: ReadonlySet<string> = new Set([
  "pending",
  "in_progress",
  "completed",
  "failed",
]);

function isOptionalSessionUpdateStatus(
  value: unknown
): value is "pending" | "in_progress" | "completed" | "failed" | undefined {
  return value === undefined || (isString(value) && SESSION_UPDATE_STATUSES.has(value));
}

function isConversationCostEstimate(value: unknown): value is ConversationCostEstimate {
  return (
    isObject(value) &&
    isNumber(value.amount) &&
    isNumber(value.hypothetical_amount) &&
    value.currency === "USD" &&
    isNumber(value.input_tokens) &&
    isNumber(value.cached_input_tokens) &&
    isNumber(value.output_tokens) &&
    isNumber(value.input_amount) &&
    isNumber(value.cached_input_amount) &&
    isNumber(value.output_amount) &&
    isNumber(value.hypothetical_input_amount) &&
    isNumber(value.hypothetical_cached_input_amount) &&
    isNumber(value.hypothetical_output_amount) &&
    Array.isArray(value.models) &&
    isNumber(value.priced_steps) &&
    isNumber(value.unpriced_steps) &&
    isBoolean(value.complete) &&
    isBoolean(value.estimated) &&
    (value.source === "atif" || value.source === "sqlite") &&
    Array.isArray(value.reasons) &&
    value.reasons.every(isString) &&
    isOptionalContextWindow(value.context)
  );
}

function isOptionalContextWindow(
  value: unknown
): value is { used: number | null; size: number | null } | undefined {
  if (value === undefined) return true;
  if (!isObject(value)) return false;
  return (
    (value.used === null || isNumber(value.used)) && (value.size === null || isNumber(value.size))
  );
}

function isSessionUpdate(value: unknown): value is SessionUpdate {
  if (!isObject(value)) return false;
  if (!isString(value.type) || !SESSION_UPDATE_TYPES.has(value.type)) return false;
  return (
    isOptionalString(value.messageId) &&
    isOptionalString(value.chunk) &&
    isOptionalString(value.toolCallId) &&
    isOptionalString(value.title) &&
    isOptionalString(value.kind) &&
    isOptionalSessionUpdateStatus(value.status) &&
    isOptionalToolCallContentArray(value.content) &&
    isOptionalNumber(value.used) &&
    isOptionalNumber(value.size) &&
    isOptionalNumber(value.inputTokens) &&
    isOptionalCost(value.cost)
  );
}

export function assertType<T>(
  value: unknown,
  guard: (value: unknown) => value is T,
  label: string
): T {
  if (!guard(value)) {
    throw new Error(`Invalid ${label}`);
  }
  return value;
}

export function assertArray<T>(
  value: unknown,
  guard: (value: unknown) => value is T,
  label: string
): T[] {
  if (!Array.isArray(value)) {
    throw new Error(`${label} must be an array`);
  }
  if (!value.every(guard)) {
    throw new Error(`${label} contains invalid items`);
  }
  return value;
}

export function assertModelsResponse(value: unknown): ModelsResponse {
  return assertType(value, isModelsResponse, "models response");
}

export function assertSessionArray(value: unknown): Session[] {
  return assertArray(value, isSession, "sessions");
}

export function assertProjectArray(value: unknown): Project[] {
  return assertArray(value, isProject, "projects");
}

export function assertBrowseResult(value: unknown): BrowseResult {
  return assertType(value, isBrowseResult, "browse result");
}

export function assertWSInbound(value: unknown): WSInbound {
  if (!isObject(value)) {
    throw new Error("Invalid WebSocket message");
  }

  const type = value.type;
  if (!isString(type)) {
    throw new Error("Invalid WebSocket message: missing type");
  }

  switch (type) {
    case "session_created":
      if (!isString(value.sessionId)) throw new Error("Invalid session_created message");
      if (!isOptionalString(value.model)) throw new Error("Invalid session_created message");
      if (!isString(value.requestId)) throw new Error("Invalid session_created message");
      break;
    case "session_update":
      if (!isString(value.sessionId) || !isSessionUpdate(value.update)) {
        throw new Error("Invalid session_update message");
      }
      break;
    case "request_permission":
      if (!isString(value.sessionId) || !isPermissionRequestData(value.data)) {
        throw new Error("Invalid request_permission message");
      }
      break;
    case "turn_complete":
      if (!isString(value.sessionId) || !isObject(value.result)) {
        throw new Error("Invalid turn_complete message");
      }
      if (!isOptionalString(value.stopReason)) throw new Error("Invalid turn_complete message");
      if (!isString(value.requestId)) throw new Error("Invalid turn_complete message");
      break;
    case "turn_cancelled":
      if (!isString(value.sessionId)) throw new Error("Invalid turn_cancelled message");
      if (!isString(value.requestId)) throw new Error("Invalid turn_cancelled message");
      break;
    case "transition_cancelled":
      if (!isString(value.sessionId)) throw new Error("Invalid transition_cancelled message");
      if (!isString(value.requestId)) throw new Error("Invalid transition_cancelled message");
      break;
    case "reasoning_update":
      if (!isString(value.sessionId) || !isReasoningStepArray(value.steps)) {
        throw new Error("Invalid reasoning_update message");
      }
      break;
    case "history_loaded":
      if (
        !isString(value.sessionId) ||
        !isMessageNodeArray(value.messages) ||
        !isReasoningStepArray(value.reasoning)
      ) {
        throw new Error("Invalid history_loaded message");
      }
      if (!isString(value.requestId)) throw new Error("Invalid history_loaded message");
      if (value.readOnly !== undefined && typeof value.readOnly !== "boolean") {
        throw new Error("Invalid history_loaded message");
      }
      break;
    case "session_resumed":
      if (!isString(value.sessionId)) throw new Error("Invalid session_resumed message");
      if (!isString(value.requestId)) throw new Error("Invalid session_resumed message");
      break;
    case "resume_failed":
      if (!isString(value.sessionId) || !isString(value.message)) {
        throw new Error("Invalid resume_failed message");
      }
      if (!isString(value.requestId)) throw new Error("Invalid resume_failed message");
      break;
    case "db_message_update":
      if (!isString(value.sessionId) || !isMessageNodeArray(value.messages)) {
        throw new Error("Invalid db_message_update message");
      }
      break;
    case "cost_update":
      if (!isString(value.sessionId) || !isConversationCostEstimate(value.estimate)) {
        throw new Error("Invalid cost_update message");
      }
      break;
    case "permission_received":
      if (!isString(value.sessionId)) {
        throw new Error("Invalid permission_received message");
      }
      if (!isOptionalString(value.toolCallId)) {
        throw new Error("Invalid permission_received message");
      }
      if (!isOptionalString(value.requestId)) {
        throw new Error("Invalid permission_received message");
      }
      if (!isOptionalStringOrNumber(value.acpRequestId)) {
        throw new Error("Invalid permission_received message");
      }
      if (value.decision !== "allow" && value.decision !== "deny") {
        throw new Error("Invalid permission_received message");
      }
      break;
    case "permission_expired":
      if (!isOptionalString(value.sessionId)) {
        throw new Error("Invalid permission_expired message");
      }
      if (!isOptionalString(value.toolCallId)) {
        throw new Error("Invalid permission_expired message");
      }
      if (
        value.requestId !== undefined &&
        value.requestId !== null &&
        !isString(value.requestId) &&
        !isNumber(value.requestId)
      ) {
        throw new Error("Invalid permission_expired message");
      }
      break;
    case "error":
      if (!isString(value.message)) throw new Error("Invalid error message");
      if (!isOptionalString(value.sessionId)) throw new Error("Invalid error message");
      if (!isOptionalString(value.requestId)) throw new Error("Invalid error message");
      if (!isErrorOperation(value.operation)) {
        throw new Error(`Invalid error message: unknown operation ${String(value.operation)}`);
      }
      break;
    default:
      throw new Error(`Unknown WebSocket message type: ${type}`);
  }

  return value as WSInbound;
}
