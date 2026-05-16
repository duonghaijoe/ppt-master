import { useCallback, useEffect, useState } from "react";

type BillingSummary = {
  tenant: string;
  balance_usd: string;
  trial_remaining_usd?: string;
  mtd_billed_usd: string;
  cap_soft_usd: string;
  cap_hard_usd: string;
  soft_threshold_crossed?: boolean;
  payment_mode?: "stripe_prepaid" | "stripe_auto" | "manual";
  billing_email?: string;
  stripe_customer_id?: string | null;
};

type UsageEvent = {
  id: number;
  ts: string;
  tenant: string;
  user_id?: string;
  project?: string;
  session_id?: string;
  model?: string;
  provider?: string;
  kind?: string;
  billed_cost_usd: string;
};

type CreditTxn = {
  id: number;
  ts: string;
  tenant: string;
  source: "trial" | "stripe" | "manual" | string;
  source_id: string;
  amount_usd: string;
  note?: string;
};

type Tab = "overview" | "usage" | "transactions";

export function BillingDrawer({
  tenantSlug,
  open,
  onClose,
}: {
  tenantSlug: string;
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");
  const [summary, setSummary] = useState<BillingSummary | null>(null);
  const [usage, setUsage] = useState<UsageEvent[]>([]);
  const [txns, setTxns] = useState<CreditTxn[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [softCapInput, setSoftCapInput] = useState<string>("");
  const [topupAmt, setTopupAmt] = useState<string>("25");
  const [topupBusy, setTopupBusy] = useState(false);

  const isOwner = !!summary?.balance_usd; // owner view returns balance; viewer/editor view does not.

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const sr = await fetch(`/api/tenants/${tenantSlug}/billing`);
      if (!sr.ok) throw new Error(`billing ${sr.status}`);
      const sj = (await sr.json()) as BillingSummary;
      setSummary(sj);
      setSoftCapInput(sj.cap_soft_usd ?? "");
      // Usage is visible to viewers+; transactions are owner-only.
      const ur = await fetch(`/api/tenants/${tenantSlug}/billing/usage?limit=200`);
      if (ur.ok) {
        const uj = await ur.json();
        setUsage(uj.events ?? []);
      }
      if (sj.balance_usd) {
        const tr = await fetch(`/api/tenants/${tenantSlug}/billing/transactions?limit=100`);
        if (tr.ok) {
          const tj = await tr.json();
          setTxns(tj.transactions ?? []);
        }
      }
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, [tenantSlug]);

  useEffect(() => {
    if (open) reload();
  }, [open, reload]);

  async function saveSoftCap() {
    try {
      const res = await fetch(`/api/tenants/${tenantSlug}/billing/caps`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cap_soft_usd: softCapInput }),
      });
      if (!res.ok) throw new Error(await res.text());
      await reload();
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  async function startCheckout() {
    setTopupBusy(true);
    setError(null);
    try {
      const here = window.location.href;
      const res = await fetch(`/api/tenants/${tenantSlug}/billing/checkout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          amount_usd: topupAmt,
          success_url: here,
          cancel_url: here,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const j = await res.json();
      window.open(j.url, "_blank", "noopener");
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setTopupBusy(false);
    }
  }

  async function openPortal() {
    setError(null);
    try {
      const res = await fetch(`/api/tenants/${tenantSlug}/billing/portal`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ return_url: window.location.href }),
      });
      if (!res.ok) throw new Error(await res.text());
      const j = await res.json();
      window.open(j.url, "_blank", "noopener");
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/30" onClick={onClose} />
      <aside className="w-[560px] max-w-full bg-white border-l border-gray-200 shadow-2xl flex flex-col">
        <header className="px-5 py-4 border-b border-gray-200 flex items-center justify-between">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-gray-400">Billing</div>
            <div className="font-semibold text-sm">{tenantSlug}</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-500 hover:text-gray-900 text-sm"
            title="Close"
          >
            ✕
          </button>
        </header>

        <nav className="px-3 pt-2 border-b border-gray-100 flex gap-1 text-xs">
          {(["overview", "usage", "transactions"] as Tab[]).map((t) => {
            const disabled = t === "transactions" && !isOwner;
            return (
              <button
                key={t}
                disabled={disabled}
                onClick={() => setTab(t)}
                className={`px-3 py-2 rounded-t-md ${
                  tab === t
                    ? "bg-gray-100 font-medium text-gray-900"
                    : "text-gray-500 hover:bg-gray-50"
                } ${disabled ? "opacity-40 cursor-not-allowed" : ""}`}
              >
                {t === "overview" ? "Overview" : t === "usage" ? "Usage" : "Transactions"}
              </button>
            );
          })}
        </nav>

        <div className="flex-1 overflow-y-auto px-5 py-4 text-sm space-y-4">
          {loading && <div className="text-gray-400 text-xs">Loading…</div>}
          {error && (
            <div className="rounded-md border border-red-200 bg-red-50 text-red-700 text-xs px-3 py-2">
              {error}
            </div>
          )}

          {tab === "overview" && summary && (
            <Overview
              s={summary}
              softCapInput={softCapInput}
              onSoftCapChange={setSoftCapInput}
              onSaveSoftCap={saveSoftCap}
              topupAmt={topupAmt}
              onTopupAmtChange={setTopupAmt}
              onTopup={startCheckout}
              topupBusy={topupBusy}
              onPortal={openPortal}
            />
          )}

          {tab === "usage" && <UsageTable events={usage} />}

          {tab === "transactions" && <TxnTable rows={txns} />}
        </div>
      </aside>
    </div>
  );
}

function Overview({
  s,
  softCapInput,
  onSoftCapChange,
  onSaveSoftCap,
  topupAmt,
  onTopupAmtChange,
  onTopup,
  topupBusy,
  onPortal,
}: {
  s: BillingSummary;
  softCapInput: string;
  onSoftCapChange: (v: string) => void;
  onSaveSoftCap: () => void;
  topupAmt: string;
  onTopupAmtChange: (v: string) => void;
  onTopup: () => void;
  topupBusy: boolean;
  onPortal: () => void;
}) {
  const isOwner = !!s.balance_usd;
  const mtd = parseFloat(s.mtd_billed_usd);
  const soft = parseFloat(s.cap_soft_usd);
  const hard = parseFloat(s.cap_hard_usd);
  const softFrac = soft > 0 ? Math.min(1, mtd / soft) : 0;
  const hardFrac = hard > 0 ? Math.min(1, mtd / hard) : 0;

  return (
    <div className="space-y-5">
      {s.soft_threshold_crossed && (
        <div className="rounded-md border border-amber-200 bg-amber-50 text-amber-800 text-xs px-3 py-2">
          MTD spend crossed the soft cap threshold. New sessions are still
          allowed; raise the soft cap or top up to clear this banner.
        </div>
      )}
      <section className="grid grid-cols-2 gap-3">
        {isOwner && (
          <Stat label="Balance" value={`$${fmt(s.balance_usd)}`} />
        )}
        {isOwner && (
          <Stat
            label="Trial remaining"
            value={`$${fmt(s.trial_remaining_usd ?? "0")}`}
            hint="Expires 90 days from tenant creation"
          />
        )}
        <Stat label="MTD spend" value={`$${fmt(s.mtd_billed_usd)}`} />
        <Stat label="Payment mode" value={(s.payment_mode || "—").replace("_", " ")} />
      </section>

      <section className="space-y-3">
        <div>
          <div className="flex justify-between text-[11px] text-gray-600 mb-1">
            <span>Soft cap · ${fmt(s.cap_soft_usd)}</span>
            <span>{Math.round(softFrac * 100)}%</span>
          </div>
          <Bar frac={softFrac} color="amber" />
        </div>
        <div>
          <div className="flex justify-between text-[11px] text-gray-600 mb-1">
            <span>Hard cap · ${fmt(s.cap_hard_usd)}</span>
            <span>{Math.round(hardFrac * 100)}%</span>
          </div>
          <Bar frac={hardFrac} color="red" />
        </div>
      </section>

      {isOwner && (
        <>
          <section className="border-t border-gray-100 pt-4">
            <div className="text-[10px] uppercase tracking-wider text-gray-400 mb-2">
              Soft cap
            </div>
            <div className="flex items-center gap-2">
              <span className="text-gray-500">$</span>
              <input
                value={softCapInput}
                onChange={(e) => onSoftCapChange(e.target.value)}
                className="flex-1 border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:border-brand-green"
              />
              <button
                onClick={onSaveSoftCap}
                className="text-xs px-3 py-1.5 bg-gray-900 text-white rounded hover:bg-gray-800"
              >
                Save
              </button>
            </div>
            <div className="text-[10px] text-gray-400 mt-1">
              Hard-cap raises require platform admin.
            </div>
          </section>

          {s.payment_mode !== "manual" && (
            <section className="border-t border-gray-100 pt-4 space-y-2">
              <div className="text-[10px] uppercase tracking-wider text-gray-400">
                Top up · Stripe
              </div>
              <div className="flex items-center gap-2">
                <span className="text-gray-500">$</span>
                <input
                  value={topupAmt}
                  onChange={(e) => onTopupAmtChange(e.target.value)}
                  className="flex-1 border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:border-brand-green"
                />
                <button
                  onClick={onTopup}
                  disabled={topupBusy}
                  className="text-xs px-3 py-1.5 bg-brand-red text-white rounded disabled:opacity-50"
                >
                  {topupBusy ? "…" : "Top up"}
                </button>
              </div>
              {s.stripe_customer_id && (
                <button
                  onClick={onPortal}
                  className="text-xs px-3 py-1.5 border border-gray-300 text-gray-700 rounded hover:bg-gray-50"
                >
                  Manage card / portal
                </button>
              )}
            </section>
          )}

          {s.payment_mode === "manual" && (
            <section className="border-t border-gray-100 pt-4">
              <div className="text-[10px] uppercase tracking-wider text-gray-400 mb-1">
                Contact billing
              </div>
              <div className="text-xs text-gray-700 leading-snug">
                This tenant is on manual / wire-transfer billing. Email{" "}
                <span className="font-mono">billing@awesomedeck.example</span>{" "}
                with the wire reference to record a top-up.
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}

function UsageTable({ events }: { events: UsageEvent[] }) {
  if (events.length === 0) {
    return <div className="text-gray-400 text-xs">No usage recorded yet.</div>;
  }
  return (
    <table className="w-full text-xs">
      <thead className="text-gray-400 text-[10px] uppercase tracking-wider">
        <tr>
          <th className="text-left py-1">When</th>
          <th className="text-left py-1">Kind</th>
          <th className="text-left py-1">Model</th>
          <th className="text-right py-1">Billed</th>
        </tr>
      </thead>
      <tbody>
        {events.map((e) => (
          <tr key={e.id} className="border-t border-gray-100">
            <td className="py-1 text-gray-600">{shortTs(e.ts)}</td>
            <td className="py-1">{e.kind || "—"}</td>
            <td className="py-1 text-gray-600">{e.model || e.provider || "—"}</td>
            <td className="py-1 text-right font-mono">${fmt(e.billed_cost_usd)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TxnTable({ rows }: { rows: CreditTxn[] }) {
  if (rows.length === 0) {
    return <div className="text-gray-400 text-xs">No credits recorded yet.</div>;
  }
  return (
    <table className="w-full text-xs">
      <thead className="text-gray-400 text-[10px] uppercase tracking-wider">
        <tr>
          <th className="text-left py-1">When</th>
          <th className="text-left py-1">Source</th>
          <th className="text-left py-1">Reference</th>
          <th className="text-right py-1">Amount</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id} className="border-t border-gray-100">
            <td className="py-1 text-gray-600">{shortTs(r.ts)}</td>
            <td className="py-1">{r.source}</td>
            <td className="py-1 text-gray-600 truncate max-w-[140px]" title={r.source_id}>
              {r.source_id}
            </td>
            <td className="py-1 text-right font-mono">${fmt(r.amount_usd)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-md border border-gray-200 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-gray-400">{label}</div>
      <div className="text-base font-semibold text-gray-900">{value}</div>
      {hint && <div className="text-[10px] text-gray-400 mt-0.5">{hint}</div>}
    </div>
  );
}

function Bar({ frac, color }: { frac: number; color: "amber" | "red" }) {
  const cls = color === "amber" ? "bg-amber-400" : "bg-red-500";
  return (
    <div className="h-2 rounded-full bg-gray-100 overflow-hidden">
      <div
        className={`h-full ${cls} transition-all`}
        style={{ width: `${Math.max(2, frac * 100)}%` }}
      />
    </div>
  );
}

function fmt(s: string | number | undefined) {
  const n = typeof s === "number" ? s : parseFloat(s ?? "0");
  if (!Number.isFinite(n)) return "0.00";
  return n.toFixed(2);
}

function shortTs(s: string) {
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  const today = new Date();
  if (
    d.getFullYear() === today.getFullYear() &&
    d.getMonth() === today.getMonth() &&
    d.getDate() === today.getDate()
  ) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString();
}
