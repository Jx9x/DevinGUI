import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Copy, ExternalLink, MessageCircle, MessageSquarePlus, Plug, RefreshCw, Send, Square, Wrench, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { chatgptAPI, connectionLabel, type ChatEvent } from "../api/chatgpt";
import { useChatGPT } from "../hooks/useChatGPT";

const buttonStyle = "inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-xs text-gray-300 hover:bg-bg-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40";
const selectStyle = "max-w-48 rounded-md border border-border bg-bg-secondary px-2 py-1.5 text-xs text-gray-200 disabled:opacity-40";

/** Render Markdown as text, never the provider's recorded HTML. */
function EventRow({ event }: { event: ChatEvent }) {
  const text = event.authoredText ?? event.message?.text ?? "";
  if (event.kind === "session_start" || event.kind === "turn_start") return null;
  if (event.kind === "tool_call" && event.call) return (
    <details className="rounded-lg border border-border bg-bg-secondary px-4 py-3 text-xs text-gray-400">
      <summary className="cursor-pointer text-gray-300">{event.call.summary?.title || event.call.tool} · {event.call.outcome}{event.call.summary?.metric ? ` · ${event.call.summary.metric}` : ""}</summary>
      {event.call.summary?.detail && <p className="mt-2">{event.call.summary.detail}</p>}
      <p className="mt-3 font-medium">Arguments</p><pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words">{event.call.args?.text}</pre>
      <p className="mt-3 font-medium">Result</p><pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words">{event.call.result?.text}</pre>
      {(event.call.args?.truncated || event.call.result?.truncated) && <p className="mt-2 text-amber-300">Recorded preview is truncated. Open Chat On Steroids for the full artifact.</p>}
    </details>
  );
  if (event.kind === "page_tool") return <p className="text-xs text-gray-400">{event.label}</p>;
  if (event.kind === "turn_end") return <p className="text-xs text-gray-500">Response {event.outcome ?? "ended"}{event.detail ? ` · ${event.detail}` : ""}</p>;
  if (!text) return null;
  const user = event.kind === "user_message";
  return <article className={`min-w-0 rounded-xl px-4 py-3 ${user ? "ml-4 bg-accent/10 sm:ml-12" : event.kind === "chat_error" ? "border border-red-500/30 bg-red-500/10" : "bg-bg-secondary/50"}`}>
    <p className="mb-2 text-xs font-medium text-gray-500">{user ? "You" : event.kind === "assistant_message" ? "ChatGPT" : event.kind === "progress" ? "Activity" : event.kind.replace(/_/g, " ")}{event.state === "streaming" && " · Responding"}</p>
    <div className="markdown-content min-w-0 break-words text-sm leading-relaxed text-gray-200 [&_pre]:max-w-full [&_pre]:overflow-auto [&_table]:block [&_table]:overflow-auto">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>, img: ({ alt }) => <span className="text-gray-500">[Image: {alt || "open in ChatGPT"}]</span> }}>{text}</ReactMarkdown>
    </div>
    {event.message?.truncated && <p className="mt-2 text-xs text-amber-300">Recorded text is truncated; view the complete message in ChatGPT.</p>}
  </article>;
}

