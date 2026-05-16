"""Tenant directory reader + mutations.

Phase 2 added read access (``/api/tenants``, ``GET /api/tenants/{t}``).
Phase 3 adds the mutation surface used by tenant management endpoints:
``create_tenant``, ``update_tenant_config``, ``delete_tenant``, plus member
CRUD (``add_member``, ``update_member_role``, ``remove_member``).

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
import re
import shutil
import time
from pathlib import Path

import users
from files import REPO_ROOT
from users import User


TENANTS_ROOT = REPO_ROOT / "tenants"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_VALID_ROLES = {"owner", "editor", "viewer"}
_VALID_FORMATS = {"ppt169", "ppt43", "a4portrait"}


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


def _write_json(p: Path, data) -> None:
    """Atomic write with one-slot backup; mirrors files._atomic_write_directive."""
    import os
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    # Validate roundtrip before touching disk.
    json.loads(body)
    tmp = p.with_suffix(p.suffix + ".tmp")
    bak = p.with_suffix(p.suffix + ".bak")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    if p.exists():
        try:
            shutil.copy2(p, bak)
        except Exception:
            pass
    os.replace(tmp, p)


def _validate_slug_for_create(slug: str) -> str:
    """Stricter than ``_safe_slug``: lowercase alnum + hyphen, ≤63 chars.

    Used at create time so tenant URLs are predictable and DNS-safe. Existing
    tenants are read with the looser guard so we don't break grandfathered
    slugs from the migration.
    """
    s = (slug or "").strip().lower()
    if not _SLUG_RE.match(s):
        raise ValueError(
            "tenant slug must be lowercase alphanumeric (or hyphen), start "
            "with a letter or digit, and be 1–63 characters"
        )
    return s


def create_tenant(
    slug: str,
    name: str,
    owner_user_id: str,
    default_format: str = "ppt169",
) -> dict:
    """Materialize a new tenant directory with a single owner.

    Side effects:
    - Creates ``tenants/<slug>/{config.json, members.json, projects/, templates/,
      design_system/, shared/}``.
    - Adds an ``owner`` membership to the owner's user record so the new
      tenant immediately appears in their ``/api/me`` and ``/api/tenants``.

    Raises:
        ValueError: slug fails validation, format is unknown, or tenant exists.
        FileNotFoundError: owner user record doesn't exist.
    """
    slug = _validate_slug_for_create(slug)
    if default_format not in _VALID_FORMATS:
        raise ValueError(f"invalid default_format {default_format!r}")
    name = (name or "").strip() or slug
    d = TENANTS_ROOT / slug
    if d.exists():
        raise ValueError(f"tenant {slug!r} already exists")
    # Confirm the owner user record actually exists; we shouldn't materialize
    # a tenant whose owner can't be referenced.
    if users._load_user(owner_user_id) is None:
        raise FileNotFoundError(f"owner user {owner_user_id!r} not found")

    d.mkdir(parents=True)
    for sub in ("projects", "templates", "design_system", "shared"):
        (d / sub).mkdir(exist_ok=True)
    now = int(time.time())
    cfg = {
        "slug": slug,
        "name": name,
        "owner_user_id": owner_user_id,
        "default_format": default_format,
        "created_at": now,
    }
    _write_json(d / "config.json", cfg)
    _write_json(d / "members.json", [{"user_id": owner_user_id, "role": "owner"}])
    users.add_membership(owner_user_id, slug, "owner")
    return cfg


def update_tenant_config(slug: str, *, name: str | None = None, default_format: str | None = None) -> dict:
    """Mutate the editable fields of a tenant's config.

    Only ``name`` and ``default_format`` are user-editable here. Slug and
    owner are managed elsewhere (slug is immutable; ownership transfer is a
    separate flow). Raises FileNotFoundError if the tenant doesn't exist.
    """
    cfg = read_tenant_config(slug)
    if cfg is None:
        raise FileNotFoundError(slug)
    if name is not None:
        cleaned = name.strip()
        if cleaned:
            cfg["name"] = cleaned
    if default_format is not None:
        if default_format not in _VALID_FORMATS:
            raise ValueError(f"invalid default_format {default_format!r}")
        cfg["default_format"] = default_format
    _write_json(tenant_dir(slug) / "config.json", cfg)
    return cfg


def delete_tenant(slug: str) -> None:
    """Remove a tenant directory and clean up member references.

    This is destructive — projects, templates, and shared assets under the
    tenant root are all removed. Callers must gate behind platform-admin
    auth. Member memberships are dropped from each user record so a deleted
    tenant doesn't haunt /api/me forever.
    """
    d = tenant_dir(slug)
    if not d.exists():
        raise FileNotFoundError(slug)
    member_records = read_members(slug)
    shutil.rmtree(d)
    for entry in member_records:
        try:
            users.remove_membership(entry["user_id"], slug)
        except Exception:
            # Best effort — don't fail the whole delete on one stale member.
            continue


# ─── Member CRUD ────────────────────────────────────────────────────────────

def _save_members(slug: str, members: list[dict]) -> None:
    _write_json(tenant_dir(slug) / "members.json", members)


def _resolve_user_by_id_or_email(identifier: str) -> User | None:
    """Members API accepts either an id or an email; resolve to a User."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    record = users._load_user(ident)
    if record is not None:
        return User(
            id=record["id"],
            email=record.get("email", ""),
            display_name=record.get("display_name") or record["id"],
            memberships=users._coerce_memberships(record.get("memberships")),
            platform_admin=bool(record.get("platform_admin")),
            created_at=int(record.get("created_at") or 0),
        )
    if "@" in ident:
        return users.find_by_email(ident)
    return None


