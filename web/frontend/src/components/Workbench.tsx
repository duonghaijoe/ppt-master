import { useEffect, useMemo, useRef, useState } from "react";
import {
  File as FileIcon,
  FileCode,
  FileImage,
  FileText,
  Image as ImageIcon,
  Layers,
  Presentation,
} from "lucide-react";
import { FilePreview, ext } from "./FilePreview";
import { SlideDeck } from "./SlideDeck";

type RecentFile = { path: string; name: string; size: number; mtime: number };
type DeckSummary = { count: number; mtime: number };

type Artifact =
  | { kind: "deck"; mtime: number; count: number }
  | { kind: "file"; path: string; name: string; mtime: number };

const DECK_KEY = "__deck__";

function artifactKey(a: Artifact): string {
  return a.kind === "deck" ? DECK_KEY : `f:${a.path}`;
}

function fmtTime(t: number): string {
  return new Date(t * 1000).toLocaleString();
}

function fmtAge(t: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000) - t);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function Workbench({
  project,
  onAskAi,
}: {
  project: string;
  onAskAi?: (text: string) => void;
}) {
  const [deck, setDeck] = useState<DeckSummary | null>(null);
  const [recent, setRecent] = useState<RecentFile[]>([]);
  const [bust, setBust] = useState(0);
  const [pickedKey, setPickedKey] = useState<string | null>(null);
  const [pickedSticky, setPickedSticky] = useState(false);
  const [showPicker, setShowPicker] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pickerRef = useRef<HTMLDivElement>(null);

  // Fetch /recent on project change + on each fs event (debounced via bust).
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/projects/${encodeURIComponent(project)}/recent?limit=20`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
      .then((d) => {
        if (cancelled) return;
        setError(null);
        setDeck(d.deck ?? null);
        setRecent(d.recent ?? []);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [project, bust]);

  // SSE refresh — debounce so a flurry of writes during generation doesn't
  // hammer the recent endpoint.
  useEffect(() => {
    const es = new EventSource(`/api/projects/${encodeURIComponent(project)}/events`);
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
  }, [project]);

  // Reset selection when switching projects.
  useEffect(() => {
    setPickedKey(null);
    setPickedSticky(false);
  }, [project]);

  // Click-outside to close the picker.
  useEffect(() => {
    if (!showPicker) return;
    function onDown(e: MouseEvent) {
      if (!pickerRef.current?.contains(e.target as Node)) setShowPicker(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [showPicker]);

  // Build the merged artifact list, sorted by mtime desc. Deck (if it exists)
  // is one virtual entry — individual svg_output files are excluded from the
  // backend response so they don't crowd out other artifacts.
  const artifacts = useMemo<Artifact[]>(() => {
    const items: Artifact[] = [];
    if (deck) items.push({ kind: "deck", mtime: deck.mtime, count: deck.count });
    for (const f of recent) {
      items.push({ kind: "file", path: f.path, name: f.name, mtime: f.mtime });
    }
    items.sort((a, b) => b.mtime - a.mtime);
    return items;
  }, [deck, recent]);

  // Resolve the active artifact. Until the user touches the picker, this is
  // the freshest one — so a brand-new flashcard or single SVG hijacks the
  // preview the moment it's written. After they pick, we stick to that
  // choice so streaming churn doesn't yank them around mid-review.
  const active: Artifact | null = useMemo(() => {
    if (artifacts.length === 0) return null;
    if (pickedSticky && pickedKey) {
      const hit = artifacts.find((a) => artifactKey(a) === pickedKey);
      if (hit) return hit;
    }
    return artifacts[0];
  }, [artifacts, pickedKey, pickedSticky]);

  function pick(a: Artifact) {
    setPickedKey(artifactKey(a));
    setPickedSticky(true);
    setShowPicker(false);
  }

  function clearSticky() {
    setPickedKey(null);
    setPickedSticky(false);
  }

  if (error && artifacts.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-red-600 p-6">
        {error}
      </div>
    );
  }

  if (artifacts.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-gray-400 p-6 text-center">
        No artifacts yet — ask the agent to generate something (slides, flashcard, SVG…).
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      <div className="border-b border-gray-200 bg-white px-3 py-2 flex items-center gap-2 relative" ref={pickerRef}>
        <button
          onClick={() => setShowPicker((v) => !v)}
          className="flex items-center gap-2 text-sm px-2 py-1 rounded border border-gray-300 hover:bg-gray-50 max-w-[60%]"
          title="Switch artifact"
        >
          <ArtifactIcon a={active} />
          <span className="truncate">{labelFor(active)}</span>
          <svg width="10" height="10" viewBox="0 0 10 10" className="text-gray-400 shrink-0">
            <path d="M2 4l3 3 3-3" stroke="currentColor" fill="none" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
        {pickedSticky ? (
          <button
            onClick={clearSticky}
            className="text-[11px] text-gray-500 hover:text-brand-green"
            title="Auto-follow the latest artifact"
          >
            Follow latest
          </button>
        ) : (
          <span className="text-[11px] text-gray-400">following latest</span>
        )}
        <div className="ml-auto flex items-center gap-3 text-[11px] text-gray-400 shrink-0">
          {active && <span>{fmtAge(active.mtime)}</span>}
          {active?.kind === "file" && onAskAi && (
            <button
              onClick={() => onAskAi(`@${active.path}`)}
              className="text-xs px-2 py-0.5 border border-brand-green/40 text-brand-green rounded hover:bg-brand-green/5"
              title="Reference this file in the chat textarea"
            >
              @chat
            </button>
          )}
          {active?.kind === "file" && (
            <a
              href={`/api/projects/${encodeURIComponent(project)}/file?path=${encodeURIComponent(active.path)}&v=${bust}`}
              target="_blank"
              rel="noreferrer"
              className="text-xs text-brand-green hover:underline"
            >
              Open ↗
            </a>
          )}
        </div>

        {showPicker && (
          <div className="absolute left-3 top-full mt-1 z-20 w-[420px] max-h-[60vh] overflow-y-auto bg-white border border-gray-200 rounded shadow-lg">
            <div className="text-[10px] uppercase tracking-wider text-gray-400 px-3 py-1.5 bg-gray-50 border-b border-gray-100 sticky top-0">
              Recent artifacts
            </div>
            <ul>
              {artifacts.map((a) => {
                const k = artifactKey(a);
                const isActive = active && artifactKey(active) === k;
                return (
                  <li
                    key={k}
                    onClick={() => pick(a)}
                    className={`px-3 py-2 text-sm cursor-pointer border-b border-gray-50 hover:bg-brand-cream/40 ${
                      isActive ? "bg-brand-cream" : ""
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <ArtifactIcon a={a} />
                      <span className="flex-1 truncate font-mono text-[13px]">{labelFor(a)}</span>
                      <span className="text-[11px] text-gray-400 shrink-0">{fmtAge(a.mtime)}</span>
                    </div>
                    <div className="text-[10px] text-gray-400 ml-6 truncate">
                      {sublabelFor(a)} · {fmtTime(a.mtime)}
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>

      <div className="flex-1 min-h-0 overflow-hidden">
        {active?.kind === "deck" ? (
          <SlideDeck project={project} onAskAi={onAskAi} />
        ) : active?.kind === "file" ? (
          <div className="h-full overflow-auto bg-gray-50">
            <FilePreview
              key={active.path}
              project={project}
              path={active.path}
              version={bust}
              onAskAi={onAskAi}
              compact
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function labelFor(a: Artifact | null): string {
  if (!a) return "—";
  if (a.kind === "deck") return `Slide deck (${a.count} slide${a.count === 1 ? "" : "s"})`;
  return a.path;
}

function sublabelFor(a: Artifact): string {
  if (a.kind === "deck") return "svg_output/";
  const e = ext(a.name).toUpperCase() || "FILE";
  return e;
}

function ArtifactIcon({ a }: { a: Artifact | null }) {
  const cls = "w-4 h-4 shrink-0";
  if (!a) return <FileIcon className={`${cls} text-gray-400`} />;
  if (a.kind === "deck") return <Layers className={`${cls} text-brand-green`} />;
  const e = ext(a.name);
  if (e === "svg") return <FileImage className={`${cls} text-violet-500`} />;
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp"].includes(e)) {
    return <ImageIcon className={`${cls} text-emerald-500`} />;
  }
  if (["md", "markdown", "txt"].includes(e)) return <FileText className={`${cls} text-sky-500`} />;
  if (e === "pdf") return <FileText className={`${cls} text-red-500`} />;
  if (["pptx", "ppt"].includes(e)) return <Presentation className={`${cls} text-orange-500`} />;
  if (["html", "htm"].includes(e)) return <FileCode className={`${cls} text-amber-500`} />;
  if (["json", "yaml", "yml", "xml", "ts", "tsx", "js", "jsx", "py", "css"].includes(e)) {
    return <FileCode className={`${cls} text-gray-500`} />;
  }
  return <FileIcon className={`${cls} text-gray-400`} />;
}
