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

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import registry
from files import (
    PROJECTS_DIR,
    REPO_ROOT,
    project_path,
    list_slides,
    list_exports,
    init_project,
    import_source,
    import_url,
    watch_project,
)


app = FastAPI(title="PPT Master Web", version="0.1.0")

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


class ChatBody(BaseModel):
    text: str


class UrlImportBody(BaseModel):
    url: str


class AttachSessionBody(BaseModel):
    name: str


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


@app.post("/api/sessions")
async def create_session(body: CreateSessionBody):
    try:
        proj = init_project(body.name, body.format)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    session = await registry.create(proj)
    return {
        "session_id": session.id,
        "project": proj.name,
        "project_path": str(proj.relative_to(REPO_ROOT)),
    }


@app.post("/api/sessions/attach")
async def attach_session(body: AttachSessionBody):
    """Attach a session to an existing project (no init)."""
    proj = project_path(body.name)
    if not proj.exists():
        raise HTTPException(status_code=404, detail=f"project not found: {body.name}")
    session = await registry.create(proj)
    return {"session_id": session.id, "project": proj.name}


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


@app.post("/api/sessions/{sid}/import/file")
async def import_file(sid: str, file: UploadFile = File(...)):
    session = registry.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    suffix = Path(file.filename or "upload").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
        chunk = await file.read(1024 * 1024)
        while chunk:
            tmp.write(chunk)
            chunk = await file.read(1024 * 1024)

    try:
        result = import_source(session.project_path, tmp_path, file.filename or tmp_path.name)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "imported": str(result.relative_to(REPO_ROOT)),
        "is_markdown": result.suffix.lower() == ".md",
    }


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


@app.get("/api/projects/{name}/svg/{filename}")
def serve_svg(name: str, filename: str):
    if not filename.endswith(".svg") or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = project_path(name) / "svg_output" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


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
