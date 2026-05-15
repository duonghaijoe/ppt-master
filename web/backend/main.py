"""FastAPI entry point.

Run:
    cd web/backend
    ../../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8787 --reload
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import registry
from authz import current_user, require_platform_admin, require_tenant_role
import tenants
import users
from users import User
from files import (
    PROJECTS_DIR,
    REPO_ROOT,
    project_path,
    list_slides,
    list_dir_slides,
    list_exports,
    list_recent,
    list_tree,
    deck_summary,
    read_output_dirs,
    register_output_dir,
    write_output_dirs,
    safe_rel,
    init_project,
    import_source,
    import_url,
    watch_project,
)
from svg_inline import fold_icons, inline_icons
from templates import (
    create_project_from_template,
    delete_template,
    list_templates,
    read_template,
    save_project_as_template,
    template_file_path,
)


app = FastAPI(title="Awesome Deck — AI-powered presentations", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CreateSessionBody(BaseModel):
    name: str
    format: str = "ppt169"
    permission_mode: str = "auto"  # "auto" | "confirm"
    model_tier: str = "auto"  # "auto" | "light" | "general" | "premium"


class ChatBody(BaseModel):
    text: str


class UrlImportBody(BaseModel):
    url: str


class AttachSessionBody(BaseModel):
    name: str
    permission_mode: str = "auto"
    model_tier: str = "auto"


class PermissionDecisionBody(BaseModel):
    request_id: str
    decision: str  # "approve" | "deny"


class PermissionModeBody(BaseModel):
    mode: str  # "auto" | "confirm"


class ModelTierBody(BaseModel):
    tier: str  # "auto" | "light" | "general" | "premium"


class SvgSaveBody(BaseModel):
    content: str


class OutputDirsBody(BaseModel):
    current: str
    dirs: list[dict]


class OutputDirsCurrentBody(BaseModel):
    current: str


class OutputDirsRegisterBody(BaseModel):
    dir: str
    label: str | None = None
    set_current: bool = False


class SaveTemplateBody(BaseModel):
    source_project: str
    name: str
    description: str | None = None
    # Project-relative paths (under images/) of brand assets to include.
    # The agent curates this list from SKILL.md so we don't accidentally
    # ship per-deck illustrations. Empty list = no images.
    include_images: list[str] = []


class CreateFromTemplateBody(BaseModel):
    template: str
    project_name: str
    format: str | None = None


class CreateTenantBody(BaseModel):
    slug: str
    name: str
    default_format: str = "ppt169"
    owner_user_id: str | None = None  # platform-admin only field; defaults to caller


class UpdateTenantBody(BaseModel):
    name: str | None = None
    default_format: str | None = None


class AddMemberBody(BaseModel):
    user: str  # id or email
    role: str  # viewer | editor | owner


class UpdateMemberBody(BaseModel):
    role: str


@app.get("/api/health")
def health():
    return {"ok": True, "repo_root": str(REPO_ROOT)}


# ─── Identity & tenant directory ───────────────────────────────────────────
# Phase 2 wiring: the proxy injects X-User-Id + X-User-Email, authz.current_user
# resolves them to a stored User (creating on first sight) and stashes it on
# request.state.user. Route-level enforcement (require_tenant_role, etc.) lands
# in Phase 3 once the tenant-prefixed routes are in.

@app.get("/api/me")
def me(user: User = Depends(current_user)):
    """Current caller — id, email, memberships, platform_admin."""
    return user.to_public()


@app.get("/api/tenants")
def list_tenants(user: User = Depends(current_user)):
    """Tenants the caller can see, with their role on each.

    Platform admins get the full directory; everyone else sees only the
    tenants referenced in their memberships.
    """
    return {"tenants": tenants.list_for_user(user)}


@app.get("/api/tenants/{slug}")
def tenant_detail(slug: str, user: User = Depends(current_user)):
    """One tenant's public config. Phase 2 leaves this readable to any member;
    Phase 3 narrows write surfaces with require_tenant_role."""
    cfg = tenants.read_tenant_config(slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    is_member = any(m.tenant_slug == slug for m in user.memberships)
    if not (user.platform_admin or is_member):
        raise HTTPException(status_code=403, detail="not a member of this tenant")
    role = next(
        (m.role for m in user.memberships if m.tenant_slug == slug),
        "admin" if user.platform_admin else None,
    )
    return {
        "slug": cfg.get("slug", slug),
        "name": cfg.get("name") or slug,
        "default_format": cfg.get("default_format") or "ppt169",
        "owner_user_id": cfg.get("owner_user_id"),
        "created_at": cfg.get("created_at"),
        "role": role,
    }


# ─── Tenant CRUD (platform-admin only for create/delete) ────────────────────

@app.post("/api/tenants")
def tenants_create(body: CreateTenantBody, user: User = Depends(require_platform_admin)):
    """Create a new tenant. Platform-admin only.

    The owner defaults to the caller; pass ``owner_user_id`` to assign someone
    else (the user record must already exist).
    """
    owner = (body.owner_user_id or user.id).strip()
    try:
        cfg = tenants.create_tenant(body.slug, body.name, owner, body.default_format)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return cfg


@app.patch("/api/tenants/{slug}")
def tenants_update(slug: str, body: UpdateTenantBody, _: User = Depends(require_tenant_role("owner"))):
    """Edit a tenant's name / default_format. Tenant owners (or platform admins)."""
    try:
        return tenants.update_tenant_config(
            slug, name=body.name, default_format=body.default_format
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="tenant not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/tenants/{slug}")
def tenants_delete(slug: str, _: User = Depends(require_platform_admin)):
    """Destroy a tenant — projects, templates, shared assets, memberships."""
    try:
        tenants.delete_tenant(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="tenant not found")
    return {"deleted": slug}


