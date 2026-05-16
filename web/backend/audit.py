"""Append-only audit log — Phase 9.

JSONL at ``platform/audit.log``. One event per line. Best-effort: a write
failure must never break the user-facing mutation, so callers wrap us in a
try/except and we never raise outside ``record``.

Schema (every event)::

    {
      "ts":       <unix int>,
      "action":   "tenant.create" | "member.add" | ... ,
      "actor":    {"user_id": str, "platform_admin": bool},
      "tenant":   str | None,
      "target":   str | None,       # free-form (project, member id, asset)
      ...extra                       # action-specific keys
    }

Reads are linear scans — fine for the v1 admin console at the volumes we
expect. When the file gets large we'll split per-day.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from files import REPO_ROOT

PLATFORM_DIR = REPO_ROOT / "platform"
AUDIT_LOG_PATH = PLATFORM_DIR / "audit.log"

_lock = threading.Lock()


def _resolve_path() -> Path:
    # Read at call time so tests can monkey-patch audit.AUDIT_LOG_PATH.
    return AUDIT_LOG_PATH


def record(
    action: str,
    *,
    actor_id: str = "",
    platform_admin: bool = False,
    tenant: Optional[str] = None,
    target: Optional[str] = None,
    **extra: Any,
) -> None:
    """Append a single audit event. Never raises."""
    try:
        path = _resolve_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        evt: dict[str, Any] = {
            "ts": int(time.time()),
            "action": str(action),
            "actor": {"user_id": str(actor_id or ""), "platform_admin": bool(platform_admin)},
            "tenant": tenant,
            "target": target,
        }
        for k, v in extra.items():
            if k in evt:
                continue
            evt[k] = v
        line = json.dumps(evt, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # tmpfs / non-syncable backing — best effort only.
                    pass
    except Exception:
        # Audit must never break the calling request path. Swallow.
        return


def _iter_lines() -> Iterable[str]:
    path = _resolve_path()
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []


def read(
    *,
    tenant: Optional[str] = None,
    limit: int = 200,
    actions: Optional[Iterable[str]] = None,
) -> list[dict]:
    """Return the most recent matching events (newest first)."""
    want_actions = set(actions) if actions else None
    out: list[dict] = []
    for line in _iter_lines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        if tenant is not None and evt.get("tenant") != tenant:
            continue
        if want_actions is not None and evt.get("action") not in want_actions:
            continue
        out.append(evt)
    out.reverse()
    if limit and limit > 0:
        out = out[:limit]
    return out
