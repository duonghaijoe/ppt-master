import { useMemo, useState } from "react";

type PermissionMode = "auto" | "confirm";
type Format = "ppt169" | "ppt43" | "a4portrait";
type Filter = "recent" | "yours";

type ProjectMeta = { name: string; slides: number; exports?: number; mtime?: number };

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

  const filtered = useMemo(() => {
    const list = existing.slice().sort((a, b) => (b.mtime ?? 0) - (a.mtime ?? 0));
    const trimmed = search.trim().toLowerCase();
    const matched = trimmed
      ? list.filter((p) => p.name.toLowerCase().includes(trimmed))
      : list;
    if (filter === "recent") return matched.slice(0, 6);
    return matched;
  }, [existing, search, filter]);

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
        <div className="px-8 pt-6 pb-2 flex items-baseline gap-3">
          <h1 className="text-xl font-semibold text-gray-900">Projects</h1>
          <span className="text-[12px] text-gray-400">
            {existing.length} total
          </span>
        </div>

        <div className="px-8 py-3 flex items-center gap-3 border-b border-[#EFE6D6]">
          <div className="inline-flex bg-[#FFE7DA] rounded-full p-0.5 text-[12px]">
            <FilterChip active={filter === "recent"} onClick={() => setFilter("recent")}>
              Recent
            </FilterChip>
            <FilterChip active={filter === "yours"} onClick={() => setFilter("yours")}>
              All
            </FilterChip>
          </div>
          <div className="ml-auto relative w-72">
            <SearchIcon className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search…"
              className="w-full pl-8 pr-3 py-1.5 text-sm border border-gray-200 rounded-full bg-white focus:outline-none focus:border-brand-green"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-8 pb-8 pt-4">
          {filtered.length === 0 ? (
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
          )}
        </div>
      </main>
    </div>
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
  // Brand mark — used wherever the app needs an inline logo.
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
