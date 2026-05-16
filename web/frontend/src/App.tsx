import { useEffect, useRef, useState } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { PreviewPanel } from "./components/PreviewPanel";
import { Dashboard } from "./components/Dashboard";
import { ProjectHeader, type ThreadEntry } from "./components/ProjectHeader";
import { BillingDrawer } from "./components/BillingDrawer";
import { TenantSettingsDrawer } from "./components/TenantSettingsDrawer";
import { SessionProvider } from "./SessionContext";
import { useAgentStream } from "./hooks/useAgentStream";
import { useTenants } from "./hooks/useTenants";
import {
  deriveTitle,
  loadThreadEvents,
  loadThreads,
  newThreadId,
  saveThreadEvents,
  upsertThread,
  type ThreadMeta,
} from "./threads";

type PermissionMode = "auto" | "confirm";
type ModelTier = "auto" | "light" | "general" | "premium";
type Format = "ppt169" | "ppt43" | "a4portrait";

type SessionInfo = {
  session_id: string;
  project: string;
  tenant_slug: string;
  permission_mode: PermissionMode;
  model_tier: ModelTier;
};

type ProjectMeta = { name: string; slides: number; exports?: number; mtime?: number };

export function App() {
  const {
    me,
    tenants,
    activeSlug,
    activeTenant,
    isOwnerOfActive,
    loading: tenantsLoading,
    error: tenantsError,
    setActiveSlug,
    refresh: refreshTenants,
  } = useTenants();

  const [session, setSession] = useState<SessionInfo | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [existing, setExisting] = useState<ProjectMeta[]>([]);
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("auto");
  const [modelTier, setModelTier] = useState<ModelTier>("auto");
  const [tenantSettingsOpen, setTenantSettingsOpen] = useState(false);

  // Thread state — only meaningful while a session is active.
  const [threadId, setThreadId] = useState<string | null>(null);
  const [threads, setThreads] = useState<ThreadEntry[]>([]);
  const [hydrated, setHydrated] = useState<{ events: ReturnType<typeof loadThreadEvents>; nonce: number } | null>(null);

  // Refresh project list whenever the active tenant changes AND whenever a
  // session opens (so the in-chat "reference another project" picker stays
  // fresh). Skipped while we're still resolving the active tenant.
  useEffect(() => {
    if (!activeSlug) {
      setExisting([]);
      return;
    }
    fetch(`/api/tenants/${activeSlug}/projects`)
      .then((r) => r.json())
      .then((d) => setExisting(d.projects ?? []))
      .catch(() => setExisting([]));
  }, [activeSlug, session?.project]);

  async function createProject(name: string, format: Format) {
    if (!activeSlug) {
      setError("No active tenant — pick one first.");
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const res = await fetch(`/api/tenants/${activeSlug}/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          format,
          permission_mode: permissionMode,
          model_tier: modelTier,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      enterSession({
        session_id: data.session_id,
        project: data.project,
        tenant_slug: data.tenant_slug ?? activeSlug,
        permission_mode: data.permission_mode ?? permissionMode,
        model_tier: data.model_tier ?? modelTier,
      });
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setCreating(false);
    }
  }

  async function attach(name: string) {
    if (!activeSlug) {
      setError("No active tenant — pick one first.");
      return;
    }
    setError(null);
    try {
      const res = await fetch(`/api/tenants/${activeSlug}/sessions/attach`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          permission_mode: permissionMode,
          model_tier: modelTier,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      enterSession({
        session_id: data.session_id,
        project: data.project,
        tenant_slug: data.tenant_slug ?? activeSlug,
        permission_mode: data.permission_mode ?? permissionMode,
        model_tier: data.model_tier ?? modelTier,
      });
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  function enterSession(info: SessionInfo) {
    setSession(info);
    const list = loadThreads(info.project);
    if (list.length > 0) {
      const latest = list[0];
      setThreadId(latest.id);
      setThreads(toEntries(list, latest.id));
      setHydrated({ events: loadThreadEvents(latest.id), nonce: Date.now() });
    } else {
      const id = newThreadId();
      const meta: ThreadMeta = {
        id,
        project: info.project,
        started: Date.now(),
        lastUsed: Date.now(),
        title: "Untitled chat",
      };
      upsertThread(meta);
      setThreadId(id);
      setThreads(toEntries(loadThreads(info.project), id));
      setHydrated({ events: [], nonce: Date.now() });
    }
  }

  async function changeMode(mode: PermissionMode) {
    setPermissionMode(mode);
    if (!session) return;
    setSession({ ...session, permission_mode: mode });
    try {
      await fetch(`/api/sessions/${session.session_id}/permission_mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
    } catch {
      // best-effort
    }
  }

  async function changeTier(tier: ModelTier) {
    setModelTier(tier);
    if (!session) return;
    setSession({ ...session, model_tier: tier });
    try {
      await fetch(`/api/sessions/${session.session_id}/model_tier`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tier }),
      });
    } catch {
      // best-effort — backend will pick up the new tier on next reconnect
    }
  }

  async function newChat() {
    if (!session) return;
    const projectName = session.project;
    try {
      const res = await fetch(`/api/tenants/${session.tenant_slug}/sessions/attach`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: projectName,
          permission_mode: permissionMode,
          model_tier: modelTier,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const id = newThreadId();
      upsertThread({
        id,
        project: projectName,
        started: Date.now(),
        lastUsed: Date.now(),
        title: "Untitled chat",
      });
      setThreadId(id);
      setThreads(toEntries(loadThreads(projectName), id));
      setHydrated({ events: [], nonce: Date.now() });
      setSession({
        session_id: data.session_id,
        project: data.project,
        tenant_slug: data.tenant_slug ?? "default",
        permission_mode: data.permission_mode ?? permissionMode,
        model_tier: data.model_tier ?? modelTier,
      });
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  async function selectThread(id: string) {
    if (!session) return;
    if (id === threadId) return;
    try {
      const res = await fetch(`/api/tenants/${session.tenant_slug}/sessions/attach`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: session.project,
          permission_mode: permissionMode,
          model_tier: modelTier,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setThreadId(id);
      setThreads(toEntries(loadThreads(session.project), id));
      setHydrated({ events: loadThreadEvents(id), nonce: Date.now() });
      setSession({
        session_id: data.session_id,
        project: data.project,
        tenant_slug: data.tenant_slug ?? "default",
        permission_mode: data.permission_mode ?? permissionMode,
        model_tier: data.model_tier ?? modelTier,
      });
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  function exitSession() {
    setSession(null);
    setThreadId(null);
    setThreads([]);
    setHydrated(null);
  }

  function switchTenant(slug: string) {
    if (slug === activeSlug) return;
    // Switching tenants invalidates the in-flight session: the dashboard and
    // sidebar projects are bound to the previously active tenant, so we drop
    // the session and let the user start fresh inside the new workspace.
    exitSession();
    setActiveSlug(slug);
  }

  if (!session) {
    return (
      <>
        <Dashboard
          activeSlug={activeSlug}
          tenants={tenants}
          tenantsLoading={tenantsLoading}
          tenantsError={tenantsError}
          isOwnerOfActiveTenant={isOwnerOfActive}
          onSwitchTenant={switchTenant}
          onOpenTenantSettings={() => setTenantSettingsOpen(true)}
          existing={existing}
          permissionMode={permissionMode}
          onPermissionChange={setPermissionMode}
          onCreate={createProject}
          onOpen={attach}
          creating={creating}
          error={error}
        />
        {activeSlug && activeTenant && (
          <TenantSettingsDrawer
            tenantSlug={activeSlug}
            tenantName={activeTenant.name}
            isOwner={isOwnerOfActive}
            open={tenantSettingsOpen}
            onClose={() => {
              setTenantSettingsOpen(false);
              refreshTenants();
            }}
          />
        )}
      </>
    );
  }

  const sessionTenant = session.tenant_slug ?? activeSlug ?? null;
  const canManageShares =
    !!me?.platform_admin ||
    !!(sessionTenant && me?.memberships.some(
      (m) => m.tenant_slug === sessionTenant && m.role === "owner",
    ));

  return (
    <SessionShell
      key={hydrated?.nonce ?? "init"}
      session={session}
      seedEvents={hydrated?.events ?? []}
      threads={threads}
      threadId={threadId}
      projects={existing}
      tenantSlug={sessionTenant}
      canManageShares={canManageShares}
      onPermissionChange={changeMode}
      onTierChange={changeTier}
      onSwitchProject={exitSession}
      onNewChat={newChat}
      onSelectThread={selectThread}
      onTitleChange={(title, lastUsed) => {
        if (!threadId) return;
        const meta: ThreadMeta = {
          id: threadId,
          project: session.project,
          started: threads.find((t) => t.id === threadId)?.started ?? Date.now(),
          lastUsed,
          title,
        };
        upsertThread(meta);
        setThreads(toEntries(loadThreads(session.project), threadId));
      }}
    />
  );
}

function toEntries(list: ThreadMeta[], activeId: string | null): ThreadEntry[] {
  return list.map((t) => ({ ...t, active: t.id === activeId }));
}

function SessionShell({
  session,
  seedEvents,
  threads,
  threadId,
  projects,
  tenantSlug,
  canManageShares,
  onPermissionChange,
  onTierChange,
  onSwitchProject,
  onNewChat,
  onSelectThread,
  onTitleChange,
}: {
  session: SessionInfo;
  seedEvents: ReturnType<typeof loadThreadEvents>;
  threads: ThreadEntry[];
  threadId: string | null;
  projects: ProjectMeta[];
  tenantSlug: string | null;
  canManageShares: boolean;
  onPermissionChange: (m: PermissionMode) => void;
  onTierChange: (t: ModelTier) => void;
  onSwitchProject: () => void;
  onNewChat: () => void;
  onSelectThread: (id: string) => void;
  onTitleChange: (title: string, lastUsed: number) => void;
}) {
  const stream = useAgentStream(session.session_id);
  const seededRef = useRef(false);
  const [billingOpen, setBillingOpen] = useState(false);
  // SVG-editor → chat handoff. SlideEditor builds a context-rich draft when
  // the user hits "Ask AI" on a selected element; ChatPanel watches the nonce
  // and refills its textarea so the user can review and send.
  const [composeReq, setComposeReq] = useState<{ text: string; nonce: number; autoSend?: boolean } | null>(null);

  // Hydrate the agent stream's event list with whatever we restored from the
  // selected thread's localStorage snapshot — runs once per remount.
  useEffect(() => {
    if (seededRef.current) return;
    seededRef.current = true;
    if (seedEvents.length > 0) stream.replaceEvents(seedEvents);
  }, [seedEvents, stream]);

  // Persist the live event log to the active thread on every change, and
  // refresh the thread title from the first user message.
  const lastTitleRef = useRef<string>("");
  useEffect(() => {
    if (!threadId) return;
    saveThreadEvents(threadId, stream.events);
    const title = deriveTitle(stream.events);
    if (title && title !== lastTitleRef.current) {
      lastTitleRef.current = title;
      onTitleChange(title, Date.now());
    }
  }, [stream.events, threadId, onTitleChange]);

  return (
    <SessionProvider value={stream}>
      <div className="h-full grid grid-cols-[400px_1fr] overflow-hidden">
        <aside className="bg-white border-r border-gray-200 flex flex-col min-h-0 min-w-0">
          <ProjectHeader
            project={session.project}
            tenantSlug={tenantSlug}
            canManageShares={canManageShares}
            threads={threads}
            activeThreadId={threadId}
            permissionMode={session.permission_mode}
            onSwitchProject={onSwitchProject}
            onNewChat={onNewChat}
            onSelectThread={onSelectThread}
            onPermissionChange={onPermissionChange}
            onOpenBilling={() => setBillingOpen(true)}
          />
          <ChatPanel
            sessionId={session.session_id}
            permissionMode={session.permission_mode}
            modelTier={session.model_tier}
            currentProject={session.project}
            projects={projects}
            onChangeMode={onPermissionChange}
            onChangeTier={onTierChange}
            composeRequest={composeReq}
            onComposeConsumed={() => setComposeReq(null)}
          />
        </aside>
        <main className="overflow-hidden min-h-0 min-w-0">
          <PreviewPanel
            project={session.project}
            onAskAi={(text) => setComposeReq({ text, nonce: Date.now() })}
            onSendAi={(text) => setComposeReq({ text, nonce: Date.now(), autoSend: true })}
          />
        </main>
      </div>
      <BillingDrawer
        tenantSlug={session.tenant_slug}
        open={billingOpen}
        onClose={() => setBillingOpen(false)}
      />
    </SessionProvider>
  );
}