export function ChatGPTPane({ onStatusChange, active, header }: { onStatusChange?: (status: string) => void; active: boolean; header: ReactNode }) {
  const chat = useChatGPT(active);
  const { draft, view, status, catalog } = chat;
  const [showConnection, setShowConnection] = useState(false);
  const [copyStatus, setCopyStatus] = useState("");
  const scroll = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const olderHeight = useRef<number | null>(null);
  const label = connectionLabel(status);
  const anyWorking = chat.sessions.some(s => s.activeTurnId && (s.activityExpiresAt == null || s.activityExpiresAt > Date.now()));
  const tabStatus = label === "Connected" ? anyWorking ? "Working" : chat.workspace.drafts.some(d => d.pending) ? "Input pending" : "Connected" : label;
  useEffect(() => { onStatusChange?.(tabStatus); }, [onStatusChange, tabStatus]);
  useEffect(() => { follow.current = true; olderHeight.current = null; setCopyStatus(""); }, [draft.id]);
  useEffect(() => {
    const el = scroll.current;
    if (!el) return;
    if (olderHeight.current !== null) { el.scrollTop += el.scrollHeight - olderHeight.current; olderHeight.current = null; }
    else if (follow.current) el.scrollTop = el.scrollHeight;
  }, [view.events]);
  const selected = draft.model ? catalog.models.find(m => m.id === draft.model) : catalog.models[0];
  const effort = draft.effort || selected?.efforts[0] || "";
  const validEffort = selected && (!selected.efforts.length || selected.efforts.includes(effort));
  const canSend = label === "Connected" && catalog.state === "ready" && selected && validEffort && draft.text.trim() && draft.text.length <= 16000 && !draft.pending;
  const controls = view.controls?.sessionId === draft.sessionId ? view.controls : null;
  const title = chat.sessions.find(s => s.id === draft.sessionId)?.title || draft.title;
  const visibleDrafts = chat.workspace.drafts.filter(d => !d.sessionId);
  const pending = draft.pending;
  const terminal = pending && ["failed", "cancelled"].includes(pending.state);

  async function copyDraft() {
    try { await navigator.clipboard.writeText(draft.text); setCopyStatus("Draft copied."); }
    catch { setCopyStatus("Clipboard unavailable. Select the draft text and copy it manually."); }
  }
  function submit() {
    if (!canSend || !selected) return;
    void chat.send({ id: crypto.randomUUID(), sessionId: draft.sessionId, text: draft.text, model: selected.id, reasoningEffort: effort || null,
      mode: controls?.activeTurnId ? "after-turn" : "auto", dueAt: 0, automation: "off" }, draft.id);
  }

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-1 flex-col md:flex-row">
      <aside aria-label="ChatGPT conversations" className="flex shrink-0 flex-col border-b border-border bg-bg-secondary md:w-72 md:border-b-0 md:border-r">
        <div className="flex min-h-12 shrink-0 items-center gap-1 border-b border-border px-3 py-2">
          {header}
          <div className="flex shrink-0"><button onClick={chat.refresh} aria-label="Refresh ChatGPT" className="rounded-md p-1.5 text-gray-400 hover:bg-bg-hover"><RefreshCw className="h-4 w-4" /></button>
            <button onClick={chat.newDraft} aria-label="New ChatGPT draft" title="New conversation" className="rounded-md p-1.5 text-gray-400 hover:bg-bg-hover"><MessageSquarePlus className="h-4 w-4" /></button></div>
        </div>
        <div className="flex max-h-36 gap-1 overflow-auto px-2 pb-2 md:max-h-none md:flex-1 md:flex-col">
          {visibleDrafts.map(item => <button key={item.id} aria-current={draft.id === item.id ? "true" : undefined} onClick={() => chat.select(item.id)} className={`min-w-32 shrink-0 rounded-md border px-3 py-2 text-left md:min-w-0 ${draft.id === item.id ? "border-accent/30 bg-accent/15" : "border-transparent hover:bg-bg-hover"}`}>
            <span className="block text-sm text-gray-200">{item.title}</span><span className="block max-w-52 truncate text-xs text-gray-500">{item.pending ? `Input ${item.pending.state}` : item.text.trim() || "New conversation"}</span></button>)}
          {chat.sessions.map(session => <button key={session.id} title={session.title} aria-current={draft.sessionId === session.id ? "true" : undefined} onClick={() => chat.openDraft(session)} className={`min-w-40 shrink-0 rounded-md border px-3 py-2 text-left md:min-w-0 ${draft.sessionId === session.id ? "border-accent/30 bg-accent/15" : "border-transparent hover:bg-bg-hover"}`}>
            <span className="block max-w-60 truncate text-sm text-gray-200">{session.title || "Untitled conversation"}</span><span className="block text-xs text-gray-500">{session.activeTurnId ? "Responding · " : ""}{new Date(session.updatedAt).toLocaleString()}{chat.workspace.drafts.some(d => d.sessionId === session.id && (d.text || d.pending)) ? " · Draft / input" : ""}</span></button>)}
          {chat.cursor && <button onClick={() => void chat.moreSessions()} disabled={chat.loadingMore} className={buttonStyle}>More conversations</button>}
        </div>
        <p className="hidden border-t border-border px-4 py-3 text-xs leading-relaxed text-gray-500 md:block">Drafts stay separate from Devin and are saved in this browser at this address when storage is available.</p>
      </aside>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-bg-secondary px-4 py-2">
          <div className="min-w-0"><h2 className="max-w-md truncate text-sm font-medium text-gray-200">ChatGPT <span className="font-normal text-gray-500">/ {title}</span></h2>
            <p className={`mt-0.5 text-xs ${label === "Connected" ? "text-emerald-400" : "text-amber-300"}`}>{label}{controls?.activeTurnId ? " · Responding" : ""}</p></div>
          <div className="flex flex-wrap gap-2">
            {draft.sessionId ? <button disabled={!status?.available || chat.actionBusy} onClick={() => void chat.action(() => chatgptAPI.open(draft.sessionId!), "Opened the conversation in Chrome.")} className={buttonStyle}><ExternalLink className="h-3.5 w-3.5" />Open in ChatGPT</button> : <a href="https://chatgpt.com/" target="_blank" rel="noopener noreferrer" className={buttonStyle}><ExternalLink className="h-3.5 w-3.5" />Open ChatGPT</a>}
            <button onClick={() => setShowConnection(v => !v)} aria-expanded={showConnection} aria-controls="chatgpt-connection" className={buttonStyle}><Plug className="h-3.5 w-3.5" />Connection & tools</button>
          </div>
        </div>
        {chat.error && <p role="alert" className="border-b border-border px-4 py-2 text-xs text-amber-300">{chat.error}</p>}
        {label !== "Connected" && <p className="border-b border-border px-4 py-2 text-xs text-gray-400">{!status?.available ? "Start the custom Chat On Steroids app and keep it running." : status.connectionState !== "connected" ? "Connect Core in Chat On Steroids." : !status.bridge?.present ? "Open ChatGPT in Chrome with the Chat On Steroids extension enabled. The Chrome DevTools connection is separate." : "Enable session recording in Chat On Steroids to receive replies."}</p>}
        <div ref={scroll} onScroll={() => { if (scroll.current) follow.current = scroll.current.scrollHeight - scroll.current.scrollTop - scroll.current.clientHeight < 90; }} className="min-h-0 flex-1 overflow-y-auto px-4 py-5">
          {showConnection && <section id="chatgpt-connection" aria-label="ChatGPT connection details" className="mx-auto mb-6 max-w-3xl rounded-xl border border-border bg-bg-secondary p-5">
            <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">Connection & tools</h3><button onClick={() => setShowConnection(false)} aria-label="Close connection details" className="rounded-sm p-1 text-gray-400 hover:bg-bg-hover"><X className="h-4 w-4" /></button></div>
            <dl className="mt-3 grid grid-cols-2 gap-2 text-xs"><dt>Local adapter</dt><dd>{status?.available ? `Available · ${status.build}` : "Unavailable"}</dd><dt>Core tunnel</dt><dd>{status?.connectionState ?? "Unknown"}</dd><dt>Chrome extension</dt><dd>{status?.bridge?.present ? "Present" : "Not detected"}</dd><dt>Recording</dt><dd>{status?.recording ? "Enabled" : "Off / unknown"}</dd><dt>Models</dt><dd>{catalog.state}</dd></dl>
            <div className="mt-4 flex items-start gap-2 text-sm text-gray-300"><Wrench className="mt-0.5 h-4 w-4 shrink-0 text-gray-500" /><p>Tools use your existing Core connection. Manage permissions and approvals in Chat On Steroids or ChatGPT. After changing tools, refresh the Core plugin in ChatGPT. Use Chat mode and keep recording enabled.</p></div>
            <a href="https://chatgpt.com/#settings/Plugins" target="_blank" rel="noopener noreferrer" className={`${buttonStyle} mt-4`}>Open ChatGPT plugin settings</a>
          </section>}
          <div className="mx-auto max-w-4xl space-y-4">
            {view.error && <p role="alert" className="text-xs text-amber-300">{view.error}</p>}
            {view.loading && <p role="status" className="text-sm text-gray-400">Loading conversation…</p>}
            {view.before !== null && Number.isFinite(view.before) && view.before > 1 && <button disabled={chat.loadingMore} onClick={() => { olderHeight.current = scroll.current?.scrollHeight ?? null; void chat.older(); }} className={buttonStyle}>Load older messages</button>}
            {view.events.map(event => <EventRow key={`${event.kind}:${event.messageId ?? event.progressId ?? event.origin ?? event.seq}`} event={event} />)}
            {!draft.sessionId && <div className="flex flex-col items-center py-8 text-center"><MessageCircle className="mb-5 h-8 w-8 text-emerald-400" /><h3 className="text-xl font-semibold text-gray-200">A separate space for ChatGPT</h3><p className="mt-3 text-sm text-gray-400">Send a prompt here while Devin works, or select an existing conversation.</p></div>}
            {draft.sessionId && !view.loading && !view.events.length && !view.error && <p className="text-sm text-gray-400">No recorded messages yet. Replies appear here as the extension records them.</p>}
          </div>
        </div>

        <div className="shrink-0 border-t border-border bg-bg-secondary p-3 sm:p-4">
          {pending && <div className="mx-auto mb-3 max-w-4xl rounded-lg border border-border p-3 text-xs" role="status">
            <p className="text-amber-300">Input {pending.state}{pending.state === "sent" && !draft.sessionId ? " · Waiting for conversation identity" : ""}</p>
            {pending.error && <p className="mt-1 text-gray-400">{pending.error}</p>}
            <details className="mt-2 text-gray-400"><summary className="cursor-pointer">Submitted prompt and receipt</summary><p className="mt-1 break-all">{pending.input.id}</p><p className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap">{pending.input.text}</p></details>
            <div className="mt-2 flex flex-wrap gap-2"><button onClick={chat.refresh} className={buttonStyle}>Check delivery</button>
              {pending.state === "uncertain" && <button disabled={label !== "Connected"} onClick={() => void chat.send(pending.input, draft.id)} className={buttonStyle}>Retry same input</button>}
              {!["submitting", "sent", "failed", "cancelled", "uncertain"].includes(pending.state) && <button disabled={chat.actionBusy} onClick={() => void chat.action(() => chatgptAPI.cancel(pending.input.id), "Cancellation requested. Check the delivery receipt.")} className={buttonStyle}>Cancel queued input</button>}
              {terminal && <button onClick={chat.dismissReceipt} className={buttonStyle}>Return prompt to draft</button>}</div>
          </div>}
          <div className="mx-auto max-w-4xl rounded-xl border border-border bg-bg-primary p-3 focus-within:border-accent/60">
            <label htmlFor="chatgpt-draft" className="mb-2 block text-xs font-medium text-gray-400">Message ChatGPT</label>
            <textarea id="chatgpt-draft" value={draft.text} onChange={event => { chat.updateDraft({ text: event.target.value }); setCopyStatus(""); }} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); submit(); } }} placeholder="Message ChatGPT…" rows={3} className="max-h-48 min-h-20 w-full resize-y bg-transparent text-sm text-gray-200 placeholder-gray-600 focus:outline-hidden" />
            <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
              <div className="flex flex-wrap items-center gap-2">
                <label className="flex items-center gap-2 text-xs text-gray-400">Model<select aria-label="ChatGPT model" value={selected?.id ?? ""} disabled={!status?.available || catalog.state !== "ready"} onChange={e => chat.updateDraft({ model: e.target.value, effort: "" })} className={selectStyle}><option value="" disabled>{catalog.models.length ? "Choose a model" : "Models unavailable"}</option>{catalog.models.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}</select></label>
                {!!selected?.efforts.length && <label className="flex items-center gap-2 text-xs text-gray-400">Reasoning<select aria-label="ChatGPT reasoning" value={validEffort ? effort : ""} onChange={e => chat.updateDraft({ effort: e.target.value })} className={selectStyle}><option value="" disabled>Choose effort</option>{selected.efforts.map(e => <option key={e} value={e}>{e}</option>)}</select></label>}
                <button aria-label="Reload ChatGPT models" title="Reload models" disabled={!status?.available || chat.actionBusy || catalog.state === "pending"} onClick={() => void chat.action(chatgptAPI.refreshModels, "Model discovery requested.")} className={buttonStyle}><RefreshCw className="h-3.5 w-3.5" /></button>
              </div>
              <div className="flex items-center gap-2"><button onClick={() => void copyDraft()} disabled={!draft.text.trim()} className={buttonStyle}><Copy className="h-3.5 w-3.5" /><span className="hidden sm:inline">Copy draft</span></button>
                <button disabled={!status?.available || !status.bridge?.present || !controls?.activeTurnId || controls.stopPending || chat.actionBusy} aria-label="Stop ChatGPT response" onClick={() => { if (controls?.activeTurnId && draft.sessionId) { const id = draft.sessionId, turn = controls.activeTurnId; void chat.action(() => chatgptAPI.stop(id, turn), "Stop requested for the selected response."); } }} className={buttonStyle}><Square className="h-3.5 w-3.5" /></button>
                <button disabled={!canSend} onClick={submit} className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-xs font-medium text-white disabled:cursor-not-allowed disabled:opacity-40"><Send className="h-3.5 w-3.5" />{controls?.activeTurnId ? "Queue for ChatGPT" : "Send to ChatGPT"}</button>
              </div>
            </div>
          </div>
          <p role="status" className="mx-auto mt-2 min-h-4 max-w-4xl text-xs text-gray-400">{draft.text.length > 16000 ? "Prompts are limited to 16,000 characters." : copyStatus || chat.notice || (controls?.activeTurnId ? "New prompts queue after the current response. Stop targets only the selected conversation." : "Enter to send · Shift+Enter for a new line. Drafts are saved in this browser when storage is available.")}</p>
        </div>
      </div>
    </div>
  );
}
