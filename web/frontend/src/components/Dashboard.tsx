import { useEffect, useMemo, useState } from "react";

type PermissionMode = "auto" | "confirm";
type Format = "ppt169" | "ppt43" | "a4portrait";
type Filter = "recent" | "yours";
type Tab = "projects" | "templates";

type ProjectMeta = { name: string; slides: number; exports?: number; mtime?: number };

type TemplateMeta = {
  name: string;
  description: string;
  source_project: string;
  format: string;
  created_at: number;
  has_thumbnail: boolean;
  brand_files: string[];
};

const FORMAT_LABELS: Record<Format, string> = {
  ppt169: "PPT 16:9 — Slide deck",
  ppt43: "PPT 4:3 — Slide deck",
  a4portrait: "A4 Portrait — Document",
};

export function Dashboard({
  existing,
  permissionMode,
  onPermissionChange,
  onCreate,
  onOpen,
  creating,
  error,
}: {
  existing: ProjectMeta[];
  permissionMode: PermissionMode;
  onPermissionChange: (m: PermissionMode) => void;
  onCreate: (name: string, format: Format) => void;
  onOpen: (name: string) => void;
  creating: boolean;
  error: string | null;
}) {
  const [name, setName] = useState("");
  const [format, setFormat] = useState<Format>("ppt169");
  const [filter, setFilter] = useState<Filter>("yours");
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState<Tab>("projects");

  // Templates state lives here so the count badge on the tab stays accurate
  // and we don't pay a refetch every time the user toggles tabs.
  const [templates, setTemplates] = useState<TemplateMeta[]>([]);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [templatesRefresh, setTemplatesRefresh] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setTemplatesLoading(true);
    fetch("/api/templates")
      .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
      .then((d) => {
        if (cancelled) return;
        setTemplates(d.templates ?? []);
        setTemplatesError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setTemplatesError(String(e));
      })
      .finally(() => {
        if (!cancelled) setTemplatesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [templatesRefresh]);

  const filtered = useMemo(() => {
    const list = existing.slice().sort((a, b) => (b.mtime ?? 0) - (a.mtime ?? 0));
    const trimmed = search.trim().toLowerCase();
    const matched = trimmed
      ? list.filter((p) => p.name.toLowerCase().includes(trimmed))
      : list;
    if (filter === "recent") return matched.slice(0, 6);
    return matched;
  }, [existing, search, filter]);

  const filteredTemplates = useMemo(() => {
    const trimmed = search.trim().toLowerCase();
    if (!trimmed) return templates;
    return templates.filter(
      (t) =>
        t.name.toLowerCase().includes(trimmed) ||
        t.description.toLowerCase().includes(trimmed),
    );
  }, [templates, search]);

  return (
    <div className="h-full flex bg-[#FBF7F1]">
      <aside className="w-[340px] border-r border-[#EFE6D6] bg-[#FFFDF8] flex flex-col">
        <div className="px-5 pt-5 pb-3 flex items-center gap-2.5">
          <DeckLogo />
          <div className="leading-tight min-w-0">
            <div
              className="text-[22px] tracking-tight"
              style={{ fontFamily: "Georgia, serif" }}
            >
              Awesome Deck
            </div>
            <a
              href="https://aideck.vn"
              target="_blank"
              rel="noreferrer noopener"
              className="text-[10px] uppercase tracking-[0.14em] text-[#E8A78A] hover:text-[#B8462A]"
            >
              By AIDECK.VN
            </a>
          </div>
          <span className="ml-auto text-[10px] border border-gray-300 rounded-full px-2 py-0.5 text-gray-500 self-start">
            Beta
          </span>
        </div>

        <div className="mx-5 mt-2 border border-[#EFE6D6] rounded-lg p-4 bg-white">
          <div className="text-sm font-semibold mb-3">New project</div>

          <label className="block text-[11px] font-medium text-gray-500 mb-1">
            Project name
          </label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my_deck"
            className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-brand-green"
          />

          <label className="block text-[11px] font-medium text-gray-500 mt-3 mb-1">
            Canvas format
          </label>
          <select
            value={format}
            onChange={(e) => setFormat(e.target.value as Format)}
            className="w-full border border-gray-200 rounded px-3 py-2 text-sm bg-white"
          >
            {(Object.keys(FORMAT_LABELS) as Format[]).map((f) => (
              <option key={f} value={f}>
                {FORMAT_LABELS[f]}
              </option>
            ))}
          </select>

          <label className="block text-[11px] font-medium text-gray-500 mt-3 mb-1">
            Tool permission mode
          </label>
          <div className="flex border border-gray-200 rounded overflow-hidden text-[12px]">
            <button
              onClick={() => onPermissionChange("auto")}
              className={`flex-1 px-2 py-1.5 ${
                permissionMode === "auto"
                  ? "bg-brand-green text-white"
                  : "bg-white text-gray-600"
              }`}
            >
              Auto
            </button>
            <button
              onClick={() => onPermissionChange("confirm")}
              className={`flex-1 px-2 py-1.5 ${
                permissionMode === "confirm"
                  ? "bg-brand-green text-white"
                  : "bg-white text-gray-600"
              }`}
            >
              Confirm
            </button>
          </div>

          <button
            onClick={() => name.trim() && onCreate(name.trim(), format)}
            disabled={creating || !name.trim()}
            className="w-full mt-4 py-2 rounded bg-[#E8A78A] text-white text-sm font-medium disabled:opacity-60 hover:opacity-90 flex items-center justify-center gap-1.5"
          >
            <PlusIcon className="w-3.5 h-3.5" /> {creating ? "Creating…" : "Create"}
          </button>

          {error && <div className="mt-3 text-[12px] text-red-600">{error}</div>}
        </div>

        <div className="mt-auto px-5 pb-5 text-[11px] text-gray-400 leading-relaxed">
          Anyone with access to this Awesome Deck instance can see all projects in
          this workspace by default.
        </div>
      </aside>

      <main className="flex-1 flex flex-col min-w-0">
        <div className="px-8 pt-6 pb-2 flex items-center gap-4">
          <TabButton active={tab === "projects"} onClick={() => setTab("projects")}>
            Projects
            <span className="ml-1.5 text-[11px] text-gray-400 font-normal">
              {existing.length}
            </span>
          </TabButton>
          <TabButton active={tab === "templates"} onClick={() => setTab("templates")}>
            Templates
            <span className="ml-1.5 text-[11px] text-gray-400 font-normal">
              {templates.length}
            </span>
          </TabButton>
        </div>

        <div className="px-8 py-3 flex items-center gap-3 border-b border-[#EFE6D6]">
          {tab === "projects" ? (
            <div className="inline-flex bg-[#FFE7DA] rounded-full p-0.5 text-[12px]">
              <FilterChip active={filter === "recent"} onClick={() => setFilter("recent")}>
                Recent
              </FilterChip>
              <FilterChip active={filter === "yours"} onClick={() => setFilter("yours")}>
                All
              </FilterChip>
            </div>
          ) : (
            <div className="text-[11px] text-gray-500 italic">
              Reusable design DNA — open a project and ask the agent to "save this as a template" to add one.
            </div>
          )}
          <div className="ml-auto relative w-72">
            <SearchIcon className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={tab === "projects" ? "Search projects…" : "Search templates…"}
              className="w-full pl-8 pr-3 py-1.5 text-sm border border-gray-200 rounded-full bg-white focus:outline-none focus:border-brand-green"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-8 pb-8 pt-4">
          {tab === "projects" ? (
            filtered.length === 0 ? (
              <div className="text-sm text-gray-500 italic py-12 text-center">
                {search.trim()
                  ? `No projects match "${search}".`
                  : "No projects yet — create one on the left to get started."}
              </div>
            ) : (
              <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-4">
                {filtered.map((p) => (
                  <ProjectCard key={p.name} project={p} onOpen={() => onOpen(p.name)} />
                ))}
              </div>
            )
          ) : (
            <TemplatesGrid
              templates={filteredTemplates}
              loading={templatesLoading}
              error={templatesError}
              search={search}
              defaultPermissionMode={permissionMode}
              existingProjectNames={existing.map((p) => p.name)}
              onCreated={(projectName) => {
                setTemplatesRefresh((n) => n + 1);
                onOpen(projectName);
              }}
              onDeleted={() => setTemplatesRefresh((n) => n + 1)}
            />
          )}
        </div>
      </main>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`text-base font-semibold py-1 border-b-2 transition ${
        active
          ? "text-gray-900 border-[#E8A78A]"
          : "text-gray-400 border-transparent hover:text-gray-600"
      }`}
    >
      {children}
    </button>
  );
}

