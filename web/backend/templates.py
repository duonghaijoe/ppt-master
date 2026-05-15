"""Template packaging and instantiation.

A template is a frozen snapshot of a finished project's *design DNA* — the
files needed to start a new deck with the same brand identity. It is **not**
a backup: source documents, generated SVGs, exports, and per-deck content
images are intentionally left behind.

Layout under ``<repo>/templates/<name>/``:

- ``template.json``     — metadata (name, description, source project, format,
                          included image paths, created_at)
- ``SKILL.md``          — copied from the source project (brand palette,
                          typography, header pattern, regen commands)
- ``design_spec.md``    — copied if present
- ``spec_lock.md``      — copied if present (locked tokens)
- ``templates/``        — copied recursively if the source has one (chrome,
                          master layouts)
- ``images/<rel>``      — only the brand assets the caller explicitly lists
                          via ``include_images``; nothing else is copied
- ``thumbnail.svg``     — first slide from ``svg_final/`` or ``svg_output/``,
                          best-effort preview for the Dashboard card
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from files import DEFAULT_TENANT_ROOT, PROJECTS_DIR, REPO_ROOT, project_path, safe_rel


# Templates live under the default tenant root in the post-Phase-1 layout. The
# directory is created lazily by save_project_as_template; if it doesn't exist
# yet (clean repos), reads just return [] as before.
TEMPLATES_DIR = DEFAULT_TENANT_ROOT / "templates"

# What we copy verbatim if it exists in the source project. The keys are
# project-relative paths; bool indicates whether the entry is a directory.
_COPY_IF_PRESENT: list[tuple[str, bool]] = [
    ("SKILL.md", False),
    ("design_spec.md", False),
    ("spec_lock.md", False),
    ("templates", True),
]

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _sanitize_template_name(name: str) -> str:
    """Coerce a name to filesystem-safe form, refusing the obviously bad ones.

    The directory layout is shared across users on the host; an unconstrained
    name lets a caller traverse out of ``templates/`` or collide with internal
    files. Stricter than project names because templates are reusable scaffold,
    not user-flavored.
    """
    n = (name or "").strip()
    if not n:
        raise ValueError("template name is required")
    # Map spaces and a few common separators to underscore so users can paste
    # "Cantonese Flashcards" without thinking about it.
    n = re.sub(r"[\s./]+", "_", n)
    if not _NAME_RE.match(n):
        raise ValueError(
            "template name must start with a letter/digit and contain only "
            "letters, digits, underscores, or hyphens (max 64 chars)"
        )
    return n


def _template_path(name: str) -> Path:
    safe = _sanitize_template_name(name)
    return TEMPLATES_DIR / safe


def _safe_image_target(project: Path, rel: str) -> Path | None:
    """Resolve a caller-supplied image path inside the project's ``images/``.

    The agent passes relative paths; refuse anything that escapes ``images/``
    or doesn't exist. Returns None when the entry is invalid — the caller
    skips silently rather than 400ing the whole save so a single bad entry
    doesn't lose the rest of the template.
    """
    rel = (rel or "").strip().lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    # Normalize "images/x.png" or just "x.png" → both end up as project/images/x.png.
    if rel.startswith("images/"):
        rel = rel[len("images/") :]
    target = (project / "images" / rel).resolve()
    images_root = (project / "images").resolve()
    try:
        target.relative_to(images_root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


def _read_metadata(path: Path) -> dict:
    """Best-effort read of a template's ``template.json``."""
    meta_path = path / "template.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _summarize(path: Path) -> dict:
    """Public metadata shape returned by list/detail endpoints."""
    meta = _read_metadata(path)
    return {
        "name": path.name,
        "description": meta.get("description", ""),
        "source_project": meta.get("source_project", ""),
        "format": meta.get("format", ""),
        "created_at": meta.get("created_at", int(path.stat().st_mtime)),
        "has_thumbnail": (path / "thumbnail.svg").exists(),
        "brand_files": meta.get("brand_files", []),
    }


