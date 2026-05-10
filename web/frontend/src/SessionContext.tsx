import { createContext, useContext } from "react";
import type { useAgentStream } from "./hooks/useAgentStream";

type Ctx = ReturnType<typeof useAgentStream>;

const SessionCtx = createContext<Ctx | null>(null);

export function SessionProvider({
  value,
  children,
}: {
  value: Ctx;
  children: React.ReactNode;
}) {
  return <SessionCtx.Provider value={value}>{children}</SessionCtx.Provider>;
}

export function useSession(): Ctx {
  const v = useContext(SessionCtx);
  if (!v) throw new Error("useSession must be used inside SessionProvider");
  return v;
}