# ─── Member CRUD (owner-level on the tenant) ────────────────────────────────

@app.get("/api/tenants/{slug}/members")
def members_list(slug: str, _: User = Depends(require_tenant_role("viewer"))):
    """Any member can see the roster."""
    return {"members": tenants.read_members(slug)}


@app.post("/api/tenants/{slug}/members")
def members_add(slug: str, body: AddMemberBody, _: User = Depends(require_tenant_role("owner"))):
    try:
        return tenants.add_member(slug, body.user, body.role)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/tenants/{slug}/members/{user_id}")
def members_update(slug: str, user_id: str, body: UpdateMemberBody, _: User = Depends(require_tenant_role("owner"))):
    try:
        return tenants.update_member_role(slug, user_id, body.role)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/tenants/{slug}/members/{user_id}")
def members_remove(slug: str, user_id: str, _: User = Depends(require_tenant_role("owner"))):
    try:
        tenants.remove_member(slug, user_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"removed": user_id}


# ─── Tenant-prefixed project routes ─────────────────────────────────────────
# These mirror /api/projects/{name}/* but enforce tenant membership and
# resolve paths under tenants/<slug>/projects/. The legacy /api/projects/*
# routes still work and resolve under tenants/default/ for backwards
# compatibility — they are deprecated and will redirect in a later phase.

@app.get("/api/tenants/{slug}/projects")
def tenant_projects_list(slug: str, _: User = Depends(require_tenant_role("viewer"))):
    from files import tenant_projects_dir
    base = tenant_projects_dir(slug)
    if not base.exists():
        return {"tenant": slug, "projects": []}
    items = []
    for p in sorted(base.iterdir()):
        if not p.is_dir():
            continue
        items.append({
            "name": p.name,
            "slides": len(list_slides(p.name, slug)),
            "exports": len(list_exports(p.name, slug)),
            "mtime": int(p.stat().st_mtime),
        })
    return {"tenant": slug, "projects": items}


@app.get("/api/tenants/{slug}/projects/{name}/slides")
def tenant_slides(slug: str, name: str, dir: str | None = None, _: User = Depends(require_tenant_role("viewer"))):
    if dir:
        return {"project": name, "slides": list_dir_slides(name, dir, slug)}
    return {"project": name, "slides": list_slides(name, slug)}