function ProjectCard({
  project,
  onOpen,
}: {
  project: ProjectMeta;
  onOpen: () => void;
}) {
  return (
    <button
      onClick={onOpen}
      className="text-left bg-white border border-[#EFE6D6] rounded-lg overflow-hidden hover:border-[#E8A78A] hover:shadow-sm transition group"
    >
      <div className="bg-[#F2EBDD] aspect-[4/3] flex items-center justify-center text-[#C8B591] group-hover:text-[#A88E5C] transition">
        <FolderIcon className="w-12 h-12" />
      </div>
      <div className="p-3">
        <div className="text-sm font-medium truncate" title={project.name}>
          {project.name}
        </div>
        <div className="text-[11px] text-gray-500 mt-0.5">
          Your project · {relTime(project.mtime)}
          {project.slides > 0 && (
            <span className="text-gray-400"> · {project.slides} slides</span>
          )}
        </div>
      </div>
    </button>
  );
}

function TemplatesGrid({
  templates,
  loading,
  error,
  search,
  defaultPermissionMode,
  existingProjectNames,
  onCreated,
  onDeleted,
}: {
  templates: TemplateMeta[];
  loading: boolean;
  error: string | null;
  search: string;
  defaultPermissionMode: PermissionMode;
  existingProjectNames: string[];
  onCreated: (projectName: string) => void;
  onDeleted: () => void;
}) {
  const [picked, setPicked] = useState<TemplateMeta | null>(null);

  if (loading && templates.length === 0) {
    return <div className="text-sm text-gray-400 py-12 text-center">Loading templates…</div>;
  }
  if (error && templates.length === 0) {
    return <div className="text-sm text-red-600 py-12 text-center">{error}</div>;
  }
  if (templates.length === 0) {
    return (
      <div className="text-sm text-gray-500 italic py-12 text-center">
        {search.trim()
          ? `No templates match "${search}".`
          : "No templates saved yet — open a project and ask the agent to \"save this as a template\"."}
      </div>
    );
  }

  return (
    <>
      <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-4">
        {templates.map((t) => (
          <TemplateCard
            key={t.name}
            template={t}
            onUse={() => setPicked(t)}
            onDelete={async () => {
              if (!confirm(`Delete template "${t.name}"? Existing projects derived from it are not affected.`)) return;
              try {
                const res = await fetch(`/api/templates/${encodeURIComponent(t.name)}`, {
                  method: "DELETE",
                });
                if (!res.ok) throw new Error(await res.text());
                onDeleted();
              } catch (e: any) {
                alert(`Failed to delete: ${e.message || e}`);
              }
            }}
          />
        ))}
      </div>
      {picked && (
        <CreateFromTemplateDialog
          template={picked}
          existingProjectNames={existingProjectNames}
          permissionMode={defaultPermissionMode}
          onClose={() => setPicked(null)}
          onCreated={(projectName) => {
            setPicked(null);
            onCreated(projectName);
          }}
        />
      )}
    </>
  );
}

