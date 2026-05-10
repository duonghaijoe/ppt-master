import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Entry = { name: string; is_dir: boolean; size: number | null; mtime: number };
type Tree = { type: "dir"; path: string; entries: Entry[] };

const IMG_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"]);
const SVG_EXT = new Set(["svg"]);
const MD_EXT = new Set(["md", "markdown"]);
const HTML_EXT = new Set(["html", "htm"]);
const TEXT_EXT = new Set([
  "txt", "json", "yaml", "yml", "csv", "tsv",
  "py", "ts", "tsx", "js", "jsx", "css",
  "sh", "toml", "ini", "log", "rels", "xml",
]);
const PDF_EXT = new Set(["pdf"]);
const PPTX_EXT = new Set(["pptx"]);

function ext(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

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
  project,
  onAskAi,
}: {
  project: string;
  onAskAi?: (text: string) => void;
}) {
  const [path, setPath] = useState("");
  const [tree, setTree] = useState<Tree | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [bust, setBust] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    fetch(`/api/projects/${encodeURIComponent(project)}/tree?path=${encodeURIComponent(path)}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await r.text());
        return r.json();
      })
      .then((d: Tree) => setTree(d))
      .catch((e) => setError(String(e.message || e)));
  }, [project, path, bust]);

  // Watch fs to refresh listing.
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
          <FilePreview project={project} path={selected} version={bust} onAskAi={onAskAi} />
        ) : (
          <div className="h-full flex items-center justify-center text-sm text-gray-400 p-6 text-center">
            Select a file to preview
          </div>
        )}
      </div>
    </div>
  );
}

function FilePreview({
  project,
  path,
  version,
  onAskAi,
}: {
  project: string;
  path: string;
  version: number;
  onAskAi?: (text: string) => void;
}) {
  const e = ext(path);
  const url = `/api/projects/${encodeURIComponent(project)}/file?path=${encodeURIComponent(path)}&v=${version}`;
  const name = path.split("/").pop() || path;

  const [text, setText] = useState<string | null>(null);
  const [textErr, setTextErr] = useState<string | null>(null);
  const [rawMd, setRawMd] = useState(false);
  const [showHtmlSource, setShowHtmlSource] = useState(false);

  const isImg = IMG_EXT.has(e);
  const isSvg = SVG_EXT.has(e);
  const isMd = MD_EXT.has(e);
  const isHtml = HTML_EXT.has(e);
  const isText = TEXT_EXT.has(e);
  const isPdf = PDF_EXT.has(e);
  const isPptx = PPTX_EXT.has(e);
  // Markdown always needs the text body. HTML only needs it when the user
  // flipped to "Source" — the rendered iframe loads from the URL directly.
  const needsText = isText || isMd || (isHtml && showHtmlSource);

  useEffect(() => {
    if (!needsText) return;
    setText(null);
    setTextErr(null);
    fetch(url)
      .then((r) => (r.ok ? r.text() : Promise.reject(r.statusText)))
      .then((t) => setText(t.length > 200_000 ? t.slice(0, 200_000) + "\n…(truncated)" : t))
      .catch((err) => setTextErr(String(err)));
  }, [url, needsText]);

  return (
    <div className="h-full flex flex-col">
      <div className="px-4 py-2 border-b border-gray-200 bg-white flex items-center justify-between gap-2">
        <div className="text-sm font-mono truncate">{name}</div>
        <div className="flex items-center gap-3 shrink-0">
          {isMd && (
            <button
              onClick={() => setRawMd((v) => !v)}
              className="text-xs text-gray-500 hover:text-brand-green"
              title="Toggle rendered / raw"
            >
              {rawMd ? "Rendered" : "Raw"}
            </button>
          )}
          {isHtml && (
            <button
              onClick={() => setShowHtmlSource((v) => !v)}
              className="text-xs text-gray-500 hover:text-brand-green"
              title="Toggle rendered / source"
            >
              {showHtmlSource ? "Rendered" : "Source"}
            </button>
          )}
          {onAskAi && (
            <button
              onClick={() => onAskAi(`@${path}`)}
              className="text-xs px-2 py-0.5 border border-brand-green/40 text-brand-green rounded hover:bg-brand-green/5"
              title="Reference this file in the chat textarea"
            >
              @chat
            </button>
          )}
          <a href={url} target="_blank" rel="noreferrer" className="text-xs text-brand-green hover:underline">
            Open ↗
          </a>
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-auto p-4">
        {isImg && <img src={url} alt={name} className="max-w-full h-auto bg-white shadow" />}
        {isSvg && (
          <div className="bg-white shadow inline-block">
            <object type="image/svg+xml" data={url} aria-label={name} />
          </div>
        )}
        {isPdf && <embed src={url} type="application/pdf" className="w-full h-[80vh]" />}
        {isPptx && <PptxPreview project={project} name={name} url={url} />}
        {isMd && (
          textErr ? (
            <div className="text-xs text-red-600">{textErr}</div>
          ) : text == null ? (
            <div className="text-xs text-gray-400">loading…</div>
          ) : rawMd ? (
            <pre className="text-xs bg-white border border-gray-200 rounded p-3 whitespace-pre-wrap font-mono">
              {text}
            </pre>
          ) : (
            <article className="prose-md bg-white border border-gray-200 rounded p-5 max-w-none text-sm leading-relaxed">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
            </article>
          )
        )}
        {isHtml && (
          showHtmlSource ? (
            textErr ? (
              <div className="text-xs text-red-600">{textErr}</div>
            ) : (
              <pre className="text-xs bg-white border border-gray-200 rounded p-3 whitespace-pre-wrap font-mono">
                {text ?? "loading…"}
              </pre>
            )
          ) : (
            // Sandboxed iframe lets bundled HTML run its own JS while
            // blocking same-origin/cookie/top-nav access. The page is
            // served from the same origin as the API so relative asset
            // URLs ('./images/foo.png') still resolve.
            <iframe
              src={url}
              title={name}
              className="w-full h-[80vh] bg-white border border-gray-200 rounded"
              sandbox="allow-scripts allow-forms allow-popups allow-modals"
            />
          )
        )}
        {isText && (
          <pre className="text-xs bg-white border border-gray-200 rounded p-3 whitespace-pre-wrap font-mono">
            {textErr ?? text ?? "loading…"}
          </pre>
        )}
        {!isImg && !isSvg && !isPdf && !isMd && !isHtml && !isText && !isPptx && (
          <div className="text-sm text-gray-500">
            Binary or unsupported preview. <a className="text-brand-green underline" href={url} target="_blank" rel="noreferrer">Download</a>
          </div>
        )}
      </div>
    </div>
  );
}

type SlideMeta = { file: string; name: string; mtime: number };

function PptxPreview({ project, name, url }: { project: string; name: string; url: string }) {
  const [slides, setSlides] = useState<SlideMeta[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [idx, setIdx] = useState(0);

  useEffect(() => {
    setSlides(null);
    setErr(null);
    setIdx(0);
    fetch(`/api/projects/${encodeURIComponent(project)}/slides`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.statusText)))
      .then((d) => setSlides(d.slides ?? []))
      .catch((e) => setErr(String(e)));
  }, [project]);

  if (err) return <div className="text-xs text-red-600">{err}</div>;
  if (slides == null) return <div className="text-xs text-gray-400">loading…</div>;

  if (slides.length === 0) {
    return (
      <div className="text-sm text-gray-500 space-y-2">
        <div>
          No rendered slides found in this project. The exported{" "}
          <span className="font-mono">{name}</span> can still be downloaded.
        </div>
        <a href={url} className="text-brand-green underline" target="_blank" rel="noreferrer">
          Download .pptx
        </a>
      </div>
    );
  }

  const safeIdx = Math.min(idx, slides.length - 1);
  const cur = slides[safeIdx];
  const slideUrl = `/api/projects/${encodeURIComponent(project)}/svg/${cur.file}?v=${cur.mtime}`;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-xs">
        <div className="font-mono text-gray-600 truncate">
          {cur.name}{" "}
          <span className="text-gray-400">
            {safeIdx + 1} / {slides.length}
          </span>
        </div>
        <a href={url} className="text-brand-green hover:underline" target="_blank" rel="noreferrer">
          Download .pptx
        </a>
      </div>
      <div className="bg-white shadow" style={{ aspectRatio: "16 / 9", width: "min(100%, 1100px)" }}>
        <object
          type="image/svg+xml"
          data={slideUrl}
          className="w-full h-full"
          aria-label={cur.name}
        />
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {slides.map((s, i) => (
          <button
            key={s.file}
            onClick={() => setIdx(i)}
            className={`shrink-0 w-28 h-16 border rounded overflow-hidden ${
              i === safeIdx ? "border-brand-green ring-2 ring-brand-green/30" : "border-gray-200"
            }`}
            style={{ aspectRatio: "16 / 9" }}
            title={s.name}
          >
            <object
              type="image/svg+xml"
              data={`/api/projects/${encodeURIComponent(project)}/svg/${s.file}?v=${s.mtime}`}
              className="w-full h-full pointer-events-none"
            />
          </button>
        ))}
      </div>
    </div>
  );
}
