"""Request-time auth resolution.

Phase 2 contract: every protected route depends on ``current_user``. The
function reads identity headers injected by the reverse proxy and translates
them into a stored :class:`users.User` (creating the record on first sight).

Header contract:

- ``X-User-Id``    — opaque IdP subject id (treated as identity primary key)
- ``X-User-Email`` — primary email (used as the secondary lookup so the
                     user's memberships survive IdP subject-id rotation)

Local development never sees those headers. The ``DEV_USER_ID`` /
``DEV_USER_EMAIL`` env vars fill them in for ``bash web/dev.sh`` workflows;
unauthenticated requests fail with 401 in production-shaped deployments where
the dev shim is absent.

Role enforcement (``require_tenant_role``, ``require_platform_admin``,
``resolve_project_role``) lands in Phase 3 alongside the route migration to
``/api/tenants/{t}/...``. Phase 2 deliberately ships identity-only so the
dashboard can render before authz tightens.
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException, Request

import users


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
