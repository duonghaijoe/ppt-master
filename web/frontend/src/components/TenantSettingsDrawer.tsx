import { useCallback, useEffect, useMemo, useState } from "react";

type Tab = "members" | "design" | "shared" | "trash";

type Member = { user_id: string; role: "viewer" | "editor" | "owner" };
type DesignSystem = {
  palette: Record<string, string>;
  typography: Record<string, string>;
  spec: string;
};
type SharedAsset = {
  path: string;
  size: number;
  mtime: number;
  kind: string;
};
type TrashEntry = {
  id: string;
  original_name: string;
  trashed_at: number;
};

/**
 * Owner-facing settings drawer for the active tenant. Viewers/editors see
 * read-only views; the backend is the final authority — every write path
 * 403s when the role is below `owner`.
 */
export function TenantSettingsDrawer({
  tenantSlug,
  tenantName,
  isOwner,
  open,
  onClose,
}: {
  tenantSlug: string;
  tenantName: string;
  isOwner: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("members");

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-40 bg-black/30 flex justify-end"
      onClick={onClose}
    >
      <div
        className="bg-white w-[680px] max-w-full h-full flex flex-col shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-4 border-b border-gray-100 flex items-center gap-3">
          <div className="flex-1">
            <div className="text-base font-semibold">Tenant settings</div>
            <div className="text-[12px] text-gray-500 mt-0.5">
              {tenantName}
              <span className="font-mono ml-1.5 text-gray-400">{tenantSlug}</span>
              {!isOwner && (
                <span className="ml-2 text-[10px] uppercase tracking-wider text-amber-700 bg-amber-50 px-1.5 py-0.5 rounded">
                  read-only
                </span>
              )}
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-700 text-xl px-2"
            title="Close"
          >
            ×
          </button>
        </div>

        <div className="px-6 pt-3 flex items-center gap-1 border-b border-gray-100">
          {(["members", "design", "shared", "trash"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-3 py-1.5 text-[13px] border-b-2 transition ${
                tab === t
                  ? "border-[#E8A78A] text-gray-900"
                  : "border-transparent text-gray-500 hover:text-gray-800"
              }`}
            >
              {t === "members" && "Members"}
              {t === "design" && "Design system"}
              {t === "shared" && "Shared assets"}
              {t === "trash" && "Trash"}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          {tab === "members" && (
            <MembersTab tenantSlug={tenantSlug} canEdit={isOwner} />
          )}
          {tab === "design" && (
            <DesignTab tenantSlug={tenantSlug} canEdit={isOwner} />
          )}
          {tab === "shared" && (
            <SharedTab tenantSlug={tenantSlug} canEdit={isOwner} />
          )}
          {tab === "trash" && (
            <TrashTab tenantSlug={tenantSlug} canMutate={isOwner} />
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Members ────────────────────────────────────────────────────────────────

function MembersTab({
  tenantSlug,
  canEdit,
}: {
  tenantSlug: string;
  canEdit: boolean;
}) {
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newUser, setNewUser] = useState("");
  const [newRole, setNewRole] = useState<Member["role"]>("editor");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/tenants/${tenantSlug}/members`);
      if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
      const d = await res.json();
      setMembers(d.members ?? []);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [tenantSlug]);

  useEffect(() => {
    reload();
  }, [reload]);

  async function addMember() {
    if (!newUser.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/tenants/${tenantSlug}/members`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user: newUser.trim(), role: newRole }),
      });
      if (!res.ok) throw new Error(await res.text());
      setNewUser("");
      await reload();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function updateRole(userId: string, role: Member["role"]) {
    try {
      const res = await fetch(
        `/api/tenants/${tenantSlug}/members/${encodeURIComponent(userId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role }),
        },
      );
      if (!res.ok) throw new Error(await res.text());
      await reload();
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  async function removeMember(userId: string) {
    if (!confirm(`Remove ${userId} from this tenant?`)) return;
    try {
      const res = await fetch(
        `/api/tenants/${tenantSlug}/members/${encodeURIComponent(userId)}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(await res.text());
      await reload();
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  return (
    <div className="space-y-4">
      {canEdit && (
        <div className="border border-[#EFE6D6] rounded-lg p-4 bg-[#FBF7F1]">
          <div className="text-[13px] font-medium mb-2">Add member</div>
          <div className="flex items-center gap-2">
            <input
              value={newUser}
              onChange={(e) => setNewUser(e.target.value)}
              placeholder="user id or email"
              className="flex-1 border border-gray-200 rounded px-2.5 py-1.5 text-sm bg-white"
            />
            <select
              value={newRole}
              onChange={(e) => setNewRole(e.target.value as Member["role"])}
              className="border border-gray-200 rounded px-2 py-1.5 text-sm bg-white"
            >
              <option value="viewer">viewer</option>
              <option value="editor">editor</option>
              <option value="owner">owner</option>
            </select>
            <button
              onClick={addMember}
              disabled={busy || !newUser.trim()}
              className="text-[13px] px-3 py-1.5 rounded bg-[#E8A78A] text-white disabled:opacity-60 hover:opacity-90"
            >
              Add
            </button>
          </div>
        </div>
      )}

      {error && <div className="text-[12px] text-red-600">{error}</div>}

      <div className="border border-[#EFE6D6] rounded-lg overflow-hidden">
        <table className="w-full text-[13px]">
          <thead className="bg-[#FBF7F1] text-[11px] uppercase tracking-wider text-gray-500">
            <tr>
              <th className="text-left px-3 py-2">User</th>
              <th className="text-left px-3 py-2">Role</th>
              {canEdit && <th className="px-3 py-2 w-12" />}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={canEdit ? 3 : 2} className="px-3 py-4 text-center text-gray-400">
                  Loading…
                </td>
              </tr>
            )}
            {!loading && members.length === 0 && (
              <tr>
                <td colSpan={canEdit ? 3 : 2} className="px-3 py-4 text-center text-gray-400 italic">
                  No members.
                </td>
              </tr>
            )}
            {members.map((m) => (
              <tr key={m.user_id} className="border-t border-gray-100">
                <td className="px-3 py-2 font-mono text-[12px]">{m.user_id}</td>
                <td className="px-3 py-2">
                  {canEdit ? (
                    <select
                      value={m.role}
                      onChange={(e) =>
                        updateRole(m.user_id, e.target.value as Member["role"])
                      }
                      className="border border-gray-200 rounded px-2 py-1 text-[12px] bg-white"
                    >
                      <option value="viewer">viewer</option>
                      <option value="editor">editor</option>
                      <option value="owner">owner</option>
                    </select>
                  ) : (
                    <span className="text-gray-700">{m.role}</span>
                  )}
                </td>
                {canEdit && (
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => removeMember(m.user_id)}
                      title="Remove"
                      className="text-gray-400 hover:text-red-600"
                    >
                      ×
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── Design system ──────────────────────────────────────────────────────────

function DesignTab({
  tenantSlug,
  canEdit,
}: {
  tenantSlug: string;
  canEdit: boolean;
}) {
  const [ds, setDs] = useState<DesignSystem | null>(null);
  const [paletteText, setPaletteText] = useState("");
  const [typographyText, setTypographyText] = useState("");
  const [spec, setSpec] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    fetch(`/api/tenants/${tenantSlug}/design-system`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
      .then((d: DesignSystem) => {
        setDs(d);
        setPaletteText(JSON.stringify(d.palette ?? {}, null, 2));
        setTypographyText(JSON.stringify(d.typography ?? {}, null, 2));
        setSpec(d.spec ?? "");
      })
      .catch((e) => setError(String(e)));
  }, [tenantSlug]);

  async function save() {
    setError(null);
    setSaving(true);
    try {
      let palette: Record<string, string>;
      let typography: Record<string, string>;
      try {
        palette = JSON.parse(paletteText || "{}");
      } catch {
        throw new Error("Palette JSON is invalid");
      }
      try {
        typography = JSON.parse(typographyText || "{}");
      } catch {
        throw new Error("Typography JSON is invalid");
      }
      const res = await fetch(`/api/tenants/${tenantSlug}/design-system`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ palette, typography, spec }),
      });
      if (!res.ok) throw new Error(await res.text());
      const d: DesignSystem = await res.json();
      setDs(d);
      setSavedAt(Date.now());
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  if (!ds) {
    return <div className="text-sm text-gray-400">Loading design system…</div>;
  }

  return (
    <div className="space-y-4">
      <div className="text-[12px] text-gray-500 leading-relaxed">
        The agent reads palette + typography when generating slides for this tenant.
        Spec markdown is appended to per-deck SKILL.md prompts so design rules carry over.
      </div>

      <PaletteEditor
        text={paletteText}
        onChange={setPaletteText}
        canEdit={canEdit}
      />

      <Field
        label="Typography (JSON)"
        hint='e.g. {"heading": "Inter", "body": "Inter"}'
      >
        <textarea
          value={typographyText}
          onChange={(e) => setTypographyText(e.target.value)}
          readOnly={!canEdit}
          rows={5}
          className={`w-full border border-gray-200 rounded px-2.5 py-2 text-[12px] font-mono ${
            canEdit ? "bg-white" : "bg-gray-50 text-gray-600"
          }`}
        />
      </Field>

      <Field label="Spec (Markdown)" hint="Narrative rules — header pattern, drift-allowed colors, gotchas">
        <textarea
          value={spec}
          onChange={(e) => setSpec(e.target.value)}
          readOnly={!canEdit}
          rows={10}
          className={`w-full border border-gray-200 rounded px-2.5 py-2 text-[12px] font-mono ${
            canEdit ? "bg-white" : "bg-gray-50 text-gray-600"
          }`}
        />
      </Field>

      {error && <div className="text-[12px] text-red-600">{error}</div>}
      {canEdit && (
        <div className="flex items-center gap-3 pt-2">
          <button
            onClick={save}
            disabled={saving}
            className="text-[13px] px-3 py-1.5 rounded bg-[#E8A78A] text-white disabled:opacity-60 hover:opacity-90"
          >
            {saving ? "Saving…" : "Save"}
          </button>
          {savedAt && (
            <div className="text-[11px] text-gray-500">
              Saved {new Date(savedAt).toLocaleTimeString()}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function PaletteEditor({
  text,
  onChange,
  canEdit,
}: {
  text: string;
  onChange: (v: string) => void;
  canEdit: boolean;
}) {
  // Best-effort palette preview: parse the textarea and render swatches. Doesn't
  // block save if the JSON is mid-edit and unparseable.
  const swatches = useMemo(() => {
    try {
      const obj = JSON.parse(text || "{}") as Record<string, string>;
      return Object.entries(obj).filter(
        ([, v]) => typeof v === "string" && /^#?[0-9a-fA-F]{3,8}$/.test(v.replace("#", "")),
      );
    } catch {
      return [];
    }
  }, [text]);

  return (
    <Field
      label="Palette (JSON)"
      hint='e.g. {"primary": "#E8A78A", "ink": "#1A1A1A"}'
    >
      <textarea
        value={text}
        onChange={(e) => onChange(e.target.value)}
        readOnly={!canEdit}
        rows={6}
        className={`w-full border border-gray-200 rounded px-2.5 py-2 text-[12px] font-mono ${
          canEdit ? "bg-white" : "bg-gray-50 text-gray-600"
        }`}
      />
      {swatches.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {swatches.map(([name, hex]) => (
            <div key={name} className="flex items-center gap-1.5 text-[11px]">
              <span
                className="inline-block w-4 h-4 rounded border border-gray-200"
                style={{ background: hex.startsWith("#") ? hex : `#${hex}` }}
              />
              <span className="text-gray-700">{name}</span>
              <span className="text-gray-400 font-mono">{hex}</span>
            </div>
          ))}
        </div>
      )}
    </Field>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-[12px] font-medium text-gray-700">{label}</div>
      {hint && <div className="text-[11px] text-gray-400 mb-1">{hint}</div>}
      {children}
    </div>
  );
}