function TemplateCard({
  template,
  onUse,
  onDelete,
}: {
  template: TemplateMeta;
  onUse: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="bg-white border border-[#EFE6D6] rounded-lg overflow-hidden hover:border-[#E8A78A] hover:shadow-sm transition group flex flex-col">
      <div className="bg-[#F2EBDD] aspect-[16/9] flex items-center justify-center text-[#C8B591] overflow-hidden">
        {template.has_thumbnail ? (
          <object
            type="image/svg+xml"
            data={`/api/templates/${encodeURIComponent(template.name)}/file?path=thumbnail.svg`}
            className="w-full h-full pointer-events-none"
            aria-label={`${template.name} preview`}
          />
        ) : (
          <FolderIcon className="w-12 h-12" />
        )}
      </div>
      <div className="p-3 flex-1 flex flex-col gap-1">
        <div className="text-sm font-medium truncate" title={template.name}>
          {template.name}
        </div>
        {template.description && (
          <div className="text-[11px] text-gray-600 line-clamp-2" title={template.description}>
            {template.description}
          </div>
        )}
        <div className="text-[10px] text-gray-400 mt-auto pt-1 truncate" title={template.source_project}>
          From {template.source_project || "—"} · {template.format || "ppt169"}
          {template.brand_files.length > 0 && (
            <> · {template.brand_files.length} brand asset{template.brand_files.length === 1 ? "" : "s"}</>
          )}
        </div>
        <div className="flex items-center gap-1.5 mt-2">
          <button
            onClick={onUse}
            className="flex-1 text-[12px] py-1.5 rounded bg-[#E8A78A] text-white hover:opacity-90"
          >
            Use template
          </button>
          <button
            onClick={onDelete}
            className="text-[12px] py-1.5 px-2 rounded border border-gray-200 text-gray-500 hover:bg-red-50 hover:text-red-600 hover:border-red-200"
            title="Delete template"
          >
            ×
          </button>
        </div>
      </div>
    </div>
  );
}

