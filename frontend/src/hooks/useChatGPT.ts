import { useCallback, useEffect, useRef, useState } from "react";
import { chatgptAPI, ChatGPTError, mergeEvents, type ChatEvent, type ChatGPTStatus, type ChatSession, type Controls, type Cursor, type ModelCatalog, type Receipt, type SendInput } from "../api/chatgpt";

export interface Draft {
  id: string; title: string; sessionId: string | null; text: string; model: string; effort: string;
  pending?: { input: SendInput; state: string; error?: string };
}
const STORAGE = "devin-gui-chatgpt-v1";
const emptyDraft = (title: string): Draft => ({ id: crypto.randomUUID(), title, sessionId: null, text: "", model: "", effort: "" });
function restore(): { drafts: Draft[]; active: string } {
  for (const kind of ["sessionStorage", "localStorage"] as const) {
    try {
      const raw = JSON.parse(window[kind].getItem(STORAGE) ?? "null");
      if (raw && Array.isArray(raw.drafts) && raw.drafts.length && raw.drafts.every((d: Draft) => typeof d.id === "string" && typeof d.title === "string" && typeof d.text === "string" && typeof d.model === "string" && typeof d.effort === "string" && (d.sessionId === null || typeof d.sessionId === "string") && (!d.pending || (typeof d.pending.input?.id === "string" && typeof d.pending.input.text === "string")))) {
        return { drafts: raw.drafts.map((d: Draft) => d.pending?.state === "submitting" ? { ...d, pending: { ...d.pending, state: "uncertain", error: "The page reloaded during delivery. Check the outbox before retrying." } } : d), active: raw.drafts.some((d: Draft) => d.id === raw.active) ? raw.active : raw.drafts[0].id };
      }
    } catch { /* This storage may be disabled or contain an older schema. */ }
  }
  const draft = emptyDraft("Draft 1");
  return { drafts: [draft], active: draft.id };
}
const message = (e: unknown) => e instanceof Error ? e.message : "ChatGPT request failed.";
const terminal = (state: string) => ["sent", "failed", "cancelled"].includes(state);

