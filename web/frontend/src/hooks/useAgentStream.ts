import { useCallback, useRef, useState } from "react";

export type AgentEvent =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: unknown }
  | { type: "tool_result"; tool_use_id: string; content: string }
  | { type: "permission_request"; id: string; tool: string; input: unknown; resolved?: "approve" | "deny" }
  | { type: "error"; message: string }
  | { type: "done" }
  | { type: "raw"; repr: string }
  | {
      type: "billing.threshold";
      tenant: string;
      mtd_billed_usd: string;
      cap_soft_usd: string;
      threshold_fraction: number;
    }
  | {
      type: "billing.cap_exceeded";
      tenant: string;
      mtd_billed_usd: string;
      cap_hard_usd: string;
    }
  | {
      type: "rate_limited";
      reason: "req_per_min" | "cost_per_min_usd" | string;
      retry_after_seconds: number;
    };

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
        // 429 from rate_limit.check_and_consume — surface as a typed event
        // so ChatPanel can render a countdown banner with a useful reason.
        if (res.status === 429) {
          let retry = 0;
          let reason = "rate_limited";
          try {
            const body = await res.json();
            const detail = body?.detail ?? body ?? {};
            retry = Number(detail.retry_after_seconds) || 0;
            if (typeof detail.reason === "string") reason = detail.reason;
          } catch {
            // Fall back to the Retry-After header — it carries a coarse
            // second-grained ceiling the backend always sets.
          }
          if (!retry) {
            const hdr = res.headers.get("Retry-After");
            const parsed = hdr ? Number(hdr) : 0;
            if (Number.isFinite(parsed) && parsed > 0) retry = parsed;
          }
          setEvents((prev) => [
            ...prev,
            { type: "rate_limited", reason, retry_after_seconds: retry },
          ]);
          return;
        }
        // 402 from check_eligibility / per-session ceiling — surface as a
        // billing event so the chat banners pick it up instead of a generic error.
        if (res.status === 402) {
          try {
            const body = await res.json();
            const detail = body?.detail ?? body ?? {};
            setEvents((prev) => [
              ...prev,
              {
                type: "billing.cap_exceeded",
                tenant: detail.tenant ?? "",
                mtd_billed_usd: detail.mtd_billed_usd ?? "0",
                cap_hard_usd: detail.cap_hard_usd ?? detail.session_ceiling_usd ?? "0",
              },
            ]);
          } catch {
            setEvents((prev) => [
              ...prev,
              { type: "error", message: "billing_blocked (402)" },
            ]);
          }
          return;
        }
        if (!res.ok) {
          const msg = await res.text().catch(() => "");
          throw new Error(msg || `HTTP ${res.status}`);
        }
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
