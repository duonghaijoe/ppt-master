import { useRef, useState } from "react";

export function ImportDropzone({ sessionId }: { sessionId: string }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [urlValue, setUrlValue] = useState("");

  async function uploadFile(file: File) {
    setBusy(true);
    setStatus(`Importing ${file.name}…`);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`/api/sessions/${sessionId}/import/file`, {
        method: "POST",
        body: fd,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "import failed");
      setStatus(`Imported → ${data.imported}`);
    } catch (e: any) {
      setStatus(`Error: ${String(e.message || e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function importUrl() {
    if (!urlValue.trim()) return;
    setBusy(true);
    setStatus(`Fetching ${urlValue}…`);
    try {
      const res = await fetch(`/api/sessions/${sessionId}/import/url`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: urlValue }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "import failed");
      setStatus(`Imported → ${data.imported}`);
      setUrlValue("");
    } catch (e: any) {
      setStatus(`Error: ${String(e.message || e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        const f = e.dataTransfer.files?.[0];
        if (f) uploadFile(f);
      }}
      className={`border-b border-gray-200 px-4 py-3 transition ${dragOver ? "bg-brand-cream" : ""}`}
    >
      <div className="flex items-center gap-2 mb-2">
        <button
          onClick={() => inputRef.current?.click()}
          disabled={busy}
          className="text-xs px-2 py-1 border border-gray-300 rounded disabled:opacity-50"
        >
          Upload file
        </button>
        <input
          ref={inputRef}
          type="file"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadFile(f);
            if (inputRef.current) inputRef.current.value = "";
          }}
          accept=".pdf,.docx,.doc,.pptx,.ppt,.md,.txt,.epub,.ipynb"
        />
        <span className="text-xs text-gray-400">or drop a file</span>
      </div>
      <div className="flex gap-2">
        <input
          value={urlValue}
          onChange={(e) => setUrlValue(e.target.value)}
          placeholder="https://… (URL to scrape)"
          disabled={busy}
          className="flex-1 border border-gray-300 rounded px-2 py-1 text-xs"
        />
        <button
          onClick={importUrl}
          disabled={busy || !urlValue.trim()}
          className="text-xs px-2 py-1 bg-brand-red text-white rounded disabled:opacity-50"
        >
          Fetch
        </button>
      </div>
      {status && <div className="text-xs text-gray-500 mt-2 truncate">{status}</div>}
    </div>
  );
}
