#!/usr/bin/env python3
"""
OpenAI Image Generator (gpt-image-2)

Thin wrapper around image_gen.py that locks the backend to OpenAI so the
agent or a human user can call it directly without juggling IMAGE_BACKEND.

Required env (process env or skills/ppt-master/.env):
    OPENAI_API_KEY    - Your OpenAI key.

Optional env:
    OPENAI_MODEL      - Defaults to gpt-image-2.
    OPENAI_BASE_URL   - For OpenAI-compatible providers.

Usage:
    python3 image_gen_openai.py "a clean isometric office" --aspect_ratio 16:9 -o images/
    python3 image_gen_openai.py "logo concept" -o images/ --filename hero --image_size 2K
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sibling modules importable when invoked from any cwd.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _ensure_openai_backend() -> None:
    """Force the backend to OpenAI before image_gen.py inspects env."""
    os.environ["IMAGE_BACKEND"] = "openai"
    os.environ.setdefault("OPENAI_MODEL", "gpt-image-2")


def main() -> None:
    _ensure_openai_backend()

    # Surface a clear error if the key is missing rather than letting the
    # OpenAI SDK throw a generic auth error halfway through.
    if not os.environ.get("OPENAI_API_KEY"):
        sys.stderr.write(
            "Error: OPENAI_API_KEY is not set.\n"
            "Set it in the process env or in the resolved .env file used by image_gen.py.\n"
        )
        sys.exit(1)

    # Reuse the unified CLI verbatim — it handles arg parsing, .env loading,
    # backend dispatch, and error reporting.
    from image_gen import main as _gen_main  # noqa: E402

    _gen_main()


if __name__ == "__main__":
    main()