def add_member(slug: str, identifier: str, role: str) -> dict:
    """Add a user to a tenant by id or email.

    Idempotent: if the user is already a member, their role is updated
    rather than duplicated. The corresponding membership is written to the
    user record so the change appears in /api/me without a re-sync.

    Raises:
        FileNotFoundError: tenant doesn't exist, or user lookup fails.
        ValueError: role is not one of viewer/editor/owner.
    """
    if not tenant_exists(slug):
        raise FileNotFoundError(slug)
    role = (role or "").strip().lower()
    if role not in _VALID_ROLES:
        raise ValueError(f"invalid role {role!r}")
    user = _resolve_user_by_id_or_email(identifier)
    if user is None:
        raise FileNotFoundError(f"user {identifier!r} not found")
    members = read_members(slug)
    found = False
    for i, m in enumerate(members):
        if m["user_id"] == user.id:
            members[i] = {"user_id": user.id, "role": role}
            found = True
            break
    if not found:
        members.append({"user_id": user.id, "role": role})
    _save_members(slug, members)
    users.add_membership(user.id, slug, role)
    return {"user_id": user.id, "email": user.email, "role": role}


def update_member_role(slug: str, user_id: str, role: str) -> dict:
    """Change an existing member's role.

    Refuses to demote the last owner — a tenant with no owners is unreachable
    by tenant-management endpoints. Raises FileNotFoundError when the
    membership doesn't exist.
    """
    if not tenant_exists(slug):
        raise FileNotFoundError(slug)
    role = (role or "").strip().lower()
    if role not in _VALID_ROLES:
        raise ValueError(f"invalid role {role!r}")
    members = read_members(slug)
    target = next((m for m in members if m["user_id"] == user_id), None)
    if target is None:
        raise FileNotFoundError(f"user {user_id!r} is not a member of {slug!r}")
    if target["role"] == "owner" and role != "owner":
        owners = [m for m in members if m["role"] == "owner"]
        if len(owners) <= 1:
            raise ValueError("cannot demote the last owner")
    for i, m in enumerate(members):
        if m["user_id"] == user_id:
            members[i] = {"user_id": user_id, "role": role}
            break
    _save_members(slug, members)
    users.add_membership(user_id, slug, role)
    return {"user_id": user_id, "role": role}


