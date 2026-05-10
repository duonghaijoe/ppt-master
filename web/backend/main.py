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

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import registry
from files import (
    PROJECTS_DIR,
    REPO_ROOT,
    project_path,
    list_slides,
    list_exports,
    list_recent,
    list_tree,
    deck_summary,
    safe_rel,
    init_project,
    import_source,
    import_url,
    watch_project,
)
from svg_inline import fold_icons, inline_icons


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


@app.get("/api/health")
def health():
    return {"ok": True, "repo_root": str(REPO_ROOT)}


@app.get("/api/projects")
def projects_list():
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


@app.post("/api/sessions")
async def create_session(body: CreateSessionBody):
    try:
        proj = init_project(body.name, body.format)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    session = await registry.create(proj)
    session.permission_mode = _norm_mode(body.permission_mode)
    tier = _norm_tier(body.model_tier)
    session.model = _TIER_TO_MODEL[tier]
    return {
        "session_id": session.id,
        "project": proj.name,
        "project_path": str(proj.relative_to(REPO_ROOT)),
        "permission_mode": session.permission_mode,
        "model_tier": tier,
    }


@app.post("/api/sessions/attach")
async def attach_session(body: AttachSessionBody):
    """Attach a session to an existing project (no init)."""
    proj = project_path(body.name)
    if not proj.exists():
        raise HTTPException(status_code=404, detail=f"project not found: {body.name}")
    session = await registry.create(proj)
    session.permission_mode = _norm_mode(body.permission_mode)
    tier = _norm_tier(body.model_tier)
    session.model = _TIER_TO_MODEL[tier]
    return {
        "session_id": session.id,
        "project": proj.name,
        "permission_mode": session.permission_mode,
        "model_tier": tier,
    }


@app.post("/api/sessions/{sid}/permission_mode")
def set_mode(sid: str, body: PermissionModeBody):
    session = registry.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    session.permission_mode = _norm_mode(body.mode)
    return {"permission_mode": session.permission_mode}


@app.post("/api/sessions/{sid}/model_tier")
async def set_model_tier(sid: str, body: ModelTierBody):
    session = registry.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    tier = _norm_tier(body.tier)
    await session.set_model(_TIER_TO_MODEL[tier])
    return {"model_tier": tier}


@app.post("/api/sessions/{sid}/interrupt")
async def interrupt_session(sid: str):
    session = registry.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    delivered = await session.interrupt()
    return {"ok": True, "delivered": delivered}


@app.post("/api/sessions/{sid}/permission")
def resolve_permission(sid: str, body: PermissionDecisionBody):
    session = registry.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    decision = "approve" if body.decision == "approve" else "deny"
    ok = session.resolve_permission(body.request_id, decision)
    if not ok:
        raise HTTPException(status_code=404, detail="request not pending")
    return {"ok": True, "decision": decision}


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    await registry.remove(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/message")
async def send_message(sid: str, body: ChatBody):
    session = registry.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

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
async def import_file(sid: str, file: UploadFile = File(...)):
    session = registry.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        return await _save_upload_to_project(session, file)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sessions/{sid}/import/files")
async def import_files(
    sid: str,
    files: list[UploadFile] = File(...),
    paths: Optional[list[str]] = Form(None),
):
    """Multi-file upload from chat (supports drag-drop of folders + images)."""
    session = registry.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

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
async def import_url_route(sid: str, body: UrlImportBody):
    session = registry.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        result = import_url(session.project_path, body.url)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"imported": str(result.relative_to(REPO_ROOT))}


@app.get("/api/projects/{name}/slides")
def slides(name: str):
    return {"project": name, "slides": list_slides(name)}


@app.get("/api/projects/{name}/exports")
def exports(name: str):
    return {"project": name, "exports": list_exports(name)}


@app.get("/api/projects/{name}/recent")
def recent(name: str, limit: int = 20):
    """Workbench feed: recent artifacts in this project, newest first.

    Returns a separate `deck` summary so the UI can offer the rendered slide
    deck as a single virtual entry instead of N individual SVGs.
    """
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
def project_tree(name: str, path: str = ""):
    if not project_path(name).exists():
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return list_tree(name, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")


@app.get("/api/projects/{name}/file")
def project_file(name: str, path: str):
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
def project_web(name: str, path: str):
    """Path-style file server for web previews.

    The query-string-based /file endpoint can't host HTML composites: a
    `<img src="./foo.png">` inside an iframe at `/file?path=report.html`
    resolves to `/foo.png`, not `/file?path=foo.png`. Serving the same
    files under a clean path lets relative URLs resolve naturally inside
    the project, so HTML artifacts with sibling assets render in the
    workbench iframe.
    """
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
def serve_svg(name: str, filename: str):
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


@app.post("/api/projects/{name}/svg/{filename}")
def save_svg(name: str, filename: str, body: SvgSaveBody):
    """Overwrite an existing SVG in svg_output/. Refuses if the target file
    is missing or the path tries to escape the folder."""
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


@app.get("/api/projects/{name}/images/{filename}")
def serve_project_image(name: str, filename: str):
    """Serve files from <project>/images/. Required so SVGs in svg_output/
    can reference ../images/foo.png via relative URL when rendered in browser."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = project_path(name) / "images" / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = _IMAGE_MEDIA.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-store"})


@app.get("/api/projects/{name}/export.pptx")
def serve_latest_pptx(name: str):
    items = list_exports(name)
    if not items:
        raise HTTPException(status_code=404, detail="no exports")
    path = project_path(name) / "exports" / items[0]["file"]
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=items[0]["file"],
    )


@app.get("/api/projects/{name}/events")
async def project_events(name: str):
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
