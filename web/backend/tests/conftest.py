"""Shared pytest fixtures for backend tests.

Backend modules are imported by bare names (``from files import ...``) rather
than as a package, so the tests have to put ``web/backend/`` on ``sys.path``
before any backend import resolves. Doing it here keeps each test file free
of path gymnastics.
"""
from __future__ import annotations

import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
