"""Dev-only login simulation.

In production a reverse proxy injects ``X-User-Id`` + ``X-User-Email`` headers
on every request. Locally there's no proxy, so every authenticated route 401s.
This module emulates the proxy from a cookie so a human can flip between
seeded users to validate tenant isolation.

Enabled iff ``PPT_DEV_AUTH=1``. ``install(app)`` is a no-op otherwise so
nothing in this file can leak to prod.

Flow:
1. ``POST /api/dev/login {email, password}`` -> looks up the user, sets
   ``ppt_dev_uid`` cookie. Password is accepted as-is (any non-empty string);
   this exists for UI realism, not security.
2. The middleware reads the cookie and rewrites the request to look like it
   came from the proxy (``X-User-Id`` + ``X-User-Email``). ``current_user``
   then works unchanged.
3. ``POST /api/dev/logout`` clears the cookie.
4. ``GET /api/dev/users`` lists seeded users so the login page can show a
   hint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

import users
from users import USERS_DIR


COOKIE_NAME = "ppt_dev_uid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7


def is_enabled() -> bool:
    return os.environ.get("PPT_DEV_AUTH", "").strip() == "1"


class LoginBody(BaseModel):
    email: str
    password: str | None = None


def _read_user_record(uid: str) -> dict | None:
    f = USERS_DIR / f"{uid}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


class DevAuthMiddleware(BaseHTTPMiddleware):
    """Rewrite the request to look like it came from the proxy.

    Real ``X-User-Id`` header wins if present so a curl with explicit headers
    still works for scripts/tests. Otherwise the ``ppt_dev_uid`` cookie is
    looked up and the matching record's id/email are injected.
    """

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("x-user-id"):
            return await call_next(request)
        uid = request.cookies.get(COOKIE_NAME)
        if uid:
            record = _read_user_record(uid)
            if record:
                scope_headers = list(request.scope["headers"])
                scope_headers.append((b"x-user-id", str(record["id"]).encode("utf-8")))
                email = str(record.get("email", "")).strip()
                if email:
                    scope_headers.append((b"x-user-email", email.encode("utf-8")))
                request.scope["headers"] = scope_headers
        return await call_next(request)


router = APIRouter()


@router.post("/api/dev/login")
def dev_login(body: LoginBody, response: Response):
    """Look up a user by email and set the dev cookie.

    Password is accepted but ignored — this only exists locally. Returns the
    public user record so the frontend doesn't need a second /api/me round trip.
    """
    email = (body.email or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    user = users.find_by_email(email)
    if user is None:
        raise HTTPException(status_code=401, detail="no user with that email")
    response.set_cookie(
        key=COOKIE_NAME,
        value=user.id,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return user.to_public()


@router.post("/api/dev/logout")
def dev_logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/api/dev/users")
def dev_list_users():
    """Enumerate every user record so the login page can hint at seeded accounts."""
    out: list[dict] = []
    if USERS_DIR.exists():
        for f in sorted(USERS_DIR.glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "id": rec.get("id") or f.stem,
                "email": rec.get("email", ""),
                "display_name": rec.get("display_name") or rec.get("id") or f.stem,
                "platform_admin": bool(rec.get("platform_admin")),
                "memberships": [
                    {"tenant_slug": m.get("tenant_slug"), "role": m.get("role")}
                    for m in (rec.get("memberships") or [])
                ],
            })
    return {"users": out}


def install(app: FastAPI) -> None:
    """Wire middleware + routes if ``PPT_DEV_AUTH=1``."""
    if not is_enabled():
        return
    app.add_middleware(DevAuthMiddleware)
    app.include_router(router)