def remove_member(slug: str, user_id: str) -> None:
    """Drop a member from a tenant.

    Refuses to remove the last owner — same reasoning as ``update_member_role``.
    Best-effort drop of the matching membership on the user record so /api/me
    no longer lists a tenant the user can't access.
    """
    if not tenant_exists(slug):
        raise FileNotFoundError(slug)
    members = read_members(slug)
    target = next((m for m in members if m["user_id"] == user_id), None)
    if target is None:
        raise FileNotFoundError(f"user {user_id!r} is not a member of {slug!r}")
    if target["role"] == "owner":
        owners = [m for m in members if m["role"] == "owner"]
        if len(owners) <= 1:
            raise ValueError("cannot remove the last owner")
    new_members = [m for m in members if m["user_id"] != user_id]
    _save_members(slug, new_members)
    users.remove_membership(user_id, slug)


# ─── Design system ──────────────────────────────────────────────────────────
# tenants/<slug>/design_system/{palette.json, typography.json, spec.md}
#
# Read by the agent (so generated decks pick up the tenant's brand without
# re-discovery) and editable by tenant owners through the Settings page.
# Stored as on-disk JSON/markdown so it's diffable and grep-able instead of
# living in a DB row.


def _design_system_dir(slug: str) -> Path:
    return tenant_dir(slug) / "design_system"


def read_design_system(slug: str) -> dict:
    """Return ``{palette, typography, spec}`` for the tenant, defaulting empty.

    Missing files are not an error — a freshly-created tenant has none until
    the owner saves through the editor. The shape is stable so the frontend
    can always render the form.
    """
    if not tenant_exists(slug):
        raise FileNotFoundError(slug)
    d = _design_system_dir(slug)
    palette = _read_json(d / "palette.json") if d.exists() else None
    typography = _read_json(d / "typography.json") if d.exists() else None
    spec_path = d / "spec.md"
    spec = ""
    if spec_path.exists():
        try:
            spec = spec_path.read_text(encoding="utf-8")
        except Exception:
            spec = ""
    return {
        "palette": palette if isinstance(palette, dict) else {},
        "typography": typography if isinstance(typography, dict) else {},
        "spec": spec,
    }


