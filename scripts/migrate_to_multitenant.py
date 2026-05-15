#!/usr/bin/env python3
"""Migrate the repo from single-tenant to the multi-tenant layout in §4 of
web/MULTITENANT_REARCHITECTURE.md.

Phase 1 scope (only):
  - Move ``projects/*`` to ``tenants/default/projects/``.
  - Move ``templates/*`` (if it exists) to ``tenants/default/templates/``.
  - Scaffold ``tenants/default/{config.json, members.json, design_system/,
    shared/images/}``.
  - Scaffold ``users/<bootstrap-id>.json`` and ``.platform.json``.

Out of Phase 1 (handled in later phases):
  - ``skills/`` move to ``platform/skills/`` (Phase 4).
  - ``examples/``, ``docs/`` moves (Phase 4).
  - Extracting palette/typography from project SKILL.mds — write empty
    scaffolds for now.

Defaults to dry-run. Pass ``--apply`` to actually move files. Idempotent on
re-run (everything is presence-guarded).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BOOTSTRAP_EMAIL = "joe.duong.alpha@katalon.com"


class Plan:
    """Collects intended actions for dry-run output and replays them on apply.

    Each step is a callable + a human label. We accumulate the full plan first
    so a dry run prints everything the script would do, then `--apply` walks
    the same list. Keeps the dry-run and real run guaranteed-identical.
    """

    def __init__(self, apply: bool, root: Path) -> None:
        self.apply = apply
        self.root = root
        self._steps: list[tuple[str, callable]] = []

    def add(self, label: str, fn) -> None:
        self._steps.append((label, fn))

    def rel(self, p: Path) -> str:
        """Path display relative to this run's root (not the script's repo)."""
        try:
            return str(p.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(p)

    def run(self) -> None:
        for label, fn in self._steps:
            mode = "DO" if self.apply else "DRY"
            print(f"[{mode}] {label}")
            if self.apply:
                fn()


def _is_empty(p: Path) -> bool:
    """True if path doesn't exist or is an empty directory."""
    if not p.exists():
        return True
    if p.is_file():
        return p.stat().st_size == 0
    return not any(p.iterdir())


def _move_children(src: Path, dst: Path, plan: Plan, label_prefix: str) -> None:
    """Move every direct child of `src` into `dst`. Skip entries already moved.

    We move children rather than renaming the parent so reruns after a partial
    migration just pick up the remainder. Hidden files (.DS_Store etc.) are
    moved too — they're harmless and excluding them complicates idempotency.
    """
    if not src.exists() or not src.is_dir():
        plan.add(f"{label_prefix}: source {plan.rel(src)} not present, skipping", lambda: None)
        return
    children = [c for c in src.iterdir()]
    if not children:
        plan.add(f"{label_prefix}: source {plan.rel(src)} already empty", lambda: None)
        return
    for child in children:
        target = dst / child.name
        if target.exists():
            plan.add(
                f"{label_prefix}: {child.name} already at {plan.rel(target)}, skipping",
                lambda: None,
            )
            continue
        plan.add(
            f"{label_prefix}: mv {plan.rel(child)} -> {plan.rel(target)}",
            (lambda c=child, t=target: (t.parent.mkdir(parents=True, exist_ok=True), shutil.move(str(c), str(t)))),
        )


def _ensure_dir(p: Path, plan: Plan) -> None:
    if p.exists():
        plan.add(f"mkdir -p {plan.rel(p)} (exists)", lambda: None)
        return
    plan.add(
        f"mkdir -p {plan.rel(p)}",
        lambda: p.mkdir(parents=True, exist_ok=True),
    )


def _write_json_if_missing(p: Path, payload: dict, plan: Plan) -> None:
    """Write JSON at `p` only if it doesn't already exist (idempotent)."""
    if p.exists():
        plan.add(f"write {plan.rel(p)} (exists, skipping)", lambda: None)
        return
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    plan.add(
        f"write {plan.rel(p)}",
        lambda: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text(body, encoding="utf-8")),
    )


def _write_text_if_missing(p: Path, text: str, plan: Plan) -> None:
    if p.exists():
        plan.add(f"write {plan.rel(p)} (exists, skipping)", lambda: None)
        return
    plan.add(
        f"write {plan.rel(p)}",
        lambda: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text(text, encoding="utf-8")),
    )


