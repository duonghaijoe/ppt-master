"""Request-time auth resolution and role enforcement.

Two layers:

1. **Identity** — ``current_user`` reads ``X-User-Id`` / ``X-User-Email``
   headers (or the ``DEV_USER_ID`` / ``DEV_USER_EMAIL`` dev shim), translates
   them into a stored :class:`users.User` via ``users.get_or_create``, and
   stashes the result on ``request.state.user`` for downstream helpers.

2. **Authorization** — ``require_tenant_role(min_role)`` and
   ``require_platform_admin`` produce FastAPI dependencies that 403 the
   request if the caller's role at the tenant in the path is below the
   required minimum. Platform admins bypass tenant-role checks.

Role ordering (``viewer < editor < owner``) lives in :data:`_ROLE_RANK`.
Project-level resolution (``resolve_project_role``) walks the project's
``.project.json`` ACL + ``shared_with_tenants`` per §6.2 of the spec — used
by share-aware endpoints; today only the tenant-role layer is wired in.
"""
from __future__ import annotations

import os

from fastapi import Depends, Header, HTTPException, Request

import tenants
import users


_ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


def _dev_identity() -> tuple[str, str] | None:
    """Return (id, email) from env if the dev shim is active.

    The shim only applies when ``DEV_USER_ID`` is set — without it we never
    invent identities, even if ``DEV_USER_EMAIL`` is exported. That keeps prod
    deployments safe: forgetting to wire the reverse proxy fails closed.
    """
    dev_id = (os.environ.get("DEV_USER_ID") or "").strip()
    if not dev_id:
        return None
    dev_email = (os.environ.get("DEV_USER_EMAIL") or "").strip()
    return dev_id, dev_email


def current_user(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> users.User:
    """FastAPI dependency — resolve the caller to a stored User.

    Resolution order:
    1. ``X-User-Id`` + ``X-User-Email`` headers (production: reverse proxy).
    2. ``DEV_USER_ID`` + ``DEV_USER_EMAIL`` env (local dev shim).
    3. 401 — no usable identity.

    The resolved User is stashed on ``request.state.user`` so downstream
    helpers (logging, metering, the in-progress role checks) can read it
    without re-resolving.
    """
    raw_id = (x_user_id or "").strip()
    raw_email = (x_user_email or "").strip()

    if not raw_id:
        dev = _dev_identity()
        if dev is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        raw_id, raw_email = dev

    user = users.get_or_create(raw_id, raw_email)
    request.state.user = user
    return user


def _user_role_at(user: users.User, slug: str) -> str | None:
    """Return the caller's effective role on ``slug``, or None if not a member.

    Platform admins are reported as ``owner`` for ordering purposes — they
    can do anything an owner can in the role checks below. Membership data
    is the source of truth otherwise.
    """
    if user.platform_admin:
        return "owner"
    for m in user.memberships:
        if m.tenant_slug == slug:
            return m.role
    return None


def require_tenant_role(min_role: str):
    """Build a FastAPI dependency that 403s callers below ``min_role``.

    Usage::

        @app.post("/api/tenants/{slug}/projects", dependencies=[Depends(require_tenant_role("editor"))])
        def create_project(slug: str, ...): ...

    The dependency reads ``slug`` from the path (``request.path_params``),
    not the function args, so the same factory works for every route
    regardless of its handler signature. The resolved tenant config is
    cached on ``request.state.tenant`` for handlers to reuse.

    404 is returned when the tenant doesn't exist — preferred over 403 so
    a caller probing for slugs can't enumerate which ones are real but
    closed to them. (Tenant existence is not a secret; tenant membership
    is, and we already 403 below for non-members of an existing tenant.)
    """
    if min_role not in _ROLE_RANK:
        raise ValueError(f"unknown role: {min_role!r}")
    required = _ROLE_RANK[min_role]

    def _dep(request: Request, user: users.User = Depends(current_user)) -> users.User:
        slug = request.path_params.get("slug") or request.path_params.get("tenant")
        if not slug:
            raise HTTPException(status_code=500, detail="route missing {slug} path param")
        cfg = tenants.read_tenant_config(slug)
        if cfg is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        request.state.tenant = cfg
        role = _user_role_at(user, slug)
        if role is None:
            raise HTTPException(status_code=403, detail="not a member of this tenant")
        if _ROLE_RANK[role] < required:
            raise HTTPException(
                status_code=403,
                detail=f"requires {min_role}; you are {role}",
            )
        return user

    return _dep


def require_platform_admin(user: users.User = Depends(current_user)) -> users.User:
    """403 callers without ``platform_admin``."""
    if not user.platform_admin:
        raise HTTPException(status_code=403, detail="platform admin only")
    return user


def resolve_project_role(
    user: users.User, tenant_slug: str, project_name: str
) -> str | None:
    """Resolve the caller's effective role on a specific project per §6.2.

    Resolution order:
    1. ``platform_admin`` → ``owner``.
    2. Any role in the project's home tenant → that role.
    3. Project ``.project.json`` ``acl`` list → the listed role.
    4. Any of the user's tenants in ``shared_with_tenants`` → ``editor``
       (capped — a viewer in their home tenant remains a viewer).
    5. None.

    Returns one of {"viewer", "editor", "owner"} or None when no rule
    grants access.
    """
    if user.platform_admin:
        return "owner"
    home_role = _user_role_at(user, tenant_slug)
    if home_role:
        return home_role
    try:
        meta = tenants.read_project_meta(tenant_slug, project_name)
    except (FileNotFoundError, ValueError):
        return None
    for entry in meta.get("acl") or []:
        if entry.get("user_id") == user.id:
            role = str(entry.get("role", "")).strip().lower()
            if role in _ROLE_RANK:
                return role
    shared = set(meta.get("shared_with_tenants") or [])
    for m in user.memberships:
        if m.tenant_slug in shared:
            return "editor" if m.role == "owner" else m.role
    return None


def require_project_role(min_role: str):
    """Build a FastAPI dependency for project-scoped routes that honors ACL.

    Reads ``slug`` and ``name`` from path params, runs ``resolve_project_role``,
    and 403s callers without sufficient role. The resolved role is stashed
    on ``request.state.project_role`` so handlers can branch on it (e.g.,
    "owner+ can manage shares") without re-resolving.

    Tenant existence still 404s — preferred over 403 for the same reason as
    ``require_tenant_role``: enumeration is annoying but slug existence is
    not the secret being guarded.
    """
    if min_role not in _ROLE_RANK:
        raise ValueError(f"unknown role: {min_role!r}")
    required = _ROLE_RANK[min_role]

    def _dep(request: Request, user: users.User = Depends(current_user)) -> users.User:
        slug = request.path_params.get("slug") or request.path_params.get("tenant")
        name = request.path_params.get("name") or request.path_params.get("project")
        if not slug or not name:
            raise HTTPException(status_code=500, detail="route missing {slug}/{name} path params")
        cfg = tenants.read_tenant_config(slug)
        if cfg is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        request.state.tenant = cfg
        role = resolve_project_role(user, slug, name)
        if role is None:
            raise HTTPException(status_code=403, detail="no access to this project")
        if _ROLE_RANK[role] < required:
            raise HTTPException(
                status_code=403,
                detail=f"requires {min_role}; you are {role}",
            )
        request.state.project_role = role
        return user

    return _dep
