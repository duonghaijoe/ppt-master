"""OpenAI image generation — Phase 5 mediated wrapper.

The agent sandbox blocks direct OpenAI calls (and the legacy
``image_gen.py`` CLI) from tenant Bash, so image generation in the web
stack funnels through here. Every successful call appends a
``UsageEvent`` to ``platform/billing.db`` priced from
``platform/pricing.json``.

The wrapper is intentionally narrow:

- One provider (OpenAI), one model family (``gpt-image-1``) — enough to
  light up metering for v1. Other backends will plug in as siblings.
- No mock fallback. Missing key or upstream failure surfaces as a 503-
  shaped ``ProviderError``; the route turns that into an HTTP 503. The
  spec is explicit that we never fake a provider response.
- Aspect-ratio → pixel-size mapping covers the canvases we actually
  emit (16:9, 9:16, 1:1, 3:2, 2:3). The ``size`` argument selects the
  pricing tier (``"1024"`` or ``"2048"``), which is what the ledger row
  records.
"""
from __future__ import annotations

import base64
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import metering

# Imported at module load so tests can monkeypatch ``OpenAI`` on this
# module. The real SDK is the production path; ``ProviderError`` raised
# below covers the case where the package is missing.
try:
    from openai import OpenAI  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    OpenAI = None  # type: ignore[assignment]


class ProviderError(RuntimeError):
    """Raised when the upstream call cannot complete.

    The route layer maps this to HTTP 503 so the tenant sees a real
    failure instead of a fabricated image path.
    """


# ─── size / aspect ratio mapping ───────────────────────────────────────────

# gpt-image-1 accepts a fixed set of sizes. We bucket by the ledger's
# pricing tier (``size`` arg) and pick the canvas-shaped variant.
_SIZE_MAP_1024 = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "3:2": "1536x1024",
    "9:16": "1024x1536",
    "2:3": "1024x1536",
}
_SIZE_MAP_2048 = {
    "1:1": "2048x2048",
    "16:9": "2048x1152",
    "3:2": "2016x1344",
    "9:16": "1152x2048",
    "2:3": "1344x2016",
}


def _resolve_size(aspect_ratio: str, size: str) -> str:
    table = _SIZE_MAP_2048 if str(size) == "2048" else _SIZE_MAP_1024
    return table.get(aspect_ratio, table["1:1"])


# ─── filename sanitiser ────────────────────────────────────────────────────

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _filename_from_prompt(prompt: str) -> str:
    stem = _SAFE.sub("_", (prompt or "image").strip())[:48].strip("_") or "image"
    return f"{stem}_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"


# ─── public API ────────────────────────────────────────────────────────────


def generate_image(
    *,
    tenant: str,
    project: str,
    user_id: str,
    session_id: Optional[str],
    prompt: str,
    output_dir: Path,
    aspect_ratio: str = "16:9",
    size: str = "1024",
    model: str = "gpt-image-1",
    filename: Optional[str] = None,
) -> dict:
    """Run one OpenAI image-gen call, save the bytes, record a UsageEvent.

    Returns a dict with ``path`` (relative to ``output_dir``), ``bytes``
    (the file size), and ``usage_event_id``. The route layer is free to
    transform ``path`` into a project-relative URL before returning.
    """
    if not prompt or not str(prompt).strip():
        raise ProviderError("prompt is required")

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ProviderError(
            "OPENAI_API_KEY is not configured on the platform; "
            "image generation is unavailable"
        )

    if OpenAI is None:
        raise ProviderError(
            "openai SDK is not installed; install the `openai` package to enable image gen"
        )

    base_url = os.environ.get("OPENAI_BASE_URL") or None
    try:
        client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"openai client init failed: {exc}") from exc

    pixel_size = _resolve_size(aspect_ratio, size)
    request_id: Optional[str] = None
    try:
        result = client.images.generate(
            model=model,
            prompt=prompt,
            size=pixel_size,
            n=1,
        )
    except Exception as exc:  # noqa: BLE001
        # The SDK raises a hierarchy of OpenAIError subclasses; we don't
        # discriminate — any upstream failure is a 503-equivalent here.
        raise ProviderError(f"openai image gen failed: {exc}") from exc

    try:
        request_id = getattr(result, "_request_id", None)
        data = getattr(result, "data", None) or []
        if not data:
            raise ProviderError("openai returned no image data")
        item = data[0]
        b64 = getattr(item, "b64_json", None)
        if not b64:
            url = getattr(item, "url", None)
            if not url:
                raise ProviderError("openai response contained neither b64_json nor url")
            import urllib.request
            with urllib.request.urlopen(url, timeout=30) as r:
                image_bytes = r.read()
        else:
            image_bytes = base64.b64decode(b64)
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"openai response decode failed: {exc}") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    out_name = filename or _filename_from_prompt(prompt)
    if not out_name.lower().endswith(".png"):
        out_name = f"{out_name}.png"
    out_path = output_dir / out_name
    out_path.write_bytes(image_bytes)

    event = metering.record_provider_call(
        tenant=tenant,
        project=project,
        user_id=user_id,
        session_id=session_id,
        provider="openai",
        model=model,
        kind="image",
        units={"images": 1, "size": str(size)},
        request_id=request_id,
    )

    return {
        "path": out_name,
        "bytes": len(image_bytes),
        "size": pixel_size,
        "usage_event_id": event.id,
    }
