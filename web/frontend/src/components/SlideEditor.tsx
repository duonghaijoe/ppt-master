import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type Props = {
  project: string;
  file: string;
  cacheKey: string;
  editing: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  onSavingChange?: (saving: boolean) => void;
  onSaveErrChange?: (err: string | null) => void;
  onCanUndoChange?: (v: boolean) => void;
  onCanRedoChange?: (v: boolean) => void;
  onAskAi?: (text: string) => void;
};

export type SlideEditorHandle = {
  save: () => void;
  revert: () => void;
  undo: () => void;
  redo: () => void;
};

const PROP_KEYS = [
  "fill",
  "stroke",
  "stroke-width",
  "opacity",
  "font-size",
  "font-family",
  "font-weight",
  "font-style",
  "text-anchor",
  "text-decoration",
  "x",
  "y",
  "width",
  "height",
] as const;

type PropKey = (typeof PROP_KEYS)[number];

// Props that live in inline `style="..."` for most generated SVGs.
// We need to write BOTH the attribute and the inline style to override
// whatever the document already had.
const STYLE_PROPS = new Set<PropKey>([
  "fill",
  "stroke",
  "stroke-width",
  "opacity",
  "font-size",
  "font-family",
  "font-weight",
  "font-style",
  "text-anchor",
  "text-decoration",
]);

// Text-related properties that benefit from cascading down to descendant
// <text>/<tspan> elements — otherwise a child's inline style silently masks
// the parent change and the user sees no preview.
const CASCADE_PROPS = new Set<PropKey>([
  "font-family",
  "font-size",
  "font-weight",
  "font-style",
  "text-anchor",
  "text-decoration",
  "fill",
]);

function readVal(el: SVGElement, k: PropKey): string {
  if (STYLE_PROPS.has(k)) {
    const inline = el.style?.getPropertyValue?.(k);
    if (inline) return String(inline).trim();
  }
  const attr = el.getAttribute(k);
  if (attr != null && attr !== "") return attr;
  if (STYLE_PROPS.has(k)) {
    try {
      const cs = window.getComputedStyle(el);
      const v = cs.getPropertyValue(k);
      if (v) return v.trim();
    } catch {
      /* ignore */
    }
  }
  return "";
}

// Atomic DOM mutations recorded in the history stack. Every user-initiated
// change builds an array of these, so a single Undo reverses every side
// effect (parent attribute + inline style + descendant cascade clears).
type Change =
  | { kind: "attr"; element: SVGElement; key: string; prev: string; next: string }
  | { kind: "style"; element: SVGElement; key: string; prev: string; next: string }
  | { kind: "text"; element: SVGElement; prev: string; next: string };

type HistoryEntry = { label: string; changes: Change[]; ts: number };

function applyChange(c: Change, dir: "next" | "prev") {
  const v = dir === "next" ? c.next : c.prev;
  if (c.kind === "attr") {
    if (v === "") c.element.removeAttribute(c.key);
    else c.element.setAttribute(c.key, v);
  } else if (c.kind === "style") {
    if (v === "") c.element.style.removeProperty(c.key);
    // !important so we win against <style> blocks the generator may have
    // emitted into <defs>.
    else c.element.style.setProperty(c.key, v, "important");
  } else {
    c.element.textContent = v;
  }
}

function applyChanges(changes: Change[]) {
  for (const c of changes) applyChange(c, "next");
}
function reverseChanges(changes: Change[]) {
  // Reverse in reverse order so dependent edits unwind cleanly.
  for (let i = changes.length - 1; i >= 0; i--) applyChange(changes[i], "prev");
}

// Build the full set of changes for "set property `k` to `val` on `el`",
// including cascade-clearing on descendants for text-style props.
function buildPropChanges(el: SVGElement, k: PropKey, val: string): Change[] {
  const out: Change[] = [];
  const isStyle = STYLE_PROPS.has(k);

  const prevAttr = el.getAttribute(k) ?? "";
  if (val === "") {
    if (prevAttr) out.push({ kind: "attr", element: el, key: k, prev: prevAttr, next: "" });
  } else if (prevAttr !== val) {
    out.push({ kind: "attr", element: el, key: k, prev: prevAttr, next: val });
  }

  if (isStyle) {
    const prevStyle = el.style.getPropertyValue(k);
    if (val === "") {
      if (prevStyle) out.push({ kind: "style", element: el, key: k, prev: prevStyle, next: "" });
    } else if (prevStyle !== val) {
      out.push({ kind: "style", element: el, key: k, prev: prevStyle, next: val });
    }
  }

  if (CASCADE_PROPS.has(k)) {
    const descendants = el.querySelectorAll<SVGElement>("text, tspan");
    descendants.forEach((d) => {
      if (d === el) return;
      const dAttr = d.getAttribute(k) ?? "";
      if (dAttr) out.push({ kind: "attr", element: d, key: k, prev: dAttr, next: "" });
      if (isStyle) {
        const dStyle = d.style.getPropertyValue(k);
        if (dStyle) out.push({ kind: "style", element: d, key: k, prev: dStyle, next: "" });
      }
    });
  }

  return out;
}