def write_design_system(
    slug: str,
    *,
    palette: dict | None = None,
    typography: dict | None = None,
    spec: str | None = None,
) -> dict:
    """Persist any provided slice of the design system; leaves others alone.

    Validates that ``palette`` and ``typography`` are plain dicts so the
    on-disk JSON stays parseable. Returns the merged read after the write so
    callers see the canonical post-save state.
    """
    if not tenant_exists(slug):
        raise FileNotFoundError(slug)
    d = _design_system_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    if palette is not None:
        if not isinstance(palette, dict):
            raise ValueError("palette must be an object")
        _write_json(d / "palette.json", palette)
    if typography is not None:
        if not isinstance(typography, dict):
            raise ValueError("typography must be an object")
        _write_json(d / "typography.json", typography)
    if spec is not None:
        if not isinstance(spec, str):
            raise ValueError("spec must be a string")
        # Atomic write — same approach as _write_json minus the JSON encode.
        import os
        body = spec
        tmp = (d / "spec.md").with_suffix(".md.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, d / "spec.md")
    return read_design_system(slug)


# ─── Shared brand assets ────────────────────────────────────────────────────
# tenants/<slug>/shared/ holds reusable logos, fonts, illustrations the tenant
# wants available across all projects without re-uploading per deck.


_MAX_SHARED_BYTES = 25 * 1024 * 1024  # 25 MB per file; aggressive enough to
# keep the upload form honest but loose enough for vector logos + small fonts.

_DENY_SHARED_EXT = {
    ".exe", ".dll", ".so", ".bin", ".sh", ".bat", ".cmd",
    ".js", ".mjs", ".cjs", ".html", ".htm", ".php", ".py",
}


def shared_dir(slug: str) -> Path:
    return tenant_dir(slug) / "shared"


def _safe_shared_rel(slug: str, rel: str) -> Path:
    """Resolve a caller-supplied path under ``shared/``, refusing escape.

    Accepts subpaths like ``logos/dark.png``. Rejects absolute paths, ``..``
    segments, hidden segments, and anything that lands outside ``shared/``.
    """
    base = shared_dir(slug).resolve()
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        raise ValueError("path is required")
    parts = Path(rel).parts
    if any(p in {"", ".", ".."} or p.startswith(".") for p in parts):
        raise ValueError("invalid path")
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError("path escape")
    return target


def list_shared(slug: str) -> list[dict]:
    """List shared assets as ``{path, size, mtime, kind}`` tuples.

    Walks the whole subtree (logos/, fonts/, etc.) so the UI can render a
    flat picker; the path key carries the subdirectory.
    """
    if not tenant_exists(slug):
        raise FileNotFoundError(slug)
    base = shared_dir(slug)
    if not base.exists():
        return []
    out: list[dict] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        rel = p.relative_to(base).as_posix()
        out.append({
            "path": rel,
            "size": p.stat().st_size,
            "mtime": int(p.stat().st_mtime),
            "kind": p.suffix.lstrip(".").lower(),
        })
    return out


def save_shared_asset(slug: str, rel: str, blob: bytes) -> dict:
    """Write a single shared asset, refusing extension blacklist + oversize.

    The caller passes the destination path (e.g. ``logos/dark.png``); the
    server validates it is inside ``shared/`` and the extension isn't on the
    denylist (executable / scripty types — we serve these to browsers).
    """
    if not tenant_exists(slug):
        raise FileNotFoundError(slug)
    target = _safe_shared_rel(slug, rel)
    if target.suffix.lower() in _DENY_SHARED_EXT:
        raise ValueError(f"file type {target.suffix!r} is not allowed in shared/")
    if len(blob) > _MAX_SHARED_BYTES:
        raise ValueError(f"file exceeds {_MAX_SHARED_BYTES} bytes")
    target.parent.mkdir(parents=True, exist_ok=True)
    import os
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    return {
        "path": rel.lstrip("/"),
        "size": target.stat().st_size,
        "mtime": int(target.stat().st_mtime),
        "kind": target.suffix.lstrip(".").lower(),
    }


def delete_shared_asset(slug: str, rel: str) -> None:
    if not tenant_exists(slug):
        raise FileNotFoundError(slug)
    target = _safe_shared_rel(slug, rel)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(rel)
    target.unlink()
    # Prune empty parent directories, but stop at shared/ itself.
    parent = target.parent
    base = shared_dir(slug).resolve()
    while parent != base and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


# ─── Project ACL (Phase 8) ──────────────────────────────────────────────────
# Per §6.2 / §7.4: a project's .project.json carries optional `acl` (per-user
# grants) and `shared_with_tenants` (whole-tenant grants capped at editor).
# These helpers read/write that file atomically and never invent semantics —
# resolution lives in ``authz.resolve_project_role``.

_PROJECT_ACL_ROLES = {"viewer", "editor", "owner"}


def _project_meta_path(slug: str, project_name: str) -> Path:
    if not project_name or "/" in project_name or ".." in project_name or project_name.startswith("."):
        raise ValueError(f"invalid project name: {project_name!r}")
    return tenant_dir(slug) / "projects" / project_name / ".project.json"


def read_project_meta(slug: str, project_name: str) -> dict:
    """Return the project's ACL metadata (always with both keys present).

    Missing file returns the empty shape ``{"acl": [], "shared_with_tenants": []}``
    so callers don't branch on existence. Raises ``FileNotFoundError`` if the
    *project* itself is missing — distinguishing "no shares yet" from "no
    such project" is load-bearing for the share endpoints.
    """
    meta_path = _project_meta_path(slug, project_name)
    project_root = meta_path.parent
    if not project_root.exists() or not project_root.is_dir():
        raise FileNotFoundError(project_name)
    if not meta_path.exists():
        return {"acl": [], "shared_with_tenants": []}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {"acl": [], "shared_with_tenants": []}
    acl = data.get("acl") if isinstance(data.get("acl"), list) else []
    shared = data.get("shared_with_tenants") if isinstance(data.get("shared_with_tenants"), list) else []
    cleaned_acl = []
    for e in acl:
        if not isinstance(e, dict):
            continue
        uid = str(e.get("user_id", "")).strip()
        role = str(e.get("role", "")).strip().lower()
        if uid and role in _PROJECT_ACL_ROLES:
            cleaned_acl.append({"user_id": uid, "role": role})
    cleaned_shared = sorted({str(s).strip() for s in shared if isinstance(s, str) and s.strip()})
    out = dict(data)
    out["acl"] = cleaned_acl
    out["shared_with_tenants"] = cleaned_shared
    return out


def _write_project_meta(slug: str, project_name: str, meta: dict) -> None:
    meta_path = _project_meta_path(slug, project_name)
    if not meta_path.parent.exists():
        raise FileNotFoundError(project_name)
    _write_json(meta_path, meta)


def add_project_share_user(slug: str, project_name: str, user_id: str, role: str) -> dict:
    """Grant ``user_id`` the given role on the project. Idempotent on user_id —
    re-sharing with a different role overwrites in place."""
    role = (role or "").strip().lower()
    if role not in _PROJECT_ACL_ROLES:
        raise ValueError(f"invalid role: {role!r}")
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    meta = read_project_meta(slug, project_name)
    acl = [e for e in meta.get("acl", []) if e.get("user_id") != uid]
    acl.append({"user_id": uid, "role": role})
    meta["acl"] = acl
    _write_project_meta(slug, project_name, meta)
    return meta


def add_project_share_tenant(slug: str, project_name: str, tenant_slug: str) -> dict:
    """Grant every member of ``tenant_slug`` the project's home-tenant role
    capped at editor. Resolution still happens at request time; this only
    records the grant."""
    target = _safe_slug(tenant_slug)
    if not tenant_exists(target):
        raise FileNotFoundError(f"tenant not found: {tenant_slug}")
    if target == slug:
        raise ValueError("cannot share a project with its own home tenant")
    meta = read_project_meta(slug, project_name)
    shared = set(meta.get("shared_with_tenants", []))
    shared.add(target)
    meta["shared_with_tenants"] = sorted(shared)
    _write_project_meta(slug, project_name, meta)
    return meta


def remove_project_share(slug: str, project_name: str, principal: str) -> dict:
    """Remove a principal grant by its ``user:<id>`` or ``tenant:<slug>`` token.

    Returns the updated meta. Raises ``KeyError`` when the principal is not
    currently listed — the route layer surfaces that as 404 so the UI can
    distinguish "already gone" from a transient failure."""
    if not principal or ":" not in principal:
        raise ValueError("principal must be 'user:<id>' or 'tenant:<slug>'")
    kind, _, value = principal.partition(":")
    value = value.strip()
    if not value:
        raise ValueError("principal value missing")
    meta = read_project_meta(slug, project_name)
    if kind == "user":
        before = len(meta.get("acl", []))
        meta["acl"] = [e for e in meta.get("acl", []) if e.get("user_id") != value]
        if len(meta["acl"]) == before:
            raise KeyError(principal)
    elif kind == "tenant":
        shared = set(meta.get("shared_with_tenants", []))
        if value not in shared:
            raise KeyError(principal)
        shared.discard(value)
        meta["shared_with_tenants"] = sorted(shared)
    else:
        raise ValueError(f"unknown principal kind: {kind!r}")
    _write_project_meta(slug, project_name, meta)
    return meta


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
