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


def import_source(project_dir: Path, upload_path: Path, original_name: str) -> Path:
    """Place a user upload into the project sources/ folder, converting if needed."""
    sources = project_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    dest = sources / original_name
    shutil.move(str(upload_path), dest)

    suffix = dest.suffix.lower()
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
    """Yield filesystem events for svg_output/ and exports/ until stop_event is set."""
    targets = [project_dir / "svg_output", project_dir / "exports"]
    targets = [t for t in targets if t.exists()] or [project_dir]
    async for changes in awatch(*[str(t) for t in targets], stop_event=stop_event, recursive=True):
        for change, path in changes:
            yield {
                "kind": Change(change).name.lower(),
                "path": str(Path(path).relative_to(project_dir)),
            }
