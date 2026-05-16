import { useEffect, useState } from "react";

type DevUser = {
  id: string;
  email: string;
  display_name: string;
  platform_admin: boolean;
  memberships: { tenant_slug: string; role: string }[];
};

/**
 * Dev-only login screen. Shown when /api/me returns 401 (no proxy in front of
 * the backend locally). Posts to /api/dev/login, which sets a cookie a backend
 * middleware translates to X-User-Id headers so current_user works unchanged.
 *
 * The password input is for UX realism only — the backend accepts anything
 * when PPT_DEV_AUTH=1.
 */
export function LoginGate({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [users, setUsers] = useState<DevUser[]>([]);
  const [usersError, setUsersError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/dev/users")
      .then((r) => {
        if (r.status === 404) {
          setUsersError("Dev login is disabled. Start the backend with PPT_DEV_AUTH=1.");
          return null;
        }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d) => {
        if (d) setUsers(d.users ?? []);
      })
      .catch((e) => setUsersError(String(e.message || e)));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/api/dev/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), password }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `HTTP ${res.status}`);
      }
      onLoggedIn();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="h-full flex items-center justify-center bg-[#FBF7F1]">
      <div className="w-[420px] bg-white border border-[#EFE6D6] rounded-lg shadow-sm p-6">
        <div className="text-center mb-4">
          <div className="text-[22px] tracking-tight" style={{ fontFamily: "Georgia, serif" }}>
            Awesome Deck
          </div>
          <div className="text-[10px] uppercase tracking-[0.14em] text-[#E8A78A]">
            Dev login
          </div>
        </div>

        {usersError && (
          <div className="mb-3 text-[12px] text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
            {usersError}
          </div>
        )}

        <form onSubmit={submit}>
          <label className="block text-[11px] font-medium text-gray-500 mb-1">Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="alice@acme.test"
            className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-brand-green"
            autoFocus
            required
          />
          <label className="block text-[11px] font-medium text-gray-500 mt-3 mb-1">Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="(any value in dev)"
            className="w-full border border-gray-200 rounded px-3 py-2 text-sm focus:outline-none focus:border-brand-green"
          />
          {error && (
            <div className="mt-3 text-[12px] text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
              {error}
            </div>
          )}
          <button
            type="submit"
            disabled={submitting || !email.trim()}
            className="mt-4 w-full bg-brand-green text-white text-sm font-medium rounded px-3 py-2 disabled:opacity-50"
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>

        {users.length > 0 && (
          <div className="mt-5 pt-4 border-t border-gray-100">
            <div className="text-[10px] uppercase tracking-wider text-gray-400 mb-2">
              Seeded accounts (click to autofill)
            </div>
            <ul className="space-y-1">
              {users.map((u) => (
                <li key={u.id}>
                  <button
                    type="button"
                    onClick={() => setEmail(u.email)}
                    className="w-full text-left px-2 py-1 rounded hover:bg-gray-50 text-[12px] flex items-center gap-2"
                  >
                    <span className="truncate font-medium text-gray-700">{u.email || u.id}</span>
                    {u.platform_admin && (
                      <span className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">
                        admin
                      </span>
                    )}
                    <span className="text-[10px] text-gray-400 ml-auto truncate">
                      {u.memberships.length === 0
                        ? "no memberships"
                        : u.memberships.map((m) => `${m.tenant_slug}:${m.role}`).join(" · ")}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Small "signed in as X · logout" badge for the dashboard chrome.
 * Posts to /api/dev/logout then triggers the parent refresh.
 */
export function AccountBadge({
  email,
  displayName,
  onLoggedOut,
}: {
  email: string;
  displayName: string;
  onLoggedOut: () => void;
}) {
  const [busy, setBusy] = useState(false);
  async function logout() {
    setBusy(true);
    try {
      await fetch("/api/dev/logout", { method: "POST" });
    } finally {
      setBusy(false);
      onLoggedOut();
    }
  }
  return (
    <div className="flex items-center gap-2 text-[11px] text-gray-500">
      <span title={email} className="truncate max-w-[160px]">
        {displayName || email}
      </span>
      <button
        onClick={logout}
        disabled={busy}
        className="text-[11px] text-[#B8462A] hover:underline disabled:opacity-50"
      >
        {busy ? "…" : "logout"}
      </button>
    </div>
  );
}