@app.get("/api/tenants/{slug}/projects/{name}/exports")
def tenant_exports(slug: str, name: str, _: User = Depends(require_tenant_role("viewer"))):
    return {"project": name, "exports": list_exports(name, slug)}


@app.get("/api/tenants/{slug}/projects/{name}/recent")
def tenant_recent(slug: str, name: str, limit: int = 20, _: User = Depends(require_tenant_role("viewer"))):
    if not project_path(name, slug).exists():
        raise HTTPException(status_code=404, detail="project not found")
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    return {
        "project": name,
        "deck": deck_summary(name, slug),
        "recent": list_recent(name, limit=limit, slug=slug),
    }


@app.get("/api/tenants/{slug}/projects/{name}/tree")
def tenant_project_tree(slug: str, name: str, path: str = "", _: User = Depends(require_tenant_role("viewer"))):
    if not project_path(name, slug).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return list_tree(name, path, slug)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")


@app.get("/api/tenants/{slug}/projects/{name}/file")
def tenant_project_file(slug: str, name: str, path: str, _: User = Depends(require_tenant_role("viewer"))):
    if not project_path(name, slug).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        target = safe_rel(name, path, slug)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    if target.suffix.lower() == ".svg":
        try:
            body = inline_icons(target.read_text(encoding="utf-8"))
        except Exception:
            return FileResponse(target, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
        return Response(content=body, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
    return FileResponse(target, headers={"Cache-Control": "no-store"})


@app.get("/api/tenants/{slug}/projects/{name}/output-dirs")
def tenant_output_dirs(slug: str, name: str, _: User = Depends(require_tenant_role("viewer"))):
    if not project_path(name, slug).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        directive = read_output_dirs(name, slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="project not found")
    enriched = []
    for entry in directive["dirs"]:
        slides = list_dir_slides(name, entry["dir"], slug)
        enriched.append({
            **entry,
            "slides": slides,
            "count": len(slides),
            "mtime": max((s["mtime"] for s in slides), default=0),
        })
    return {"current": directive["current"], "dirs": enriched}


@app.put("/api/tenants/{slug}/projects/{name}/output-dirs")
def tenant_put_output_dirs(slug: str, name: str, body: OutputDirsBody, _: User = Depends(require_tenant_role("editor"))):
    if not project_path(name, slug).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return write_output_dirs(name, body.model_dump(), slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/tenants/{slug}/projects/{name}/output-dirs/register")
def tenant_register_dir(slug: str, name: str, body: OutputDirsRegisterBody, _: User = Depends(require_tenant_role("editor"))):
    if not project_path(name, slug).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return register_output_dir(name, body.dir, body.label, body.set_current, slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="project not found")


# ─── Tenant-prefixed template routes ────────────────────────────────────────

@app.get("/api/tenants/{slug}/templates")
def tenant_templates_list(slug: str, _: User = Depends(require_tenant_role("viewer"))):
    return {"templates": list_templates(slug)}


@app.get("/api/tenants/{slug}/templates/{name}")
def tenant_templates_detail(slug: str, name: str, _: User = Depends(require_tenant_role("viewer"))):
    try:
        return read_template(name, slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/tenants/{slug}/templates")
def tenant_templates_create(slug: str, body: SaveTemplateBody, _: User = Depends(require_tenant_role("editor"))):
    try:
        return save_project_as_template(
            body.source_project, body.name, body.description or "", body.include_images, slug
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="source project not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/tenants/{slug}/templates/{name}")
def tenant_templates_delete(slug: str, name: str, _: User = Depends(require_tenant_role("editor"))):
    try:
        delete_template(name, slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted": name}


@app.get("/api/projects")
def projects_list(_: User = Depends(current_user)):
    """Legacy: lists projects under tenants/default/. Use the tenant-prefixed
    route ``/api/tenants/{slug}/projects`` for non-default tenants."""
    if not PROJECTS_DIR.exists():
        return {"projects": []}
    items = []
    for p in sorted(PROJECTS_DIR.iterdir()):
        if not p.is_dir():
            continue
        items.append({
            "name": p.name,
            "slides": len(list_slides(p.name)),
            "exports": len(list_exports(p.name)),
            "mtime": int(p.stat().st_mtime),
        })
    return {"projects": items}


def _norm_mode(m: str) -> str:
    return "confirm" if m == "confirm" else "auto"


# UI tier → CLI model alias. We deliberately don't surface raw model IDs to
# users; the tier is the contract.
_TIER_TO_MODEL: dict[str, Optional[str]] = {
    "auto": None,
    "light": "haiku",
    "general": "sonnet",
    "premium": "opus",
}


def _norm_tier(t: str) -> str:
    return t if t in _TIER_TO_MODEL else "auto"


def _ensure_default_viewer(user: User) -> None:
    """Legacy-route read check for the ``default`` tenant.

    Any membership on ``default`` is sufficient; platform admins bypass.
    Used by legacy ``/api/projects/...`` and ``/api/templates/...`` reads
    that don't have ``{slug}`` in the path and can't use ``require_tenant_role``.
    """
    if user.platform_admin:
        return
    for m in user.memberships:
        if m.tenant_slug == "default":
            return
    raise HTTPException(status_code=403, detail="not a member of tenant 'default'")


def _ensure_default_editor(user: User) -> None:
    """Legacy-route write check for the ``default`` tenant.

    Same shape as :func:`_ensure_default_viewer` but requires editor+ role.
    """
    if user.platform_admin:
        return
    for m in user.memberships:
        if m.tenant_slug == "default" and m.role in {"editor", "owner"}:
            return
    raise HTTPException(status_code=403, detail="requires editor on tenant 'default'")


def _resolve_session_for_user(sid: str, user: User):
    """Fetch a session and verify the caller created it (or is platform admin).

    Sessions carry ``user_id`` so cross-user access is denied even within the
    same tenant — the chat surface is single-user. 404 (not 403) when the
    session doesn't exist *or* belongs to someone else; we don't want a
    probing caller to learn that a sid is real but theirs to read.
    """
    session = registry.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if user.platform_admin:
        return session
    if session.user_id and session.user_id != user.id:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@app.post("/api/sessions")
async def create_session(
    body: CreateSessionBody,
    user: User = Depends(current_user),
):
    """Legacy: defaults to the ``default`` tenant. Tenant-prefixed callers
    should use ``POST /api/tenants/{slug}/sessions`` instead."""
    _ensure_default_editor(user)
    try:
        proj = init_project(body.name, body.format, slug="default")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    session = await registry.create(proj, tenant_slug="default", user_id=user.id)
    session.permission_mode = _norm_mode(body.permission_mode)
    tier = _norm_tier(body.model_tier)
    session.model = _TIER_TO_MODEL[tier]
    return {
        "session_id": session.id,
        "project": proj.name,
        "project_path": str(proj.relative_to(REPO_ROOT)),
        "tenant_slug": "default",
        "permission_mode": session.permission_mode,
        "model_tier": tier,
    }


@app.post("/api/sessions/attach")
async def attach_session(
    body: AttachSessionBody,
    user: User = Depends(current_user),
):
    """Attach a session to an existing project (no init). Legacy default-tenant alias."""
    _ensure_default_editor(user)
    proj = project_path(body.name, slug="default")
    if not proj.exists():
        raise HTTPException(status_code=404, detail=f"project not found: {body.name}")
    session = await registry.create(proj, tenant_slug="default", user_id=user.id)
    session.permission_mode = _norm_mode(body.permission_mode)
    tier = _norm_tier(body.model_tier)
    session.model = _TIER_TO_MODEL[tier]
    return {
        "session_id": session.id,
        "project": proj.name,
        "tenant_slug": "default",
        "permission_mode": session.permission_mode,
        "model_tier": tier,
    }


@app.post("/api/tenants/{slug}/sessions")
async def create_tenant_session(
    slug: str,
    body: CreateSessionBody,
    user: User = Depends(require_tenant_role("editor")),
):
    """Create a session bound to a specific tenant. Caller must be at
    least editor on the tenant — viewers cannot run agent chats."""
    try:
        proj = init_project(body.name, body.format, slug=slug)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    session = await registry.create(proj, tenant_slug=slug, user_id=user.id)
    session.permission_mode = _norm_mode(body.permission_mode)
    tier = _norm_tier(body.model_tier)
    session.model = _TIER_TO_MODEL[tier]
    return {
        "session_id": session.id,
        "project": proj.name,
        "project_path": str(proj.relative_to(REPO_ROOT)),
        "tenant_slug": slug,
        "permission_mode": session.permission_mode,
        "model_tier": tier,
    }


@app.post("/api/tenants/{slug}/sessions/attach")
async def attach_tenant_session(
    slug: str,
    body: AttachSessionBody,
    user: User = Depends(require_tenant_role("editor")),
):
    """Attach a session to an existing project under a specific tenant."""
    proj = project_path(body.name, slug=slug)
    if not proj.exists():
        raise HTTPException(status_code=404, detail=f"project not found: {body.name}")
    session = await registry.create(proj, tenant_slug=slug, user_id=user.id)
    session.permission_mode = _norm_mode(body.permission_mode)
    tier = _norm_tier(body.model_tier)
    session.model = _TIER_TO_MODEL[tier]
    return {
        "session_id": session.id,
        "project": proj.name,
        "tenant_slug": slug,
        "permission_mode": session.permission_mode,
        "model_tier": tier,
    }


@app.post("/api/sessions/{sid}/permission_mode")
def set_mode(sid: str, body: PermissionModeBody, user: User = Depends(current_user)):
    session = _resolve_session_for_user(sid, user)
    session.permission_mode = _norm_mode(body.mode)
    return {"permission_mode": session.permission_mode}


@app.post("/api/sessions/{sid}/model_tier")
async def set_model_tier(
    sid: str, body: ModelTierBody, user: User = Depends(current_user)
):
    session = _resolve_session_for_user(sid, user)
    tier = _norm_tier(body.tier)
    await session.set_model(_TIER_TO_MODEL[tier])
    return {"model_tier": tier}


@app.post("/api/sessions/{sid}/interrupt")
async def interrupt_session(sid: str, user: User = Depends(current_user)):
    session = _resolve_session_for_user(sid, user)
    delivered = await session.interrupt()
    return {"ok": True, "delivered": delivered}


@app.post("/api/sessions/{sid}/permission")
def resolve_permission(
    sid: str, body: PermissionDecisionBody, user: User = Depends(current_user)
):
    session = _resolve_session_for_user(sid, user)
    decision = "approve" if body.decision == "approve" else "deny"
    ok = session.resolve_permission(body.request_id, decision)
    if not ok:
        raise HTTPException(status_code=404, detail="request not pending")
    return {"ok": True, "decision": decision}


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str, user: User = Depends(current_user)):
    # Look up before removing so we can enforce ownership.
    _resolve_session_for_user(sid, user)
    await registry.remove(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/message")
async def send_message(sid: str, body: ChatBody, user: User = Depends(current_user)):
    session = _resolve_session_for_user(sid, user)

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            async for evt in session.send(body.text):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode("utf-8")
        except Exception as e:
            err = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(err)}\n\n".encode("utf-8")

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


async def _save_upload_to_project(session, file: UploadFile, relpath: Optional[str] = None) -> dict:
    """Save one UploadFile into the session's project, returning a JSON-friendly dict."""
    name = relpath or file.filename or "upload"
    suffix = Path(name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
        chunk = await file.read(1024 * 1024)
        while chunk:
            tmp.write(chunk)
            chunk = await file.read(1024 * 1024)
    result = import_source(session.project_path, tmp_path, Path(name).name)
    return {
        "name": name,
        "imported": str(result.relative_to(REPO_ROOT)),
        "is_markdown": result.suffix.lower() == ".md",
        "is_image": result.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"},
    }


@app.post("/api/sessions/{sid}/import/file")
async def import_file(
    sid: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
):
    session = _resolve_session_for_user(sid, user)
    try:
        return await _save_upload_to_project(session, file)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sessions/{sid}/import/files")
async def import_files(
    sid: str,
    files: list[UploadFile] = File(...),
    paths: Optional[list[str]] = Form(None),
    user: User = Depends(current_user),
):
    """Multi-file upload from chat (supports drag-drop of folders + images)."""
    session = _resolve_session_for_user(sid, user)

    results: list[dict] = []
    errors: list[dict] = []
    for i, f in enumerate(files):
        rel = paths[i] if paths and i < len(paths) else None
        try:
            results.append(await _save_upload_to_project(session, f, rel))
        except Exception as e:
            errors.append({"name": rel or f.filename, "error": str(e)})
    return {"imported": results, "errors": errors}


@app.post("/api/sessions/{sid}/import/url")
async def import_url_route(
    sid: str, body: UrlImportBody, user: User = Depends(current_user)
):
    session = _resolve_session_for_user(sid, user)
    try:
        result = import_url(session.project_path, body.url)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"imported": str(result.relative_to(REPO_ROOT))}


@app.get("/api/projects/{name}/slides")
def slides(name: str, dir: str | None = None, user: User = Depends(current_user)):
    """List SVG slides. Defaults to `svg_output/` for back-compat.

    When `dir` is supplied, lists `*.svg` under that project-relative folder
    instead — used by Workbench to render decks in registered working dirs
    other than `svg_output/` (svg_final, flashcards/templates, …).
    """
    _ensure_default_viewer(user)
    if dir:
        return {"project": name, "slides": list_dir_slides(name, dir)}
    return {"project": name, "slides": list_slides(name)}


@app.get("/api/projects/{name}/exports")
def exports(name: str, user: User = Depends(current_user)):
    _ensure_default_viewer(user)
    return {"project": name, "exports": list_exports(name)}


@app.get("/api/projects/{name}/recent")
def recent(name: str, limit: int = 20, user: User = Depends(current_user)):
    """Workbench feed: recent artifacts in this project, newest first.

    Returns a separate `deck` summary so the UI can offer the rendered slide
    deck as a single virtual entry instead of N individual SVGs.
    """
    _ensure_default_viewer(user)
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    return {
        "project": name,
        "deck": deck_summary(name),
        "recent": list_recent(name, limit=limit),
    }


@app.get("/api/projects/{name}/tree")
def project_tree(name: str, path: str = "", user: User = Depends(current_user)):
    _ensure_default_viewer(user)
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return list_tree(name, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")


@app.get("/api/projects/{name}/file")
def project_file(name: str, path: str, user: User = Depends(current_user)):
    _ensure_default_viewer(user)
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        target = safe_rel(name, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    if target.suffix.lower() == ".svg":
        # Mirror /svg/{filename}: expand <use data-icon> placeholders so the
        # browser preview matches the post-export PPTX rendering instead of
        # showing empty circles where icons should be.
        try:
            body = inline_icons(target.read_text(encoding="utf-8"))
        except Exception:
            return FileResponse(target, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
        return Response(content=body, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
    return FileResponse(target, headers={"Cache-Control": "no-store"})


@app.get("/api/projects/{name}/web/{path:path}")
def project_web(name: str, path: str, user: User = Depends(current_user)):
    """Path-style file server for web previews.

    The query-string-based /file endpoint can't host HTML composites: a
    `<img src="./foo.png">` inside an iframe at `/file?path=report.html`
    resolves to `/foo.png`, not `/file?path=foo.png`. Serving the same
    files under a clean path lets relative URLs resolve naturally inside
    the project, so HTML artifacts with sibling assets render in the
    workbench iframe.
    """
    _ensure_default_viewer(user)
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        target = safe_rel(name, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    if target.suffix.lower() == ".svg":
        try:
            body = inline_icons(target.read_text(encoding="utf-8"))
        except Exception:
            return FileResponse(target, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
        return Response(content=body, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
    return FileResponse(target, headers={"Cache-Control": "no-store"})


_IMAGE_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


@app.get("/api/projects/{name}/svg/{filename}")
def serve_svg(name: str, filename: str, user: User = Depends(current_user)):
    _ensure_default_viewer(user)
    if not filename.endswith(".svg") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = project_path(name) / "svg_output" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    # Expand <use data-icon="…"/> placeholders so the browser can render
    # icons. The source file is untouched — the export pipeline does its
    # own expansion when producing PPTX.
    raw = path.read_text(encoding="utf-8")
    try:
        body = inline_icons(raw)
    except Exception:
        body = raw
    return Response(
        content=body,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/projects/{name}/output-dirs")
def output_dirs(name: str, user: User = Depends(current_user)):
    """Return the project's output_dirs directive, enriched with slide listings.

    The directive is the single source of truth for Workbench: it lists every
    deck the agent has produced (svg_output/, svg_final/, flashcards/…), each
    with a friendly label the UI displays in place of the raw path.
    """
    _ensure_default_viewer(user)
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        directive = read_output_dirs(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="project not found")
    enriched = []
    for entry in directive["dirs"]:
        slides = list_dir_slides(name, entry["dir"])
        enriched.append({
            **entry,
            "slides": slides,
            "count": len(slides),
            "mtime": max((s["mtime"] for s in slides), default=0),
        })
    return {"current": directive["current"], "dirs": enriched}


@app.put("/api/projects/{name}/output-dirs")
def put_output_dirs(name: str, body: OutputDirsBody, user: User = Depends(current_user)):
    """Replace the directive (used by the agent to register a new working dir)."""
    _ensure_default_editor(user)
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return write_output_dirs(name, body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/projects/{name}/output-dirs/current")
def set_output_dirs_current(
    name: str, body: OutputDirsCurrentBody, user: User = Depends(current_user)
):
    """Flip the active working dir (used when the user picks one in Workbench)."""
    _ensure_default_editor(user)
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    directive = read_output_dirs(name)
    target = body.current.strip("/")
    if target not in {d["dir"] for d in directive["dirs"]}:
        raise HTTPException(status_code=400, detail="dir not registered")
    directive["current"] = target
    return write_output_dirs(name, directive)


@app.post("/api/projects/{name}/output-dirs/register")
def register_dir(
    name: str, body: OutputDirsRegisterBody, user: User = Depends(current_user)
):
    """Append (or update) a single deck entry without resending the full list.

    This is the safe path for the agent: it preserves every existing dir, so
    a forgetful caller can never accidentally unregister other decks.
    """
    _ensure_default_editor(user)
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return register_output_dir(name, body.dir, body.label, body.set_current)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="project not found")


@app.post("/api/projects/{name}/svg/{filename}")
def save_svg(
    name: str, filename: str, body: SvgSaveBody, user: User = Depends(current_user)
):
    """Overwrite an existing SVG in svg_output/. Refuses if the target file
    is missing or the path tries to escape the folder."""
    _ensure_default_editor(user)
    if not filename.endswith(".svg") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    folder = project_path(name) / "svg_output"
    target = folder / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    # Path containment guard.
    try:
        target.resolve().relative_to(folder.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path escape")
    text = body.content
    # Cheap sanity check: must contain an <svg ...> tag near the start.
    head = text.lstrip()[:200].lower()
    if "<svg" not in head:
        raise HTTPException(status_code=400, detail="not an SVG document")
    # Restore <use data-icon="…"/> placeholders. The GET endpoint expands
    # them so the browser can render real geometry; without this fold, an
    # editor save would persist the expansion and lose the abstraction.
    try:
        text = fold_icons(text)
    except Exception:
        pass
    target.write_text(text, encoding="utf-8")
    return {"ok": True, "bytes": len(text.encode("utf-8"))}


@app.post("/api/projects/{name}/svg-save")
def save_svg_path(
    name: str, path: str, body: SvgSaveBody, user: User = Depends(current_user)
):
    """Path-based save: overwrites any existing `.svg` inside the project.

    The legacy `/svg/{filename}` endpoint is hardcoded to svg_output/. This
    one accepts any registered working dir (svg_final, flashcards/templates, …)
    so the SlideEditor stays useful when the user switches decks.
    """
    _ensure_default_editor(user)
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        target = safe_rel(name, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if target.suffix.lower() != ".svg":
        raise HTTPException(status_code=400, detail="not an svg path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    text = body.content
    head = text.lstrip()[:200].lower()
    if "<svg" not in head:
        raise HTTPException(status_code=400, detail="not an SVG document")
    try:
        text = fold_icons(text)
    except Exception:
        pass
    target.write_text(text, encoding="utf-8")
    return {"ok": True, "bytes": len(text.encode("utf-8"))}


@app.get("/api/projects/{name}/images/{filename}")
def serve_project_image(name: str, filename: str, user: User = Depends(current_user)):
    """Serve files from <project>/images/. Required so SVGs in svg_output/
    can reference ../images/foo.png via relative URL when rendered in browser."""
    _ensure_default_viewer(user)
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = project_path(name) / "images" / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = _IMAGE_MEDIA.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-store"})


@app.get("/api/projects/{name}/export.pptx")
def serve_latest_pptx(name: str, user: User = Depends(current_user)):
    _ensure_default_viewer(user)
    items = list_exports(name)
    if not items:
        raise HTTPException(status_code=404, detail="no exports")
    path = project_path(name) / "exports" / items[0]["file"]
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=items[0]["file"],
    )


# ─── Templates ─────────────────────────────────────────────────────────────
# Reusable design DNA snapshots. See web/backend/templates.py for the
# packaging contract. Agent-facing flow: user says "save this as a template"
# → agent picks brand image files from SKILL.md context → POST /api/templates.

@app.get("/api/templates")
def templates_list(user: User = Depends(current_user)):
    _ensure_default_viewer(user)
    return {"templates": list_templates()}


@app.get("/api/templates/{name}")
def templates_detail(name: str, user: User = Depends(current_user)):
    _ensure_default_viewer(user)
    try:
        return read_template(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/templates/{name}/file")
def templates_file(name: str, path: str = "", user: User = Depends(current_user)):
    """Serve a file from a template directory (thumbnail, SKILL.md, etc.)."""
    _ensure_default_viewer(user)
    try:
        target = template_file_path(name, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    media = "application/octet-stream"
    suffix = target.suffix.lower()
    if suffix == ".svg":
        media = "image/svg+xml"
    elif suffix in {".md", ".txt"}:
        media = "text/plain; charset=utf-8"
    elif suffix == ".json":
        media = "application/json"
    elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        media = f"image/{ 'jpeg' if suffix == '.jpg' else suffix[1:] }"
    return FileResponse(target, media_type=media, headers={"Cache-Control": "no-store"})


@app.post("/api/templates")
def templates_create(body: SaveTemplateBody, user: User = Depends(current_user)):
    _ensure_default_editor(user)
    try:
        return save_project_as_template(
            body.source_project,
            body.name,
            body.description or "",
            body.include_images,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="source project not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/templates/{name}")
def templates_delete(name: str, user: User = Depends(current_user)):
    _ensure_default_editor(user)
    try:
        delete_template(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted": name}


@app.post("/api/projects/from-template")
def projects_from_template(
    body: CreateFromTemplateBody, user: User = Depends(current_user)
):
    _ensure_default_editor(user)
    try:
        project_dir = create_project_from_template(
            body.template,
            body.project_name,
            body.format,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "project": project_dir.name,
        "path": str(project_dir.relative_to(REPO_ROOT)),
    }


@app.get("/api/projects/{name}/events")
async def project_events(name: str, user: User = Depends(current_user)):
    _ensure_default_viewer(user)
    proj = project_path(name)
    if not proj.exists():
        raise HTTPException(status_code=404, detail="project not found")

    stop_event = asyncio.Event()

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for evt in watch_project(proj, stop_event):
                yield f"data: {json.dumps(evt)}\n\n".encode("utf-8")
        except asyncio.CancelledError:
            stop_event.set()
            raise

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# Serve built frontend if present
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
