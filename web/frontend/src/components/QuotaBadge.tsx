import { useEffect, useState } from "react";

type QuotaUsage = {
  projects: number;
  max_projects: number;
  storage_bytes: number;
  max_storage_bytes: number;
};

/**
 * Compact badge showing project + storage usage vs caps for the active
 * tenant. Polls /api/tenants/{slug}/quota on mount and whenever `refreshKey`
 * changes. Goes amber at >= 80% and red at >= 95% on either dimension so
 * the user sees pressure before a 403 lands.
 */
export function QuotaBadge({
  tenantSlug,
  refreshKey,
}: {
  tenantSlug: string | null;
  refreshKey?: number | string;
}) {
  const [usage, setUsage] = useState<QuotaUsage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!tenantSlug) {
      setUsage(null);
      return;
    }
    let cancelled = false;
    fetch(`/api/tenants/${tenantSlug}/quota`)
      .then((r) => (r.ok ? r.json() : Promise.reject(`${r.status}`)))
      .then((d: QuotaUsage) => {
        if (cancelled) return;
        setUsage(d);
        setError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [tenantSlug, refreshKey]);

  if (!tenantSlug || error || !usage) return null;

  const projectsPct = pct(usage.projects, usage.max_projects);
  const storagePct = pct(usage.storage_bytes, usage.max_storage_bytes);
  const peak = Math.max(projectsPct, storagePct);
  const tone =
    peak >= 95
      ? "bg-red-50 border-red-200 text-red-700"
      : peak >= 80
      ? "bg-amber-50 border-amber-200 text-amber-800"
      : "bg-white border-[#EFE6D6] text-gray-600";

  return (
    <div
      className={`inline-flex items-center gap-2 text-[11px] px-2.5 py-1 rounded-md border ${tone}`}
      title={`Projects: ${usage.projects} / ${usage.max_projects}\nStorage: ${humanBytes(
        usage.storage_bytes,
      )} / ${humanBytes(usage.max_storage_bytes)}`}
    >
      <Metric
        label="Projects"
        value={`${usage.projects}/${usage.max_projects}`}
        pct={projectsPct}
      />
      <span className="w-px h-3 bg-current opacity-20" />
      <Metric
        label="Storage"
        value={`${humanBytes(usage.storage_bytes)} / ${humanBytes(
          usage.max_storage_bytes,
        )}`}
        pct={storagePct}
      />
    </div>
  );
}

function Metric({
  label,
  value,
  pct,
}: {
  label: string;
  value: string;
  pct: number;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="uppercase tracking-wider opacity-60 text-[9px]">
        {label}
      </span>
      <span className="font-mono">{value}</span>
      <span className="opacity-50 text-[9px]">{pct}%</span>
    </span>
  );
}

function pct(n: number, cap: number): number {
  if (!cap) return 0;
  return Math.min(100, Math.round((n / cap) * 100));
}

function humanBytes(n: number) {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)}KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)}GB`;
}