def list_templates() -> list[dict]:
    if not TEMPLATES_DIR.exists():
        return []
    out: list[dict] = []
    for p in sorted(TEMPLATES_DIR.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        # Require at least a template.json so half-written entries don't
        # show up while the agent is mid-save.
        if not (p / "template.json").exists():
            continue
        out.append(_summarize(p))
    return out


def read_template(name: str) -> dict:
    """Return template metadata; raises FileNotFoundError when missing."""
    p = _template_path(name)
    if not p.is_dir() or not (p / "template.json").exists():
        raise FileNotFoundError(name)
    return _summarize(p)


def _detect_format(project_name: str, fallback: str = "ppt169") -> str:
    """Project names follow ``<slug>_<format>_<date>``; pull the format out."""
    parts = project_name.rsplit("_", 2)
    if len(parts) == 3 and parts[1] in {"ppt169", "ppt43", "a4portrait"}:
        return parts[1]
    return fallback


def _pick_thumbnail(project: Path) -> Path | None:
    """First slide of svg_final/ → svg_output/ → None.

    Templates exist to seed new decks, so the freshest finished slide is the
    most useful preview. Falls back through the working dir for in-progress
    projects.
    """
    for sub in ("svg_final", "svg_output"):
        d = project / sub
        if d.is_dir():
            candidates = sorted(d.glob("*.svg"))
            if candidates:
                return candidates[0]
    return None


def save_project_as_template(
    source_project: str,
    name: str,
    description: str = "",
    include_images: list[str] | None = None,
) -> dict:
    """Snapshot the design DNA of ``source_project`` into a reusable template.

    ``include_images`` is an agent-curated list of paths under the project's
    ``images/`` directory. The backend never guesses what counts as a brand
    asset vs. one-off slide content — that decision needs SKILL.md context
    the agent has and we don't.

    Raises:
        FileNotFoundError: source project doesn't exist.
        ValueError:        name fails validation, or template already exists.
    """
    project = project_path(source_project)
    if not project.exists():
        raise FileNotFoundError(source_project)
    target = _template_path(name)
    if target.exists():
        raise ValueError(f"template {name!r} already exists; delete it first to replace")

    TEMPLATES_DIR.mkdir(exist_ok=True)
    target.mkdir(parents=True)

    copied_brand: list[str] = []
    copied_design_files: list[str] = []

    try:
        for rel, is_dir in _COPY_IF_PRESENT:
            src = project / rel
            if not src.exists():
                continue
            dst = target / rel
            if is_dir:
                if not any(src.rglob("*")):
                    continue  # empty dir — nothing useful to seed with
                shutil.copytree(src, dst, dirs_exist_ok=False)
            else:
                shutil.copy2(src, dst)
            copied_design_files.append(rel)

        for rel in include_images or []:
            src = _safe_image_target(project, rel)
            if src is None:
                continue
            rel_clean = src.relative_to(project / "images").as_posix()
            dst = target / "images" / rel_clean
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied_brand.append(rel_clean)

        thumb = _pick_thumbnail(project)
        if thumb is not None:
            shutil.copy2(thumb, target / "thumbnail.svg")

        meta = {
            "name": target.name,
            "description": (description or "").strip(),
            "source_project": source_project,
            "format": _detect_format(source_project),
            "created_at": int(time.time()),
            "design_files": copied_design_files,
            "brand_files": copied_brand,
        }
        (target / "template.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        # Roll back a partial template so list_templates doesn't show a husk.
        shutil.rmtree(target, ignore_errors=True)
        raise

    return _summarize(target)


def delete_template(name: str) -> None:
    p = _template_path(name)
    if not p.is_dir():
        raise FileNotFoundError(name)
    # Guard against path escape — _template_path already sanitizes, but a
    # defensive containment check costs nothing and shields against future
    # refactors that loosen the name regex.
    try:
        p.resolve().relative_to(TEMPLATES_DIR.resolve())
    except ValueError:
        raise ValueError(f"refusing to delete outside templates/: {name!r}")
    shutil.rmtree(p)


def create_project_from_template(
    template_name: str,
    project_name: str,
    fmt: str | None = None,
) -> Path:
    """Initialize a new project pre-populated with the template's artifacts.

    Calls project_manager.py init to create the standard scaffold, then layers
    the template files on top. Existing scaffold files are preserved unless
    the template explicitly provides a replacement (e.g., template SKILL.md
    overwrites the empty default).
    """
    from files import init_project  # local import to avoid circulars

    template = _template_path(template_name)
    if not template.is_dir() or not (template / "template.json").exists():
        raise FileNotFoundError(template_name)
    meta = _read_metadata(template)

    chosen_fmt = (fmt or meta.get("format") or "ppt169").strip()
    if chosen_fmt not in {"ppt169", "ppt43", "a4portrait"}:
        raise ValueError(f"invalid format {chosen_fmt!r}")

    project_dir = init_project(project_name, chosen_fmt)

    # Layer template artifacts on top of the fresh scaffold. shutil.copytree
    # with dirs_exist_ok merges subtrees; copy2 overwrites individual files.
    for rel, is_dir in _COPY_IF_PRESENT:
        src = template / rel
        if not src.exists():
            continue
        dst = project_dir / rel
        if is_dir:
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)

    src_images = template / "images"
    if src_images.is_dir():
        shutil.copytree(src_images, project_dir / "images", dirs_exist_ok=True)

    # Drop a marker so the agent (and any future audit) can see where this
    # project came from without parsing project names. Pure provenance —
    # never read back by the backend itself.
    provenance = {
        "template": template.name,
        "template_source_project": meta.get("source_project", ""),
        "instantiated_at": int(time.time()),
    }
    (project_dir / ".template_origin.json").write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return project_dir


def template_file_path(name: str, rel: str) -> Path:
    """Resolve a file under a template, refusing path escape.

    Used by the file-serving endpoint to render thumbnails / SKILL.md previews
    in the UI without giving callers free filesystem access.
    """
    base = _template_path(name).resolve()
    if not base.is_dir():
        raise FileNotFoundError(name)
    rel = (rel or "").lstrip("/")
    target = (base / rel).resolve() if rel else base
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError("path escape")
    return target


# Keep PROJECTS_DIR referenced so a future refactor that drops it from `files`
# breaks here loudly instead of silently. (No-op at runtime.)
_ = PROJECTS_DIR
