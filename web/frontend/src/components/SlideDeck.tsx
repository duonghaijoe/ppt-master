import { useEffect, useRef, useState } from "react";
import { useSlideWatcher } from "../hooks/useSlideWatcher";
import { useSession } from "../SessionContext";
import { SlideEditor, type SlideEditorHandle } from "./SlideEditor";

type ExportMeta = { file: string; mtime: number; bytes: number };

export function SlideDeck({
  project,
  dir,
  label,
  onAskAi,
  onSendAi,
}: {
  project: string;
  // Project-relative working dir (e.g. `svg_output`, `flashcards/templates`).
  dir: string;
  // Friendly display name from output_dirs.json — surfaced in the empty state
  // so the user knows which deck they're looking at when it's still streaming.
  label?: string;
  onAskAi?: (text: string) => void;
  // Dispatch a chat message immediately. Used to trigger the export pipeline
  // — the agent reads SKILL.md and runs svg_to_pptx.py, and the chat shows
  // progress while the .pptx is rendered.
  onSendAi?: (text: string) => void;
}) {
  const { slides, version } = useSlideWatcher(project, dir);
  const { streaming } = useSession();

  // Latest export, if any. Polled on mount and refreshed on every (debounced)
  // SSE filesystem event — when the agent writes the .pptx the button flips
  // from "Export pptx" → "Download .pptx" without a page reload.
  const [latestExport, setLatestExport] = useState<ExportMeta | null>(null);
  // Marker for "the user just clicked Export and the agent hasn't produced
  // a fresh .pptx yet" — drives the disabled "Exporting…" state. We track
  // by mtime so a successful export (new file, newer mtime) clears it.
  const [exportTriggeredAt, setExportTriggeredAt] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    function load() {
      fetch(`/api/projects/${encodeURIComponent(project)}/exports`)
        .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
        .then((data) => {
          if (cancelled) return;
          const items: ExportMeta[] = data.exports ?? [];
          setLatestExport(items[0] ?? null);
        })
        .catch(() => {
          /* non-fatal */
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

  // Clear the "exporting" marker once a newer .pptx lands. We compare mtime
  // (seconds, server-side) against the click timestamp (ms, client-side) so
  // the comparison needs the same unit — convert mtime to ms.
  useEffect(() => {
    if (exportTriggeredAt == null || !latestExport) return;
    if (latestExport.mtime * 1000 >= exportTriggeredAt) {
      setExportTriggeredAt(null);
    }
  }, [exportTriggeredAt, latestExport]);

  // If the agent's streaming turn ends without producing a fresh .pptx
  // (export failed, agent declined, etc.), reset so the user can retry.
  useEffect(() => {
    if (!streaming && exportTriggeredAt != null) {
      // Give the SSE-driven /exports refetch a beat to catch up before
      // assuming nothing was produced.
      const t = window.setTimeout(() => {
        setLatestExport((cur) => {
          if (cur && cur.mtime * 1000 >= exportTriggeredAt) {
            // A new export landed; the other effect already cleared.
            return cur;
          }
          setExportTriggeredAt(null);
          return cur;
        });
      }, 1500);
      return () => window.clearTimeout(t);
    }
  }, [streaming, exportTriggeredAt]);

  function triggerExport() {
    if (!onSendAi) return;
    setExportTriggeredAt(Date.now());
    onSendAi(
      "Export the current deck to PPTX using the post-processing pipeline. " +
        "Use the working dir from the [output dir: …] preface and place the " +
        "result under exports/. Report when it's done.",
    );
  }

  const [index, setIndex] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);
  const editorRef = useRef<SlideEditorHandle>(null);

  // Auto-jump to the latest slide as new ones stream in (only when not editing).
  useEffect(() => {
    if (!streaming || dirty) return;
    if (slides.length > 0) setIndex(slides.length - 1);
  }, [slides.length, streaming, dirty]);

  // Switching slides drops out of edit mode and clears any save error.
  useEffect(() => {
    setEditing(false);
    setSaveErr(null);
  }, [index]);

  // Switching working dir resets everything — different decks, different
  // slide count, different paths.
  useEffect(() => {
    setIndex(0);
    setEditing(false);
    setSaveErr(null);
    setDirty(false);
  }, [dir]);

  if (slides.length === 0) {
    return (
      <div className="h-full flex flex-col items-center justify-center text-gray-400 gap-3">
        {streaming ? (
          <>
            <div className="bg-white border border-gray-200 rounded shadow-sm flex items-center justify-center text-gray-400 text-sm" style={{ aspectRatio: "16 / 9", width: "min(60%, 720px)" }}>
              <span className="animate-pulse">Generating slides…</span>
            </div>
            <div className="text-xs">The first slides will appear here as they're written.</div>
          </>
        ) : (
          <div>{label ? `No slides yet in ${label} — ask the agent to start generating.` : "No slides yet — ask the agent to start generating."}</div>
        )}
      </div>
    );
  }

  const safeIndex = Math.min(index, slides.length - 1);
  const current = slides[safeIndex];
  const cacheKey = `${version}-${current.mtime}`;
  const thumbUrl = (p: string, mtime: number) =>
    `/api/projects/${encodeURIComponent(project)}/file?path=${encodeURIComponent(p)}&v=${version}-${mtime}`;

  return (
    <div className="h-full flex flex-col">
      <header className="flex items-center justify-between px-4 py-2 border-b border-gray-200 bg-white gap-3">
        <div className="text-sm flex items-center gap-2 min-w-0">
          <span className="font-mono truncate">{current.name}</span>
          <span className="text-gray-400 shrink-0">
            {safeIndex + 1} / {slides.length}
          </span>
          {dirty && (
            <span className="text-[10px] uppercase tracking-wider bg-amber-100 text-amber-800 px-1.5 py-0.5 rounded shrink-0">
              edited
            </span>
          )}
          {streaming && (
            <span className="text-[10px] uppercase tracking-wider bg-brand-green/10 text-brand-green px-1.5 py-0.5 rounded animate-pulse shrink-0">
              live
            </span>
          )}
          {saveErr && (
            <span className="text-[11px] text-red-600 truncate">{saveErr}</span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <button
            onClick={() => setEditing((v) => !v)}
            className={`text-xs px-2.5 py-1 rounded border ${
              editing
                ? "bg-brand-green text-white border-brand-green"
                : "border-gray-300 text-gray-700 hover:bg-gray-50"
            }`}
            title={editing ? "Exit edit mode" : "Enter edit mode"}
          >
            {editing ? "Editing" : "Edit"}
          </button>
          {editing && (
            <>
              <button
                onClick={() => editorRef.current?.undo()}
                disabled={!canUndo || saving}
                title="Undo (⌘/Ctrl+Z)"
                className="text-xs px-2.5 py-1 rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                ↶ Undo
              </button>
              <button
                onClick={() => editorRef.current?.redo()}
                disabled={!canRedo || saving}
                title="Redo (⇧⌘Z / ⌘Y)"
                className="text-xs px-2.5 py-1 rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                ↷ Redo
              </button>
              <button
                onClick={() => editorRef.current?.revert()}
                disabled={!dirty || saving}
                className="text-xs px-2.5 py-1 rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                Revert
              </button>
              <button
                onClick={() => editorRef.current?.save()}
                disabled={!dirty || saving}
                className="text-xs px-2.5 py-1 rounded bg-brand-red text-white disabled:opacity-40"
              >
                {saving ? "Saving…" : "Save"}
              </button>
            </>
          )}
          <ExportButton
            project={project}
            latestExport={latestExport}
            exporting={exportTriggeredAt != null}
            canTrigger={!!onSendAi && !streaming}
            onTrigger={triggerExport}
          />
        </div>
      </header>
      <div className="flex-1 min-h-0 min-w-0 bg-gray-100">
        <SlideEditor
          ref={editorRef}
          key={current.path}
          project={project}
          path={current.path}
          cacheKey={cacheKey}
          editing={editing}
          onDirtyChange={setDirty}
          onSavingChange={setSaving}
          onSaveErrChange={setSaveErr}
          onCanUndoChange={setCanUndo}
          onCanRedoChange={setCanRedo}
          onAskAi={onAskAi}
        />
      </div>
      <nav className="border-t border-gray-200 bg-white p-2 flex gap-2 overflow-x-auto">
        {slides.map((s, i) => (
          <button
            key={s.path}
            onClick={() => setIndex(i)}
            className={`shrink-0 w-32 border rounded overflow-hidden text-xs ${
              i === safeIndex ? "border-brand-green ring-2 ring-brand-green/30" : "border-gray-200"
            }`}
            style={{ aspectRatio: "16 / 9" }}
            title={s.name}
          >
            <object
              type="image/svg+xml"
              data={thumbUrl(s.path, s.mtime)}
              className="w-full h-full pointer-events-none"
            />
          </button>
        ))}
        {streaming && (
          <div
            className="shrink-0 w-32 border border-dashed border-brand-green/40 rounded flex items-center justify-center text-[10px] text-brand-green/80 bg-brand-green/5 animate-pulse"
            style={{ aspectRatio: "16 / 9" }}
            title="More slides being generated"
          >
            generating…
          </div>
        )}
      </nav>
    </div>
  );
}

// Three states:
// 1. No export yet → "Export pptx" — triggers the agent to run the export.
// 2. Export in flight (we just clicked, no fresh .pptx yet) → "Exporting…".
// 3. Export available → "Download .pptx" pointing at the latest file.
// The agent message we author is generic ("export the current deck to PPTX
// using the post-processing pipeline") so it works against whatever working
// dir is active — the chat preface already says `[output dir: …]`.
function ExportButton({
  project,
  latestExport,
  exporting,
  canTrigger,
  onTrigger,
}: {
  project: string;
  latestExport: ExportMeta | null;
  exporting: boolean;
  canTrigger: boolean;
  onTrigger: () => void;
}) {
  if (exporting) {
    return (
      <button
        disabled
        className="text-sm px-3 py-1 border border-brand-green/40 text-brand-green/60 rounded bg-brand-green/5 cursor-wait"
        title="The agent is producing your .pptx. Watch the chat for progress."
      >
        Exporting…
      </button>
    );
  }
  if (latestExport) {
    return (
      <a
        href={`/api/projects/${encodeURIComponent(project)}/export.pptx`}
        className="text-sm px-3 py-1 border border-brand-green text-brand-green rounded hover:bg-brand-green hover:text-white"
        title={`${latestExport.file} · ${(latestExport.bytes / 1024).toFixed(0)} KB`}
      >
        Download .pptx
      </a>
    );
  }
  return (
    <button
      onClick={onTrigger}
      disabled={!canTrigger}
      className="text-sm px-3 py-1 border border-brand-green text-brand-green rounded hover:bg-brand-green hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
      title={
        canTrigger
          ? "Render the current deck to PowerPoint"
          : "Wait for the current agent turn to finish"
      }
    >
      Export pptx
    </button>
  );
}
