import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useSession } from "../SessionContext";
import type { AgentEvent } from "../hooks/useAgentStream";
import { projectApi } from "../api/projectUrls";

type Upload = {
  key: string;
  name: string;
  status: "uploading" | "done" | "error";
  imported?: string;
  error?: string;
};

type AttachedFile = {
  key: string;
  file: File;
  relpath: string;
};

type PermissionMode = "auto" | "confirm";
type ModelTier = "auto" | "light" | "general" | "premium";

// `autoSend` triggers an immediate dispatch (Export button etc.) instead of
// the default behavior of prefilling the textarea for the user to review.
type ComposeRequest = { text: string; nonce: number; autoSend?: boolean };

type ProjectRef = { name: string; slides?: number; mtime?: number };

type Props = {
  sessionId: string;
  permissionMode: PermissionMode;
  modelTier: ModelTier;
  tenant: string;
  currentProject: string;
  projects: ProjectRef[];
  onChangeMode: (m: PermissionMode) => void;
  onChangeTier: (t: ModelTier) => void;
  composeRequest?: ComposeRequest | null;
  onComposeConsumed?: () => void;
};

const USER_PREFIX = "🧑 ";

const TIER_LABELS: Record<ModelTier, { short: string; full: string; hint: string }> = {
  auto: { short: "Auto", full: "Auto", hint: "Let the system pick" },
  light: { short: "Light", full: "Light · Haiku", hint: "Fastest, cheapest" },
  general: { short: "General", full: "General · Sonnet", hint: "Balanced default" },
  premium: { short: "Premium", full: "Premium · Opus", hint: "Best reasoning" },
};

