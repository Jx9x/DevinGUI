import { useState, useEffect, useRef, useCallback } from "react";
import {
  MessageSquarePlus,
  Loader2,
  Wifi,
  WifiOff,
  Settings2,
  Menu,
  Play,
} from "lucide-react";
import type { ImageAttachment, WSInbound } from "./types";
import { useWebSocket } from "./hooks/useWebSocket";
import { useSessions } from "./hooks/useSessions";
import { useModels } from "./hooks/useModels";
import { useUsage } from "./hooks/useUsage";
import { useDiagnostics } from "./hooks/useDiagnostics";
import { DiagnosticsPanel } from "./components/DiagnosticsPanel";
import { readIntegrationSettings, saveIntegrationSettings, type IntegrationSettings } from "./integrationSettings";
import { useChatSession } from "./hooks/useChatSession";
import { Sidebar } from "./components/Sidebar";
import { ChatArea } from "./components/ChatArea";
import { FolderPicker } from "./components/FolderPicker";
import { PromptInput } from "./components/PromptInput";
import { PermissionPanel } from "./components/PermissionPanel";
import { ExportButton } from "./components/ExportButton";
import { StatsBar } from "./components/StatsBar";
import { ModelSelector } from "./components/ModelSelector";
import { ModelManager } from "./components/ModelManager";
import { UsageBox } from "./components/UsageBox";
import { AssistantSwitcher, type AssistantPane } from "./components/AssistantSwitcher";
import { ChatGPTPane } from "./components/ChatGPTPane";
import { clearActiveSession, listPromptDrafts, readActiveSession, saveActiveSession } from "./promptPersistence";

