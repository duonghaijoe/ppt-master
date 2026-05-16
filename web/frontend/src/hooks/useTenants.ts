import { useCallback, useEffect, useState } from "react";

export type TenantRole = "viewer" | "editor" | "owner" | "admin";

export type TenantSummary = {
  slug: string;
  name: string;
  default_format: string;
  role: TenantRole;
};

export type Me = {
  user: { id: string; email: string; display_name: string };
  platform_admin: boolean;
  memberships: { tenant_slug: string; role: TenantRole }[];
};

const ACTIVE_TENANT_KEY = "awesomedeck.activeTenant";

/**
 * Loads /api/me + /api/tenants on mount and tracks the user's active tenant.
 *
 * The active slug is persisted in localStorage so users land back in the same
 * workspace after a refresh. If the persisted slug is no longer in the user's
 * memberships (revoked / deleted tenant), we fall back to the first available.
 */
export function useTenants() {
  const [me, setMe] = useState<Me | null>(null);
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const [activeSlug, setActiveSlugState] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [meRes, tenantsRes] = await Promise.all([
        fetch("/api/me"),
        fetch("/api/tenants"),
      ]);
      if (!meRes.ok) throw new Error(`/api/me ${meRes.status}`);
      if (!tenantsRes.ok) throw new Error(`/api/tenants ${tenantsRes.status}`);
      const meData = (await meRes.json()) as Me;
      const tenantsData = (await tenantsRes.json()) as { tenants: TenantSummary[] };
      setMe(meData);
      setTenants(tenantsData.tenants ?? []);

      const stored = localStorage.getItem(ACTIVE_TENANT_KEY);
      const available = (tenantsData.tenants ?? []).map((t) => t.slug);
      const next =
        stored && available.includes(stored)
          ? stored
          : available[0] ?? null;
      setActiveSlugState(next);
      if (next) localStorage.setItem(ACTIVE_TENANT_KEY, next);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const setActiveSlug = useCallback((slug: string) => {
    setActiveSlugState(slug);
    localStorage.setItem(ACTIVE_TENANT_KEY, slug);
  }, []);

  const activeTenant = tenants.find((t) => t.slug === activeSlug) ?? null;
  const isOwnerOfActive =
    !!me?.platform_admin || activeTenant?.role === "owner";

  return {
    me,
    tenants,
    activeSlug,
    activeTenant,
    isOwnerOfActive,
    loading,
    error,
    setActiveSlug,
    refresh,
  };
}
