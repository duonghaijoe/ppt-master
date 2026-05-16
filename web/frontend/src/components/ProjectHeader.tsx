import { useEffect, useRef, useState } from "react";
import { ClockIcon, DeckIcon, PlusIcon } from "./Dashboard";
import { ShareDialog } from "./ShareDialog";

export type ThreadEntry = {
  id: string;
  project: string;
  started: number;
  lastUsed: number;
  title: string;
  active?: boolean;
};

type PermissionMode = "auto" | "confirm";

export function ProjectHeader({
  project,
  tenantSlug,
  canManageShares,
  threads,
  activeThreadId,
  onSwitchProject,
  onNewChat,
  onSelectThread,
  onOpenBilling,
}: {
  project: string;
  tenantSlug: string | null;
  canManageShares: boolean;
  threads: ThreadEntry[];
  activeThreadId: string | null;
  permissionMode?: PermissionMode;
  onSwitchProject: () => void;
  onNewChat: () => void;
  onSelectThread: (id: string) => void;
  onPermissionChange?: (m: PermissionMode) => void;
  onOpenBilling?: () => void;
}) {
  const [shareOpen, setShareOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!historyOpen) return;
    function onDocClick(e: MouseEvent) {
      if (!wrapRef.current?.contains(e.target as Node)) setHistoryOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [historyOpen]);

  return (
    <header className="px-3 py-2 border-b border-gray-200 flex items-center gap-2">
      <button
        onClick={onSwitchProject}
        title="Switch project · Awesome Deck"
        className="w-11 h-11 flex items-center justify-center shrink-0 rounded-md hover:bg-gray-100 transition"
      >
        <DeckIcon className="w-11 h-11" />
      </button>

      <div className="font-medium text-sm truncate flex-1 min-w-0" title={project}>
        {project}
      </div>

      <div className="relative shrink-0" ref={wrapRef}>
        <button
          onClick={() => setHistoryOpen((v) => !v)}
          title="Chat history for this project"
          className={`w-8 h-8 rounded-full flex items-center justify-center text-gray-500 hover:bg-gray-100 ${
            historyOpen ? "bg-gray-100" : ""
          }`}
        >
          <ClockIcon className="w-4 h-4" />
        </button>
        {historyOpen && (
          <div className="absolute right-0 top-full mt-1 w-72 max-h-80 overflow-y-auto bg-white border border-gray-200 rounded-lg shadow-md z-30 py-1">
            <div className="px-3 pt-2 pb-1 text-[11px] uppercase tracking-wider text-gray-400">
              History · {project}
            </div>
            {threads.length === 0 && (
              <div className="px-3 py-3 text-[12px] text-gray-500 italic">
                No prior chats on this project yet.
              </div>
            )}
            {threads.map((t) => {
              const isActive = t.id === activeThreadId;
              return (
                <button
                  key={t.id}
                  onClick={() => {
                    onSelectThread(t.id);
                    setHistoryOpen(false);
                  }}
                  className={`w-full text-left px-3 py-2 text-[12px] hover:bg-gray-50 flex flex-col gap-0.5 ${
                    isActive ? "bg-brand-green/5" : ""
                  }`}
                >
                  <div className="font-medium truncate">{t.title || "Untitled chat"}</div>
                  <div className="text-[10px] text-gray-500 flex justify-between">
                    <span>{relTime(t.lastUsed)}</span>
                    {isActive && <span className="text-brand-green">current</span>}
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {tenantSlug && (
        <button
          onClick={() => setShareOpen(true)}
          title={canManageShares ? "Share this project" : "View who has access"}
          className="w-8 h-8 rounded-full flex items-center justify-center text-gray-500 hover:bg-gray-100 shrink-0"
        >
          <ShareIcon className="w-4 h-4" />
        </button>
      )}

      {onOpenBilling && (
        <button
          onClick={onOpenBilling}
          title="Billing · caps · transactions"
          className="w-8 h-8 rounded-full flex items-center justify-center text-gray-500 hover:bg-gray-100 shrink-0"
        >
          <DollarIcon className="w-4 h-4" />
        </button>
      )}

      <button
        onClick={onNewChat}
        title="New chat on this project"
        className="w-8 h-8 rounded-full flex items-center justify-center text-gray-500 hover:bg-gray-100 shrink-0"
      >
        <PlusIcon className="w-4 h-4" />
      </button>

      {shareOpen && tenantSlug && (
        <ShareDialog
          tenantSlug={tenantSlug}
          project={project}
          canManage={canManageShares}
          onClose={() => setShareOpen(false)}
        />
      )}
    </header>
  );
}

function ShareIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      <circle cx="18" cy="5" r="3" />
      <circle cx="6" cy="12" r="3" />
      <circle cx="18" cy="19" r="3" />
      <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
      <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
    </svg>
  );
}

function DollarIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      <line x1="12" y1="2" x2="12" y2="22" />
      <path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
    </svg>
  );
}

function relTime(ts: number) {
  const delta = (Date.now() - ts) / 1000;
  if (delta < 60) return "just now";
  if (delta < 60 * 60) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 60 * 60 * 24) return `${Math.floor(delta / 3600)}h ago`;
  if (delta < 60 * 60 * 24 * 7) return `${Math.floor(delta / 86400)}d ago`;
  return new Date(ts).toLocaleDateString();
}
