const PROJECT_KEY = "devin-gui-selected-project-v1";
const SESSION_KEY = "devin-gui-active-session-v1";
const DRAFT_PREFIX = "devin-gui-prompt-draft-v1:";

export interface SavedSession {
  id: string;
  projectPath: string | null;
}

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): boolean {
  try {
    if (value === null) {
      window.localStorage.removeItem(key);
    } else {
      window.localStorage.setItem(key, value);
    }
    return true;
  } catch {
    return false;
  }
}

export function readActiveSession(): SavedSession | null {
  const raw = read(SESSION_KEY);
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (
      typeof value === "object" &&
      value !== null &&
      "id" in value &&
      typeof value.id === "string" &&
      value.id &&
      "projectPath" in value &&
      (value.projectPath === null || typeof value.projectPath === "string")
    ) {
      return { id: value.id, projectPath: value.projectPath };
    }
  } catch {
    // Ignore a partial or older storage entry.
  }
  return null;
}

export function saveActiveSession(session: SavedSession): void {
  write(SESSION_KEY, JSON.stringify(session));
}

export function clearActiveSession(): void {
  write(SESSION_KEY, null);
}

export function readSelectedProject(): string | null {
  // When a conversation was selected from All Projects, recover its folder.
  return readActiveSession()?.projectPath || read(PROJECT_KEY);
}

export function saveSelectedProject(path: string | null): void {
  write(PROJECT_KEY, path);
}

function draftKey(sessionId: string): string {
  return `${DRAFT_PREFIX}${sessionId}`;
}

export function readPromptDraft(sessionId: string | null): string {
  return sessionId ? read(draftKey(sessionId)) ?? "" : "";
}

export function savePromptDraft(sessionId: string, text: string): boolean {
  return write(draftKey(sessionId), text || null);
}

export interface SavedPromptDraft {
  sessionId: string;
  text: string;
}

export function listPromptDrafts(): SavedPromptDraft[] {
  try {
    const drafts: SavedPromptDraft[] = [];
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (!key?.startsWith(DRAFT_PREFIX)) continue;
      const text = window.localStorage.getItem(key);
      if (text) drafts.push({ sessionId: key.slice(DRAFT_PREFIX.length), text });
    }
    return drafts;
  } catch {
    return [];
  }
}
