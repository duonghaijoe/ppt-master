import { useEffect, useRef, useState } from "react";
import { useSlideWatcher } from "../hooks/useSlideWatcher";
import { useSession } from "../SessionContext";
import { SlideEditor, type SlideEditorHandle } from "./SlideEditor";

export function SlideDeck({
  project,
  onAskAi,
}: {
  project: string;
  onAskAi?: (text: string) => void;
}) {
  const { slides, version } = useSlideWatcher(project);
  const { streaming } = useSession();
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
          <div>No slides yet — ask the agent to start generating.</div>
        )}
      </div>
    );
  }

  const safeIndex = Math.min(index, slides.length - 1);
  const current = slides[safeIndex];
  const cacheKey = `${version}-${current.mtime}`;

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
          <a
            href={`/api/projects/${encodeURIComponent(project)}/export.pptx`}
            className="text-sm px-3 py-1 border border-brand-green text-brand-green rounded hover:bg-brand-green hover:text-white"
          >
            Download .pptx
          </a>
        </div>
      </header>
      <div className="flex-1 min-h-0 min-w-0 bg-gray-100">
        <SlideEditor
          ref={editorRef}
          key={current.file}
          project={project}
          file={current.file}
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
            key={s.file}
            onClick={() => setIndex(i)}
            className={`shrink-0 w-32 border rounded overflow-hidden text-xs ${
              i === safeIndex ? "border-brand-green ring-2 ring-brand-green/30" : "border-gray-200"
            }`}
            style={{ aspectRatio: "16 / 9" }}
            title={s.name}
          >
            <object
              type="image/svg+xml"
              data={`/api/projects/${encodeURIComponent(project)}/svg/${s.file}?v=${version}-${s.mtime}`}
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
