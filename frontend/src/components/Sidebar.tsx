import { useState } from "react";
import type { ReactNode } from "react";
import { RefreshCw, Clock, Folder, FolderOpen, X, Trash2, Loader2 } from "lucide-react";
import type { Session, Project } from "../types";

interface SidebarProps {
  sessions: Session[];
  projects: Project[];
  selectedProject: string | null;
  onSelectProject: (path: string | null) => void;
  onPickFolder: () => void;
  activeSessionId: string | null;
  onSelect: (sessionId: string) => void;
  onArchive: (session: Session) => Promise<void>;
  archiveDisabledSessionId?: string | null;
  onRefresh: () => void;
  header: ReactNode;
  className?: string;
  onClose?: () => void;
}

function formatRelativeTime(dateStr: string | null): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  const diffHr = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHr / 24);

  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffHr < 24) return `${diffHr}h ago`;
  if (diffDay < 7) return `${diffDay}d ago`;
  return date.toLocaleDateString();
}

export function Sidebar({
  sessions,
  projects,
  selectedProject,
  onSelectProject,
  onPickFolder,
  activeSessionId,
  onSelect,
  onArchive,
  archiveDisabledSessionId,
  onRefresh,
  header,
  className,
  onClose,
}: SidebarProps) {
  const [archivingId, setArchivingId] = useState<string | null>(null);

  const handleArchive = async (session: Session) => {
    const label = session.title || session.id;
    const confirmed = window.confirm(
      `Archive “${label}”?\n\nIt will be removed from the sidebar and exported to Devin_Chat, but the original Devin conversation will not be deleted.`
    );
    if (!confirmed) return;

    setArchivingId(session.id);
    try {
      await onArchive(session);
    } catch {
      // The sessions hook surfaces the API error in the app-wide error banner.
    } finally {
      setArchivingId((current) => (current === session.id ? null : current));
    }
  };

  return (
    <div className={`flex w-72 flex-col border-r border-border bg-bg-secondary ${className ?? ""}`}>
      <div className="flex min-h-12 shrink-0 items-center gap-1 border-b border-border px-3 py-2">
        {header}
        <div className="flex shrink-0 items-center gap-1">
          <button
            onClick={onRefresh}
            aria-label="Refresh sessions"
            className="rounded-sm p-1 text-gray-500 hover:bg-bg-hover hover:text-gray-300"
            title="Refresh sessions"
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
          {onClose && (
            <button
              onClick={onClose}
              aria-label="Close sidebar"
              className="rounded-sm p-1 text-gray-500 hover:bg-bg-hover hover:text-gray-300 md:hidden"
              title="Close sidebar"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      <div className="border-b border-border px-3 py-2">
        <label className="mb-1 flex items-center gap-1.5 text-xs font-medium text-gray-500">
          <Folder className="h-3 w-3" />
          Project
        </label>
        <select
          value={selectedProject ?? ""}
          onChange={(event) => onSelectProject(event.target.value || null)}
          className="w-full rounded-md border border-border bg-bg-tertiary px-2 py-1.5 text-sm text-gray-200 focus:border-accent focus:outline-hidden"
        >
          <option value="">All Projects</option>
          {projects.map((project) => (
            <option key={project.path} value={project.path} title={project.path}>
              {project.name} ({project.session_count})
            </option>
          ))}
        </select>
        {selectedProject && (
          <div
            className="mt-1 truncate font-mono text-[10px] text-gray-600"
            title={selectedProject}
          >
            {selectedProject}
          </div>
        )}
        <button
          type="button"
          onClick={onPickFolder}
          className="mt-1.5 flex w-full items-center gap-1.5 rounded-md border border-border bg-bg-tertiary px-2 py-1 text-xs text-gray-300 hover:bg-bg-hover hover:text-gray-100"
          title="Browse the filesystem for a project folder not listed above"
        >
          <FolderOpen className="h-3 w-3" />
          Choose folder…
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {sessions.length === 0 ? (
          <div className="px-3 py-8 text-center text-xs text-gray-600">
            {selectedProject
              ? "No unarchived conversations for this project."
              : "No conversations found. Start a new one above."}
          </div>
        ) : (
          <div className="space-y-0.5">
            {sessions.map((session) => {
              const active = activeSessionId === session.id;
              const archiving = archivingId === session.id;
              const archiveDisabled = archiveDisabledSessionId === session.id;
              return (
                <div
                  key={session.id}
                  className={`group flex items-start rounded-md border transition-colors ${
                    active
                      ? "border-accent/30 bg-accent/15"
                      : "border-transparent hover:bg-bg-hover"
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => onSelect(session.id)}
                    className="min-w-0 flex-1 px-3 py-2 text-left"
                  >
                    <div className="truncate text-sm font-medium text-gray-200">
                      {session.title || session.id}
                    </div>
                    <div className="mt-0.5 truncate font-mono text-xs text-gray-500">
                      {session.id}
                    </div>
                    <div className="mt-1 flex items-center gap-2 text-xs text-gray-600">
                      <Clock className="h-3 w-3" />
                      {formatRelativeTime(session.last_activity_at)}
                      {session.model && (
                        <span className="truncate text-gray-700">· {session.model}</span>
                      )}
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleArchive(session)}
                    disabled={archiving || archiveDisabled}
                    aria-label={`Archive ${session.title || session.id}`}
                    title={
                      archiveDisabled
                        ? "Wait for the current operation to finish before archiving"
                        : "Archive conversation (does not delete it)"
                    }
                    className="mr-1 mt-1.5 rounded-sm p-1.5 text-gray-600 opacity-40 hover:bg-red-950/40 hover:text-red-400 focus:opacity-100 disabled:cursor-not-allowed disabled:opacity-30 group-hover:opacity-100"
                  >
                    {archiving ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Trash2 className="h-3.5 w-3.5" />
                    )}
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