function CreateFromTemplateDialog({
  template,
  existingProjectNames,
  onClose,
  onCreated,
}: {
  template: TemplateMeta;
  existingProjectNames: string[];
  permissionMode: PermissionMode;
  onClose: () => void;
  onCreated: (projectName: string) => void;
}) {
  // Pre-fill with a sensible default the user is likely to keep: the template
  // name slug + today's date keeps things unique without forcing the user to
  // think about naming up front.
  const today = useMemo(() => {
    const d = new Date();
    return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  }, []);
  const [name, setName] = useState(`${template.name}_${today}`);
  const [fmt, setFmt] = useState<Format>(
    (template.format as Format) in FORMAT_LABELS ? (template.format as Format) : "ppt169",
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const taken = existingProjectNames.includes(name.trim());
  const valid = !!name.trim() && !taken;

  async function submit() {
    if (!valid) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch("/api/projects/from-template", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          template: template.name,
          project_name: name.trim(),
          format: fmt,
        }),
      });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(body || res.statusText);
      }
      const data = await res.json();
      onCreated(data.project);
    } catch (e: any) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/30 flex items-center justify-center px-4"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-lg shadow-xl w-[460px] max-w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 pt-5 pb-3 border-b border-gray-100">
          <div className="text-base font-semibold">Create project from template</div>
          <div className="text-[12px] text-gray-500 mt-0.5 truncate" title={template.name}>
            Template: <span className="font-mono">{template.name}</span>
          </div>
        </div>
        <div className="px-5 py-4 space-y-3">
          <div>
            <label className="block text-[11px] font-medium text-gray-500 mb-1">
              New project name
            </label>
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-brand-green"
            />
            {taken && (
              <div className="text-[11px] text-red-600 mt-1">
                A project with this name already exists.
              </div>
            )}
          </div>
          <div>
            <label className="block text-[11px] font-medium text-gray-500 mb-1">
              Canvas format
            </label>
            <select
              value={fmt}
              onChange={(e) => setFmt(e.target.value as Format)}
              className="w-full border border-gray-200 rounded px-3 py-2 text-sm bg-white"
            >
              {(Object.keys(FORMAT_LABELS) as Format[]).map((f) => (
                <option key={f} value={f}>
                  {FORMAT_LABELS[f]}
                </option>
              ))}
            </select>
            {template.format && template.format !== fmt && (
              <div className="text-[11px] text-amber-700 mt-1">
                Heads up: this template was captured as <span className="font-mono">{template.format}</span>. Overriding the format may break alignments.
              </div>
            )}
          </div>
          {err && <div className="text-[12px] text-red-600">{err}</div>}
        </div>
        <div className="px-5 pb-5 pt-1 flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            className="text-sm px-3 py-1.5 rounded border border-gray-200 text-gray-700 hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={!valid || busy}
            className="text-sm px-3 py-1.5 rounded bg-[#E8A78A] text-white disabled:opacity-60 hover:opacity-90"
          >
            {busy ? "Creating…" : "Create & open"}
          </button>
        </div>
      </div>
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1 rounded-full ${
        active ? "bg-white text-gray-900 shadow-sm" : "text-gray-600 hover:text-gray-800"
      }`}
    >
      {children}
    </button>
  );
}

function relTime(mtime?: number) {
  if (!mtime) return "recently";
  const now = Date.now() / 1000;
  const delta = now - mtime;
  if (delta < 60 * 60 * 12) return "Today";
  if (delta < 60 * 60 * 36) return "Yesterday";
  const days = Math.floor(delta / (60 * 60 * 24));
  if (days < 7) return `${days} days ago`;
  if (days < 14) return "Last week";
  if (days < 60) return `${Math.floor(days / 7)} weeks ago`;
  return `${Math.floor(days / 30)} months ago`;
}

function DeckLogo() {
  return (
    <img
      src="/logo.png"
      alt="Awesome Deck"
      className="w-14 h-14 shrink-0 select-none"
      draggable={false}
    />
  );
}

export function DeckIcon({ className }: { className?: string }) {
  return (
    <img
      src="/logo.png"
      alt="Awesome Deck"
      className={className}
      draggable={false}
    />
  );
}

export function PlusIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className}>
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

export function ClockIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className}>
      <circle cx="12" cy="12" r="8.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M12 7.5v4.7l3 1.8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function SearchIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className}>
      <circle cx="11" cy="11" r="6" stroke="currentColor" strokeWidth="1.6" />
      <path d="M20 20l-3.5-3.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

function FolderIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className}>
      <path
        d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"
        fill="currentColor"
        opacity="0.6"
      />
      <path
        d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"
        stroke="currentColor"
        strokeOpacity="0.4"
      />
    </svg>
  );
}