export function ChatPanel({
  sessionId,
  permissionMode,
  modelTier,
  tenant,
  currentProject,
  projects,
  onChangeMode,
  onChangeTier,
  composeRequest,
  onComposeConsumed,
}: Props) {
  const { events, streaming, send, interrupt, respondPermission } = useSession();
  const [input, setInput] = useState("");
  const [sessionCost, setSessionCost] = useState<{
    cost_usd: string;
    model: string;
    ceiling_usd: string;
  } | null>(null);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [attached, setAttached] = useState<AttachedFile[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showTier, setShowTier] = useState(false);
  const [showUrl, setShowUrl] = useState(false);
  const [urlValue, setUrlValue] = useState("");
  const [urlBusy, setUrlBusy] = useState(false);
  const [showRefs, setShowRefs] = useState(false);
  const [refSearch, setRefSearch] = useState("");
  // Names of sibling projects the user has chosen to reference for design
  // patterns / inspiration. Cleared on send (one-shot, like attachments).
  const [refs, setRefs] = useState<string[]>([]);
  // Pending requests the user typed while the agent was still streaming.
  // Drained automatically (one per turn) once `streaming` flips false.
  const [queue, setQueue] = useState<string[]>([]);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);
  const settingsRef = useRef<HTMLDivElement>(null);
  const tierRef = useRef<HTMLDivElement>(null);
  const urlRef = useRef<HTMLDivElement>(null);
  const refsRef = useRef<HTMLDivElement>(null);
  // Auto-scroll plumbing. We pin to the bottom while the user is "near the
  // bottom" — if they scroll up to read history, we stop following so we
  // don't yank them away from what they're reading.
  const scrollerRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

  // Auto-grow textarea up to ~8 rows.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [input]);

  // Pin to bottom unless the user has scrolled up to read history. We
  // re-check `atBottom` on every scroll event; the scroll-to-bottom effect
  // below only fires when `atBottom` is still true.
  function onScroll() {
    const el = scrollerRef.current;
    if (!el) return;
    const slack = 64; // ~one short message
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < slack;
  }

  // Follow the tail as new events stream in. If the user scrolled up, we
  // leave them alone; we only re-pin once they scroll back down.
  useEffect(() => {
    if (!atBottomRef.current) return;
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [events.length, streaming]);

  // Per-session cost counter. Refresh on mount and whenever a turn completes
  // (streaming flips true → false). The backend tallies usage_events for sid.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const r = await fetch(`/api/sessions/${sessionId}/cost`);
        if (!r.ok) return;
        const j = await r.json();
        if (!cancelled) {
          setSessionCost({
            cost_usd: j.cost_usd ?? "0",
            model: j.model ?? "",
            ceiling_usd: j.ceiling_usd ?? "0",
          });
        }
      } catch {
        /* best-effort */
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [sessionId, streaming]);

  // SVG editor → chat handoff: the slide editor builds a context-rich draft
  // ("element X at … please …") and bumps a nonce. We append (or replace if
  // the textarea is empty) and focus, so the user reviews before sending.
  //
  // `autoSend` skips that review step — used by the Export button so a single
  // click both authors the prompt and dispatches it.
  useEffect(() => {
    if (!composeRequest) return;
    const incoming = composeRequest.text;
    if (composeRequest.autoSend) {
      // Don't touch the textarea state — preserve whatever the user was
      // typing. Dispatch directly with no attachments/refs.
      atBottomRef.current = true;
      if (streaming) {
        // Queue behind whatever's already running; the queue drain effect
        // will pick it up exactly like a manually-typed message.
        setQueue((q) => [...q, incoming]);
      } else {
        void dispatchMessage(incoming, [], []);
      }
      onComposeConsumed?.();
      return;
    }
    setInput((prev) => (prev.trim() ? prev.trimEnd() + "\n\n" + incoming : incoming));
    requestAnimationFrame(() => {
      const ta = taRef.current;
      if (!ta) return;
      ta.focus();
      const len = ta.value.length;
      ta.setSelectionRange(len, len);
      ta.scrollTop = ta.scrollHeight;
    });
    onComposeConsumed?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composeRequest?.nonce]);

  // Click-outside dismiss for the popovers.
  useEffect(() => {
    if (!showSettings && !showTier && !showUrl && !showRefs) return;
    function onDoc(e: MouseEvent) {
      const t = e.target as Node;
      if (showSettings && settingsRef.current && !settingsRef.current.contains(t)) {
        setShowSettings(false);
      }
      if (showTier && tierRef.current && !tierRef.current.contains(t)) {
        setShowTier(false);
      }
      if (showUrl && urlRef.current && !urlRef.current.contains(t)) {
        setShowUrl(false);
      }
      if (showRefs && refsRef.current && !refsRef.current.contains(t)) {
        setShowRefs(false);
      }
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [showSettings, showTier, showUrl, showRefs]);

  function queueAttached(files: { file: File; relpath: string }[]) {
    if (files.length === 0) return;
    setAttached((prev) => [
      ...prev,
      ...files.map((f, i) => ({ key: `${Date.now()}-${i}-${f.file.name}`, ...f })),
    ]);
  }

  async function uploadFiles(items: { file: File; relpath: string }[]) {
    if (items.length === 0) return [] as Upload[];
    const startKey = Date.now();
    const fresh: Upload[] = items.map((it, i) => ({
      key: `${startKey}-${i}-${it.relpath}`,
      name: it.relpath,
      status: "uploading",
    }));
    setUploads((prev) => [...prev, ...fresh]);

    const fd = new FormData();
    for (const it of items) {
      fd.append("files", it.file, it.relpath.split("/").pop() || it.file.name);
      fd.append("paths", it.relpath);
    }
    try {
      const res = await fetch(`/api/sessions/${sessionId}/import/files`, {
        method: "POST",
        body: fd,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "import failed");
      const importedByName = new Map<string, string>();
      for (const r of data.imported ?? []) importedByName.set(r.name, r.imported);
      const errorByName = new Map<string, string>();
      for (const r of data.errors ?? []) errorByName.set(r.name, r.error);
      setUploads((prev) =>
        prev.map((u) => {
          if (!fresh.find((f) => f.key === u.key)) return u;
          if (importedByName.has(u.name))
            return { ...u, status: "done", imported: importedByName.get(u.name) };
          if (errorByName.has(u.name))
            return { ...u, status: "error", error: errorByName.get(u.name) };
          return { ...u, status: "error", error: "unknown" };
        }),
      );
      return fresh.map((u) => ({
        ...u,
        status: importedByName.has(u.name) ? "done" : "error",
        imported: importedByName.get(u.name),
        error: errorByName.get(u.name),
      })) as Upload[];
    } catch (e: any) {
      const msg = String(e.message || e);
      setUploads((prev) =>
        prev.map((u) =>
          fresh.find((f) => f.key === u.key) ? { ...u, status: "error", error: msg } : u,
        ),
      );
      return fresh.map((u) => ({ ...u, status: "error" as const, error: msg }));
    }
  }

  async function importUrl(raw: string) {
    const url = raw.trim();
    if (!url) return;
    const key = `url-${Date.now()}`;
    setUploads((prev) => [...prev, { key, name: url, status: "uploading" }]);
    setUrlBusy(true);
    try {
      const res = await fetch(`/api/sessions/${sessionId}/import/url`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "import failed");
      setUploads((prev) =>
        prev.map((u) => (u.key === key ? { ...u, status: "done", imported: data.imported } : u)),
      );
      setUrlValue("");
      setShowUrl(false);
    } catch (e: any) {
      setUploads((prev) =>
        prev.map((u) =>
          u.key === key ? { ...u, status: "error", error: String(e.message || e) } : u,
        ),
      );
    } finally {
      setUrlBusy(false);
    }
  }

  // Build the final outbound message (uploads → "Attached:…" preface + text)
  // and dispatch via `send`. Hoisted out of submit() so the queue drain
  // effect can reuse the same path without going through the form handler.
  async function dispatchMessage(
    text: string,
    queuedAttachments: AttachedFile[],
    queuedRefs: string[],
  ) {
    let preface = "";
    // Tell the agent which working dir the user is looking at. The Workbench
    // also POSTs /output-dirs/current when the user picks a deck, so the two
    // stay in sync — but read at send time so a deck flip just before submit
    // still wins. Best-effort; if the directive 404s we just send without it.
    try {
      const res = await fetch(
        `${projectApi(tenant, currentProject)}/output-dirs`,
      );
      if (res.ok) {
        const data = await res.json();
        const cur = String(data.current || "").trim();
        if (cur) preface += `[output dir: ${cur}]\n\n`;
      }
    } catch {
      /* non-fatal — fall through */
    }
    if (queuedRefs.length > 0) {
      preface +=
        "References (read-only — sibling projects you may consult for design " +
        "patterns / inspiration; do not edit anything outside the active " +
        "project):\n" +
        queuedRefs.map((n) => `- ../${n}/`).join("\n") +
        "\n\n";
    }
    if (queuedAttachments.length > 0) {
      const results = await uploadFiles(queuedAttachments);
      const ok = results.filter((r) => r.status === "done");
      const bad = results.filter((r) => r.status === "error");
      if (ok.length > 0) {
        preface +=
          `Attached ${ok.length} file${ok.length > 1 ? "s" : ""}:\n` +
          ok.map((u) => `- ${u.imported}`).join("\n") +
          (bad.length > 0
            ? `\n\nFailed: ${bad.map((u) => u.name).join(", ")}`
            : "") +
          "\n\n";
      } else if (bad.length > 0) {
        preface += `Upload failed: ${bad.map((u) => `${u.name} (${u.error})`).join(", ")}\n\n`;
      }
    }
    const composed = (preface + text).trim();
    if (composed) send(composed);
  }

  function submit(e?: React.FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    const hasAttached = attached.length > 0;
    if (!text && !hasAttached) return;

    // Pressing Send always pins us to the bottom — the user just typed,
    // they want to see what comes back.
    atBottomRef.current = true;

    const queuedAttachments = attached;
    const queuedRefs = refs;
    setAttached([]);
    setRefs([]);
    setInput("");

    if (streaming) {
      // Claude-CLI-style queueing: keep the input flow free, drain when
      // the current turn finishes. Attachments and refs only ride along
      // with the FIRST queued message — chaining them per queued item
      // would be confusing (the user picked them once).
      if (queuedAttachments.length > 0) setAttached(queuedAttachments);
      if (queuedRefs.length > 0) setRefs(queuedRefs);
      setQueue((q) => [...q, text]);
      return;
    }

    void dispatchMessage(text, queuedAttachments, queuedRefs);
  }

  // Drain the queue once the current turn finishes. We pull one item per
  // pass so each queued request gets its own turn (matching the user's
  // mental model of "I queued these, run them in order").
  useEffect(() => {
    if (streaming) return;
    if (queue.length === 0) return;
    const [next, ...rest] = queue;
    setQueue(rest);
    const carriedAttachments = attached;
    const carriedRefs = refs;
    setAttached([]);
    setRefs([]);
    void dispatchMessage(next, carriedAttachments, carriedRefs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streaming, queue]);

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  }

  async function readEntries(
    entry: any,
    base: string,
    out: { file: File; relpath: string }[],
  ) {
    if (entry.isFile) {
      await new Promise<void>((resolve) => {
        entry.file((file: File) => {
          out.push({ file, relpath: base + file.name });
          resolve();
        });
      });
      return;
    }
    if (entry.isDirectory) {
      const reader = entry.createReader();
      const all: any[] = [];
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const batch: any[] = await new Promise((resolve) =>
          reader.readEntries((es: any[]) => resolve(es)),
        );
        if (batch.length === 0) break;
        all.push(...batch);
      }
      for (const e of all) await readEntries(e, base + entry.name + "/", out);
    }
  }

  async function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    const dt = e.dataTransfer;
    const collected: { file: File; relpath: string }[] = [];

    if (dt.items && dt.items.length > 0 && (dt.items[0] as any).webkitGetAsEntry) {
      for (let i = 0; i < dt.items.length; i++) {
        const entry = (dt.items[i] as any).webkitGetAsEntry?.();
        if (!entry) continue;
        await readEntries(entry, "", collected);
      }
    }
    if (collected.length === 0 && dt.files) {
      for (let i = 0; i < dt.files.length; i++) {
        const f = dt.files[i];
        collected.push({ file: f, relpath: f.name });
      }
    }
    if (collected.length > 0) queueAttached(collected);
  }

  function onPaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: { file: File; relpath: string }[] = [];
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) {
          const ext = f.type.split("/")[1] || "png";
          const name = f.name && f.name !== "image.png"
            ? f.name
            : `clipboard-${Date.now()}.${ext}`;
          files.push({ file: f, relpath: name });
        }
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      queueAttached(files);
    }
  }

  function onFileInput(ev: React.ChangeEvent<HTMLInputElement>) {
    const list = ev.target.files;
    if (!list) return;
    const files: { file: File; relpath: string }[] = [];
    for (let i = 0; i < list.length; i++) {
      const f = list[i];
      const rel = (f as any).webkitRelativePath || f.name;
      files.push({ file: f, relpath: rel });
    }
    queueAttached(files);
    ev.target.value = "";
  }

  function removeAttached(key: string) {
    setAttached((prev) => prev.filter((f) => f.key !== key));
  }

  return (
    <div
      className={`flex-1 flex flex-col min-h-0 relative ${
        dragOver ? "ring-2 ring-brand-green ring-inset" : ""
      }`}
      onDragOver={(e) => {
        e.preventDefault();
        if (!dragOver) setDragOver(true);
      }}
      onDragLeave={(e) => {
        if (e.currentTarget.contains(e.relatedTarget as Node)) return;
        setDragOver(false);
      }}
      onDrop={onDrop}
    >
      {dragOver && (
        <div className="absolute inset-0 z-10 pointer-events-none flex items-center justify-center bg-brand-green/5">
          <div className="text-sm text-brand-green font-medium">Drop files or folders to attach</div>
        </div>
      )}

      <div
        ref={scrollerRef}
        onScroll={onScroll}
        className="flex-1 overflow-y-auto px-4 py-3 space-y-2 text-sm"
      >
        {events.length === 0 && (
          <div className="text-gray-400 text-center pt-10">
            Try: <em>"Generate a 5-slide deck on Q1 sales"</em>
          </div>
        )}
        {(() => {
          const blocks = groupEvents(events);
          return blocks.map((b, i) => {
            if (b.kind === "user") {
              return (
                <div
                  key={i}
                  className="bg-brand-cream/60 rounded px-3 py-1.5 whitespace-pre-wrap"
                >
                  {b.text}
                </div>
              );
            }
            if (b.kind === "assistant") {
              return (
                <article key={i} className="prose-md max-w-none">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{b.text}</ReactMarkdown>
                </article>
              );
            }
            if (b.kind === "tools") {
              const isLast = i === blocks.length - 1;
              const anyUnresolved = b.calls.some((c) => c.result === undefined);
              return (
                <ToolCluster
                  key={i}
                  calls={b.calls}
                  busy={streaming && isLast && anyUnresolved}
                />
              );
            }
            if (b.kind === "permission") {
              const evt = b.evt;
              const preview = (() => {
                try {
                  return typeof evt.input === "string"
                    ? evt.input
                    : JSON.stringify(evt.input, null, 2);
                } catch {
                  return String(evt.input);
                }
              })();
              return (
                <div
                  key={i}
                  className="border border-amber-300 bg-amber-50 rounded p-2 space-y-1.5"
                >
                  <div className="text-xs font-medium text-amber-800">
                    Permission requested · <span className="font-mono">{evt.tool}</span>
                  </div>
                  <pre className="text-[11px] bg-white border border-amber-200 rounded p-2 max-h-40 overflow-auto whitespace-pre-wrap font-mono text-gray-700">
                    {preview.length > 1200 ? preview.slice(0, 1200) + "\n…(truncated)" : preview}
                  </pre>
                  {evt.resolved ? (
                    <div className="text-[11px] text-gray-500">
                      {evt.resolved === "approve" ? "✓ approved" : "✗ denied"}
                    </div>
                  ) : (
                    <div className="flex gap-2">
                      <button
                        onClick={() => respondPermission(evt.id, "approve")}
                        className="px-3 py-1 text-xs bg-brand-green text-white rounded hover:opacity-90"
                      >
                        Approve
                      </button>
                      <button
                        onClick={() => respondPermission(evt.id, "deny")}
                        className="px-3 py-1 text-xs bg-white border border-gray-300 text-gray-700 rounded hover:bg-gray-50"
                      >
                        Deny
                      </button>
                    </div>
                  )}
                </div>
              );
            }
            if (b.kind === "error") {
              return (
                <div key={i} className="text-xs text-red-600">
                  ⚠ {b.message}
                </div>
              );
            }
            if (b.kind === "done") {
              return (
                <div key={i} className="text-xs text-gray-300 border-t pt-2">
                  — turn complete —
                </div>
              );
            }
            if (b.kind === "billing") {
              const isBlock = b.level === "block";
              return (
                <div
                  key={i}
                  className={`rounded-md border text-xs px-3 py-2 ${
                    isBlock
                      ? "border-red-300 bg-red-50 text-red-800"
                      : "border-amber-300 bg-amber-50 text-amber-800"
                  }`}
                >
                  <span className="font-medium mr-1">
                    {isBlock ? "Hard cap reached" : "Soft cap crossed"}
                  </span>
                  {b.text}
                </div>
              );
            }
            if (b.kind === "rate_limit") {
              return (
                <RateLimitBanner
                  key={i}
                  reason={b.reason}
                  retryAfterSeconds={b.retryAfterSeconds}
                />
              );
            }
            return null;
          });
        })()}

        {uploads.slice(-4).map((u) => (
          <div key={u.key} className="text-[11px] text-gray-500 font-mono">
            {u.status === "uploading"
              ? `… uploading ${u.name}`
              : u.status === "done"
              ? `✓ imported ${u.imported}`
              : `✗ ${u.name} — ${u.error}`}
          </div>
        ))}
      </div>

      <form
        onSubmit={submit}
        className="px-3 pb-3 pt-2 border-t border-gray-200 bg-gray-50/40"
      >
        <div className="bg-white border border-gray-200 rounded-xl shadow-sm focus-within:border-brand-green focus-within:ring-1 focus-within:ring-brand-green/20 transition">
          {attached.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
              {attached.map((a) => (
                <span
                  key={a.key}
                  className="inline-flex items-center gap-1.5 text-[11px] bg-gray-100 border border-gray-200 rounded px-2 py-0.5 max-w-full"
                  title={a.relpath}
                >
                  <span className="truncate max-w-[180px]">{a.relpath}</span>
                  <button
                    type="button"
                    onClick={() => removeAttached(a.key)}
                    className="text-gray-400 hover:text-brand-red"
                    title="Remove"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}

          {queue.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
              {queue.map((q, i) => (
                <span
                  key={`q-${i}`}
                  className="inline-flex items-center gap-1.5 text-[11px] bg-amber-50 border border-amber-200 text-amber-800 rounded px-2 py-0.5 max-w-full"
                  title={q}
                >
                  <span className="font-mono text-[10px]">queued</span>
                  <span className="truncate max-w-[200px]">{q}</span>
                  <button
                    type="button"
                    onClick={() => setQueue((cur) => cur.filter((_, j) => j !== i))}
                    className="text-amber-600/70 hover:text-brand-red"
                    title="Drop from queue"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}

          {refs.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
              {refs.map((name) => (
                <span
                  key={`ref-${name}`}
                  className="inline-flex items-center gap-1.5 text-[11px] bg-sky-50 border border-sky-200 text-sky-800 rounded px-2 py-0.5 max-w-full"
                  title={`Reference project: ${name}`}
                >
                  <LinkIcon />
                  <span className="truncate max-w-[200px]">{name}</span>
                  <button
                    type="button"
                    onClick={() => setRefs((cur) => cur.filter((n) => n !== name))}
                    className="text-sky-600/70 hover:text-brand-red"
                    title="Remove reference"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}

          <textarea
            ref={taRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            placeholder={
              streaming
                ? "Type while it works — your message will queue…"
                : "Describe what you want to create…"
            }
            rows={3}
            className="w-full bg-transparent px-3 pt-3 pb-1 text-sm resize-none focus:outline-none"
          />

          <div className="flex items-center justify-between gap-2 px-2 pb-2">
            <div className="flex items-center gap-1">
              {/* Settings (auto / confirm) */}
              <div className="relative" ref={settingsRef}>
                <ToolbarIconButton
                  title={`Tool permissions: ${permissionMode}`}
                  onClick={() => {
                    setShowUrl(false);
                    setShowTier(false);
                    setShowSettings((v) => !v);
                  }}
                  active={showSettings}
                >
                  <GearIcon />
                  {permissionMode === "confirm" && (
                    <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-amber-500" />
                  )}
                </ToolbarIconButton>
                {showSettings && (
                  <div className="absolute bottom-full mb-2 left-0 z-20 w-64 bg-white border border-gray-200 rounded-lg shadow-lg p-2 text-xs">
                    <div className="text-[10px] uppercase tracking-wider text-gray-400 px-1 pb-1">
                      Tool permissions
                    </div>
                    <ModeRow
                      active={permissionMode === "auto"}
                      title="Auto-approve"
                      desc="Run tools without asking. Faster."
                      onClick={() => {
                        onChangeMode("auto");
                        setShowSettings(false);
                      }}
                    />
                    <ModeRow
                      active={permissionMode === "confirm"}
                      title="Confirm"
                      desc="Prompt before each tool runs."
                      onClick={() => {
                        onChangeMode("confirm");
                        setShowSettings(false);
                      }}
                    />
                  </div>
                )}
              </div>

              {/* Model tier (Auto / Light / General / Premium) */}
              <div className="relative" ref={tierRef}>
                <button
                  type="button"
                  title={`Model tier: ${TIER_LABELS[modelTier].full} — ${TIER_LABELS[modelTier].hint}`}
                  onClick={() => {
                    setShowSettings(false);
                    setShowUrl(false);
                    setShowTier((v) => !v);
                  }}
                  className={`text-xs px-2.5 py-1.5 border rounded-md transition inline-flex items-center gap-1 ${
                    showTier
                      ? "border-brand-green text-brand-green bg-brand-green/5"
                      : "border-gray-200 text-gray-700 hover:bg-gray-50"
                  }`}
                >
                  <SparkIcon />
                  <span>{TIER_LABELS[modelTier].short}</span>
                </button>
                {showTier && (
                  <div className="absolute bottom-full mb-2 left-0 z-20 w-64 bg-white border border-gray-200 rounded-lg shadow-lg p-2 text-xs">
                    <div className="text-[10px] uppercase tracking-wider text-gray-400 px-1 pb-1">
                      Model tier
                    </div>
                    {(["auto", "light", "general", "premium"] as ModelTier[]).map((t) => (
                      <ModeRow
                        key={t}
                        active={modelTier === t}
                        title={TIER_LABELS[t].full}
                        desc={TIER_LABELS[t].hint}
                        onClick={() => {
                          onChangeTier(t);
                          setShowTier(false);
                        }}
                      />
                    ))}
                    <div className="text-[10px] text-gray-400 px-1 pt-1 leading-snug">
                      Switching tiers reconnects the agent. In-session memory resets.
                    </div>
                  </div>
                )}
              </div>

              {/* Files */}
              <ToolbarIconButton
                title="Attach files"
                onClick={() => fileRef.current?.click()}
              >
                <PaperclipIcon />
              </ToolbarIconButton>

              {/* Folder */}
              <ToolbarIconButton
                title="Attach a folder"
                onClick={() => folderRef.current?.click()}
              >
                <FolderIcon />
              </ToolbarIconButton>

              {/* Reference another project (read-only inspiration) */}
              <div className="relative" ref={refsRef}>
                <ToolbarIconButton
                  title="Reference another project for design patterns / inspiration"
                  onClick={() => {
                    setShowSettings(false);
                    setShowTier(false);
                    setShowUrl(false);
                    setShowRefs((v) => !v);
                  }}
                  active={showRefs}
                >
                  <LinkIcon />
                  {refs.length > 0 && (
                    <span className="absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] px-1 rounded-full bg-sky-500 text-white text-[9px] leading-[14px] text-center">
                      {refs.length}
                    </span>
                  )}
                </ToolbarIconButton>
                {showRefs && (
                  <div className="absolute bottom-full mb-2 left-0 z-20 w-80 bg-white border border-gray-200 rounded-lg shadow-lg p-2 text-xs space-y-2">
                    <div className="text-[10px] uppercase tracking-wider text-gray-400 px-1">
                      Reference another project
                    </div>
                    <input
                      autoFocus
                      value={refSearch}
                      onChange={(e) => setRefSearch(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Escape") setShowRefs(false);
                      }}
                      placeholder="Search projects…"
                      className="w-full border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:border-brand-green"
                    />
                    <div className="max-h-56 overflow-y-auto -mx-1 px-1">
                      {(() => {
                        const q = refSearch.trim().toLowerCase();
                        const others = projects
                          .filter((p) => p.name !== currentProject)
                          .filter((p) => !q || p.name.toLowerCase().includes(q))
                          .slice()
                          .sort((a, b) => (b.mtime ?? 0) - (a.mtime ?? 0));
                        if (others.length === 0) {
                          return (
                            <div className="text-[11px] text-gray-400 italic px-2 py-3 text-center">
                              {q
                                ? `No projects match "${refSearch}".`
                                : "No other projects to reference yet."}
                            </div>
                          );
                        }
                        return others.map((p) => {
                          const picked = refs.includes(p.name);
                          return (
                            <button
                              key={p.name}
                              type="button"
                              onClick={() => {
                                setRefs((cur) =>
                                  picked
                                    ? cur.filter((n) => n !== p.name)
                                    : [...cur, p.name],
                                );
                              }}
                              className={`w-full text-left flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 ${
                                picked ? "bg-sky-50/60" : ""
                              }`}
                            >
                              <span
                                className={`w-3.5 h-3.5 rounded border flex items-center justify-center shrink-0 ${
                                  picked
                                    ? "bg-sky-500 border-sky-500 text-white"
                                    : "border-gray-300"
                                }`}
                              >
                                {picked && (
                                  <svg viewBox="0 0 16 16" className="w-2.5 h-2.5" fill="none">
                                    <path
                                      d="M3 8.5l3 3 7-7"
                                      stroke="currentColor"
                                      strokeWidth="2"
                                      strokeLinecap="round"
                                      strokeLinejoin="round"
                                    />
                                  </svg>
                                )}
                              </span>
                              <span className="truncate flex-1">{p.name}</span>
                              {typeof p.slides === "number" && p.slides > 0 && (
                                <span className="text-[10px] text-gray-400 shrink-0">
                                  {p.slides} slides
                                </span>
                              )}
                            </button>
                          );
                        });
                      })()}
                    </div>
                    <div className="text-[10px] text-gray-400 px-1 leading-snug">
                      The agent gets read-only paths like{" "}
                      <span className="font-mono">../&lt;name&gt;/</span>. It won't
                      edit anything outside the active project.
                    </div>
                  </div>
                )}
              </div>

              {/* Import URL */}
              <div className="relative" ref={urlRef}>
                <button
                  type="button"
                  onClick={() => {
                    setShowSettings(false);
                    setShowTier(false);
                    setShowUrl((v) => !v);
                  }}
                  className={`text-xs px-2.5 py-1.5 border rounded-md transition ${
                    showUrl
                      ? "border-brand-green text-brand-green bg-brand-green/5"
                      : "border-gray-200 text-gray-700 hover:bg-gray-50"
                  }`}
                  title="Import from URL"
                >
                  Import
                </button>
                {showUrl && (
                  <div className="absolute bottom-full mb-2 left-0 z-20 w-80 bg-white border border-gray-200 rounded-lg shadow-lg p-2 text-xs space-y-2">
                    <div className="text-[10px] uppercase tracking-wider text-gray-400 px-1">
                      Import from URL
                    </div>
                    <div className="flex gap-1.5">
                      <input
                        autoFocus
                        value={urlValue}
                        onChange={(e) => setUrlValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            importUrl(urlValue);
                          } else if (e.key === "Escape") {
                            setShowUrl(false);
                          }
                        }}
                        placeholder="https://… (page or PDF)"
                        disabled={urlBusy}
                        className="flex-1 border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:border-brand-green"
                      />
                      <button
                        type="button"
                        onClick={() => importUrl(urlValue)}
                        disabled={urlBusy || !urlValue.trim()}
                        className="text-xs px-2.5 py-1 bg-brand-red text-white rounded disabled:opacity-50"
                      >
                        {urlBusy ? "…" : "Fetch"}
                      </button>
                    </div>
                    <div className="text-[10px] text-gray-400 px-1">
                      The agent imports it into the project's <span className="font-mono">sources/</span>.
                    </div>
                  </div>
                )}
              </div>

              {sessionCost && (
                <span
                  title={`This session · model ${
                    sessionCost.model || "auto"
                  } · ceiling $${fmt2(sessionCost.ceiling_usd)}`}
                  className="ml-1 inline-flex items-center gap-1 text-[11px] text-gray-500 px-2 py-1 rounded-md bg-gray-50 border border-gray-200"
                >
                  <span className="font-mono">${fmt2(sessionCost.cost_usd)}</span>
                  <span className="text-gray-400">·</span>
                  <span>{modelShort(sessionCost.model)}</span>
                </span>
              )}
            </div>

            {(() => {
              const hasInput = !!input.trim() || attached.length > 0;
              if (streaming) {
                // Stop is always available while the agent is working — even
                // when the user has queued more (or has stashed attachments
                // riding along with a queued item). Without this, queueing a
                // message with attachments would leave the user no way to
                // interrupt until the queue drained.
                return (
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => interrupt()}
                      title="Stop generating"
                      className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-gray-800 text-white rounded-md text-sm hover:bg-black"
                    >
                      <StopIcon />
                      Stop
                    </button>
                    {hasInput && (
                      <button
                        type="submit"
                        title="Queue this message — will run after the current turn"
                        className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-amber-500 text-white rounded-md text-sm hover:bg-amber-600"
                      >
                        <SendIcon />
                        Queue
                      </button>
                    )}
                  </div>
                );
              }
              return (
                <button
                  type="submit"
                  disabled={!hasInput}
                  className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-brand-red text-white rounded-md text-sm disabled:opacity-50 hover:opacity-95"
                >
                  <SendIcon />
                  Send
                </button>
              );
            })()}
          </div>
        </div>

        <input
          ref={fileRef}
          type="file"
          multiple
          className="hidden"
          onChange={onFileInput}
        />
        <input
          ref={folderRef}
          type="file"
          // @ts-expect-error — non-standard but supported by Chromium/Safari
          webkitdirectory=""
          directory=""
          multiple
          className="hidden"
          onChange={onFileInput}
        />

        <div className="flex justify-between text-[10px] text-gray-400 px-1 pt-1">
          <span>Drop / paste images · ⌘/Ctrl+Enter to send</span>
          <span>
            {TIER_LABELS[modelTier].short} ·{" "}
            {permissionMode === "auto" ? "Auto-approve" : "Confirm each tool"}
            {streaming ? " · Working…" : ""}
          </span>
        </div>
      </form>
    </div>
  );
}

function ToolbarIconButton({
  title,
  onClick,
  active = false,
  children,
}: {
  title: string;
  onClick: () => void;
  active?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={`relative inline-flex items-center justify-center w-8 h-8 rounded-md border transition ${
        active
          ? "border-brand-green text-brand-green bg-brand-green/5"
          : "border-gray-200 text-gray-600 hover:bg-gray-50"
      }`}
    >
      {children}
    </button>
  );
}

function ModeRow({
  active,
  title,
  desc,
  onClick,
}: {
  active: boolean;
  title: string;
  desc: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left px-2 py-1.5 rounded ${
        active ? "bg-brand-green/10 text-brand-green" : "hover:bg-gray-50 text-gray-700"
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`w-2.5 h-2.5 rounded-full border ${
            active ? "border-brand-green bg-brand-green" : "border-gray-300"
          }`}
        />
        <span className="text-sm font-medium">{title}</span>
      </div>
      <div className="text-[11px] text-gray-500 pl-5 leading-snug">{desc}</div>
    </button>
  );
}

function GearIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

function PaperclipIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 17.93 8.83l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
    </svg>
  );
}

function FolderIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M3 12l18-9-9 18-2-7-7-2z" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
    </svg>
  );
}

function SparkIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v3M12 18v3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M3 12h3M18 12h3M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1" />
    </svg>
  );
}

function LinkIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 1 0-7.07-7.07l-1.5 1.5" />
      <path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 1 0 7.07 7.07l1.5-1.5" />
    </svg>
  );
}

// ── Tool clustering ──────────────────────────────────────────────────────
// The agent fires many small tool calls per turn (Read, Bash, Edit…). The
// raw stream is noisy, so we collapse consecutive tool_use events into a
// single accordion row that summarizes them ("Reading ×3 · Running ×2"),
// pulses while the latest call is still in flight, and folds out per-call
// detail on click. Permission/error/done events break a cluster.

type ToolCall = {
  id: string;
  name: string;
  input: unknown;
  result?: string;
};

type RenderBlock =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string }
  | { kind: "tools"; calls: ToolCall[] }
  | {
      kind: "permission";
      evt: Extract<AgentEvent, { type: "permission_request" }>;
    }
  | { kind: "error"; message: string }
  | { kind: "done" }
  | { kind: "billing"; level: "warn" | "block"; text: string }
  | { kind: "rate_limit"; reason: string; retryAfterSeconds: number };

function groupEvents(events: AgentEvent[]): RenderBlock[] {
  const blocks: RenderBlock[] = [];
  let cluster: ToolCall[] | null = null;

  const flush = () => {
    if (cluster && cluster.length > 0) {
      blocks.push({ kind: "tools", calls: cluster });
    }
    cluster = null;
  };

  const foldResult = (toolUseId: string, content: string) => {
    if (cluster) {
      const c = cluster.find((c) => c.id === toolUseId);
      if (c) {
        c.result = content;
        return;
      }
    }
    for (let b = blocks.length - 1; b >= 0; b--) {
      const blk = blocks[b];
      if (blk.kind === "tools") {
        const c = blk.calls.find((c) => c.id === toolUseId);
        if (c) {
          c.result = content;
          return;
        }
      }
    }
  };

  for (const evt of events) {
    if (evt.type === "tool_use") {
      if (!cluster) cluster = [];
      cluster.push({ id: evt.id, name: evt.name, input: evt.input });
      continue;
    }
    if (evt.type === "tool_result") {
      foldResult(evt.tool_use_id, evt.content);
      continue;
    }
    flush();
    if (evt.type === "text") {
      const isUser = evt.text.startsWith(USER_PREFIX);
      blocks.push({
        kind: isUser ? "user" : "assistant",
        text: isUser ? evt.text.slice(USER_PREFIX.length) : evt.text,
      });
    } else if (evt.type === "permission_request") {
      blocks.push({ kind: "permission", evt });
    } else if (evt.type === "error") {
      blocks.push({ kind: "error", message: evt.message });
    } else if (evt.type === "done") {
      blocks.push({ kind: "done" });
    } else if (evt.type === "billing.threshold") {
      const pct = Math.round((evt.threshold_fraction || 0.8) * 100);
      blocks.push({
        kind: "billing",
        level: "warn",
        text: `MTD spend crossed ${pct}% of the $${fmt2(evt.cap_soft_usd)} soft cap (now $${fmt2(evt.mtd_billed_usd)}). Sessions continue; raise the cap or top up to clear.`,
      });
    } else if (evt.type === "billing.cap_exceeded") {
      blocks.push({
        kind: "billing",
        level: "block",
        text: `Billing blocked: MTD $${fmt2(evt.mtd_billed_usd)} reached the $${fmt2(evt.cap_hard_usd)} hard cap. New messages are paused until the cap is raised or credit is added.`,
      });
    } else if (evt.type === "rate_limited") {
      blocks.push({
        kind: "rate_limit",
        reason: evt.reason,
        retryAfterSeconds: Number(evt.retry_after_seconds) || 0,
      });
    }
    // raw events are ignored
  }
  flush();
  return blocks;
}

