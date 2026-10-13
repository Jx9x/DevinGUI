import { useState, useEffect, useCallback, useRef } from "react";
import { api, ApiError } from "../api/client";
import type { Session, Project, ArchiveResponse } from "../types";
import { readSelectedProject, saveSelectedProject } from "../promptPersistence";

export interface UseSessionsReturn {
  sessions: Session[];
  projects: Project[];
  selectedProject: string | null;
  setSelectedProject: (path: string | null) => void;
  sessionsLoaded: boolean;
  archiveSession: (sessionId: string) => Promise<ArchiveResponse>;
  refresh: () => Promise<void>;
  error: string | null;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError
    ? error.message
    : error instanceof Error
      ? error.message
      : fallback;
}

export function useSessions(): UseSessionsReturn {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProjectState] = useState<string | null>(readSelectedProject);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionsRequestRef = useRef(0);
  const projectsRequestRef = useRef(0);

  const refreshSessions = useCallback(async (cwd?: string) => {
    const request = ++sessionsRequestRef.current;
    try {
      const data = await api.getSessions(cwd);
      if (request !== sessionsRequestRef.current) return;
      setSessions(data);
      setSessionsLoaded(true);
      setError(null);
    } catch (e) {
      if (request !== sessionsRequestRef.current) return;
      const message = errorMessage(e, "Failed to fetch sessions");
      console.error(message, e);
      setError(message);
    }
  }, []);

  const setSelectedProject = useCallback((path: string | null) => {
    saveSelectedProject(path);
    sessionsRequestRef.current += 1;
    setSessions([]);
    setSessionsLoaded(false);
    setSelectedProjectState(path);
  }, []);

  const refreshProjects = useCallback(async () => {
    const request = ++projectsRequestRef.current;
    try {
      const data = await api.getProjects();
      if (request !== projectsRequestRef.current) return;
      setProjects(data);
      setError(null);
    } catch (e) {
      if (request !== projectsRequestRef.current) return;
      const message = errorMessage(e, "Failed to fetch projects");
      console.error(message, e);
      setError(message);
    }
  }, []);

  useEffect(() => {
    void refreshSessions(selectedProject || undefined);
  }, [refreshSessions, selectedProject]);

  useEffect(() => {
    void refreshProjects();
  }, [refreshProjects]);

  const archiveSession = useCallback(
    async (sessionId: string) => {
      try {
        const result = await api.archiveSession(sessionId);
        setSessions((current) => current.filter((session) => session.id !== sessionId));
        await refreshProjects();
        setError(null);
        return result;
      } catch (e) {
        const message = errorMessage(e, "Failed to archive session");
        console.error(message, e);
        setError(message);
        throw e;
      }
    },
    [refreshProjects]
  );

  const refresh = useCallback(async () => {
    await Promise.all([refreshSessions(selectedProject || undefined), refreshProjects()]);
  }, [refreshSessions, refreshProjects, selectedProject]);

  return {
    sessions,
    projects,
    selectedProject,
    setSelectedProject,
    sessionsLoaded,
    archiveSession,
    refresh,
    error,
  };
}
