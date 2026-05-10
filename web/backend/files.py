"""Project file helpers: list/serve SVG slides, run import scripts, watch for changes."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import AsyncIterator

from watchfiles import Change, awatch


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTS_DIR = REPO_ROOT / "projects"
SCRIPTS_DIR = REPO_ROOT / "skills" / "ppt-master" / "scripts"
SOURCE_TO_MD_DIR = SCRIPTS_DIR / "source_to_md"


def _python_bin() -> str:
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else "python3"


def project_path(name: str) -> Path:
    safe = name.strip().replace("/", "_").replace("..", "_")
    return PROJECTS_DIR / safe


def safe_rel(name: str, rel: str) -> Path:
    """Resolve project_path(name) / rel, refusing path escape."""
    base = project_path(name).resolve()
    rel = (rel or "").lstrip("/")
    target = (base / rel).resolve() if rel else base
    if base != target and base not in target.parents:
        raise ValueError("path escape")
    return target


_HIDDEN = {".DS_Store"}

# Directories that should never appear in workbench/event streams. Sources are
# user inputs, not artifacts; the rest are tooling noise.
_NOISE_DIRS = {
    "sources",
    "__pycache__",
    "node_modules",
    ".venv",
    ".git",
    ".idea",
    ".vscode",
    ".pytest_cache",
}


def _is_noise(rel: Path) -> bool:
    return any(part.startswith(".") or part in _NOISE_DIRS for part in rel.parts)


def list_tree(name: str, rel: str = "") -> dict:
    target = safe_rel(name, rel)
    if not target.exists():
        raise FileNotFoundError(rel or "/")
    if target.is_file():
        st = target.stat()
        return {
            "type": "file",
            "name": target.name,
            "path": rel,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        }
    entries = []
    for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if p.name in _HIDDEN or p.name.startswith("."):
            continue
        st = p.stat()
        entries.append({
            "name": p.name,
            "is_dir": p.is_dir(),
            "size": None if p.is_dir() else st.st_size,
            "mtime": int(st.st_mtime),
        })
    return {"type": "dir", "path": rel, "entries": entries}


def list_slides(name: str) -> list[dict]:
    p = project_path(name) / "svg_output"
    if not p.exists():
        return []
    out = []
    for svg in sorted(p.glob("*.svg")):
        out.append({
            "name": svg.stem,
            "file": svg.name,
            "mtime": int(svg.stat().st_mtime),
        })
    return out


def list_recent(name: str, limit: int = 20) -> list[dict]:
    """Recent artifact files in a project, newest first.

    Excludes input materials (sources/), tooling noise (.git, node_modules, …),
    hidden files, and the rendered slide deck under svg_output/ (those are
    aggregated as a single 'Slide deck' workbench entry on the frontend).
    """
    base = project_path(name)
    if not base.exists():
        return []
    items: list[dict] = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        if _is_noise(rel):
            continue
        if p.name in _HIDDEN:
            continue
        if rel.parts and rel.parts[0] == "svg_output":
            continue
        st = p.stat()
        items.append({
            "path": str(rel),
            "name": p.name,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def deck_summary(name: str) -> dict | None:
    """Aggregate metadata for the rendered slide deck, or None if empty."""
    slides = list_slides(name)
    if not slides:
        return None
    return {
        "count": len(slides),
        "mtime": max(s["mtime"] for s in slides),
    }


def list_exports(name: str) -> list[dict]:
    p = project_path(name) / "exports"
    if not p.exists():
        return []
    return [
        {"file": f.name, "mtime": int(f.stat().st_mtime), "bytes": f.stat().st_size}
        for f in sorted(p.glob("*.pptx"), key=lambda x: x.stat().st_mtime, reverse=True)
    ]


def init_project(name: str, fmt: str = "ppt169") -> Path:
    cmd = [_python_bin(), str(SCRIPTS_DIR / "project_manager.py"), "init", name, "--format", fmt]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"project_manager.py init failed:\n{proc.stderr}")
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("/") and (REPO_ROOT in Path(line).parents or "projects/" in line):
            p = Path(line)
            if p.exists():
                return p
    candidates = sorted(PROJECTS_DIR.glob(f"{name}_{fmt}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError(f"project_manager.py init produced no project dir.\nstdout: {proc.stdout}")
    return candidates[0]


_IMPORT_SCRIPTS = {
    ".pdf": "pdf_to_md.py",
    ".docx": "doc_to_md.py",
    ".doc": "doc_to_md.py",
    ".pptx": "ppt_to_md.py",
    ".ppt": "ppt_to_md.py",
    ".epub": "doc_to_md.py",
    ".ipynb": "doc_to_md.py",
    ".md": None,
    ".txt": None,
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def _safe_basename(name: str) -> str:
    """Return a clean basename — strip directories and invalid chars."""
    base = Path(name).name or "upload"
    # Disallow leading dots so we don't write hidden files.
    return base.lstrip(".") or "upload"


def _unique_dest(folder: Path, name: str) -> Path:
    """Return a non-clobbering destination path inside `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    base = _safe_basename(name)
    target = folder / base
    if not target.exists():
        return target
    stem, suffix = Path(base).stem, Path(base).suffix
    i = 1
    while True:
        candidate = folder / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def import_source(project_dir: Path, upload_path: Path, original_name: str) -> Path:
    """Place a user upload into the project, routing by file type.

    - Documents (pdf, docx, etc.) → sources/, converted to .md when possible.
    - Images (png, jpg, svg, …)  → images/.
    - Anything else              → sources/ as-is (no conversion).
    """
    suffix = Path(original_name).suffix.lower()

    if suffix in _IMAGE_EXTS:
        dest = _unique_dest(project_dir / "images", original_name)
        shutil.move(str(upload_path), dest)
        return dest

    sources = project_dir / "sources"
    dest = _unique_dest(sources, original_name)
    shutil.move(str(upload_path), dest)

    script = _IMPORT_SCRIPTS.get(suffix)
    if script is None:
        return dest

    cmd = [_python_bin(), str(SOURCE_TO_MD_DIR / script), str(dest)]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{script} failed for {original_name}:\n{proc.stderr}")
    md_candidate = dest.with_suffix(".md")
    return md_candidate if md_candidate.exists() else dest


def import_url(project_dir: Path, url: str) -> Path:
    sources = project_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    cmd = [_python_bin(), str(SOURCE_TO_MD_DIR / "web_to_md.py"), url]
    proc = subprocess.run(cmd, cwd=str(sources), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"web_to_md.py failed for {url}:\n{proc.stderr}")
    mds = sorted(sources.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not mds:
        raise RuntimeError("web_to_md.py produced no markdown file")
    return mds[0]


async def watch_project(project_dir: Path, stop_event: asyncio.Event) -> AsyncIterator[dict]:
    """Yield filesystem events for the whole project, minus tooling noise.

    The workbench needs to react to any artifact change — flashcards, single
    SVGs, markdown drops — not just slides under svg_output/. Hidden dirs,
    sources/ inputs, and tooling caches are filtered out so we don't flood
    the SSE stream.
    """
    async for changes in awatch(str(project_dir), stop_event=stop_event, recursive=True):
        for change, path in changes:
            try:
                rel = Path(path).relative_to(project_dir)
            except ValueError:
                continue
            if _is_noise(rel):
                continue
            if Path(path).name in _HIDDEN:
                continue
            yield {
                "kind": Change(change).name.lower(),
                "path": str(rel),
            }
