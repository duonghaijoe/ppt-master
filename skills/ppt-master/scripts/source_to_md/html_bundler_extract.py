#!/usr/bin/env python3
"""
HTML Bundler Extractor (Gamma / asset-pack style single-file HTML)

Some design-system / branding hand-offs (Gamma, Framer-style exports, custom
bundlers) ship as a single HTML file whose actual content lives inside three
inline `<script type="__bundler/...">` tags:

    __bundler/manifest        JSON map  uuid → { mime, compressed, data(base64) }
    __bundler/ext_resources   JSON list [ { id, uuid }, ... ]   (human-readable id → uuid)
    __bundler/template        JSON-string-encoded HTML page that uses window.__resources

A naive HTML→Markdown converter strips `<script>` tags before parsing, so the
visible markdown ends up empty (or just a placeholder thumbnail). This script
detects the bundler signature and unpacks it instead.

Outputs (next to the input file by default):
    <input>.real.html         The decoded template HTML (what the page actually renders)
    <input>_assets/<name>     Each manifest asset, named via ext_resources when possible:
                                  - logoHLight.png, iconWhite.png, ...
                                  - text/* and application/javascript saved with .jsx/.css/.js/.txt
                                  - everything else falls back to <uuid>.<ext-from-mime>
    <input>_assets/INDEX.md   A table of contents: id, uuid, mime, output filename

Detection is conservative: if the input does not contain
`type="__bundler/manifest"`, the script exits 0 with a "not a bundler" notice
so callers can chain it safely.

Usage:
    python3 html_bundler_extract.py <file.html> [--out-dir DIR] [--quiet]

Exit codes:
    0  extracted successfully, OR file is not a bundler (no-op)
    1  input missing / unreadable / malformed bundler payload
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import mimetypes
import re
import sys
from pathlib import Path

BUNDLER_MARKER = 'type="__bundler/manifest"'

# Known mime → preferred extension overrides (mimetypes.guess_extension picks
# odd choices for some types — e.g. .jpe instead of .jpg).
MIME_EXT_OVERRIDES = {
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "text/jsx": ".jsx",
    "application/x-jsx": ".jsx",
    "text/css": ".css",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/json": ".json",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
}

# JSX detection: code that imports React / declares JSX-style components.
JSX_HINTS = (
    "import React",
    "from 'react'",
    'from "react"',
    "createRoot(",
    "ReactDOM.render",
    "<div",
    "</div>",
)


def _ext_for(mime: str, payload: bytes) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in MIME_EXT_OVERRIDES:
        return MIME_EXT_OVERRIDES[mime]
    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed:
        return guessed
    if payload[:4] == b"\x89PNG":
        return ".png"
    if payload[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if payload[:4] == b"GIF8":
        return ".gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def _looks_like_jsx(text: str) -> bool:
    head = text[:4000]
    return any(hint in head for hint in JSX_HINTS) and "<" in head and "/>" in head


_SCRIPT_PATTERNS = {
    "manifest": re.compile(
        r'<script\s+type="__bundler/manifest"[^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    ),
    "ext_resources": re.compile(
        r'<script\s+type="__bundler/ext_resources"[^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    ),
    "template": re.compile(
        r'<script\s+type="__bundler/template"[^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    ),
}


def _extract_script(raw: str, key: str) -> str | None:
    pattern = _SCRIPT_PATTERNS[key]
    match = pattern.search(raw)
    if not match:
        return None
    return match.group(1).strip()


def is_bundler_html(raw_html: str) -> bool:
    return BUNDLER_MARKER in raw_html


def extract(input_file: Path, out_dir: Path | None = None, quiet: bool = False) -> dict:
    """Extract a bundler HTML file. Returns a summary dict.

    Caller is responsible for checking is_bundler_html() first if they want a
    no-op on non-bundler inputs; this function will raise ValueError otherwise.
    """
    raw = input_file.read_text(encoding="utf-8", errors="replace")
    if not is_bundler_html(raw):
        raise ValueError("Input is not a bundler-style HTML (no __bundler/manifest script)")

    manifest_text = _extract_script(raw, "manifest")
    ext_text = _extract_script(raw, "ext_resources")
    template_text = _extract_script(raw, "template")
    if manifest_text is None:
        raise ValueError("Bundler marker present but __bundler/manifest script body not found")

    manifest = json.loads(manifest_text)
    ext_resources = json.loads(ext_text) if ext_text else []
    # ext_resources is typically a list of {id, uuid} pairs — flip to uuid → id.
    uuid_to_name: dict[str, str] = {}
    if isinstance(ext_resources, list):
        for entry in ext_resources:
            if isinstance(entry, dict) and "uuid" in entry and "id" in entry:
                uuid_to_name[str(entry["uuid"])] = str(entry["id"])
    elif isinstance(ext_resources, dict):
        # Some bundlers serialize as { id: uuid } directly
        for human_id, uuid in ext_resources.items():
            uuid_to_name[str(uuid)] = str(human_id)

    base = input_file.with_suffix("")
    out_html = base.with_name(base.name + ".real.html")
    assets_dir = out_dir or base.with_name(base.name + "_assets")
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Write the decoded template HTML if present
    if template_text:
        # The template script body is a JSON-encoded string ("...."); decode it.
        try:
            decoded_template = json.loads(template_text)
            if not isinstance(decoded_template, str):
                decoded_template = template_text
        except json.JSONDecodeError:
            decoded_template = template_text
        out_html.write_text(decoded_template, encoding="utf-8")

    # Write each manifest asset
    index_rows: list[tuple[str, str, str, str]] = []  # (id, uuid, mime, filename)
    for uuid, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        mime = str(entry.get("mime", "application/octet-stream"))
        compressed = bool(entry.get("compressed", False))
        b64 = entry.get("data") or entry.get("b64") or ""
        if not isinstance(b64, str):
            continue
        try:
            payload = base64.b64decode(b64)
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            if not quiet:
                print(f"[WARN] {uuid}: base64 decode failed ({exc}); skipping")
            continue
        if compressed:
            try:
                payload = gzip.decompress(payload)
            except OSError as exc:
                if not quiet:
                    print(f"[WARN] {uuid}: gzip decompress failed ({exc}); writing raw")

        ext = _ext_for(mime, payload)
        human_id = uuid_to_name.get(uuid)
        if human_id:
            stem = re.sub(r"[^A-Za-z0-9_.-]", "_", human_id)
            filename = stem if Path(stem).suffix else stem + ext
        else:
            filename = uuid + ext

        out_path = assets_dir / filename
        out_path.write_bytes(payload)

        # If the payload is text-y (JS/JSX/CSS/HTML), also write a sibling .jsx
        # when it's actually JSX-shaped so reviewers don't have to guess.
        if mime.startswith("application/javascript") or mime in (
            "text/javascript",
            "text/jsx",
            "application/x-jsx",
        ):
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and _looks_like_jsx(text) and not out_path.suffix == ".jsx":
                jsx_path = out_path.with_suffix(".jsx")
                jsx_path.write_text(text, encoding="utf-8")

        index_rows.append((human_id or "", uuid, mime, filename))

    # Write INDEX.md
    index_path = assets_dir / "INDEX.md"
    lines = [
        f"# Bundler assets — extracted from {input_file.name}",
        "",
        f"Total assets: {len(index_rows)}",
        "",
        "| id | uuid | mime | file |",
        "| --- | --- | --- | --- |",
    ]
    for human_id, uuid, mime, filename in sorted(index_rows, key=lambda r: r[0] or r[1]):
        lines.append(f"| {human_id or '—'} | `{uuid}` | {mime} | `{filename}` |")
    if template_text:
        lines.extend(
            [
                "",
                f"Decoded template HTML: `{out_html.name}` (next to input)",
            ]
        )
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": str(input_file),
        "real_html": str(out_html) if template_text else None,
        "assets_dir": str(assets_dir),
        "asset_count": len(index_rows),
        "named_count": sum(1 for r in index_rows if r[0]),
        "index": str(index_path),
    }
    if not quiet:
        print(
            f"[OK] Bundler extracted: {summary['asset_count']} assets "
            f"({summary['named_count']} named) → {assets_dir}"
        )
        if template_text:
            print(f"     Decoded template HTML → {out_html}")
        print(f"     Index → {index_path}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract Gamma/bundler-style assets from a single-file HTML export."
    )
    parser.add_argument("input", type=Path, help="Path to the .html file")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Directory for extracted assets (default: <input>_assets/)",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress non-error output")
    args = parser.parse_args(argv)

    if not args.input.is_file():
        print(f"[ERROR] Input not found: {args.input}", file=sys.stderr)
        return 1

    raw = args.input.read_text(encoding="utf-8", errors="replace")
    if not is_bundler_html(raw):
        if not args.quiet:
            print(f"[SKIP] {args.input.name} is not a bundler HTML (no __bundler/manifest)")
        return 0

    try:
        extract(args.input, out_dir=args.out_dir, quiet=args.quiet)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] Bundler extraction failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
