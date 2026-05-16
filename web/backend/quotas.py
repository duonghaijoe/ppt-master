"""Per-tenant quotas — Phase 9.

Two limits configurable in ``tenants/<slug>/config.json`` under ``quota``:

- ``max_projects`` (default 50) — applied at project create / from-template.
- ``max_storage_bytes`` (default 5 GiB) — applied at shared-asset upload,
  output-dir register, image-gen mediation. Walks the tenant tree (cheap at
  this scale) and includes ``.trash`` in the total so soft-deletes still
  count against you until purged.

Quota errors raise ``HTTPException(403, {error: 'quota_exceeded', ...})``
so the API surface is consistent with role denials.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import HTTPException

import tenants as tenants_mod


DEFAULT_MAX_PROJECTS = 50
DEFAULT_MAX_STORAGE_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB


def _quota_cfg(tenant: str) -> dict:
    cfg = tenants_mod.read_tenant_config(tenant) or {}
    return cfg.get("quota") or {}


def max_projects(tenant: str) -> int:
    val = _quota_cfg(tenant).get("max_projects")
    try:
        n = int(val) if val is not None else DEFAULT_MAX_PROJECTS
    except Exception:
        n = DEFAULT_MAX_PROJECTS
    return max(1, n)


def max_storage_bytes(tenant: str) -> int:
    val = _quota_cfg(tenant).get("max_storage_bytes")
    try:
        n = int(val) if val is not None else DEFAULT_MAX_STORAGE_BYTES
    except Exception:
        n = DEFAULT_MAX_STORAGE_BYTES
    return max(1024 * 1024, n)


def _projects_root(tenant: str) -> Path:
    return tenants_mod.tenant_dir(tenant) / "projects"


def count_projects(tenant: str) -> int:
    """Number of live project dirs (trash excluded)."""
    root = _projects_root(tenant)
    if not root.exists():
        return 0
    return sum(1 for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def compute_storage_bytes(tenant: str) -> int:
    """Recursive size of the tenant dir, trash included. Skips symlinks."""
    base = tenants_mod.tenant_dir(tenant)
    if not base.exists():
        return 0
    total = 0
    for p in base.rglob("*"):
        try:
            if p.is_symlink() or not p.is_file():
                continue
            total += p.stat().st_size
        except OSError:
            continue
    return total


def usage(tenant: str) -> dict:
    pc = count_projects(tenant)
    sb = compute_storage_bytes(tenant)
    return {
        "projects": pc,
        "max_projects": max_projects(tenant),
        "storage_bytes": sb,
        "max_storage_bytes": max_storage_bytes(tenant),
    }


def _raise(tenant: str, reason: str, **extra) -> None:
    detail = {"error": "quota_exceeded", "tenant": tenant, "reason": reason}
    detail.update(extra)
    raise HTTPException(status_code=403, detail=detail)


def assert_project_slot_available(tenant: str) -> None:
    current = count_projects(tenant)
    cap = max_projects(tenant)
    if current >= cap:
        _raise(tenant, "max_projects", current=current, cap=cap)


def assert_storage_available(tenant: str, additional_bytes: int) -> None:
    if additional_bytes <= 0:
        return
    current = compute_storage_bytes(tenant)
    cap = max_storage_bytes(tenant)
    if current + additional_bytes > cap:
        _raise(
            tenant,
            "max_storage_bytes",
            current_bytes=current,
            additional_bytes=additional_bytes,
            cap_bytes=cap,
        )
