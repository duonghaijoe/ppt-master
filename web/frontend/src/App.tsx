import { useEffect, useState } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { SlideDeck } from "./components/SlideDeck";
import { ImportDropzone } from "./components/ImportDropzone";

type SessionInfo = {
  session_id: string;
  project: string;
};

export function App() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [projectName, setProjectName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [existing, setExisting] = useState<{ name: string; slides: number }[]>([]);

  useEffect(() => {
    fetch("/api/projects").then((r) => r.json()).then((d) => setExisting(d.projects ?? []));
  }, [session]);

  async function createProject() {
    if (!projectName.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const res = await fetch("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: projectName, format: "ppt169" }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setSession({ session_id: data.session_id, project: data.project });
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setCreating(false);
    }
  }

  async function attach(name: string) {
    setError(null);
    const fd = new FormData();
    fd.append("name", name);
    const res = await fetch("/api/sessions/attach", { method: "POST", body: fd });
    if (!res.ok) {
      setError(await res.text());
      return;
    }
    const data = await res.json();
    setSession({ session_id: data.session_id, project: data.project });
  }

  if (!session) {
    return (
      <div className="h-full flex items-center justify-center p-6">
        <div className="bg-white rounded-lg shadow-md p-8 w-full max-w-xl space-y-6">
          <header>
            <h1 className="text-2xl font-bold">PPT Master</h1>
            <p className="text-sm text-gray-500">Chat-driven slide generation</p>
          </header>

          <section className="space-y-2">
            <label className="text-sm font-medium">New project name</label>
            <div className="flex gap-2">
              <input
                value={projectName}
                onChange={(e) => setProjectName(e.target.value)}
                placeholder="my_deck"
                className="flex-1 border border-gray-300 rounded px-3 py-2 text-sm"
              />
              <button
                onClick={createProject}
                disabled={creating || !projectName.trim()}
                className="px-4 py-2 bg-brand-green text-white rounded text-sm disabled:opacity-50"
              >
                {creating ? "Creating..." : "Create"}
              </button>
            </div>
            {error && <div className="text-red-600 text-sm">{error}</div>}
          </section>

          {existing.length > 0 && (
            <section>
              <h2 className="text-sm font-medium mb-2">Existing projects</h2>
              <ul className="border rounded divide-y">
                {existing.map((p) => (
                  <li key={p.name} className="flex items-center justify-between px-3 py-2 text-sm">
                    <span>
                      {p.name} <span className="text-gray-400">({p.slides} slides)</span>
                    </span>
                    <button onClick={() => attach(p.name)} className="text-brand-red hover:underline">
                      Open
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="h-full grid grid-cols-[400px_1fr]">
      <aside className="bg-white border-r border-gray-200 flex flex-col">
        <header className="px-4 py-3 border-b border-gray-200 flex items-center justify-between">
          <div>
            <div className="text-xs uppercase text-gray-400">Project</div>
            <div className="font-mono text-sm">{session.project}</div>
          </div>
          <button
            onClick={() => setSession(null)}
            className="text-xs text-gray-500 hover:text-brand-red"
          >
            Switch
          </button>
        </header>
        <ImportDropzone sessionId={session.session_id} />
        <ChatPanel sessionId={session.session_id} />
      </aside>
      <main className="overflow-hidden">
        <SlideDeck project={session.project} />
      </main>
    </div>
  );
}