export default function App() {
  const {
    sessions: sessionList,
    projects,
    selectedProject,
    setSelectedProject,
    sessionsLoaded,
    archiveSession,
    refresh: refreshSessions,
    error: sessionsError,
  } = useSessions();
  const {
    models: modelList,
    selectableModels,
    selectedModel,
    setSelectedModel,
    lastUsedVariants,
    hasNewModels,
    refresh: refreshModels,
    excludeModel,
    includeModel,
    excludeModels,
    includeModels,
    acknowledgeModel,
    cacheUpdatedAt,
    error: modelsError,
  } = useModels();
  const diagnostics = useDiagnostics();
  const [integrations, setIntegrations] = useState(readIntegrationSettings);
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [showModelManager, setShowModelManager] = useState(false);
  const [showFolderPicker, setShowFolderPicker] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [activePane, setActivePane] = useState<AssistantPane>("devin");
  const [chatgptStatus, setChatgptStatus] = useState("Checking connection");
  const [showNewSessionConfirm, setShowNewSessionConfirm] = useState(false);
  const [showDraftRecovery, setShowDraftRecovery] = useState(false);
  const [savedDrafts, setSavedDrafts] = useState(listPromptDrafts);
  const [draftCopyStatus, setDraftCopyStatus] = useState("");
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [bypassMode, setBypassMode] = useState(() => localStorage.getItem("devin-gui-bypass-mode") === "true");
  const bypassModeRef = useRef(bypassMode);
  bypassModeRef.current = bypassMode;
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  // Persistent "follow output" state. Recomputing "near bottom" only after
  // new content renders is too late: scrollHeight has already grown, making a
  // user who *was* pinned to the bottom suddenly look scrolled up. Instead a
  // user scroll event explicitly enters/leaves follow mode.
  const followOutputRef = useRef(true);
  const { snapshot: usageSnapshot, codexSnapshot, loading: usageLoading, codexLoading, refresh: refreshUsage } = useUsage(integrations);
  const [savedSessionAtStartup] = useState(readActiveSession);
  const pendingSessionRestoreRef = useRef(savedSessionAtStartup);

  const refreshSavedDrafts = useCallback(() => setSavedDrafts(listPromptDrafts()), []);

  useEffect(() => {
    window.addEventListener("storage", refreshSavedDrafts);
    return () => window.removeEventListener("storage", refreshSavedDrafts);
  }, [refreshSavedDrafts]);

  const onServerMessageRef = useRef<((msg: WSInbound) => void) | undefined>(undefined);
  const activeSessionIdRef = useRef<string | null>(null);
  const sessionLiveRef = useRef(false);
  const selectedModelRef = useRef<string | null>(selectedModel);
  selectedModelRef.current = selectedModel;
  const resumeStartedRef = useRef<((requestId: string) => void) | undefined>(undefined);
  const onPromptFlushedRef = useRef<((requestId: string) => void) | undefined>(undefined);
  const onQueueDroppedRef = useRef<
    ((requestId: string, reason: "cancelled" | "expired" | "resume_failed") => void) | undefined
  >(undefined);
  const onConnectionLostRef = useRef<
    | ((payload: {
        permission: { toolCallId?: string; correlationId?: string } | null;
        cancelRequestId: string | null;
      }) => void)
    | undefined
  >(undefined);
  const { send, connected, cancelQueuedPrompt } = useWebSocket(
    (msg: WSInbound) => onServerMessageRef.current?.(msg),
    () => (sessionLiveRef.current ? activeSessionIdRef.current : null),
    (requestId: string) => resumeStartedRef.current?.(requestId),
    (requestId: string) => onPromptFlushedRef.current?.(requestId),
    (requestId: string, reason) => onQueueDroppedRef.current?.(requestId, reason),
    (payload) => onConnectionLostRef.current?.(payload),
    () => selectedModelRef.current,
    () => bypassModeRef.current
  );

  const cancelQueuedPromptRef = useRef(cancelQueuedPrompt);
  cancelQueuedPromptRef.current = cancelQueuedPrompt;

  const onSessionCreated = useCallback(
    (sessionId: string, model?: string) => {
      pendingSessionRestoreRef.current = null;
      saveActiveSession({ id: sessionId, projectPath: selectedProject });
      if (model) setSelectedModel(model);
      void refreshSessions();
      void refreshModels();
    },
    [selectedProject, setSelectedModel, refreshSessions, refreshModels]
  );

  const { refresh: refreshDiagnostics } = diagnostics;
  const onTurnComplete = useCallback(() => {
    void refreshSessions();
    void refreshDiagnostics();
    // Quota numbers change after each inference, so refresh once the turn ends.
    void refreshUsage();
  }, [refreshSessions, refreshUsage, refreshDiagnostics]);

  const chat = useChatSession(
    send,
    { onSessionCreated, onTurnComplete },
    {
      cancelQueuedPrompt: (requestId: string) =>
        cancelQueuedPromptRef.current?.(requestId) ?? false,
    }
  );
  onServerMessageRef.current = chat.onServerMessage;
  activeSessionIdRef.current = chat.activeSessionId;
  sessionLiveRef.current = chat.sessionLive;
  resumeStartedRef.current = chat.notifyResumeStarted;
  onPromptFlushedRef.current = chat.onPromptFlushed;
  onQueueDroppedRef.current = chat.onQueueDropped;
  onConnectionLostRef.current = chat.onConnectionLost;
  const { activeSessionId, selectSession } = chat;

  useEffect(() => {
    const pending = pendingSessionRestoreRef.current;
    if (!pending || !connected || !sessionsLoaded || activeSessionId) return;
    const session = sessionList.find((item) => item.id === pending.id);
    if (!session) {
      pendingSessionRestoreRef.current = null;
      clearActiveSession();
      return;
    }
    const projectPath = pending.projectPath || session.working_directory;
    if (projectPath && selectedProject !== projectPath) {
      pendingSessionRestoreRef.current = { id: pending.id, projectPath };
      saveActiveSession(pendingSessionRestoreRef.current);
      setSelectedProject(projectPath);
      return;
    }
    if (selectSession(pending.id)) {
      pendingSessionRestoreRef.current = null;
    }
  }, [activeSessionId, selectSession, connected, selectedProject, sessionList, sessionsLoaded, setSelectedProject]);

  useEffect(() => {
    localStorage.setItem("devin-gui-bypass-mode", String(bypassMode));
  }, [bypassMode]);

  // Chrome can keep CSS animations ticking on a background/minimized page,
  // especially when hardware acceleration is unavailable. Mark the document
  // hidden so CSS can pause purely decorative activity until the tab is used
  // again. Streaming itself continues over the WebSocket.
  useEffect(() => {
    const root = document.documentElement;
    const syncVisibility = () => {
      if (document.hidden) {
        root.setAttribute("data-devin-gui-hidden", "true");
      } else {
        root.removeAttribute("data-devin-gui-hidden");
      }
    };
    syncVisibility();
    document.addEventListener("visibilitychange", syncVisibility);
    return () => {
      document.removeEventListener("visibilitychange", syncVisibility);
      root.removeAttribute("data-devin-gui-hidden");
    };
  }, []);

  useEffect(() => {
    const onError = (event: ErrorEvent) => {
      console.error("Unhandled error:", event.error);
      setGlobalError(
        event.error instanceof Error ? event.error.message : "An unexpected error occurred"
      );
    };
    const onRejection = (event: PromiseRejectionEvent) => {
      console.error("Unhandled promise rejection:", event.reason);
      setGlobalError(
        event.reason instanceof Error ? event.reason.message : "An unexpected error occurred"
      );
    };
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);

  // A newly selected conversation should initially show its newest content.
  useEffect(() => {
    followOutputRef.current = true;
  }, [chat.activeSessionId]);

  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;

    const updateFollowMode = () => {
      const distanceFromBottom =
        container.scrollHeight - container.clientHeight - container.scrollTop;
      followOutputRef.current = distanceFromBottom < 80;
    };

    container.addEventListener("scroll", updateFollowMode, { passive: true });
    return () => container.removeEventListener("scroll", updateFollowMode);
  }, [chat.activeSessionId]);

  useEffect(() => {
    const endMarker = messagesEndRef.current;
    if (activePane !== "devin" || !endMarker || !followOutputRef.current) return;

    const id = requestAnimationFrame(() => {
      // Use an immediate jump for both live and final reconciliation. Smooth
      // scrolling can still be in flight when the next streamed batch lands,
      // which both loses the bottom lock and needlessly wakes the compositor.
      endMarker.scrollIntoView({ behavior: "auto", block: "end" });
    });
    return () => cancelAnimationFrame(id);
  }, [chat.messages, chat.reasoningSteps, chat.costEstimate, chat.isStreaming, activePane]);

  const handleSelectProject = (path: string | null) => {
    if (path === selectedProject) return;
    // A project selection controls both the sidebar filter and the working
    // directory used for the next session. Clear any active conversation so
    // a prompt cannot accidentally be sent to a session from the old project.
    pendingSessionRestoreRef.current = null;
    clearActiveSession();
    chat.clearSession();
    setSelectedProject(path);
  };

  const startNewSession = () => {
    if (!selectedProject) return false;
    setSidebarOpen(false);
    setShowNewSessionConfirm(false);
    const started = chat.newSession(selectedProject, selectedModel || undefined, bypassMode ? "bypass" : undefined);
    if (started) {
      pendingSessionRestoreRef.current = null;
      clearActiveSession();
    }
    return started;
  };

  const handleNewSession = () => {
    const wouldInterrupt =
      chat.isStreaming ||
      chat.queued ||
      chat.loadingHistory ||
      chat.resuming ||
      Boolean(chat.permissionRequest);
    if (wouldInterrupt) {
      setShowNewSessionConfirm(true);
      return false;
    }
    return startNewSession();
  };
  const handleSelectSession = (sessionId: string) => {
    setSidebarOpen(false);
    if (chat.selectSession(sessionId)) {
      pendingSessionRestoreRef.current = null;
      const session = sessionList.find((item) => item.id === sessionId);
      saveActiveSession({ id: sessionId, projectPath: selectedProject || session?.working_directory || null });
    }
  };
  const handleStartSession = () => {
    chat.startSession(selectedModel || undefined, bypassMode ? "bypass" : undefined);
  };
  const handleArchiveSession = async (session: (typeof sessionList)[number]) => {
    await archiveSession(session.id);
    if (chat.activeSessionId === session.id) {
      pendingSessionRestoreRef.current = null;
      clearActiveSession();
      chat.clearSession();
    }
  };
  const handleSendPrompt = (text: string, images?: ImageAttachment[]) =>
    chat.sendPrompt(text, images, selectedModel || undefined);
  const handleCancel = () => chat.cancel();
  const handleCopyDraft = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setDraftCopyStatus(`Copied draft from ${label}.`);
    } catch {
      setDraftCopyStatus("Clipboard unavailable. Select the draft text above and copy it manually.");
    }
  };
  const handlePermissionResponse = (decision: "allow" | "deny", optionId?: string) => chat.respondPermission(decision, optionId);
  const handleDiscardPermission = () => chat.discardPermission();

  const handleRefresh = async () => {
    try {
      await Promise.all([refreshSessions(), refreshModels()]);
    } catch {
      // errors are already captured by the hooks and surfaced in the banner
    }
  };

  const changeIntegrations = (settings: IntegrationSettings) => {
    setIntegrations(settings);
    if (!settings.chatgpt) setActivePane("devin");
    return saveIntegrationSettings(settings);
  };
  const retryChecks = async () => {
    await refreshDiagnostics(true);
    await Promise.allSettled([refreshSessions(), refreshModels()]);
  };
  const setupIssues = diagnostics.snapshot?.checks.filter(check =>
    ["python", "devin", "devin_auth", "database", "frontend"].includes(check.id) && check.status !== "ok"
  ) ?? [];
  const activeSession = sessionList.find((s) => s.id === chat.activeSessionId);
  const displayError =
    [chat.error, chat.connectionError, sessionsError, modelsError, globalError].filter(Boolean).join(" — ") || null;
  const assistantHeader = (
    <AssistantSwitcher
      active={activePane}
      onSelect={(pane) => { setActivePane(pane); setSidebarOpen(false); }}
      devinStatus={chat.permissionRequest ? "Approval needed" : !connected ? "Disconnected" : chat.isStreaming ? "Working" : chat.queued ? "Queued" : chat.loadingHistory || chat.resuming ? "Loading" : "Ready"}
      devinNeedsAttention={Boolean(chat.permissionRequest)}
      chatgptStatus={chatgptStatus}
      chatgptEnabled={integrations.chatgpt}
      onDiagnostics={() => { setShowDiagnostics(true); void refreshDiagnostics(); }}
      diagnosticsNeedsAttention={setupIssues.length > 0 || Boolean(diagnostics.error)}
    />
  );

  return (
    <div className="flex h-dvh w-full flex-col overflow-hidden bg-bg-primary text-gray-100">
      {(setupIssues.length > 0 || diagnostics.error) && <div role="status" className="max-h-40 shrink-0 overflow-y-auto border-b border-amber-700 bg-amber-900/20 px-4 py-2 text-xs text-amber-200">
        {setupIssues.map(check => <p key={check.id} className="mb-1">{check.message}</p>)}
        {diagnostics.error && <p>Could not check local setup: {diagnostics.error}</p>}
        <div className="mt-2 flex gap-3"><button onClick={() => setShowDiagnostics(true)} className="underline">View diagnostics</button><button disabled={diagnostics.loading} onClick={() => void retryChecks()} className="underline disabled:opacity-40">{diagnostics.loading ? "Checking…" : "Retry checks"}</button></div>
      </div>}
      {showDiagnostics && <DiagnosticsPanel snapshot={diagnostics.snapshot} loading={diagnostics.loading} error={diagnostics.error}
        settings={integrations} onChange={changeIntegrations} onRetry={() => void retryChecks()} onClose={() => setShowDiagnostics(false)} />}
      {/* Keep both panes mounted: switching must preserve drafts, sessions and live output. */}
      <div id="devin-panel" role="region" aria-label="Devin workspace" hidden={activePane !== "devin"} className={activePane === "devin" ? "flex min-h-0 min-w-0 flex-1" : "hidden"}>
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/50 md:hidden"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}
      <Sidebar
        sessions={sessionList}
        projects={projects}
        selectedProject={selectedProject}
        onSelectProject={handleSelectProject}
        onPickFolder={() => setShowFolderPicker(true)}
        activeSessionId={chat.activeSessionId}
        onSelect={handleSelectSession}
        onArchive={handleArchiveSession}
        archiveDisabledSessionId={
          chat.isStreaming || chat.queued || chat.loadingHistory || chat.resuming
            ? chat.activeSessionId
            : null
        }
        onRefresh={handleRefresh}
        header={assistantHeader}
        className={`${sidebarOpen ? "fixed inset-y-0 left-0 z-40 flex md:static" : "hidden"} md:flex`}
        onClose={() => setSidebarOpen(false)}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-bg-secondary px-4 py-2">
          <div className="flex w-full min-w-0 items-center gap-2 md:hidden">
            <button
              onClick={() => setSidebarOpen(true)}
              aria-label="Open sidebar"
              title="Open sidebar"
              className="shrink-0 rounded-sm p-1 text-gray-500 hover:bg-bg-hover hover:text-gray-300"
            >
              <Menu className="h-4 w-4" />
            </button>
            {assistantHeader}
          </div>
          <div className="flex items-center gap-2 text-sm text-gray-400">
            {chat.activeSessionId ? (
              <>
                <span className="font-mono text-accent">{chat.activeSessionId}</span>
                {(chat.loadingHistory || chat.resuming) && (
                  <span className="flex items-center gap-1 text-xs text-gray-500">
                    <Loader2 className="h-3 w-3 animate-spin" />{" "}
                    {chat.loadingHistory ? "Loading history…" : "Resuming session…"}
                  </span>
                )}
                {!chat.loadingHistory && !chat.resuming && !chat.sessionLive && (
                  <span className="text-xs text-amber-400">Read-only — click Start to continue</span>
                )}
              </>
            ) : (
              <span>No session selected — click &quot;New Session&quot; to start</span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <ModelSelector
              models={selectableModels}
              selectedModel={selectedModel}
              lastUsedVariants={lastUsedVariants}
              hasNewModels={hasNewModels}
              cacheUpdatedAt={cacheUpdatedAt}
              onSelect={setSelectedModel}
              onOpenManager={() => setShowModelManager(true)}
            />
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-amber-300" title="Starts the next new session with Devin permission prompts bypassed">
              <input
                type="checkbox"
                checked={bypassMode}
                onChange={(event) => setBypassMode(event.target.checked)}
                className="accent-amber-400"
              />
              Bypass approvals
            </label>
            <button
              onClick={() => setShowModelManager(true)}
              aria-label="Manage models"
              className={`relative rounded-md p-1.5 text-gray-500 hover:bg-bg-hover hover:text-gray-300 ${hasNewModels ? "text-blue-400" : ""}`}
              title="Manage models"
            >
              <Settings2 className="h-4 w-4" />{" "}
              {hasNewModels && (
                <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-blue-500" />
              )}
            </button>
            {connected ? (
              <span className="flex items-center gap-1 text-xs text-green-400">
                <Wifi className="h-3 w-3" /> Connected
              </span>
            ) : (
              <span className="flex items-center gap-1 text-xs text-red-400">
                <WifiOff className="h-3 w-3" /> Disconnected
              </span>
            )}
            <button
              type="button"
              onClick={() => { refreshSavedDrafts(); setDraftCopyStatus(""); setShowDraftRecovery(true); }}
              className="rounded-md border border-border px-3 py-1 text-xs text-gray-300 hover:bg-bg-hover"
            >
              Saved drafts{savedDrafts.length ? ` (${savedDrafts.length})` : ""}
            </button>
            <button
              onClick={handleNewSession}
              disabled={!selectedProject}
              aria-label="New session"
              title={selectedProject ? undefined : "Select a project folder first"}
              className="flex items-center gap-1 rounded-md bg-accent px-3 py-1 text-xs font-medium text-white hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
            >
              <MessageSquarePlus className="h-3.5 w-3.5" /> New Session
            </button>
          </div>
        </div>
        <StatsBar
          usage={chat.usage}
          metrics={chat.aggregateMetrics}
          costEstimate={chat.costEstimate}
        />
        {displayError && (
          <div className="border-b border-red-800 bg-red-900/30 px-4 py-2 text-sm text-red-300">
            {displayError}
          </div>
        )}
        <PermissionPanel
          request={chat.permissionRequest}
          messages={chat.messages}
          connected={connected}
          onRespond={handlePermissionResponse}
          onDiscard={handleDiscardPermission}
        />
        <ChatArea
          messages={chat.messages}
          reasoningSteps={chat.reasoningSteps}
          status={chat.status}
          messagesEndRef={messagesEndRef}
          scrollContainerRef={scrollContainerRef}
          onCancel={handleCancel}
          costEstimate={chat.costEstimate}
        />
        <PromptInput
          key={chat.activeSessionId ?? "no-session"}
          draftSessionId={chat.activeSessionId}
          onDraftChange={refreshSavedDrafts}
          onSend={handleSendPrompt}
          onCancel={handleCancel}
          isStreaming={chat.isStreaming}
          isQueued={chat.queued}
          disabled={!chat.activeSessionId || !chat.sessionLive}
          placeholder={
            chat.activeSessionId && !chat.sessionLive && !chat.resuming
              ? "This conversation is read-only. Click Start to continue it with Devin."
              : undefined
          }
          left={
            <div className="flex flex-col gap-2">
              {chat.activeSessionId && !chat.sessionLive && !chat.loadingHistory && !chat.resuming && (
                <button
                  type="button"
                  onClick={handleStartSession}
                  disabled={!connected}
                  aria-label="Start conversation"
                  title="Start Devin CLI for this conversation"
                  className="flex items-center justify-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Play className="h-3.5 w-3.5 fill-current" /> Start
                </button>
              )}
              <ExportButton
                sessionId={chat.activeSessionId}
                defaultDirectory={selectedProject || activeSession?.working_directory || ""}
                title={activeSession?.title}
              />
            </div>
          }
          aside={
            <UsageBox snapshot={usageSnapshot} loading={usageLoading} codexSnapshot={codexSnapshot}
              devinEnabled={integrations.devinUsage} codexEnabled={integrations.codexUsage}
              codexLoading={codexLoading} onRefresh={() => { void refreshUsage(); }} />
          }
        />
      </div>
      {showNewSessionConfirm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 px-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="new-session-confirm-title"
        >
          <div className="w-full max-w-md rounded-lg border border-border bg-bg-secondary p-5 shadow-2xl">
            <h2 id="new-session-confirm-title" className="text-base font-semibold text-gray-100">
              Stop the current conversation?
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-gray-400">
              Devin is active. Starting a new session will stop the current conversation or
              operation.
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowNewSessionConfirm(false)}
                className="rounded-md border border-border px-3 py-1.5 text-sm text-gray-300 hover:bg-bg-hover"
              >
                Go Back
              </button>
              <button
                type="button"
                onClick={startNewSession}
                className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover"
              >
                Continue
              </button>
            </div>
          </div>
        </div>
      )}
      {showDraftRecovery && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 px-4" role="dialog" aria-modal="true" aria-labelledby="saved-drafts-title" onKeyDown={(event) => { if (event.key === "Escape") setShowDraftRecovery(false); }}>
          <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-lg border border-border bg-bg-secondary p-5 shadow-2xl">
            <div className="flex items-center justify-between gap-4">
              <h2 id="saved-drafts-title" className="text-base font-semibold text-gray-100">Saved Devin drafts</h2>
              <button type="button" onClick={() => setShowDraftRecovery(false)} aria-label="Close saved drafts" className="text-sm text-gray-400 hover:text-gray-100">Close</button>
            </div>
            <p className="mt-2 text-sm text-gray-400">Drafts saved at this browser address stay with their original conversations. You can reopen one or copy its text into a new chat.</p>
            <div className="mt-4 space-y-4 overflow-y-auto">
              {savedDrafts.length === 0 ? (
                <p className="py-6 text-center text-sm text-gray-500">No saved Devin drafts were found at this browser address.</p>
              ) : savedDrafts.map((draft) => {
                const session = sessionList.find((item) => item.id === draft.sessionId);
                return (
                  <div key={draft.sessionId} className="rounded-md border border-border bg-bg-tertiary p-3">
                    <div className="truncate text-sm font-medium text-gray-200">{session?.title || `Conversation ${draft.sessionId}`}</div>
                    <div className="mt-0.5 truncate font-mono text-xs text-gray-500">{draft.sessionId}</div>
                    <textarea aria-label={`Saved draft for ${session?.title || draft.sessionId}`} value={draft.text} readOnly rows={4} className="mt-3 max-h-48 w-full resize-y rounded-md border border-border bg-bg-primary p-2 text-sm text-gray-200" />
                    <div className="mt-2 flex flex-wrap gap-2">
                      {session && <button type="button" onClick={() => { handleSelectSession(draft.sessionId); setShowDraftRecovery(false); }} className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover">Open conversation</button>}
                      <button type="button" onClick={() => void handleCopyDraft(draft.text, session?.title || draft.sessionId)} className="rounded-md border border-border px-3 py-1.5 text-xs text-gray-300 hover:bg-bg-hover">Copy text</button>
                    </div>
                  </div>
                );
              })}
            </div>
            <p role="status" className="mt-2 min-h-5 text-xs text-gray-400">{draftCopyStatus}</p>
          </div>
        </div>
      )}
      {showModelManager && (
        <ModelManager
          models={modelList}
          hasNewModels={hasNewModels}
          onExclude={excludeModel}
          onInclude={includeModel}
          onExcludeMany={excludeModels}
          onIncludeMany={includeModels}
          onAcknowledge={acknowledgeModel}
          onClose={() => setShowModelManager(false)}
        />
      )}
      {showFolderPicker && (
        <FolderPicker
          initialPath={selectedProject}
          onConfirm={(path) => {
            setShowFolderPicker(false);
            handleSelectProject(path);
          }}
          onClose={() => setShowFolderPicker(false)}
        />
      )}
      </div>
      {integrations.chatgpt && <div id="chatgpt-panel" role="region" aria-label="ChatGPT workspace" hidden={activePane !== "chatgpt"} className={activePane === "chatgpt" ? "flex min-h-0 min-w-0 flex-1" : "hidden"}>
        <ChatGPTPane active={activePane === "chatgpt"} onStatusChange={setChatgptStatus} header={assistantHeader} />
      </div>}
    </div>
  );
}
