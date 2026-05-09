import { useState } from "react";
import { useAgentStream } from "../hooks/useAgentStream";

export function ChatPanel({ sessionId }: { sessionId: string }) {
  const { events, streaming, send } = useAgentStream(sessionId);
  const [input, setInput] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim() || streaming) return;
    const text = input;
    setInput("");
    send(text);
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2 text-sm">
        {events.length === 0 && (
          <div className="text-gray-400 text-center pt-10">
            Try: <em>"Generate a 5-slide deck on Q1 sales"</em>
          </div>
        )}
        {events.map((evt, i) => {
          if (evt.type === "text")
            return (
              <div key={i} className="whitespace-pre-wrap">
                {evt.text}
              </div>
            );
          if (evt.type === "tool_use")
            return (
              <div key={i} className="text-xs font-mono text-brand-green">
                ⚡ {evt.name}
              </div>
            );
          if (evt.type === "tool_result") return null;
          if (evt.type === "error")
            return (
              <div key={i} className="text-xs text-red-600">
                ⚠ {evt.message}
              </div>
            );
          if (evt.type === "done")
            return (
              <div key={i} className="text-xs text-gray-300 border-t pt-2">
                — turn complete —
              </div>
            );
          return null;
        })}
      </div>
      <form onSubmit={submit} className="border-t border-gray-200 px-3 py-2 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={streaming ? "Working..." : "Tell the agent what to do..."}
          disabled={streaming}
          className="flex-1 border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-50"
        />
        <button
          type="submit"
          disabled={streaming || !input.trim()}
          className="px-4 py-2 bg-brand-green text-white rounded text-sm disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </div>
  );
}
