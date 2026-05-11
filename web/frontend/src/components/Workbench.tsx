import { useEffect, useMemo, useRef, useState } from "react";
import { Layers } from "lucide-react";
import { SlideDeck } from "./SlideDeck";

type SlideMeta = { name: string; file: string; path: string; mtime: number };
type DirEntry = {
  dir: string;
  label: string;
  slides: SlideMeta[];
  count: number;
  mtime: number;
};

function fmtAge(t: number): string {
  if (!t) return "—";
  const s = Math.max(0, Math.floor(Date.now() / 1000) - t);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function Workbench({
  project,
  onAskAi,
  onSendAi,
}: {
  project: string;
  onAskAi?: (text: string) => void;
  onSendAi?: (text: string) => void;
}) {
  // Backend-driven directive: which dirs the agent has registered as decks
  // it's producing, plus the current pick. The UI never shows the raw `dir`
  // path — always the friendly `label`. Individual file browsing lives in
  // the ProjectFiles tab, so Workbench is intentionally deck-only.
  const [dirs, setDirs] = useState<DirEntry[]>([]);
  const [backendCurrent, setBackendCurrent] = useState<string>("");
  const [pickedDir, setPickedDir] = useState<string | null>(null);
  const [pickedSticky, setPickedSticky] = useState(false);
  const [showPicker, setShowPicker] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pickerRef = useRef<HTMLDivElement>(null);

  // Fetch the directive on mount + refetch on every (debounced) SSE event.
  useEffect(() => {
    let cancelled = false;
    function load() {
      fetch(`/api/projects/${encodeURIComponent(project)}/output-dirs`)
        .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
        .then((od) => {
          if (cancelled) return;
          setError(null);
          setDirs(od.dirs ?? []);
          setBackendCurrent(od.current ?? "");
        })
        .catch((e) => {
          if (cancelled) return;
          setError(String(e));
        });
    }
    load();

    const es = new EventSource(`/api/projects/${encodeURIComponent(project)}/events`);
    let pending: number | null = null;
    es.onmessage = () => {
      if (pending) return;
      pending = window.setTimeout(() => {
        pending = null;
        load();
      }, 300);
    };
    return () => {
      cancelled = true;
      es.close();
      if (pending) window.clearTimeout(pending);
    };
  }, [project]);

  // Reset selection when switching projects.
  useEffect(() => {
    setPickedDir(null);
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

  // Visible decks: those with at least one slide on disk. Empty registered
  // dirs would just confuse the user — there's nothing to render.
  const decks = useMemo(() => dirs.filter((d) => d.count > 0), [dirs]);

  // Resolve the active deck:
  // - When the user explicitly picked one, stay sticky so streaming churn
  //   doesn't yank them around mid-review.
  // - Otherwise, follow the backend's `current` so chat and Workbench stay
  //   in sync; if `current` is registered but still empty (deck is being
  //   generated right now), fall through to the freshest non-empty deck.
  const active: DirEntry | null = useMemo(() => {
    if (decks.length === 0) return null;
    if (pickedSticky && pickedDir) {
      const hit = decks.find((d) => d.dir === pickedDir);
      if (hit) return hit;
    }
    if (backendCurrent) {
      const cur = decks.find((d) => d.dir === backendCurrent);
      if (cur) return cur;
    }
    // Freshest first.
    return [...decks].sort((a, b) => b.mtime - a.mtime)[0];
  }, [decks, pickedDir, pickedSticky, backendCurrent]);

  async function setCurrentBackend(dir: string) {
    if (!dir || dir === backendCurrent) return;
    try {
      const res = await fetch(
        `/api/projects/${encodeURIComponent(project)}/output-dirs/current`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ current: dir }),
        },
      );
      if (res.ok) {
        const data = await res.json();
        setBackendCurrent(data.current ?? dir);
      }
    } catch {
      // Non-fatal: UI selection still works locally; chat preface just
      // won't reflect the flip until next refresh.
    }
  }

  function pick(d: DirEntry) {
    setPickedDir(d.dir);
    setPickedSticky(true);
    setShowPicker(false);
    void setCurrentBackend(d.dir);
  }

  function clearSticky() {
    setPickedDir(null);
    setPickedSticky(false);
  }

  if (error && decks.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-red-600 p-6">
        {error}
      </div>
    );
  }

  if (decks.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-gray-400 p-6 text-center">
        No deck yet — ask the agent to start generating slides.
      </div>
    );
  }

  const onlyOne = decks.length === 1;

  return (
    <div className="h-full flex flex-col">
      <div className="border-b border-gray-200 bg-white px-3 py-2 flex items-center gap-2 relative" ref={pickerRef}>
        <button
          onClick={() => !onlyOne && setShowPicker((v) => !v)}
          disabled={onlyOne}
          className={`flex items-center gap-2 text-sm px-2 py-1 rounded border max-w-[60%] ${
            onlyOne
              ? "border-transparent cursor-default"
              : "border-gray-300 hover:bg-gray-50"
          }`}
          title={onlyOne ? "" : "Switch deck"}
        >
          <Layers className="w-4 h-4 shrink-0 text-brand-green" />
          <span className="truncate">
            {active
              ? `${active.label} · ${active.count} slide${active.count === 1 ? "" : "s"}`
              : "—"}
          </span>
          {!onlyOne && (
            <svg width="10" height="10" viewBox="0 0 10 10" className="text-gray-400 shrink-0">
              <path d="M2 4l3 3 3-3" stroke="currentColor" fill="none" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          )}
        </button>
        {!onlyOne &&
          (pickedSticky ? (
            <button
              onClick={clearSticky}
              className="text-[11px] text-gray-500 hover:text-brand-green"
              title="Auto-follow the agent's current deck"
            >
              Follow current
            </button>
          ) : (
            <span className="text-[11px] text-gray-400">following current</span>
          ))}
        <div className="ml-auto flex items-center gap-3 text-[11px] text-gray-400 shrink-0">
          {active && <span>{fmtAge(active.mtime)}</span>}
        </div>

        {showPicker && (
          <div className="absolute left-3 top-full mt-1 z-20 w-[420px] max-h-[60vh] overflow-y-auto bg-white border border-gray-200 rounded shadow-lg">
            <div className="text-[10px] uppercase tracking-wider text-gray-400 px-3 py-1.5 bg-gray-50 border-b border-gray-100 sticky top-0">
              Working decks
            </div>
            <ul>
              {decks.map((d) => {
                const isActive = active?.dir === d.dir;
                return (
                  <li
                    key={d.dir}
                    onClick={() => pick(d)}
                    className={`px-3 py-2 text-sm cursor-pointer border-b border-gray-50 hover:bg-brand-cream/40 ${
                      isActive ? "bg-brand-cream" : ""
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <Layers className="w-4 h-4 shrink-0 text-brand-green" />
                      <span className="flex-1 truncate text-[13px]">{d.label}</span>
                      <span className="text-[11px] text-gray-400 shrink-0">{fmtAge(d.mtime)}</span>
                    </div>
                    <div className="text-[10px] text-gray-400 ml-6 truncate">
                      {d.count} slide{d.count === 1 ? "" : "s"}
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>

      <div className="flex-1 min-h-0 overflow-hidden">
        {active ? (
          <SlideDeck
            key={active.dir}
            project={project}
            dir={active.dir}
            label={active.label}
            onAskAi={onAskAi}
            onSendAi={onSendAi}
          />
        ) : null}
      </div>
    </div>
  );
}
