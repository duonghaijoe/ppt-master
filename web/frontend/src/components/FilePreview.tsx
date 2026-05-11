import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Presentation } from "lucide-react";

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

export function ext(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

export function FilePreview({
  project,
  path,
  version,
  onAskAi,
  compact,
}: {
  project: string;
  path: string;
  version: number;
  onAskAi?: (text: string) => void;
  // When true the host (Workbench) is already showing the file name and
  // primary actions in its toolbar — render only the toggles and content
  // so we don't repeat the same path twice on screen.
  compact?: boolean;
}) {
  const e = ext(path);
  const url = `/api/projects/${encodeURIComponent(project)}/file?path=${encodeURIComponent(path)}&v=${version}`;
  // Path-style URL for the iframe: relative refs inside HTML (./image.png,
  // ./styles.css) resolve to siblings under the same project root instead of
  // failing against the query-string-based /file endpoint.
  const webUrl = `/api/projects/${encodeURIComponent(project)}/web/${path
    .split("/")
    .map(encodeURIComponent)
    .join("/")}?v=${version}`;
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
      {!compact && (
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
      )}
      {compact && (isMd || isHtml) && (
        <div className="px-4 py-1 bg-white flex items-center justify-end gap-3 border-b border-gray-100">
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
        </div>
      )}
      <div className="flex-1 min-h-0 overflow-auto p-4">
        {isImg && <img src={url} alt={name} className="max-w-full h-auto bg-white shadow" />}
        {isSvg && (
          <div className="bg-white shadow inline-block">
            <object type="image/svg+xml" data={url} aria-label={name} />
          </div>
        )}
        {isPdf && <embed src={url} type="application/pdf" className="w-full h-[80vh]" />}
        {isPptx && <PptxPreview name={name} url={url} />}
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
            // blocking same-origin/cookie/top-nav access. We serve the
            // page through the path-style /web/<path> route so relative
            // asset URLs ('./images/foo.png') resolve to siblings under
            // the project root.
            <iframe
              src={webUrl}
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

// .pptx is a binary archive — no point trying to render it in-browser. The
// SVGs that produced the deck are already first-class artifacts (rendered by
// SlideDeck in their working dir), so this is just a download affordance.
function PptxPreview({ name, url }: { name: string; url: string }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-3 px-4 py-3 bg-white border border-gray-200 rounded hover:border-brand-green/40 hover:bg-brand-green/5 text-sm"
    >
      <Presentation className="w-6 h-6 text-orange-500 shrink-0" />
      <span className="flex flex-col">
        <span className="font-mono text-gray-700">{name}</span>
        <span className="text-xs text-gray-400">PowerPoint export · click to download</span>
      </span>
    </a>
  );
}
