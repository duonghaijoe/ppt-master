"""Tenant directory reader.

Phase 2 only needs read access: ``/api/tenants`` lists the tenants the caller
is a member of (or all tenants for a platform admin), and ``GET /api/tenants/{t}``
returns one tenant's config. Mutations (create, member CRUD, design-system
edit) land in Phase 3 alongside role enforcement.

Layout on disk (created by ``scripts/migrate_to_multitenant.py``)::

    tenants/<slug>/
        config.json       # slug, name, owner_user_id, default_format, ...
        members.json      # [{user_id, role}]
        design_system/
        templates/
        projects/
        shared/
"""
from __future__ import annotations

import json
from pathlib import Path

from files import REPO_ROOT
from users import User


TENANTS_ROOT = REPO_ROOT / "tenants"


def _safe_slug(slug: str) -> str:
    """Defense in depth — refuse anything that could escape ``tenants/``.

    Slugs are also constrained by the create-tenant API (Phase 3) but the
    listing endpoint takes path parameters from request URLs, so we still
    validate here.
    """
    s = (slug or "").strip().strip("/")
    if not s or s in {".", ".."} or "/" in s or "\\" in s:
        raise ValueError(f"invalid tenant slug: {slug!r}")
    return s


def _read_json(p: Path) -> dict | list | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def tenant_dir(slug: str) -> Path:
    return TENANTS_ROOT / _safe_slug(slug)


def tenant_exists(slug: str) -> bool:
    try:
        return (tenant_dir(slug) / "config.json").exists()
    except ValueError:
        return False


def read_tenant_config(slug: str) -> dict | None:
    """Return the on-disk config or None if the tenant doesn't exist."""
    try:
        d = tenant_dir(slug)
    except ValueError:
        return None
    cfg = _read_json(d / "config.json")
    if not isinstance(cfg, dict):
        return None
    cfg.setdefault("slug", slug)
    return cfg


def read_members(slug: str) -> list[dict]:
    try:
        d = tenant_dir(slug)
    except ValueError:
        return []
    raw = _read_json(d / "members.json")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        uid = str(entry.get("user_id", "")).strip()
        role = str(entry.get("role", "")).strip().lower()
        if uid and role in {"owner", "editor", "viewer"}:
            out.append({"user_id": uid, "role": role})
    return out


def list_all_slugs() -> list[str]:
    if not TENANTS_ROOT.exists():
        return []
    return sorted(
        p.name
        for p in TENANTS_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / "config.json").exists()
    )


def _summarize(slug: str) -> dict | None:
    cfg = read_tenant_config(slug)
    if cfg is None:
        return None
    return {
        "slug": cfg.get("slug", slug),
        "name": cfg.get("name") or slug,
        "default_format": cfg.get("default_format") or "ppt169",
        "owner_user_id": cfg.get("owner_user_id"),
        "created_at": cfg.get("created_at"),
    }


def list_for_user(user: User) -> list[dict]:
    """Tenants the user can see, with their role per tenant.

    Platform admins see every tenant. Regular users see only the tenants
    referenced in their ``memberships`` (and only if the on-disk tenant
    actually exists — a stale membership row doesn't conjure a tenant).
    """
    if user.platform_admin:
        out: list[dict] = []
        for slug in list_all_slugs():
            summary = _summarize(slug)
            if summary is None:
                continue
            # Cross-reference membership for the role label; admins without
            # an explicit membership are reported as ``admin`` to make their
            # status legible in the UI.
            role = next(
                (m.role for m in user.memberships if m.tenant_slug == slug),
                "admin",
            )
            summary["role"] = role
            out.append(summary)
        return out

    seen: set[str] = set()
    out = []
    for m in user.memberships:
        if m.tenant_slug in seen:
            continue
        seen.add(m.tenant_slug)
        summary = _summarize(m.tenant_slug)
        if summary is None:
            continue
        summary["role"] = m.role
        out.append(summary)
    return out
