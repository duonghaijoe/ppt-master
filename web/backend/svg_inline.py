"""Browser-side preview helper: expand `<use data-icon="…"/>` placeholders.

The agent writes icons as `<use data-icon="lib/name" .../>`. Browsers can't
render those — there's no `href` to resolve — so the slide cards show empty
circles. The PPTX export pipeline expands the same placeholders before
rasterizing, but the live preview served by `/api/projects/{name}/svg/{file}`
sends the raw SVG. This module rebuilds the placeholder into a real `<g>` of
path elements so the browser can draw the icon.

The expansion is reversible: every produced `<g>` carries `data-icon-…`
attributes encoding the original `<use>` placeholder, and `fold_icons`
swaps each expanded group back to a `<use>` element. SlideEditor save
serializes the live DOM and POSTs it back — running the saved payload
through `fold_icons` keeps the source file pristine even after edits.
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

from files import REPO_ROOT, SCRIPTS_DIR

_FINALIZE_DIR = SCRIPTS_DIR / "svg_finalize"
if str(_FINALIZE_DIR) not in sys.path:
    sys.path.insert(0, str(_FINALIZE_DIR))

from embed_icons import (  # noqa: E402
    extract_paths_from_icon,
    generate_icon_group,
    parse_use_element,
    resolve_icon_path,
)

_ICONS_DIR = REPO_ROOT / "platform" / "skills" / "ppt-master" / "templates" / "icons"
_USE_ICON_PATTERN = re.compile(r'<use\s+[^>]*data-icon="[^"]*"[^>]*/>')


@lru_cache(maxsize=512)
def _resolve_cached(icon_name: str, color: str) -> tuple[tuple[str, ...], str, float]:
    icon_path, _ = resolve_icon_path(icon_name, _ICONS_DIR)
    elements, style, base_size = extract_paths_from_icon(icon_path, color)
    return tuple(elements), style, base_size


_USE_ATTR_KEYS = ("x", "y", "width", "height", "fill", "stroke", "stroke-width")
_FOLD_PATTERN = re.compile(
    r'<g\b[^>]*\bdata-icon-expanded="([^"]+)"[^>]*>.*?</g>',
    re.DOTALL,
)
_DATA_ATTR_PATTERN = re.compile(r'\bdata-icon-([a-z-]+)="([^"]*)"')
_ID_ATTR_PATTERN = re.compile(r'\bid="([^"]+)"')


def _serialize_attr(name: str, value: object) -> str:
    s = str(value).replace('"', "&quot;")
    return f' {name}="{s}"'


_THUMBNAIL_STYLE = (
    "<style>g[data-icon-expanded] * { vector-effect: non-scaling-stroke; }</style>"
)


def inline_icons(content: str) -> str:
    """Expand every `<use data-icon="…"/>` in *content* into a rendered `<g>`.

    The produced `<g>` carries `data-icon-expanded` plus `data-icon-x` /
    `-y` / `-width` / `-height` / `-fill` / `-stroke` / `-stroke-width`
    attrs that mirror the original placeholder. `fold_icons` uses those
    to round-trip back to `<use>` on save.

    Also injects a tiny `<style>` so icon strokes survive thumbnail-scale
    rendering. Without it, a 2px stroke at 1/10 scale becomes 0.2px and
    vanishes — the thumbnail row in the slide deck shows an empty circle
    where the icon should be. `fold_icons` strips the style so the source
    file is never polluted.
    """
    matches = list(_USE_ICON_PATTERN.finditer(content))
    if not matches:
        return content
    out = content
    for match in reversed(matches):
        use_str = match.group(0)
        try:
            attrs = parse_use_element(use_str)
            icon_name = attrs.get("icon")
            if not icon_name:
                continue
            color = str(attrs.get("fill", "#000000"))
            elements, style, base_size = _resolve_cached(str(icon_name), color)
        except Exception:
            continue
        if not elements:
            continue
        replacement = generate_icon_group(attrs, list(elements), style, base_size)
        # Stash the placeholder's raw attrs so fold_icons can rebuild the
        # original `<use>` from the rendered `<g>` after editor saves.
        injected = f' data-icon-expanded="{icon_name}"'
        for key in _USE_ATTR_KEYS:
            if key in attrs:
                injected += _serialize_attr(f"data-icon-{key}", attrs[key])
        id_match = _ID_ATTR_PATTERN.search(use_str)
        if id_match:
            injected = f' id="{id_match.group(1)}"' + injected
        replacement = replacement.replace("<g ", "<g" + injected + " ", 1)
        out = out[: match.start()] + replacement + out[match.end():]
    # Inject the non-scaling-stroke style right after the opening <svg ...>
    # tag so it applies to every expanded icon group regardless of where
    # they sit in the document.
    svg_open = re.search(r"<svg\b[^>]*>", out)
    if svg_open:
        insert_at = svg_open.end()
        out = out[:insert_at] + _THUMBNAIL_STYLE + out[insert_at:]
    return out


def fold_icons(content: str) -> str:
    """Reverse of `inline_icons`: convert `<g data-icon-expanded="…">…</g>`
    blocks back to `<use data-icon="…" x="…" …/>` placeholders.

    Idempotent on content that has no expansion markers. Used on the save
    path so the editor never overwrites the agent's icon abstractions
    with raw geometry.
    """
    if "data-icon-expanded" not in content and _THUMBNAIL_STYLE not in content:
        return content
    # Strip the non-scaling-stroke style we injected on serve.
    content = content.replace(_THUMBNAIL_STYLE, "")

    def repl(m: re.Match) -> str:
        head = m.group(0)
        # Pull the open-tag attribute soup so we can read every data-icon-* attr.
        open_end = head.find(">")
        if open_end < 0:
            return head
        head_tag = head[: open_end + 1]
        icon_name = m.group(1)
        data: dict[str, str] = {}
        for k, v in _DATA_ATTR_PATTERN.findall(head_tag):
            if k == "expanded":
                continue
            data[k] = v.replace("&quot;", '"')
        attrs = "".join(_serialize_attr(k, data[k]) for k in _USE_ATTR_KEYS if k in data)
        id_match = _ID_ATTR_PATTERN.search(head_tag)
        id_attr = f' id="{id_match.group(1)}"' if id_match else ""
        return f'<use{id_attr} data-icon="{icon_name}"{attrs}/>'

    return _FOLD_PATTERN.sub(repl, content)
