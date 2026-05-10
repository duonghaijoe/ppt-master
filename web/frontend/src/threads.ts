import type { AgentEvent } from "./hooks/useAgentStream";

export type ThreadMeta = {
  id: string;
  project: string;
  started: number;
  lastUsed: number;
  title: string;
};

const THREADS_KEY = "pptmaster:threads:v1";
const EVENTS_PREFIX = "pptmaster:thread-events:v1:";
const MAX_EVENTS_PER_THREAD = 500;
const MAX_THREADS_PER_PROJECT = 20;

function readAll(): Record<string, ThreadMeta[]> {
  try {
    const raw = localStorage.getItem(THREADS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed ? parsed : {};
  } catch {
    return {};
  }
}

function writeAll(all: Record<string, ThreadMeta[]>) {
  try {
    localStorage.setItem(THREADS_KEY, JSON.stringify(all));
  } catch {
    // quota or disabled localStorage — ignore
  }
}

export function loadThreads(project: string): ThreadMeta[] {
  const all = readAll();
  const list = all[project] ?? [];
  return list.slice().sort((a, b) => b.lastUsed - a.lastUsed);
}

export function upsertThread(meta: ThreadMeta) {
  const all = readAll();
  const list = all[meta.project] ?? [];
  const idx = list.findIndex((t) => t.id === meta.id);
  if (idx >= 0) list[idx] = meta;
  else list.push(meta);
  list.sort((a, b) => b.lastUsed - a.lastUsed);
  if (list.length > MAX_THREADS_PER_PROJECT) {
    const dropped = list.slice(MAX_THREADS_PER_PROJECT);
    for (const t of dropped) clearThreadEvents(t.id);
    list.length = MAX_THREADS_PER_PROJECT;
  }
  all[meta.project] = list;
  writeAll(all);
}

export function loadThreadEvents(threadId: string): AgentEvent[] {
  try {
    const raw = localStorage.getItem(EVENTS_PREFIX + threadId);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as AgentEvent[]) : [];
  } catch {
    return [];
  }
}

export function saveThreadEvents(threadId: string, events: AgentEvent[]) {
  try {
    const trimmed = events.slice(-MAX_EVENTS_PER_THREAD);
    localStorage.setItem(EVENTS_PREFIX + threadId, JSON.stringify(trimmed));
  } catch {
    // ignore quota
  }
}

export function clearThreadEvents(threadId: string) {
  try {
    localStorage.removeItem(EVENTS_PREFIX + threadId);
  } catch {
    // ignore
  }
}

export function deriveTitle(events: AgentEvent[]): string {
  for (const e of events) {
    if (e.type === "text" && typeof e.text === "string") {
      const t = e.text.replace(/^🧑\s*/, "").trim();
      if (t) return t.length > 60 ? t.slice(0, 57) + "…" : t;
    }
  }
  return "Untitled chat";
}

export function newThreadId(): string {
  return `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}
