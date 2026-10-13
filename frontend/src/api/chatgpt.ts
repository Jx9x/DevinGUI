export interface ChatGPTStatus {
  available: boolean;
  connectionState?: string;
  recording?: boolean;
  modelCatalogState?: string;
  build?: string;
  detail?: string;
  bridge?: { running?: boolean; paired?: boolean; present?: boolean };
}
export interface ChatModel { id: string; label: string; efforts: string[] }
export interface ModelCatalog { state: string; models: ChatModel[]; error?: string }
export interface ChatSession { id: string; title: string; updatedAt: number; activeTurnId?: string | null; activityExpiresAt?: number | null }
export interface Cursor { id: string; updatedAt: number }
export interface SessionPage { sessions: ChatSession[]; total: number; nextCursor: Cursor | null }
export interface StoredText { text: string; truncated?: boolean }
export interface ChatEvent {
  seq: number; kind: string; time?: number; origin?: number; messageId?: string; progressId?: string;
  inputId?: string; authoredText?: string; message?: StoredText; state?: string; final?: boolean; label?: string;
  outcome?: string; detail?: string;
  call?: { tool: string; outcome: string; args?: StoredText; result?: StoredText; summary?: { title: string; detail?: string; metric?: string } };
}
export interface EventPage { summary: ChatSession; events: ChatEvent[]; nextFrom: number; total: number }
export interface Controls { sessionId: string; activeTurnId: string | null; stopPending: boolean; blocked?: string }
export interface SendInput {
  id: string; sessionId: string | null; text: string; model: string; reasoningEffort: string | null;
  mode: "auto" | "after-turn"; dueAt: 0; automation: "off";
}
export interface Receipt { id: string; sessionId: string | null; deliveredSessionId?: string; state: string; error?: string; requiresAuthorization?: boolean }

export class ChatGPTError extends Error {
  constructor(message: string, public uncertain = false) { super(message); }
}
const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const session = (v: unknown) => object(v) && typeof v.id === "string" && typeof v.title === "string" && typeof v.updatedAt === "number";

async function request<T>(path: string, valid: (value: unknown) => boolean, body?: unknown, signal?: AbortSignal): Promise<T> {
  // No retry, especially after a send timeout. Abort read polls when switching conversations.
  const timeout = new AbortController();
  const abort = () => timeout.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) timeout.abort();
  const timer = setTimeout(abort, body === undefined ? 15000 : 95000);
  try {
    const res = await fetch(`/api/chatgpt${path}`, { method: body === undefined ? "GET" : "POST", signal: timeout.signal,
      cache: "no-store", ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
    const data: unknown = await res.json();
    if (!res.ok) throw new ChatGPTError(object(data) && typeof data.detail === "string" ? data.detail : `ChatGPT request failed (${res.status}).`, object(data) && data.deliveryUncertain === true);
    if (!valid(data)) throw new ChatGPTError("Unexpected ChatGPT backend response. Check that the Python server is up to date.", body !== undefined);
    return data as T;
  } catch (error) {
    if (error instanceof ChatGPTError) throw error;
    throw new ChatGPTError(body === undefined ? "Could not reach the ChatGPT backend." : "Connection interrupted. Check delivery before retrying this input.", body !== undefined);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

const receipt = (v: unknown) => object(v) && typeof v.id === "string" && typeof v.state === "string";
const controls = (v: unknown) => object(v) && typeof v.sessionId === "string" && (v.activeTurnId === null || typeof v.activeTurnId === "string");
export const chatgptAPI = {
  status: (signal?: AbortSignal) => request<ChatGPTStatus>("/status", v => object(v) && typeof v.available === "boolean", undefined, signal),
  sessions: (cursor?: Cursor | null, signal?: AbortSignal) => request<SessionPage>(`/sessions?limit=60${cursor ? `&cursorId=${encodeURIComponent(cursor.id)}&cursorUpdatedAt=${cursor.updatedAt}` : ""}`, v => object(v) && Array.isArray(v.sessions) && v.sessions.every(session) && typeof v.total === "number", undefined, signal),
  models: (signal?: AbortSignal) => request<ModelCatalog>("/models", v => object(v) && typeof v.state === "string" && Array.isArray(v.models) && v.models.every(m => object(m) && typeof m.id === "string" && typeof m.label === "string" && Array.isArray(m.efforts) && m.efforts.every(e => typeof e === "string")), undefined, signal),
  outbox: (signal?: AbortSignal) => request<Receipt[]>("/outbox", v => Array.isArray(v) && v.every(receipt), undefined, signal),
  events: (id: string, options: { from?: number; before?: number } = {}, signal?: AbortSignal) => request<EventPage>(`/sessions/${encodeURIComponent(id)}/events?limit=80${options.from !== undefined ? `&from=${options.from}` : ""}${options.before !== undefined ? `&before=${options.before}` : ""}`, v => object(v) && session(v.summary) && typeof v.nextFrom === "number" && Array.isArray(v.events) && v.events.every(e => object(e) && typeof e.kind === "string" && typeof e.seq === "number"), undefined, signal),
  controls: (id: string, signal?: AbortSignal) => request<Controls>(`/sessions/${encodeURIComponent(id)}/controls`, controls, undefined, signal),
  send: (input: SendInput) => request<Receipt>("/send", receipt, input),
  stop: (id: string, expectedTurnId: string) => request<Controls>(`/sessions/${encodeURIComponent(id)}/stop`, controls, { expectedTurnId }),
  cancel: (id: string) => request<unknown>(`/outbox/${encodeURIComponent(id)}/cancel`, () => true, {}),
  open: (id: string) => request<unknown>(`/sessions/${encodeURIComponent(id)}/open`, () => true, {}),
  refreshModels: () => request<unknown>("/models/refresh", () => true, {}),
};

/** A later snapshot replaces the same logical message at its original position. */
export function mergeEvents(previous: ChatEvent[], incoming: ChatEvent[]): ChatEvent[] {
  const key = (e: ChatEvent) => `${e.kind}:${e.messageId ?? e.progressId ?? e.origin ?? e.seq}`;
  const result = new Map<string, ChatEvent>();
  for (const event of [...previous, ...incoming]) {
    const old = result.get(key(event));
    if (!old || event.seq >= old.seq) result.set(key(event), { ...event, origin: event.origin ?? old?.origin ?? old?.seq ?? event.seq });
  }
  return [...result.values()].sort((a, b) => (a.origin ?? a.seq) - (b.origin ?? b.seq));
}

export function connectionLabel(status: ChatGPTStatus | null): string {
  if (!status) return "Checking connection";
  if (!status.available) return "Adapter unavailable";
  if (status.connectionState !== "connected") return "Core disconnected";
  if (!status.bridge?.present) return "Chrome unavailable";
  if (!status.recording) return "Recording off";
  return "Connected";
}
