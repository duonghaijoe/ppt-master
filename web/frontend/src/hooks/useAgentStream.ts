import { useCallback, useRef, useState } from "react";

export type AgentEvent =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: unknown }
  | { type: "tool_result"; tool_use_id: string; content: string }
  | { type: "permission_request"; id: string; tool: string; input: unknown; resolved?: "approve" | "deny" }
  | { type: "error"; message: string }
  | { type: "done" }
  | { type: "raw"; repr: string };

export function useAgentStream(sessionId: string) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const respondPermission = useCallback(
    async (requestId: string, decision: "approve" | "deny") => {
      try {
        await fetch(`/api/sessions/${sessionId}/permission`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: requestId, decision }),
        });
        setEvents((prev) =>
          prev.map((e) =>
            e.type === "permission_request" && e.id === requestId
              ? { ...e, resolved: decision }
              : e,
          ),
        );
      } catch (e) {
        // silent — backend will time out anyway
      }
    },
    [sessionId],
  );

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

  // Tell the backend to stop the running model AND drop the local stream.
  // The backend interrupt is best-effort — if the SDK has already produced
  // the final result it's a no-op.
  const interrupt = useCallback(async () => {
    try {
      await fetch(`/api/sessions/${sessionId}/interrupt`, { method: "POST" });
    } catch {
      /* ignore — we still abort the local fetch below */
    }
    abortRef.current?.abort();
  }, [sessionId]);

  const replaceEvents = useCallback((next: AgentEvent[]) => {
    setEvents(next);
  }, []);

  return {
    events,
    streaming,
    send,
    cancel,
    interrupt,
    respondPermission,
    replaceEvents,
  };
}
