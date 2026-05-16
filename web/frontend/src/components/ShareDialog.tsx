import { useCallback, useEffect, useMemo, useState } from "react";

export type ProjectShareState = {
  acl: { user_id: string; role: "viewer" | "editor" | "owner" }[];
  shared_with_tenants: string[];
};

type ShareTarget = "email" | "tenant_slug";

export function ShareDialog({
  tenantSlug,
  project,
  canManage,
  onClose,
}: {
  tenantSlug: string;
  project: string;
  canManage: boolean;
  onClose: () => void;
}) {
  const [state, setState] = useState<ProjectShareState>({ acl: [], shared_with_tenants: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [targetKind, setTargetKind] = useState<ShareTarget>("email");
  const [targetValue, setTargetValue] = useState("");
  const [role, setRole] = useState<"viewer" | "editor" | "owner">("editor");
  const [submitting, setSubmitting] = useState(false);

  const base = useMemo(
    () => `/api/tenants/${tenantSlug}/projects/${encodeURIComponent(project)}/share`,
    [tenantSlug, project],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(base);
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      setState(await r.json());
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [base]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const add = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    try {
      const body: Record<string, string> =
        targetKind === "tenant_slug"
          ? { tenant_slug: targetValue.trim() }
          : { email: targetValue.trim(), role };
      const r = await fetch(base, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const txt = await r.text();
        throw new Error(`${r.status}: ${txt}`);
      }
      setState(await r.json());
      setTargetValue("");
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setSubmitting(false);
    }
  }, [base, targetKind, targetValue, role]);

  const remove = useCallback(
    async (principal: string) => {
      setError(null);
      try {
        const r = await fetch(`${base}/${encodeURIComponent(principal)}`, { method: "DELETE" });
        if (!r.ok) {
          const txt = await r.text();
          throw new Error(`${r.status}: ${txt}`);
        }
        setState(await r.json());
      } catch (e: any) {
        setError(String(e.message || e));
      }
    },
    [base],
  );

  const noGrants = state.acl.length === 0 && state.shared_with_tenants.length === 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-lg shadow-xl w-[520px] max-w-[95vw] max-h-[85vh] flex flex-col">
        <header className="px-4 py-3 border-b border-gray-200 flex items-center justify-between">
          <div>
            <div className="text-sm font-medium">Share project</div>
            <div className="text-[11px] text-gray-500 truncate">{project}</div>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded hover:bg-gray-100 text-gray-500"
            title="Close"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {error && (
            <div className="px-3 py-2 rounded bg-red-50 border border-red-200 text-red-700 text-[12px]">
              {error}
            </div>
          )}

          <section>
            <div className="text-[11px] uppercase tracking-wider text-gray-400 mb-2">
              Current grants
            </div>
            {loading ? (
              <div className="text-[12px] text-gray-500 italic">Loading…</div>
            ) : noGrants ? (
              <div className="text-[12px] text-gray-500 italic">
                Nobody outside this tenant has access yet.
              </div>
            ) : (
              <ul className="divide-y divide-gray-100 border border-gray-200 rounded">
                {state.acl.map((entry) => (
                  <li
                    key={`user:${entry.user_id}`}
                    className="px-3 py-2 flex items-center gap-2 text-[12px]"
                  >
                    <span className="font-mono text-gray-700 flex-1 truncate" title={entry.user_id}>
                      user · {entry.user_id}
                    </span>
                    <span className="text-gray-500">{entry.role}</span>
                    {canManage && (
                      <button
                        onClick={() => remove(`user:${entry.user_id}`)}
                        className="text-red-600 hover:bg-red-50 px-2 py-0.5 rounded text-[11px]"
                      >
                        Remove
                      </button>
                    )}
                  </li>
                ))}
                {state.shared_with_tenants.map((slug) => (
                  <li
                    key={`tenant:${slug}`}
                    className="px-3 py-2 flex items-center gap-2 text-[12px]"
                  >
                    <span className="font-mono text-gray-700 flex-1 truncate">
                      tenant · {slug}
                    </span>
                    <span className="text-gray-500">editor (capped)</span>
                    {canManage && (
                      <button
                        onClick={() => remove(`tenant:${slug}`)}
                        className="text-red-600 hover:bg-red-50 px-2 py-0.5 rounded text-[11px]"
                      >
                        Remove
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {canManage && (
            <section>
              <div className="text-[11px] uppercase tracking-wider text-gray-400 mb-2">
                Add grant
              </div>
              <div className="space-y-2">
                <div className="flex gap-2 text-[12px]">
                  <label className="flex items-center gap-1">
                    <input
                      type="radio"
                      checked={targetKind === "email"}
                      onChange={() => setTargetKind("email")}
                    />
                    User by email
                  </label>
                  <label className="flex items-center gap-1">
                    <input
                      type="radio"
                      checked={targetKind === "tenant_slug"}
                      onChange={() => setTargetKind("tenant_slug")}
                    />
                    Tenant by slug
                  </label>
                </div>
                <div className="flex gap-2">
                  <input
                    type={targetKind === "email" ? "email" : "text"}
                    placeholder={targetKind === "email" ? "user@example.com" : "acme"}
                    value={targetValue}
                    onChange={(e) => setTargetValue(e.target.value)}
                    className="flex-1 border border-gray-300 rounded px-2 py-1 text-[12px]"
                  />
                  {targetKind === "email" && (
                    <select
                      value={role}
                      onChange={(e) => setRole(e.target.value as any)}
                      className="border border-gray-300 rounded px-2 py-1 text-[12px]"
                    >
                      <option value="viewer">viewer</option>
                      <option value="editor">editor</option>
                      <option value="owner">owner</option>
                    </select>
                  )}
                  <button
                    onClick={add}
                    disabled={submitting || !targetValue.trim()}
                    className="px-3 py-1 bg-brand-green text-white rounded text-[12px] disabled:opacity-50"
                  >
                    {submitting ? "Saving…" : "Add"}
                  </button>
                </div>
                {targetKind === "tenant_slug" && (
                  <div className="text-[11px] text-gray-500">
                    Tenant grants are capped at editor — owners on the shared tenant become editors
                    on this project.
                  </div>
                )}
              </div>
            </section>
          )}

          {!canManage && (
            <div className="text-[11px] text-gray-500 italic">
              Only home-tenant owners can change who has access.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