function fmt2(s: string | number | undefined) {
  const n = typeof s === "number" ? s : parseFloat(s ?? "0");
  if (!Number.isFinite(n)) return "0.00";
  return n.toFixed(2);
}

function RateLimitBanner({
  reason,
  retryAfterSeconds,
}: {
  reason: string;
  retryAfterSeconds: number;
}) {
  // Tick a local countdown rather than sleeping the message bar — the user can
  // still scroll, send other clicks, etc. while waiting. We freeze at 0 once
  // the bucket has notionally refilled; the next /message attempt will either
  // pass or render a fresh banner with a fresh deadline.
  const [remaining, setRemaining] = useState(() =>
    Math.max(0, Math.ceil(retryAfterSeconds)),
  );
  useEffect(() => {
    if (remaining <= 0) return;
    const id = window.setInterval(() => {
      setRemaining((s) => (s <= 1 ? 0 : s - 1));
    }, 1000);
    return () => window.clearInterval(id);
  }, [remaining]);
  const label =
    reason === "cost_per_min_usd"
      ? "Cost rate limit"
      : reason === "req_per_min"
      ? "Request rate limit"
      : "Rate limited";
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 text-amber-800 text-xs px-3 py-2">
      <span className="font-medium mr-1">{label}.</span>
      {remaining > 0 ? (
        <>Try again in <span className="font-mono">{remaining}s</span>.</>
      ) : (
        <>The cap has refilled — send the next message to retry.</>
      )}
    </div>
  );
}