function rewriteImageRefs(svgText: string, project: string): string {
  const base = `/api/projects/${encodeURIComponent(project)}/images/`;
  return svgText.replace(
    /(href|xlink:href)\s*=\s*"([^"]+)"/g,
    (m, attr, val) => {
      if (
        val.startsWith("data:") ||
        val.startsWith("http://") ||
        val.startsWith("https://") ||
        val.startsWith("/") ||
        val.startsWith("#")
      )
        return m;
      const idx = val.indexOf("images/");
      if (idx === -1) return m;
      const filename = val.slice(idx + "images/".length);
      if (filename.includes("/") || filename.includes("..")) return m;
      return `${attr}="${base}${filename}"`;
    },
  );
}

function extractInnerSvg(svgText: string): { open: string; inner: string; close: string } | null {
  const m = svgText.match(/<svg\b([^>]*)>([\s\S]*)<\/svg\s*>/i);
  if (!m) return null;
  return { open: `<svg${m[1]}>`, inner: m[2], close: "</svg>" };
}

type TextEdit = {
  el: SVGElement;
  value: string;
  rect: { top: number; left: number; width: number; height: number };
  fontSize: number;
};

export const SlideEditor = forwardRef<SlideEditorHandle, Props>(function SlideEditor(
  {
    project,
    file,
    cacheKey,
    editing,
    onDirtyChange,
    onSavingChange,
    onSaveErrChange,
    onCanUndoChange,
    onCanRedoChange,
    onAskAi,
  },
  ref,
) {
  const url = useMemo(
    () => `/api/projects/${encodeURIComponent(project)}/svg/${file}?v=${cacheKey}`,
    [project, file, cacheKey],
  );
  const hostRef = useRef<HTMLDivElement>(null);          // div that *contains* the inline svg
  const [svgOpen, setSvgOpen] = useState<string>("");
  const [svgInner, setSvgInner] = useState<string>("");
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<SVGElement | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [textEdit, setTextEdit] = useState<TextEdit | null>(null);
  // Bumps after every successful DOM mutation so the property panel re-reads
  // values from the live SVG (covers undo/redo + cascade side effects).
  const [historyTick, setHistoryTick] = useState(0);
  const historyRef = useRef<HistoryEntry[]>([]);
  const futureRef = useRef<HistoryEntry[]>([]);
  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);

  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);
  useEffect(() => onSavingChange?.(saving), [saving, onSavingChange]);
  useEffect(() => onSaveErrChange?.(saveErr), [saveErr, onSaveErrChange]);
  useEffect(() => onCanUndoChange?.(canUndo), [canUndo, onCanUndoChange]);
  useEffect(() => onCanRedoChange?.(canRedo), [canRedo, onCanRedoChange]);

  function refreshHistoryFlags() {
    setCanUndo(historyRef.current.length > 0);
    setCanRedo(futureRef.current.length > 0);
  }

  function pushHistory(entry: HistoryEntry) {
    if (entry.changes.length === 0) return;
    const last = historyRef.current[historyRef.current.length - 1];
    const sameShape =
      last &&
      last.label === entry.label &&
      entry.ts - last.ts < 800 &&
      last.changes.length === entry.changes.length &&
      last.changes.every((c, i) => {
        const n = entry.changes[i];
        return (
          c.kind === n.kind &&
          c.element === n.element &&
          (c.kind === "text" || (c as any).key === (n as any).key)
        );
      });
    if (sameShape && last) {
      // Coalesce: keep `prev` from the older entry, take `next` from the new.
      for (let i = 0; i < last.changes.length; i++) {
        last.changes[i].next = entry.changes[i].next;
      }
      last.ts = entry.ts;
    } else {
      historyRef.current.push(entry);
      if (historyRef.current.length > 200) historyRef.current.shift();
    }
    futureRef.current = [];
    refreshHistoryFlags();
    setHistoryTick((t) => t + 1);
  }

  function undo() {
    const e = historyRef.current.pop();
    if (!e) return;
    reverseChanges(e.changes);
    futureRef.current.push(e);
    setDirty(historyRef.current.length > 0);
    setTextEdit(null);
    refreshHistoryFlags();
    setHistoryTick((t) => t + 1);
  }

  function redo() {
    const e = futureRef.current.pop();
    if (!e) return;
    applyChanges(e.changes);
    historyRef.current.push(e);
    setDirty(true);
    setTextEdit(null);
    refreshHistoryFlags();
    setHistoryTick((t) => t + 1);
  }

  useEffect(() => {
    setSelected(null);
    setDirty(false);
    setLoadErr(null);
    setTextEdit(null);
    historyRef.current = [];
    futureRef.current = [];
    refreshHistoryFlags();
    fetch(url)
      .then((r) => (r.ok ? r.text() : Promise.reject(r.statusText)))
      .then((t) => {
        const fixed = rewriteImageRefs(t, project);
        const parts = extractInnerSvg(fixed);
        if (!parts) {
          setLoadErr("not an SVG document");
          return;
        }
        setSvgOpen(parts.open);
        setSvgInner(parts.inner);
      })
      .catch((e) => setLoadErr(String(e)));
  }, [url, project]);

  // Wire selection / dblclick handlers when in edit mode.
  useEffect(() => {
    const root = hostRef.current?.querySelector("svg") as SVGSVGElement | null;
    if (!root) return;
    if (!editing) {
      root.querySelectorAll("[data-edit-selected]").forEach((el) =>
        el.removeAttribute("data-edit-selected"),
      );
      setSelected(null);
      return;
    }

    function pickEditable(target: Element | null): SVGElement | null {
      let el: Element | null = target;
      while (el && el !== root) {
        const tag = el.tagName.toLowerCase();
        if (
          tag === "text" ||
          tag === "tspan" ||
          tag === "rect" ||
          tag === "circle" ||
          tag === "ellipse" ||
          tag === "line" ||
          tag === "path" ||
          tag === "image" ||
          tag === "polygon" ||
          tag === "polyline" ||
          tag === "g"
        )
          return el as SVGElement;
        el = el.parentElement;
      }
      return null;
    }

    function onClick(ev: MouseEvent) {
      if (!root) return;
      const found = pickEditable(ev.target as Element | null);
      ev.stopPropagation();
      root.querySelectorAll("[data-edit-selected]").forEach((n) =>
        n.removeAttribute("data-edit-selected"),
      );
      if (!found) {
        setSelected(null);
        return;
      }
      found.setAttribute("data-edit-selected", "1");
      setSelected(found);
    }

    function onDblClick(ev: MouseEvent) {
      const target = ev.target as Element | null;
      let el: Element | null = target;
      while (el && el !== root) {
        const tag = el.tagName.toLowerCase();
        if (tag === "text" || tag === "tspan") break;
        el = el.parentElement;
      }
      if (!el || el === root) return;
      ev.preventDefault();
      ev.stopPropagation();
      openTextEditor(el as SVGElement);
    }

    root.addEventListener("click", onClick);
    root.addEventListener("dblclick", onDblClick);
    return () => {
      root.removeEventListener("click", onClick);
      root.removeEventListener("dblclick", onDblClick);
    };
  }, [editing, svgInner, svgOpen]);

  function openTextEditor(el: SVGElement) {
    const host = hostRef.current;
    if (!host) return;
    const r = el.getBoundingClientRect();
    const hr = host.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    const fontSize = parseFloat(cs.fontSize) || 16;
    setTextEdit({
      el,
      value: el.textContent ?? "",
      rect: {
        top: r.top - hr.top,
        left: r.left - hr.left,
        width: Math.max(r.width, 80),
        height: Math.max(r.height, fontSize * 1.4),
      },
      fontSize,
    });
  }

  function commitTextEdit(commit: boolean) {
    if (!textEdit) return;
    if (commit) {
      const prev = textEdit.el.textContent ?? "";
      const next = textEdit.value;
      if (next !== prev) {
        const change: Change = { kind: "text", element: textEdit.el, prev, next };
        applyChanges([change]);
        pushHistory({ label: `text:${textEdit.el.tagName}`, changes: [change], ts: Date.now() });
        setDirty(true);
      }
    }
    setTextEdit(null);
  }

  function applyAttr(key: PropKey, val: string) {
    if (!selected) return;
    const changes = buildPropChanges(selected, key, val);
    if (changes.length === 0) return;
    applyChanges(changes);
    pushHistory({ label: `attr:${key}`, changes, ts: Date.now() });
    setDirty(true);
  }

  // Build the chat draft for a "/edit this element" request. Includes the
  // slide filename, tag/id/class, computed bbox in SVG units, key style props,
  // visible text content, and a (truncated) outerHTML snippet so the agent
  // can find and rewrite the right node when it edits the file.
  function askAiForSelected(instruction: string) {
    if (!selected || !onAskAi) return;
    const trimmed = instruction.trim();
    if (!trimmed) return;
    const draft = buildElementPrompt(selected, file, trimmed);
    onAskAi(draft);
  }

  async function save() {
    const root = hostRef.current?.querySelector("svg");
    if (!root) return;
    root.querySelectorAll("[data-edit-selected]").forEach((n) =>
      n.removeAttribute("data-edit-selected"),
    );
    const serialized = new XMLSerializer().serializeToString(root);
    setSaving(true);
    setSaveErr(null);
    try {
      const res = await fetch(
        `/api/projects/${encodeURIComponent(project)}/svg/${file}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: serialized }),
        },
      );
      if (!res.ok) throw new Error(await res.text());
      setDirty(false);
      // Saved bytes match what's on disk — clear history so undo can't roll
      // past the save point and confuse the dirty indicator.
      historyRef.current = [];
      futureRef.current = [];
      refreshHistoryFlags();
    } catch (e: any) {
      setSaveErr(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  function revert() {
    setLoadErr(null);
    setTextEdit(null);
    fetch(url, { cache: "no-store" })
      .then((r) => (r.ok ? r.text() : Promise.reject(r.statusText)))
      .then((t) => {
        const fixed = rewriteImageRefs(t, project);
        const parts = extractInnerSvg(fixed);
        if (parts) {
          setSvgOpen(parts.open);
          setSvgInner(parts.inner);
          setDirty(false);
          setSelected(null);
          historyRef.current = [];
          futureRef.current = [];
          refreshHistoryFlags();
        }
      })
      .catch((e) => setLoadErr(String(e)));
  }

  useImperativeHandle(ref, () => ({ save, revert, undo, redo }), [file, project, svgInner, url]);

  // ⌘/Ctrl + S to save, ⌘/Ctrl + Z / ⇧⌘Z (or ⌘Y) for undo/redo while editing.
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (!editing) return;
      const meta = ev.metaKey || ev.ctrlKey;
      if (!meta) return;
      const k = ev.key.toLowerCase();
      if (k === "s") {
        ev.preventDefault();
        save();
      } else if (k === "z") {
        ev.preventDefault();
        if (ev.shiftKey) redo();
        else undo();
      } else if (k === "y") {
        ev.preventDefault();
        redo();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editing, file, project, svgInner]);

  // Focus the overlay input as soon as a text edit starts.
  const overlayInputRef = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    if (textEdit) {
      overlayInputRef.current?.focus();
      overlayInputRef.current?.select();
    }
  }, [textEdit?.el]);

  const html = svgOpen + svgInner + "</svg>";

  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 min-h-0 grid grid-cols-[1fr_auto] gap-0 bg-gray-100">
        <div className="flex items-center justify-center p-6 overflow-auto min-h-0 min-w-0">
          {loadErr ? (
            <div className="text-sm text-red-600">{loadErr}</div>
          ) : (
            <div
              ref={hostRef}
              className="relative bg-white shadow-lg svg-host"
              style={{ aspectRatio: "16 / 9", width: "min(100%, 1280px)" }}
            >
              <div
                className="absolute inset-0"
                dangerouslySetInnerHTML={{ __html: html }}
              />
              {textEdit && (
                <textarea
                  ref={overlayInputRef}
                  value={textEdit.value}
                  onChange={(e) =>
                    setTextEdit((cur) => (cur ? { ...cur, value: e.target.value } : cur))
                  }
                  onBlur={(e) => {
                    // Keep the inline editor open if focus moved into the
                    // property panel — user is tweaking attributes for the
                    // same text element.
                    const next = e.relatedTarget as HTMLElement | null;
                    if (next?.closest?.("[data-property-panel]")) return;
                    commitTextEdit(true);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      e.preventDefault();
                      commitTextEdit(false);
                    } else if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      commitTextEdit(true);
                    }
                  }}
                  className="absolute z-10 border-2 border-brand-green bg-white/95 px-1 py-0 outline-none resize-none font-medium leading-tight"
                  style={{
                    top: textEdit.rect.top - 4,
                    left: textEdit.rect.left - 4,
                    width: textEdit.rect.width + 8,
                    height: textEdit.rect.height + 8,
                    fontSize: Math.max(12, Math.min(textEdit.fontSize, 64)),
                  }}
                />
              )}
            </div>
          )}
        </div>

        {editing && (
          <PropertyPanel
            selected={selected}
            historyTick={historyTick}
            applyAttr={applyAttr}
            askEnabled={!!onAskAi}
            onAskAi={askAiForSelected}
            onClose={() => {
              const root = hostRef.current?.querySelector("svg");
              root?.querySelectorAll("[data-edit-selected]").forEach((n) =>
                n.removeAttribute("data-edit-selected"),
              );
              setSelected(null);
            }}
          />
        )}
      </div>

      {editing && (
        <div className="text-[11px] text-gray-500 px-4 py-1.5 border-t bg-white">
          Click · double-click text to edit · Enter commits · Esc cancels · ⌘/Ctrl+Z undo · ⇧⌘Z / ⌘Y redo · ⌘/Ctrl+S save
        </div>
      )}
    </div>
  );
});

type ApplyAttr = (key: PropKey, val: string) => void;

const SHAPE_TAGS = new Set([
  "rect",
  "image",
  "line",
  "circle",
  "ellipse",
  "polygon",
  "polyline",
  "path",
  "g",
  "use",
]);

function PropertyPanel({
  selected,
  historyTick,
  applyAttr,
  askEnabled,
  onAskAi,
  onClose,
}: {
  selected: SVGElement | null;
  historyTick: number;
  applyAttr: ApplyAttr;
  askEnabled: boolean;
  onAskAi: (instruction: string) => void;
  onClose: () => void;
}) {
  if (!selected) {
    return (
      <aside
        data-property-panel
        className="w-72 border-l border-gray-200 bg-white p-3 text-xs text-gray-500 overflow-y-auto"
      >
        <div className="text-[10px] uppercase tracking-wider text-gray-400 mb-1">Inspector</div>
        Click an element on the canvas to inspect.
        <div className="mt-3 text-[11px] text-gray-400 leading-relaxed">
          Tip: tspans inside a text element have their own inline styles.
          Selecting the parent text and changing a font property will cascade
          and clear matching child overrides automatically.
        </div>
      </aside>
    );
  }
  const tag = selected.tagName.toLowerCase();
  const isText = tag === "text" || tag === "tspan";
  const isShape = SHAPE_TAGS.has(tag);

  return (
    <aside
      data-property-panel
      className="w-72 border-l border-gray-200 bg-white p-3 text-xs flex flex-col gap-3 overflow-y-auto"
    >
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[10px] uppercase text-gray-400 tracking-wider">Element</div>
          <div className="font-mono">&lt;{tag}&gt;</div>
        </div>
        <button
          onClick={onClose}
          className="text-gray-400 hover:text-brand-red text-base leading-none px-1"
          title="Deselect"
        >
          ×
        </button>
      </div>

      {askEnabled && (
        <AskAiSection key={selectedKey(selected)} onSend={onAskAi} />
      )}

      {isText && (
        <Section title="Text">
          <div className="text-[11px] text-gray-500 mb-1">
            Content:{" "}
            <span className="font-mono break-words">
              {(selected.textContent || "—").slice(0, 80)}
            </span>
          </div>
          <FontFamilyRow element={selected} tick={historyTick} apply={applyAttr} />
          <Grid2>
            <NumberRow label="Size" k="font-size" element={selected} tick={historyTick} apply={applyAttr} min={0} step={0.5} />
            <FontWeightRow element={selected} tick={historyTick} apply={applyAttr} />
          </Grid2>
          <StyleToggleRow element={selected} tick={historyTick} apply={applyAttr} />
          <TextAlignRow element={selected} tick={historyTick} apply={applyAttr} />
          <ColorRow label="Color" k="fill" element={selected} tick={historyTick} apply={applyAttr} />
        </Section>
      )}

      {!isText && (
        <Section title="Fill">
          <ColorRow label="Color" k="fill" element={selected} tick={historyTick} apply={applyAttr} />
        </Section>
      )}

      <Section title="Stroke">
        <ColorRow label="Color" k="stroke" element={selected} tick={historyTick} apply={applyAttr} />
        <NumberRow label="Width" k="stroke-width" element={selected} tick={historyTick} apply={applyAttr} min={0} step={0.5} />
      </Section>

      {(isShape || tag === "text") && (
        <Section title="Position">
          <Grid2>
            <NumberRow label="X" k="x" element={selected} tick={historyTick} apply={applyAttr} step={1} />
            <NumberRow label="Y" k="y" element={selected} tick={historyTick} apply={applyAttr} step={1} />
          </Grid2>
          <Grid2>
            <NumberRow label="W" k="width" element={selected} tick={historyTick} apply={applyAttr} min={0} step={1} />
            <NumberRow label="H" k="height" element={selected} tick={historyTick} apply={applyAttr} min={0} step={1} />
          </Grid2>
        </Section>
      )}

      <Section title="Effects">
        <OpacityRow element={selected} tick={historyTick} apply={applyAttr} />
      </Section>
    </aside>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-[10px] uppercase tracking-wider text-gray-400 border-b border-gray-100 pb-1">
        {title}
      </div>
      {children}
    </div>
  );
}

function Grid2({ children }: { children: React.ReactNode }) {
  return <div className="grid grid-cols-2 gap-2">{children}</div>;
}

const ROW_INPUT =
  "border border-gray-300 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:border-brand-green";
const ROW_LABEL = "text-[10px] text-gray-500 uppercase tracking-wider";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className={ROW_LABEL}>{label}</span>
      {children}
    </label>
  );
}

// Hook: live-bound value of an SVG attribute that re-reads after every history
// tick (which fires after every applyChanges/undo/redo). Lets controlled inputs
// pick up DOM mutations the user didn't directly type.
function useLiveProp(element: SVGElement, key: PropKey, tick: number) {
  const [val, setVal] = useState(() => readVal(element, key));
  useEffect(() => {
    setVal(readVal(element, key));
  }, [element, key, tick]);
  return [val, setVal] as const;
}

function NumberRow({
  label,
  k,
  element,
  tick,
  apply,
  min,
  max,
  step,
}: {
  label: string;
  k: PropKey;
  element: SVGElement;
  tick: number;
  apply: ApplyAttr;
  min?: number;
  max?: number;
  step?: number;
}) {
  const [val, setVal] = useLiveProp(element, k, tick);
  // Strip trailing units so <input type="number"> can render the value;
  // we always write back unitless.
  const num = (val.match(/^-?[\d.]+/)?.[0]) ?? "";
  return (
    <Row label={label}>
      <input
        type="number"
        value={num}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          setVal(e.target.value);
          apply(k, e.target.value);
        }}
        className={ROW_INPUT}
        placeholder="—"
      />
    </Row>
  );
}

function OpacityRow({
  element,
  tick,
  apply,
}: {
  element: SVGElement;
  tick: number;
  apply: ApplyAttr;
}) {
  const [val, setVal] = useLiveProp(element, "opacity", tick);
  const num = parseFloat(val);
  const safe = Number.isFinite(num) ? Math.min(1, Math.max(0, num)) : 1;
  return (
    <Row label="Opacity">
      <div className="flex items-center gap-2">
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={safe}
          onChange={(e) => {
            setVal(e.target.value);
            apply("opacity", e.target.value);
          }}
          className="flex-1 accent-brand-green"
        />
        <input
          type="number"
          value={val}
          min={0}
          max={1}
          step={0.05}
          onChange={(e) => {
            setVal(e.target.value);
            apply("opacity", e.target.value);
          }}
          className={`${ROW_INPUT} w-16`}
          placeholder="1"
        />
      </div>
    </Row>
  );
}

const QUICK_SWATCHES = [
  "#000000", "#FFFFFF", "#6F8E3F", "#C0202F", "#F5E5C4",
  "#E8A78A", "#B8462A", "#3F6FAA", "#888888", "transparent",
];

function ColorRow({
  label,
  k,
  element,
  tick,
  apply,
}: {
  label: string;
  k: PropKey;
  element: SVGElement;
  tick: number;
  apply: ApplyAttr;
}) {
  const [val, setVal] = useLiveProp(element, k, tick);
  const hex = toHex(val) ?? "#000000";
  const set = (v: string) => {
    setVal(v);
    apply(k, v);
  };
  return (
    <Row label={label}>
      <div className="flex gap-1 items-center">
        <input
          type="color"
          value={hex}
          onChange={(e) => set(e.target.value)}
          className="w-8 h-7 p-0 border border-gray-300 rounded cursor-pointer bg-white"
          title={val || "—"}
        />
        <input
          value={val}
          onChange={(e) => set(e.target.value)}
          className={`${ROW_INPUT} flex-1 min-w-0`}
          placeholder="none"
        />
      </div>
      <div className="flex flex-wrap gap-1 mt-1">
        {QUICK_SWATCHES.map((c) => (
          <button
            key={c}
            onClick={() => set(c)}
            className={`w-4 h-4 rounded border ${
              c === "transparent"
                ? "border-gray-300 bg-[linear-gradient(45deg,#fff_25%,#ddd_25%,#ddd_50%,#fff_50%,#fff_75%,#ddd_75%)] bg-[length:6px_6px]"
                : "border-gray-300"
            }`}
            style={c === "transparent" ? undefined : { background: c }}
            title={c}
          />
        ))}
      </div>
    </Row>
  );
}

const FONT_FAMILIES = [
  "Inter, system-ui, sans-serif",
  "system-ui, sans-serif",
  "Helvetica, Arial, sans-serif",
  "Arial, sans-serif",
  "Georgia, serif",
  "'Times New Roman', Times, serif",
  "Menlo, monospace",
  "'Courier New', monospace",
];

function FontFamilyRow({
  element,
  tick,
  apply,
}: {
  element: SVGElement;
  tick: number;
  apply: ApplyAttr;
}) {
  const [val, setVal] = useLiveProp(element, "font-family", tick);
  const inList = FONT_FAMILIES.includes(val);
  return (
    <Row label="Font">
      <select
        value={inList ? val : "__custom__"}
        onChange={(e) => {
          if (e.target.value === "__custom__") return;
          setVal(e.target.value);
          apply("font-family", e.target.value);
        }}
        className={`${ROW_INPUT} bg-white`}
      >
        {!inList && (
          <option value="__custom__">{val ? `Custom: ${truncate(val, 28)}` : "—"}</option>
        )}
        {FONT_FAMILIES.map((f) => (
          <option key={f} value={f}>
            {f}
          </option>
        ))}
      </select>
      <input
        value={val}
        onChange={(e) => {
          setVal(e.target.value);
          apply("font-family", e.target.value);
        }}
        className={`${ROW_INPUT} mt-1`}
        placeholder="custom font stack…"
      />
    </Row>
  );
}

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

const FONT_WEIGHTS = [
  "normal",
  "bold",
  "100",
  "200",
  "300",
  "400",
  "500",
  "600",
  "700",
  "800",
  "900",
];

function FontWeightRow({
  element,
  tick,
  apply,
}: {
  element: SVGElement;
  tick: number;
  apply: ApplyAttr;
}) {
  const [val, setVal] = useLiveProp(element, "font-weight", tick);
  const inList = FONT_WEIGHTS.includes(val);
  return (
    <Row label="Weight">
      <select
        value={val}
        onChange={(e) => {
          setVal(e.target.value);
          apply("font-weight", e.target.value);
        }}
        className={`${ROW_INPUT} bg-white`}
      >
        <option value=""></option>
        {!inList && val && <option value={val}>{val}</option>}
        {FONT_WEIGHTS.map((w) => (
          <option key={w} value={w}>
            {w}
          </option>
        ))}
      </select>
    </Row>
  );
}

// Bold / Italic / Underline toggles. They write the corresponding CSS prop
// (with cascade) so child <tspan>s pick the change up immediately.
function StyleToggleRow({
  element,
  tick,
  apply,
}: {
  element: SVGElement;
  tick: number;
  apply: ApplyAttr;
}) {
  const [weight] = useLiveProp(element, "font-weight", tick);
  const [style] = useLiveProp(element, "font-style", tick);
  const [decoration] = useLiveProp(element, "text-decoration", tick);

  const isBold = isWeightBold(weight);
  const isItalic = /italic|oblique/i.test(style);
  const isUnderline = /underline/i.test(decoration);

  const btn = (active: boolean) =>
    `flex-1 px-2 py-1 text-xs font-semibold border rounded ${
      active
        ? "bg-brand-green text-white border-brand-green"
        : "bg-white text-gray-700 border-gray-300 hover:bg-gray-50"
    }`;

  return (
    <div className="flex gap-1">
      <button
        onClick={() => apply("font-weight", isBold ? "normal" : "700")}
        className={btn(isBold)}
        title="Bold (⌘B)"
      >
        <span className="font-bold">B</span>
      </button>
      <button
        onClick={() => apply("font-style", isItalic ? "normal" : "italic")}
        className={btn(isItalic)}
        title="Italic (⌘I)"
      >
        <span className="italic">I</span>
      </button>
      <button
        onClick={() => apply("text-decoration", isUnderline ? "none" : "underline")}
        className={btn(isUnderline)}
        title="Underline (⌘U)"
      >
        <span className="underline">U</span>
      </button>
    </div>
  );
}

function isWeightBold(v: string): boolean {
  if (!v) return false;
  if (/^bold(er)?$/i.test(v.trim())) return true;
  const n = parseInt(v, 10);
  return Number.isFinite(n) && n >= 600;
}

const TEXT_ANCHORS = [
  { value: "start", label: "L" },
  { value: "middle", label: "C" },
  { value: "end", label: "R" },
];

function TextAlignRow({
  element,
  tick,
  apply,
}: {
  element: SVGElement;
  tick: number;
  apply: ApplyAttr;
}) {
  const [val] = useLiveProp(element, "text-anchor", tick);
  return (
    <Row label="Align">
      <div className="flex gap-1">
        {TEXT_ANCHORS.map((a) => {
          const active = val === a.value;
          return (
            <button
              key={a.value}
              onClick={() => apply("text-anchor", a.value)}
              className={`flex-1 px-2 py-1 text-xs border rounded ${
                active
                  ? "bg-brand-green text-white border-brand-green"
                  : "bg-white text-gray-700 border-gray-300 hover:bg-gray-50"
              }`}
              title={`text-anchor: ${a.value}`}
            >
              {a.label}
            </button>
          );
        })}
      </div>
    </Row>
  );
}

// Inline "Ask AI" composer at the top of the property panel. The user types a
// plain-English instruction; we hand it (plus structured element context) to
// the chat textarea. The user reviews the draft in chat before sending so they
// can tweak the wording, drop the XML block, etc.
function AskAiSection({ onSend }: { onSend: (instruction: string) => void }) {
  const [text, setText] = useState("");
  const send = () => {
    const t = text.trim();
    if (!t) return;
    onSend(t);
    setText("");
  };
  return (
    <div className="rounded-md border border-brand-green/30 bg-brand-green/5 p-2 flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-brand-green">
        <span>💬</span>
        <span>Ask AI to edit</span>
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            send();
          }
        }}
        placeholder="e.g. make this title bolder and red"
        rows={2}
        className="w-full text-xs bg-white border border-gray-200 rounded px-2 py-1 resize-none focus:outline-none focus:border-brand-green"
      />
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-gray-400">⌘/Ctrl+Enter to send</span>
        <button
          onClick={send}
          disabled={!text.trim()}
          className="text-[11px] px-2 py-0.5 rounded bg-brand-green text-white disabled:opacity-40"
        >
          Send to chat →
        </button>
      </div>
    </div>
  );
}

// Stable identity for an SVG element so React can reset child component state
// (the AskAiSection draft) when the selection changes.
function selectedKey(el: SVGElement): string {
  const id = el.getAttribute("id");
  if (id) return `id:${id}`;
  // Fall back to tag + position in parent — close enough; we just want this to
  // change when the selection changes to a different node.
  let path = "";
  let cur: Element | null = el;
  let depth = 0;
  while (cur && depth < 6) {
    const p: Element | null = cur.parentElement;
    if (!p) break;
    const idx = Array.prototype.indexOf.call(p.children, cur);
    path = `${cur.tagName.toLowerCase()}[${idx}]/${path}`;
    cur = p;
    depth++;
  }
  return path;
}

// Build the structured chat draft that gets pushed into the textarea. Includes
// enough context (filename + element identity + bbox + key style + truncated
// outerHTML) for the agent to find and rewrite the right node when it edits
// the SVG file.
function buildElementPrompt(el: SVGElement, file: string, instruction: string): string {
  const tag = el.tagName.toLowerCase();
  const id = el.getAttribute("id");
  const cls = el.getAttribute("class");

  let bbox = "";
  try {
    const bb = (el as SVGGraphicsElement).getBBox?.();
    if (bb) {
      bbox = `x=${round(bb.x)} y=${round(bb.y)} w=${round(bb.width)} h=${round(bb.height)} (SVG units)`;
    }
  } catch {
    /* getBBox can throw on non-renderable nodes */
  }

  const styleKeys: PropKey[] = [
    "fill", "stroke", "stroke-width", "opacity",
    "font-size", "font-family", "font-weight", "font-style",
    "text-anchor", "text-decoration",
  ];
  const styleParts = styleKeys
    .map((k) => {
      const v = readVal(el, k);
      return v ? `${k}=${v}` : "";
    })
    .filter(Boolean)
    .join(", ");

  const isText = tag === "text" || tag === "tspan";
  const content = isText ? (el.textContent ?? "").replace(/\s+/g, " ").trim() : "";

  const xml = (() => {
    const s = (el as any).outerHTML || "";
    return s.length > 800 ? s.slice(0, 800) + "…" : s;
  })();

  const lines: string[] = [];
  lines.push(`Please edit slide \`${file}\`.`);
  lines.push("");
  lines.push(`Element: <${tag}${id ? ` id="${id}"` : ""}${cls ? ` class="${cls}"` : ""}>`);
  if (bbox) lines.push(`Position: ${bbox}`);
  if (styleParts) lines.push(`Style: ${styleParts}`);
  if (content) lines.push(`Content: ${JSON.stringify(content.slice(0, 240))}`);
  if (xml) {
    lines.push("");
    lines.push("Current XML:");
    lines.push("```svg");
    lines.push(xml);
    lines.push("```");
  }
  lines.push("");
  lines.push(`Request: ${instruction}`);
  return lines.join("\n");
}

function round(n: number): number {
  return Math.round(n * 100) / 100;
}

function toHex(v: string): string | null {
  if (!v) return null;
  if (/^#[0-9a-f]{6}$/i.test(v)) return v.toLowerCase();
  if (/^#[0-9a-f]{3}$/i.test(v))
    return ("#" + v[1] + v[1] + v[2] + v[2] + v[3] + v[3]).toLowerCase();
  try {
    const cv = document.createElement("canvas");
    cv.width = cv.height = 1;
    const ctx = cv.getContext("2d");
    if (!ctx) return null;
    ctx.fillStyle = "#000";
    ctx.fillStyle = v;
    const styled = String(ctx.fillStyle);
    if (styled.startsWith("#")) return styled.toLowerCase();
    const m = styled.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (m) {
      const hx = (n: number) => n.toString(16).padStart(2, "0");
      return ("#" + hx(+m[1]) + hx(+m[2]) + hx(+m[3])).toLowerCase();
    }
  } catch {
    /* ignore */
  }
  return null;
}
