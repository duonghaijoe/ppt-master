import { useEffect, useMemo, useState } from "react";
import { FilePreview, ext } from "./FilePreview";
import { projectApi } from "../api/projectUrls";

type Entry = { name: string; is_dir: boolean; size: number | null; mtime: number };
type Tree = { type: "dir"; path: string; entries: Entry[] };

function fmtBytes(n: number | null): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function fmtTime(t: number): string {
  return new Date(t * 1000).toLocaleString();
}

function joinPath(dir: string, name: string): string {
  return dir ? `${dir}/${name}` : name;
}

function parentPath(p: string): string {
  const i = p.lastIndexOf("/");
  return i < 0 ? "" : p.slice(0, i);
}

export function ProjectFiles({
  tenant,
  project,
  onAskAi,
}: {
  tenant: string;
  project: string;
  onAskAi?: (text: string) => void;
}) {
  const apiBase = projectApi(tenant, project);
  const [path, setPath] = useState("");
  const [tree, setTree] = useState<Tree | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [bust, setBust] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    fetch(`${apiBase}/tree?path=${encodeURIComponent(path)}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await r.text());
        return r.json();
      })
      .then((d: Tree) => setTree(d))
      .catch((e) => setError(String(e.message || e)));
  }, [apiBase, path, bust]);

  // Watch fs to refresh listing.
  useEffect(() => {
    const es = new EventSource(`${apiBase}/events`);
    let pending: number | null = null;
    es.onmessage = () => {
      if (pending) return;
      pending = window.setTimeout(() => {
        pending = null;
        setBust((b) => b + 1);
      }, 300);
    };
    return () => {
      es.close();
      if (pending) window.clearTimeout(pending);
    };
  }, [apiBase]);

  const crumbs = useMemo(() => {
    const parts = path ? path.split("/") : [];
    return [{ label: project, p: "" }, ...parts.map((_, i) => ({
      label: parts[i],
      p: parts.slice(0, i + 1).join("/"),
    }))];
  }, [project, path]);

  function openEntry(e: Entry) {
    if (e.is_dir) {
      setPath(joinPath(path, e.name));
      setSelected(null);
    } else {
      setSelected(joinPath(path, e.name));
    }
  }

  return (
    <div className="h-full flex">
      {/* Left: tree list */}
      <div className="w-[420px] border-r border-gray-200 flex flex-col min-h-0">
        <div className="border-b border-gray-200 px-3 py-2 flex items-center gap-2 bg-gray-50">
          <button
            onClick={() => setPath(parentPath(path))}
            disabled={!path}
            className="text-xs px-2 py-1 border border-gray-300 rounded disabled:opacity-30"
            title="Up"
          >
            ↑
          </button>
          <button
            onClick={() => setBust((b) => b + 1)}
            className="text-xs px-2 py-1 border border-gray-300 rounded"
            title="Refresh"
          >
            ↻
          </button>
          <div className="text-xs text-gray-600 truncate flex-1">
            {crumbs.map((c, i) => (
              <span key={i}>
                <button
                  onClick={() => { setPath(c.p); setSelected(null); }}
                  className="hover:underline"
                >
                  {c.label}
                </button>
                {i < crumbs.length - 1 && <span className="text-gray-300 mx-1">/</span>}
              </span>
            ))}
          </div>
        </div>

        <div className="text-[10px] uppercase tracking-wider text-gray-400 px-3 py-1.5 bg-white border-b border-gray-100">
          Folders & files
        </div>

        <div className="flex-1 overflow-y-auto">
          {error && <div className="text-xs text-red-600 px-3 py-2">{error}</div>}
          {tree?.entries.length === 0 && (
            <div className="text-xs text-gray-400 px-3 py-6 text-center">empty</div>
          )}
          <ul>
            {tree?.entries.map((e) => {
              const full = joinPath(path, e.name);
              const isSel = selected === full;
              return (
                <li
                  key={e.name}
                  onClick={() => openEntry(e)}
                  className={`px-3 py-1.5 text-sm cursor-pointer border-b border-gray-50 hover:bg-brand-cream/40 ${
                    isSel ? "bg-brand-cream" : ""
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <span className="w-4 text-center">{e.is_dir ? "📁" : "📄"}</span>
                    <span className="flex-1 truncate">{e.name}</span>
                    <span className="text-[11px] text-gray-400">{fmtBytes(e.size)}</span>
                    {!e.is_dir && onAskAi && (
                      <button
                        type="button"
                        onClick={(ev) => {
                          ev.stopPropagation();
                          onAskAi(`@${full}`);
                        }}
                        className="text-[11px] px-1.5 py-0.5 rounded border border-gray-200 text-gray-500 hover:text-brand-green hover:border-brand-green"
                        title="Reference this file in chat"
                      >
                        @chat
                      </button>
                    )}
                  </div>
                  <div className="text-[10px] text-gray-400 ml-6 truncate">
                    {e.is_dir ? "Folder" : ext(e.name).toUpperCase() || "FILE"} · {fmtTime(e.mtime)}
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      </div>

      {/* Right: preview */}
      <div className="flex-1 min-h-0 overflow-auto bg-gray-50">
        {selected ? (
          <FilePreview tenant={tenant} project={project} path={selected} version={bust} onAskAi={onAskAi} />
        ) : (
          <div className="h-full flex items-center justify-center text-sm text-gray-400 p-6 text-center">
            Select a file to preview
          </div>
        )}
      </div>
    </div>
  );
}

