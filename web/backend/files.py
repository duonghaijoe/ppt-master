"""Project file helpers: list/serve SVG slides, run import scripts, watch for changes."""
from __future__ import annotations

import asyncio
import json
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
    return list_dir_slides(name, "svg_output")


def list_dir_slides(name: str, rel_dir: str) -> list[dict]:
    """List `*.svg` files in a project subdirectory, sorted by name.

    Returns the same shape as list_slides for backwards compat — adds a `path`
    field so callers in arbitrary working dirs (flashcards/, svg_final/, …)
    can build URLs without re-joining the dir.
    """
    if not rel_dir:
        return []
    try:
        target = safe_rel(name, rel_dir)
    except ValueError:
        return []
    if not target.exists() or not target.is_dir():
        return []
    rel_dir = rel_dir.strip("/")
    out: list[dict] = []
    for svg in sorted(target.glob("*.svg")):
        out.append({
            "name": svg.stem,
            "file": svg.name,
            "path": f"{rel_dir}/{svg.name}" if rel_dir else svg.name,
            "mtime": int(svg.stat().st_mtime),
        })
    return out


# Friendly default labels for well-known project subdirs. The agent can override
# these by writing custom labels into output_dirs.json; the UI just shows
# whatever's there.
_DEFAULT_DIR_LABELS = {
    "svg_output": "Working slides",
    "svg_final": "Finalized deck",
    "templates": "Template SVGs",
}


def _prettify_segment(seg: str) -> str:
    return seg.replace("_", " ").replace("-", " ").strip().capitalize() or seg


def _default_label(rel_dir: str) -> str:
    """Friendly fallback label when the directive doesn't supply one.

    Nested paths get parent context so a flat picker list stays unambiguous
    (e.g., `flashcards/templates` → "Flashcards › Templates"). Well-known
    project dirs (svg_output, svg_final) keep their canonical labels.
    """
    rel = rel_dir.strip("/")
    if not rel:
        return "/"
    if rel in _DEFAULT_DIR_LABELS:
        return _DEFAULT_DIR_LABELS[rel]
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return rel
    # One-level path → just the leaf, prettified.
    if len(parts) == 1:
        return _prettify_segment(parts[0])
    # Nested → join with the breadcrumb separator so the deck picker can show
    # context without leaking the underlying path style.
    return " › ".join(_prettify_segment(p) for p in parts)


def _bootstrap_output_dirs(base: Path) -> dict:
    """Minimal default directive when ``output_dirs.json`` is missing.

    Maintaining the directive is the **agent**'s job — when it creates a new
    output folder it must register it via the API. This bootstrap only covers
    the bare standard layout (svg_output / svg_final) so the UI has something
    to show on a fresh project before the agent has acted.
    """
    candidates: list[tuple[str, int]] = []  # (rel_dir, latest_mtime)
    for rel in ("svg_output", "svg_final"):
        d = base / rel
        if d.is_dir() and any(d.glob("*.svg")):
            mt = max((p.stat().st_mtime for p in d.glob("*.svg")), default=0)
            candidates.append((rel, int(mt)))
    candidates.sort(key=lambda x: (x[1], x[0] == "svg_output"), reverse=True)
    current = candidates[0][0] if candidates else "svg_output"
    dirs = [
        {"dir": rel, "label": _default_label(rel)}
        for rel, _ in candidates
    ] or [{"dir": "svg_output", "label": _default_label("svg_output")}]
    return {"current": current, "dirs": dirs}


def read_output_dirs(name: str) -> dict:
    """Return the project's output_dirs directive, bootstrapping if missing.

    Schema: ``{"current": "<rel>", "dirs": [{"dir": "<rel>", "label": "..."}]}``.
    The file is **agent-owned**: the agent registers each output folder it
    creates (svg_output, svg_final, flashcards/templates, …). This reader
    does not auto-discover dirs — if it's not declared, the UI won't show it.
    """
    base = project_path(name)
    if not base.exists():
        raise FileNotFoundError(name)
    f = base / "output_dirs.json"
    if not f.exists():
        return _bootstrap_output_dirs(base)
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt directive — fall back to disk scan rather than 500ing the UI.
        return _bootstrap_output_dirs(base)
    dirs_in = raw.get("dirs") or []
    dirs: list[dict] = []
    seen: set[str] = set()
    for entry in dirs_in:
        if isinstance(entry, str):
            rel = entry.strip("/")
            label = _default_label(rel)
        elif isinstance(entry, dict):
            rel = str(entry.get("dir") or "").strip("/")
            label = str(entry.get("label") or "").strip() or _default_label(rel)
        else:
            continue
        if not rel or rel in seen:
            continue
        seen.add(rel)
        dirs.append({"dir": rel, "label": label})
    if not dirs:
        return _bootstrap_output_dirs(base)
    current = str(raw.get("current") or "").strip("/")
    if current not in {d["dir"] for d in dirs}:
        current = dirs[0]["dir"]
    return {"current": current, "dirs": dirs}


