import { useCallback, useRef, useState } from "react";

export type AgentEvent =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: unknown }
  | { type: "tool_result"; tool_use_id: string; content: string }
  | { type: "error"; message: string }
  | { type: "done" }
  | { type: "raw"; repr: string };

export function useAgentStream(sessionId: string) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const send = useCallback(
    async (text: string) => {
      if (streaming) return;
      setStreaming(true);
      setEvents((e) => [...e, { type: "text", text: `🧑 ${text}` }]);

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        const res = await fetch(`/api/sessions/${sessionId}/message`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
          signal: ctrl.signal,
        });
        if (!res.body) throw new Error("no response body");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const frames = buf.split("\n\n");
          buf = frames.pop() ?? "";
          for (const frame of frames) {
            const line = frame.split("\n").find((l) => l.startsWith("data:"));
            if (!line) continue;
            const payload = line.slice(5).trim();
            if (!payload) continue;
            try {
              const evt = JSON.parse(payload) as AgentEvent;
              setEvents((prev) => [...prev, evt]);
              if (evt.type === "done" || evt.type === "error") break;
            } catch {
              // skip malformed frame
            }
          }
        }
      } catch (e: any) {
        if (e.name !== "AbortError") {
          setEvents((prev) => [...prev, { type: "error", message: String(e.message || e) }]);
        }
      } finally {
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [sessionId, streaming],
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return { events, streaming, send, cancel };
}