def _existing_user_id(users_dir: Path, email: str) -> str | None:
    """Return the user-id of an existing user with this email, or None.

    Lets the script be idempotent across reruns where someone hand-edits the
    user file or the bootstrap admin record was already created.
    """
    if not users_dir.exists():
        return None
    for f in users_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("email") == email:
            return data.get("id") or f.stem
    return None


def build_plan(repo_root: Path, apply: bool, bootstrap_email: str) -> Plan:
    plan = Plan(apply=apply, root=repo_root)

    tenants_default = repo_root / "tenants" / "default"
    users_dir = repo_root / "users"

    # 1. Tenant + platform scaffolding.
    _ensure_dir(tenants_default / "projects", plan)
    _ensure_dir(tenants_default / "templates", plan)
    _ensure_dir(tenants_default / "design_system", plan)
    _ensure_dir(tenants_default / "shared" / "images", plan)
    _ensure_dir(users_dir, plan)

    # 2. Move existing projects/* into tenants/default/projects/.
    _move_children(
        repo_root / "projects",
        tenants_default / "projects",
        plan,
        "projects",
    )

    # 3. Move existing templates/* (if templates/ exists at all) into
    #    tenants/default/templates/. Templates dir is created lazily by the
    #    template-save endpoint, so often it just doesn't exist yet.
    _move_children(
        repo_root / "templates",
        tenants_default / "templates",
        plan,
        "templates",
    )

    # 4. Bootstrap admin user. Reuse existing record if email matches.
    existing_id = _existing_user_id(users_dir, bootstrap_email)
    bootstrap_id = existing_id or f"u-{uuid.uuid4().hex[:12]}"
    user_payload = {
        "id": bootstrap_id,
        "email": bootstrap_email,
        "display_name": bootstrap_email.split("@", 1)[0],
        "memberships": [{"tenant_slug": "default", "role": "owner"}],
        "platform_admin": True,
        "created_at": int(time.time()),
    }
    _write_json_if_missing(
        users_dir / f"{bootstrap_id}.json",
        user_payload,
        plan,
    )

    # 5. Tenant config + members.
    _write_json_if_missing(
        tenants_default / "config.json",
        {
            "slug": "default",
            "name": "Default",
            "owner_user_id": bootstrap_id,
            "default_format": "ppt169",
            "created_at": int(time.time()),
        },
        plan,
    )
    _write_json_if_missing(
        tenants_default / "members.json",
        [{"user_id": bootstrap_id, "role": "owner"}],
        plan,
    )

    # 6. Empty design_system scaffolds. Real extraction from project SKILL.mds
    #    happens later; Phase 1 just establishes the slots.
    _write_json_if_missing(
        tenants_default / "design_system" / "palette.json",
        {},
        plan,
    )
    _write_json_if_missing(
        tenants_default / "design_system" / "typography.json",
        {},
        plan,
    )
    _write_text_if_missing(
        tenants_default / "design_system" / "spec.md",
        "# Default tenant design system\n\nNo tokens captured yet.\n",
        plan,
    )

    # 7. Platform record (admins list + schema version). Phase 1 leaves the
    #    platform/ tree itself alone — skills/ stays at repo root until Phase 4.
    _write_json_if_missing(
        repo_root / ".platform.json",
        {
            "admins": [bootstrap_id],
            "schema_version": 1,
            "created_at": int(time.time()),
            "migration_phase": 1,
        },
        plan,
    )

    return plan


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually run the migration. Default is dry-run.",
    )
    p.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repo root to operate on (default: this repo).",
    )
    p.add_argument(
        "--bootstrap-email",
        default=os.environ.get("BOOTSTRAP_ADMIN_EMAIL", DEFAULT_BOOTSTRAP_EMAIL),
        help="Email of the bootstrap platform admin / default tenant owner.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not args.root.exists():
        print(f"root does not exist: {args.root}", file=sys.stderr)
        return 2
    plan = build_plan(args.root, args.apply, args.bootstrap_email)
    print(f"# Migration plan for {args.root}")
    print(f"# Bootstrap admin: {args.bootstrap_email}")
    print(f"# Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    plan.run()
    if not args.apply:
        print("\n# Re-run with --apply to perform the migration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