def _sanitize_dirs(name: str, base: Path, dirs_in) -> list[dict]:
    """Coerce a raw dirs payload into validated [{dir,label}] entries.

    Drops empties, duplicates, non-string types, and paths that escape the
    project root. Never raises — callers decide what to do with an empty
    result. Used by both write_output_dirs and register_output_dir so the
    same containment + dedup logic applies to every write path.
    """
    out: list[dict] = []
    seen: set[str] = set()
    base_resolved = base.resolve()
    for entry in dirs_in or []:
        if isinstance(entry, dict):
            rel = str(entry.get("dir") or "").strip("/")
            label = str(entry.get("label") or "").strip() or _default_label(rel)
        elif isinstance(entry, str):
            rel = entry.strip("/")
            label = _default_label(rel)
        else:
            continue
        if not rel or rel in seen:
            continue
        # Path containment guard — directive must point inside the project.
        try:
            target = safe_rel(name, rel)
        except ValueError:
            continue
        try:
            target.resolve().relative_to(base_resolved)
        except ValueError:
            continue
        seen.add(rel)
        out.append({"dir": rel, "label": label})
    return out


def _atomic_write_directive(base: Path, payload: dict) -> None:
    """Write output_dirs.json atomically with a one-slot backup.

    Failure modes we're guarding against:
    - Agent-side crash mid-write leaving a truncated JSON file (UI would
      silently fall back to bootstrap and forget all registered dirs).
    - Concurrent writes from two callers racing the same file.

    Strategy: serialize → write to .tmp → fsync → rename over the target.
    Keep the previous good version as `.bak` so a corrupt parse upstream can
    recover. ``os.replace`` is atomic on POSIX.
    """
    import os
    target = base / "output_dirs.json"
    tmp = base / "output_dirs.json.tmp"
    bak = base / "output_dirs.json.bak"
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    # Validate roundtrip before touching disk — we serialized it ourselves
    # so this should always pass, but a self-check costs nothing and shields
    # against schema drift sneaking in.
    json.loads(body)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    if target.exists():
        try:
            shutil.copy2(target, bak)
        except Exception:
            pass  # best-effort backup; don't block the write
    os.replace(tmp, target)


def write_output_dirs(name: str, data: dict) -> dict:
    """Persist a sanitized output_dirs directive and return what got written.

    Replaces the whole directive. Prefer ``register_output_dir`` when you just
    want to append a single entry — it avoids the "agent forgot existing
    entries" failure mode entirely.
    """
    base = project_path(name)
    if not base.exists():
        raise FileNotFoundError(name)
    dirs = _sanitize_dirs(name, base, data.get("dirs"))
    if not dirs:
        raise ValueError("output_dirs requires at least one dir")
    current = str(data.get("current") or "").strip("/")
    if current not in {d["dir"] for d in dirs}:
        current = dirs[0]["dir"]
    out = {"current": current, "dirs": dirs}
    _atomic_write_directive(base, out)
    return out


def register_output_dir(
    name: str,
    rel_dir: str,
    label: str | None = None,
    set_current: bool = False,
) -> dict:
    """Append (or update) a single directive entry without resending the list.

    This is the safe append path for the agent: it reads the current directive,
    inserts/updates the one entry, and atomically writes the result. Existing
    entries are preserved, so the agent can never accidentally drop decks by
    forgetting to include them in a full PUT.

    If the dir is already registered, its label is updated when a non-empty
    ``label`` is supplied; otherwise the existing label is kept. When
    ``set_current`` is True, the new dir becomes the active one.
    """
    base = project_path(name)
    if not base.exists():
        raise FileNotFoundError(name)
    rel = (rel_dir or "").strip("/")
    if not rel:
        raise ValueError("dir is required")
    # Reuse the same sanitizer for the single entry — catches path escapes,
    # empty leaf segments, etc., consistent with full-PUT behavior.
    entry = {"dir": rel, "label": (label or "").strip() or _default_label(rel)}
    cleaned = _sanitize_dirs(name, base, [entry])
    if not cleaned:
        raise ValueError(f"invalid output dir: {rel_dir!r}")
    new_entry = cleaned[0]

    current = read_output_dirs(name)
    dirs = list(current["dirs"])
    found = False
    for i, d in enumerate(dirs):
        if d["dir"] == new_entry["dir"]:
            # Preserve label unless the caller supplied a new one.
            if label and label.strip():
                dirs[i] = {"dir": d["dir"], "label": new_entry["label"]}
            found = True
            break
    if not found:
        dirs.append(new_entry)
    active = new_entry["dir"] if set_current else current["current"]
    return write_output_dirs(name, {"current": active, "dirs": dirs})


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