export function useChatGPT(active: boolean) {
  const [workspace, setWorkspace] = useState(restore);
  const [status, setStatus] = useState<ChatGPTStatus | null>(null);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [catalog, setCatalog] = useState<ModelCatalog>({ state: "unknown", models: [] });
  const [cursor, setCursor] = useState<Cursor | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [view, setView] = useState<{ id: string | null; events: ChatEvent[]; controls: Controls | null; loading: boolean; error: string; before: number | null }>({ id: null, events: [], controls: null, loading: false, error: "", before: null });
  const [revision, setRevision] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const mounted = useRef(true);
  const sending = useRef(new Set<string>());
  const loadedMore = useRef(false);
  const current = useRef(workspace);
  current.current = workspace;
  const draft = workspace.drafts.find(d => d.id === workspace.active)!;
  const sessionId = draft.sessionId;
  const refresh = useCallback(() => setRevision(v => v + 1), []);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const value = JSON.stringify(workspace);
    for (const kind of ["sessionStorage", "localStorage"] as const) {
      try { window[kind].setItem(STORAGE, value); } catch { /* Draft remains available in memory or the other storage. */ }
    }
  }, [workspace]);

  const reconcile = useCallback((receipts: Receipt[]) => {
    setWorkspace(w => ({ ...w, drafts: w.drafts.map(d => {
      const receipt = receipts.find(r => r.id === d.pending?.input.id);
      if (!receipt || !d.pending) return d;
      const target = receipt.deliveredSessionId || d.sessionId;
      const text = d.text === d.pending.input.text && !["failed", "cancelled"].includes(receipt.state) ? "" : d.text;
      return { ...d, sessionId: target, text, pending: receipt.state === "sent" && target ? undefined : { ...d.pending, state: receipt.state, error: receipt.error || (receipt.requiresAuthorization ? "Review this input in Chat On Steroids." : undefined) } };
    }) }));
  }, []);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;
    const poll = async () => {
      try {
        const health = await chatgptAPI.status(controller.signal);
        if (stopped) return;
        setStatus(health);
        if (health.available) {
          const results = await Promise.allSettled([chatgptAPI.sessions(null, controller.signal), chatgptAPI.models(controller.signal), chatgptAPI.outbox(controller.signal)]);
          if (stopped) return;
          const [list, models, receipts] = results;
          if (list.status === "fulfilled") {
            setSessions(old => [...new Map([...old, ...list.value.sessions].map(s => [s.id, s])).values()].sort((a, b) => b.updatedAt - a.updatedAt));
            if (!loadedMore.current) setCursor(list.value.nextCursor);
          }
          if (models.status === "fulfilled") setCatalog(models.value);
          if (receipts.status === "fulfilled") reconcile(receipts.value);
          setError(results.some(r => r.status === "rejected") ? "Some ChatGPT data could not be refreshed. Previously loaded content is retained." : "");
        } else setError(health.detail ?? "Start the custom Chat On Steroids app.");
      } catch (e) {
        if (!stopped) { setStatus({ available: false }); setError(message(e)); }
      } finally { if (!stopped) timer = setTimeout(poll, 3000); }
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, [active, revision, reconcile]);

  useEffect(() => {
    if (!sessionId) { setView({ id: null, events: [], controls: null, loading: false, error: "", before: null }); return; }
    if (!active) return;
    const controller = new AbortController();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let from: number | undefined;
    setView(old => old.id === sessionId ? { ...old, controls: null, loading: old.events.length === 0, error: "" } : { id: sessionId, events: [], controls: null, loading: true, error: "", before: null });
    const poll = async () => {
      const [events, controls] = await Promise.allSettled([chatgptAPI.events(sessionId, { from }, controller.signal), chatgptAPI.controls(sessionId, controller.signal)]);
      if (stopped) return;
      if (events.status === "fulfilled") from = events.value.nextFrom;
      setView(old => ({ ...old, loading: false,
        events: events.status === "fulfilled" ? mergeEvents(old.events, events.value.events) : old.events,
        before: old.events.length === 0 && events.status === "fulfilled" && events.value.events.length ? Math.min(...events.value.events.map(e => e.origin ?? e.seq)) : old.before,
        controls: controls.status === "fulfilled" ? controls.value : null,
        error: events.status === "rejected" ? message(events.reason) : controls.status === "rejected" ? "Turn controls unavailable; use Chat On Steroids to inspect this conversation." : "" }));
      timer = setTimeout(poll, 1500);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, [sessionId, revision, active]);

  function updateDraft(change: Partial<Draft>) {
    setWorkspace(w => ({ ...w, drafts: w.drafts.map(d => d.id === w.active ? { ...d, ...change } : d) }));
  }
  function select(id: string) { setWorkspace(w => ({ ...w, active: id })); setNotice(""); }
  function newDraft() {
    const d = emptyDraft(`Draft ${workspace.drafts.filter(d => !d.sessionId).length + 1}`);
    setWorkspace(w => ({ drafts: [...w.drafts, d], active: d.id })); setNotice("");
  }
  function openDraft(session: ChatSession) {
    const existing = workspace.drafts.find(d => d.sessionId === session.id);
    if (existing) { select(existing.id); return; }
    const d = { ...emptyDraft(session.title), sessionId: session.id };
    setWorkspace(w => ({ drafts: [...w.drafts, d], active: d.id })); setNotice("");
  }
  async function send(input: SendInput, key: string) {
    if (sending.current.has(key)) return;
    sending.current.add(key);
    // Persist before network I/O so reload cannot turn an uncertain request into a new UUID.
    const next = { ...current.current, drafts: current.current.drafts.map(d => d.id === key ? { ...d, pending: { input, state: "submitting" } } : d) };
    current.current = next;
    setWorkspace(next);
    for (const kind of ["sessionStorage", "localStorage"] as const) {
      try { window[kind].setItem(STORAGE, JSON.stringify(next)); } catch { /* Memory still retains original identity. */ }
    }
    try {
      const receipt = await chatgptAPI.send(input);
      if (mounted.current) { reconcile([receipt]); refresh(); }
    } catch (e) {
      if (mounted.current) setWorkspace(w => ({ ...w, drafts: w.drafts.map(d => d.id === key && d.pending?.input.id === input.id ? { ...d, pending: { input, state: e instanceof ChatGPTError && !e.uncertain ? "failed" : "uncertain", error: message(e) } } : d) }));
    } finally { sending.current.delete(key); }
  }
  async function action(run: () => Promise<unknown>, success: string) {
    if (actionBusy) return;
    setActionBusy(true); setNotice("");
    try { await run(); if (mounted.current) { setNotice(success); refresh(); } }
    catch (e) { if (mounted.current) setNotice(message(e)); }
    finally { if (mounted.current) setActionBusy(false); }
  }
  async function moreSessions() {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try { const page = await chatgptAPI.sessions(cursor); loadedMore.current = true; setSessions(old => [...new Map([...old, ...page.sessions].map(s => [s.id, s])).values()]); setCursor(page.nextCursor); }
    catch (e) { setError(message(e)); } finally { setLoadingMore(false); }
  }
  async function older() {
    if (!sessionId || !view.before || !Number.isFinite(view.before) || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await chatgptAPI.events(sessionId, { before: view.before });
      setView(old => old.id !== sessionId ? old : { ...old, events: mergeEvents(old.events, page.events), before: page.events.length ? Math.min(...page.events.map(e => e.origin ?? e.seq)) : null });
    } catch (e) { setNotice(message(e)); } finally { setLoadingMore(false); }
  }
  return { workspace, draft, status, sessions, catalog, cursor, view: view.id === sessionId ? view : { ...view, events: [], controls: null, loading: true }, error, notice, actionBusy, loadingMore,
    select, newDraft, openDraft, updateDraft, refresh, send, action, moreSessions, older,
    dismissReceipt: () => { if (draft.pending && terminal(draft.pending.state)) updateDraft({ text: draft.text || draft.pending.input.text, pending: undefined }); },
  };
}