// ─── Shared assets ──────────────────────────────────────────────────────────

function SharedTab({
  tenantSlug,
  canEdit,
}: {
  tenantSlug: string;
  canEdit: boolean;
}) {
  const [assets, setAssets] = useState<SharedAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploadPath, setUploadPath] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/tenants/${tenantSlug}/shared`);
      if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
      const d = await res.json();
      setAssets(d.assets ?? []);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [tenantSlug]);

  useEffect(() => {
    reload();
  }, [reload]);

  async function upload(file: File) {
    const path = (uploadPath || file.name).replace(/^\/+/, "");
    setBusy(true);
    setError(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("path", path);
      const res = await fetch(`/api/tenants/${tenantSlug}/shared`, {
        method: "POST",
        body: fd,
      });
      if (!res.ok) throw new Error(await res.text());
      setUploadPath("");
      await reload();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(path: string) {
    if (!confirm(`Delete shared asset "${path}"?`)) return;
    try {
      const res = await fetch(
        `/api/tenants/${tenantSlug}/shared?path=${encodeURIComponent(path)}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(await res.text());
      await reload();
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  return (
    <div className="space-y-4">
      {canEdit && (
        <div className="border border-[#EFE6D6] rounded-lg p-4 bg-[#FBF7F1]">
          <div className="text-[13px] font-medium mb-2">Upload asset</div>
          <div className="flex items-center gap-2">
            <input
              value={uploadPath}
              onChange={(e) => setUploadPath(e.target.value)}
              placeholder="logos/dark.png (optional — defaults to filename)"
              className="flex-1 border border-gray-200 rounded px-2.5 py-1.5 text-sm bg-white"
            />
            <label
              className={`text-[13px] px-3 py-1.5 rounded text-white cursor-pointer hover:opacity-90 ${
                busy ? "bg-gray-400" : "bg-[#E8A78A]"
              }`}
            >
              {busy ? "Uploading…" : "Choose file"}
              <input
                type="file"
                className="hidden"
                disabled={busy}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) upload(f);
                  e.target.value = "";
                }}
              />
            </label>
          </div>
          <div className="mt-2 text-[11px] text-gray-500 leading-relaxed">
            Up to 25 MB. Executable / script types (.exe .js .html .py …) are rejected.
          </div>
        </div>
      )}

      {error && <div className="text-[12px] text-red-600">{error}</div>}

      <div className="border border-[#EFE6D6] rounded-lg overflow-hidden">
        <table className="w-full text-[13px]">
          <thead className="bg-[#FBF7F1] text-[11px] uppercase tracking-wider text-gray-500">
            <tr>
              <th className="text-left px-3 py-2">Path</th>
              <th className="text-left px-3 py-2">Kind</th>
              <th className="text-right px-3 py-2">Size</th>
              {canEdit && <th className="px-3 py-2 w-12" />}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={canEdit ? 4 : 3} className="px-3 py-4 text-center text-gray-400">
                  Loading…
                </td>
              </tr>
            )}
            {!loading && assets.length === 0 && (
              <tr>
                <td colSpan={canEdit ? 4 : 3} className="px-3 py-4 text-center text-gray-400 italic">
                  No shared assets yet.
                </td>
              </tr>
            )}
            {assets.map((a) => (
              <tr key={a.path} className="border-t border-gray-100">
                <td className="px-3 py-2">
                  <a
                    href={`/api/tenants/${tenantSlug}/shared/file?path=${encodeURIComponent(a.path)}`}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="font-mono text-[12px] text-gray-700 hover:text-[#B8462A] hover:underline"
                  >
                    {a.path}
                  </a>
                </td>
                <td className="px-3 py-2 text-[12px] text-gray-500">{a.kind || "—"}</td>
                <td className="px-3 py-2 text-right text-[12px] text-gray-500">
                  {humanSize(a.size)}
                </td>
                {canEdit && (
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => remove(a.path)}
                      className="text-gray-400 hover:text-red-600"
                      title="Delete"
                    >
                      ×
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function humanSize(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ─── Trash ──────────────────────────────────────────────────────────────────

function TrashTab({
  tenantSlug,
  canMutate,
}: {
  tenantSlug: string;
  canMutate: boolean;
}) {
  // Backend gates listing at editor; restore/purge at owner. We surface a
  // forbidden state if the viewer somehow lands here — TenantSettingsDrawer
  // already shows a read-only chip for non-owners, so this is belt-and-braces.
  const [entries, setEntries] = useState<TrashEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/tenants/${tenantSlug}/trash`);
      if (res.status === 403) {
        setForbidden(true);
        setEntries([]);
        setError(null);
        return;
      }
      if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
      const d = await res.json();
      setEntries(d.trash ?? []);
      setForbidden(false);
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [tenantSlug]);

  useEffect(() => {
    reload();
  }, [reload]);

  async function restore(id: string) {
    setBusyId(id);
    setError(null);
    try {
      const res = await fetch(
        `/api/tenants/${tenantSlug}/trash/${encodeURIComponent(id)}/restore`,
        { method: "POST" },
      );
      if (!res.ok) {
        const body = await res.text();
        // Collision (409) carries a specific message — surface verbatim so
        // the owner knows to rename or purge first.
        throw new Error(body || res.statusText);
      }
      await reload();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusyId(null);
    }
  }

  async function purge(entry: TrashEntry) {
    if (
      !confirm(
        `Permanently delete "${entry.original_name}"? This cannot be undone.`,
      )
    ) {
      return;
    }
    setBusyId(entry.id);
    setError(null);
    try {
      const res = await fetch(
        `/api/tenants/${tenantSlug}/trash/${encodeURIComponent(entry.id)}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(await res.text());
      await reload();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusyId(null);
    }
  }

  if (forbidden) {
    return (
      <div className="text-[12px] text-amber-700 bg-amber-50 border border-amber-200 rounded p-3">
        Trash is visible to editors and owners. Ask a workspace owner to
        promote your role if you need access.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="text-[12px] text-gray-500 leading-relaxed">
        Deleted projects sit here until purged. Trash counts toward the
        tenant's storage quota. Restoring uses the original project name —
        rename first if a live project already owns it.
      </div>

      {error && <div className="text-[12px] text-red-600">{error}</div>}

      <div className="border border-[#EFE6D6] rounded-lg overflow-hidden">
        <table className="w-full text-[13px]">
          <thead className="bg-[#FBF7F1] text-[11px] uppercase tracking-wider text-gray-500">
            <tr>
              <th className="text-left px-3 py-2">Project</th>
              <th className="text-left px-3 py-2">Deleted</th>
              {canMutate && <th className="px-3 py-2 w-40 text-right">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td
                  colSpan={canMutate ? 3 : 2}
                  className="px-3 py-4 text-center text-gray-400"
                >
                  Loading…
                </td>
              </tr>
            )}
            {!loading && entries.length === 0 && (
              <tr>
                <td
                  colSpan={canMutate ? 3 : 2}
                  className="px-3 py-4 text-center text-gray-400 italic"
                >
                  Trash is empty.
                </td>
              </tr>
            )}
            {entries.map((e) => (
              <tr key={e.id} className="border-t border-gray-100 align-top">
                <td className="px-3 py-2">
                  <div className="font-medium text-gray-800">
                    {e.original_name}
                  </div>
                  <div className="font-mono text-[11px] text-gray-400 truncate" title={e.id}>
                    {e.id}
                  </div>
                </td>
                <td className="px-3 py-2 text-[12px] text-gray-500">
                  {relTimeFromUnix(e.trashed_at)}
                </td>
                {canMutate && (
                  <td className="px-3 py-2 text-right space-x-1.5">
                    <button
                      onClick={() => restore(e.id)}
                      disabled={busyId === e.id}
                      className="text-[12px] px-2 py-1 rounded border border-gray-200 text-gray-700 hover:bg-[#FBF7F1] disabled:opacity-60"
                    >
                      Restore
                    </button>
                    <button
                      onClick={() => purge(e)}
                      disabled={busyId === e.id}
                      className="text-[12px] px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-60"
                      title="Permanently delete"
                    >
                      Purge
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function relTimeFromUnix(ts: number) {
  if (!ts) return "—";
  const now = Date.now() / 1000;
  const delta = now - ts;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)} min ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} h ago`;
  const days = Math.floor(delta / 86400);
  if (days < 30) return `${days} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
