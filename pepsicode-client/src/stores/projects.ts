import { create } from "zustand";
import { sendRequest, useConnectionStore } from "./connection";
import type { SessionMeta } from "./session";

export interface Project {
  path: string;
  expanded: boolean;
}

interface ProjectSessions {
  sessions: SessionMeta[];
  loading: boolean;
  error: string;
}

const STORAGE_KEY = "pepsicode.projects.v1";

export function projectKey(path: string): string {
  const normalized = path.replace(/\\/g, "/").replace(/\/+$/, "") || "/";
  return /^(?:[a-z]:|\/\/)/i.test(normalized) ? normalized.toLowerCase() : normalized;
}

export function projectName(path: string): string {
  return path.replace(/\\/g, "/").split("/").filter(Boolean).pop() || path;
}

function readProjects(): Project[] {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (!Array.isArray(saved)) return [];
    const seen = new Set<string>();
    return saved.flatMap((item) => {
      if (!item || typeof item.path !== "string" || !item.path.trim()) return [];
      const key = projectKey(item.path);
      if (seen.has(key)) return [];
      seen.add(key);
      return [{ path: item.path, expanded: item.expanded !== false }];
    });
  } catch {
    return [];
  }
}

function saveProjects(projects: Project[]) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(projects)); }
  catch { /* The sidebar remains usable if local storage is unavailable. */ }
}

interface ProjectsState {
  projects: Project[];
  byProject: Record<string, ProjectSessions>;
  rememberProject: (path: string) => void;
  setExpanded: (path: string, expanded: boolean) => void;
  loadSessions: (path: string) => Promise<void>;
  syncSessions: (path: string, sessions: SessionMeta[]) => void;
}

const requests = new Map<string, symbol>();

export const useProjectsStore = create<ProjectsState>((set, get) => ({
  projects: readProjects(),
  byProject: {},

  rememberProject: (path) => {
    if (!path.trim() || get().projects.some((item) => projectKey(item.path) === projectKey(path))) return;
    const projects = [...get().projects, { path, expanded: true }];
    saveProjects(projects);
    set({ projects });
  },

  setExpanded: (path, expanded) => {
    const projects = get().projects.map((item) => projectKey(item.path) === projectKey(path) ? { ...item, expanded } : item);
    saveProjects(projects);
    set({ projects });
  },

  loadSessions: async (path) => {
    const key = projectKey(path);
    const { ws } = useConnectionStore.getState();
    if (!ws || requests.has(key)) return;
    const request = Symbol(key);
    requests.set(key, request);
    set((state) => ({ byProject: { ...state.byProject, [key]: {
      sessions: state.byProject[key]?.sessions || [], loading: true, error: "",
    } } }));
    try {
      const result = await sendRequest(ws, "session/list", { workspace: path });
      if (requests.get(key) !== request) return;
      set((state) => ({ byProject: { ...state.byProject, [key]: {
        sessions: result.sessions || [], loading: false, error: "",
      } } }));
    } catch (error) {
      if (requests.get(key) !== request) return;
      set((state) => ({ byProject: { ...state.byProject, [key]: {
        sessions: state.byProject[key]?.sessions || [], loading: false,
        error: error instanceof Error ? error.message : String(error),
      } } }));
    } finally {
      if (requests.get(key) === request) requests.delete(key);
    }
  },

  syncSessions: (path, sessions) => {
    // A live session refresh is newer than a pending sidebar query.
    const key = projectKey(path);
    requests.delete(key);
    set((state) => ({ byProject: { ...state.byProject, [key]: { sessions, loading: false, error: "" } } }));
  },
}));
