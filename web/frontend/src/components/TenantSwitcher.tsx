import { useEffect, useRef, useState } from "react";
import type { TenantSummary } from "../hooks/useTenants";

/**
 * Compact dropdown listing the user's tenants. Selecting one fires `onSelect`;
 * the parent decides whether to also exit the current session (because the
 * dashboard / sidebar projects are bound to the previously active tenant).
 */
export function TenantSwitcher({
  tenants,
  activeSlug,
  onSelect,
  onOpenSettings,
  isOwnerOfActive,
}: {
  tenants: TenantSummary[];
  activeSlug: string | null;
  onSelect: (slug: string) => void;
  onOpenSettings?: () => void;
  isOwnerOfActive: boolean;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const active = tenants.find((t) => t.slug === activeSlug);

  if (tenants.length === 0) {
    return (
      <div className="text-[11px] text-amber-700 italic px-3 py-1.5 bg-amber-50 border border-amber-200 rounded">
        No tenants — ask your platform admin to add you.
      </div>
    );
  }

  return (
    <div className="relative inline-block" ref={wrapRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        title="Switch tenant"
        className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[12px] transition ${
          open
            ? "border-[#E8A78A] bg-[#FFF6EE] text-gray-800"
            : "border-[#EFE6D6] bg-white text-gray-700 hover:border-[#E8A78A]"
        }`}
      >
        <BuildingIcon className="w-3.5 h-3.5 text-[#B8462A]" />
        <span className="font-medium truncate max-w-[140px]" title={active?.name ?? activeSlug ?? ""}>
          {active?.name ?? activeSlug ?? "—"}
        </span>
        <span className="text-[10px] uppercase tracking-wider text-gray-400">
          {active?.role ?? ""}
        </span>
        <ChevronIcon className="w-3 h-3 text-gray-400" />
      </button>

      {open && (
        <div className="absolute left-0 top-full mt-1 w-64 max-h-80 overflow-y-auto bg-white border border-[#EFE6D6] rounded-lg shadow-md z-40 py-1">
          <div className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wider text-gray-400">
            Workspaces
          </div>
          {tenants.map((t) => {
            const isActive = t.slug === activeSlug;
            return (
              <button
                key={t.slug}
                onClick={() => {
                  onSelect(t.slug);
                  setOpen(false);
                }}
                className={`w-full text-left px-3 py-1.5 text-[12px] flex items-center justify-between hover:bg-gray-50 ${
                  isActive ? "bg-[#FFF6EE]" : ""
                }`}
              >
                <span className="truncate" title={`${t.name} (${t.slug})`}>
                  {t.name}
                </span>
                <span className="text-[10px] uppercase tracking-wider text-gray-400">
                  {t.role}
                </span>
              </button>
            );
          })}
          {onOpenSettings && (
            <>
              <div className="border-t border-gray-100 mt-1 pt-1" />
              <button
                onClick={() => {
                  setOpen(false);
                  onOpenSettings();
                }}
                className="w-full text-left px-3 py-1.5 text-[12px] text-gray-700 hover:bg-gray-50 flex items-center gap-2"
              >
                <GearIcon className="w-3.5 h-3.5 text-gray-500" />
                Tenant settings
                {!isOwnerOfActive && (
                  <span className="ml-auto text-[10px] text-gray-400">view</span>
                )}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function BuildingIcon({ className = "" }: { className?: string }) {
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
      <path d="M3 21h18" />
      <path d="M5 21V7l7-4 7 4v14" />
      <path d="M9 9h.01M9 13h.01M9 17h.01M15 9h.01M15 13h.01M15 17h.01" />
    </svg>
  );
}

function ChevronIcon({ className = "" }: { className?: string }) {
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
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

function GearIcon({ className = "" }: { className?: string }) {
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
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33 1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82 1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}
