"""Shared pytest fixtures for backend tests.

Backend modules are imported by bare names (``from files import ...``) rather
than as a package, so the tests have to put ``web/backend/`` on ``sys.path``
before any backend import resolves. Doing it here keeps each test file free
of path gymnastics.

We also seed ``DEV_USER_ID`` so Phase 1's pre-Phase-3 tests (which don't
send identity headers) survive the Phase 3 enforcement layer. Phase 2 and
later tests opt out via ``monkeypatch.delenv("DEV_USER_ID")`` in their
fixtures so they exercise the unauthenticated path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DEV_USER_ID", "u-db28cc3e9d66")
os.environ.setdefault("DEV_USER_EMAIL", "dev@test.local")