function modelShort(m: string) {
  if (!m) return "Auto";
  const lower = m.toLowerCase();
  if (lower.includes("opus")) return "Opus";
  if (lower.includes("sonnet")) return "Sonnet";
  if (lower.includes("haiku")) return "Haiku";
  return m;
}

const TOOL_LABELS: Record<string, [string, string]> = {
  // [in-progress, completed]
  Read: ["Reading", "Read"],
  NotebookRead: ["Reading", "Read"],
  Write: ["Writing", "Wrote"],
  Edit: ["Editing", "Edited"],
  MultiEdit: ["Editing", "Edited"],
  NotebookEdit: ["Editing", "Edited"],
  Bash: ["Running", "Ran"],
  Glob: ["Searching", "Searched"],
  Grep: ["Searching", "Searched"],
  WebFetch: ["Fetching", "Fetched"],
  WebSearch: ["Searching", "Searched"],
  TodoWrite: ["Planning", "Planned"],
  Task: ["Delegating", "Delegated"],
};

function friendlyLabel(name: string, done: boolean): string {
  const pair = TOOL_LABELS[name];
  if (!pair) return name;
  return done ? pair[1] : pair[0];
}

function inputSummary(input: unknown): string {
  if (!input || typeof input !== "object") {
    return typeof input === "string" ? (input as string) : "";
  }
  const obj = input as Record<string, unknown>;
  const candidate =
    obj.file_path ?? obj.command ?? obj.pattern ?? obj.url ?? obj.path ?? obj.query;
  if (typeof candidate === "string") return candidate;
  try {
    return JSON.stringify(obj);
  } catch {
    return "";
  }
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function ToolCluster({ calls, busy }: { calls: ToolCall[]; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const counts = new Map<string, number>();
  for (const c of calls) {
    const label = friendlyLabel(c.name, c.result !== undefined);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  const summary = Array.from(counts.entries())
    .map(([k, v]) => (v > 1 ? `${k} ×${v}` : k))
    .join(" · ");

  return (
    <div className="rounded-md border border-gray-200 bg-gray-50/70 text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-2.5 py-1.5 hover:bg-gray-100 rounded-md text-left"
      >
        <span
          className={`w-1.5 h-1.5 rounded-full shrink-0 ${
            busy ? "bg-brand-green animate-pulse" : "bg-gray-300"
          }`}
        />
        <span className="text-gray-700 font-medium truncate">{summary}</span>
        <span className="text-gray-400 shrink-0">·</span>
        <span className="text-gray-400 shrink-0">
          {calls.length} {calls.length === 1 ? "call" : "calls"}
        </span>
        <span
          className="ml-auto text-gray-400 shrink-0 transition-transform"
          style={{ transform: open ? "rotate(90deg)" : undefined }}
        >
          ▸
        </span>
      </button>
      {open && (
        <div className="border-t border-gray-200 px-2.5 py-1.5 space-y-1 font-mono">
          {calls.map((c, i) => {
            const summaryText = inputSummary(c.input);
            return (
              <div
                key={c.id || i}
                className="flex items-start gap-2 text-[11px]"
                title={summaryText}
              >
                <span className="text-gray-500 w-16 shrink-0">{c.name}</span>
                <span className="text-gray-700 truncate flex-1">
                  {truncate(summaryText, 120)}
                </span>
                {c.result === undefined ? (
                  busy ? (
                    <span className="text-amber-500 shrink-0 animate-pulse">·</span>
                  ) : null
                ) : (
                  <span className="text-brand-green shrink-0">✓</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
