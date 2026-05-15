"""User records — get-or-create by header identity.

Phase 2 contract: the reverse proxy injects ``X-User-Id`` + ``X-User-Email``
on every request. We trust them, look up ``users/<id>.json``, and create a
record on first sight. Role enforcement lives in Phase 3 (``authz.py``);
this module only resolves identity + memberships.

Storage shape — ``users/<id>.json``::

    {
      "id": "u-abc",
      "email": "alice@example.com",
      "display_name": "alice",
      "memberships": [{"tenant_slug": "default", "role": "owner"}],
      "platform_admin": false,
      "created_at": 1736000000
    }

The bootstrap admin is written by ``scripts/migrate_to_multitenant.py``; this
module also keeps ``.platform.json``'s ``admins`` list in sync with each
user's ``platform_admin`` flag so the two sources of truth can't drift.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from files import REPO_ROOT


USERS_DIR = REPO_ROOT / "users"
PLATFORM_FILE = REPO_ROOT / ".platform.json"


@dataclass
class Membership:
    tenant_slug: str
    role: str  # "owner" | "editor" | "viewer"


@dataclass
class User:
    id: str
    email: str
    display_name: str
    memberships: list[Membership] = field(default_factory=list)
    platform_admin: bool = False
    created_at: int = 0

    def to_public(self) -> dict:
        """Wire shape returned by /api/me."""
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "memberships": [
                {"tenant_slug": m.tenant_slug, "role": m.role}
                for m in self.memberships
            ],
            "platform_admin": self.platform_admin,
            "created_at": self.created_at,
        }


def _load_platform() -> dict:
    """Read ``.platform.json`` or return an empty record.

    The file is created by the migration script. If it's missing (fresh
    checkout, tests in a temp dir, etc.) we treat it as a brand-new platform
    with no admins yet — the first caller becomes one.
    """
    if not PLATFORM_FILE.exists():
        return {"admins": [], "schema_version": 1, "created_at": int(time.time())}
    try:
        return json.loads(PLATFORM_FILE.read_text(encoding="utf-8"))
    except Exception:
        # Don't crash the request loop on a corrupted platform record — fall
        # back to no-admins and let the operator notice via the empty UI.
        return {"admins": [], "schema_version": 1, "created_at": int(time.time())}


def _save_platform(data: dict) -> None:
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    PLATFORM_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLATFORM_FILE.write_text(body, encoding="utf-8")


def _user_file(user_id: str) -> Path:
    return USERS_DIR / f"{user_id}.json"


def _safe_id(user_id: str) -> str:
    """Sanitize an external id for use as a filename.

    Auth providers can return ids with characters we don't want on disk (`:`,
    `/`, etc.). Normalize them so ``users/<id>.json`` is always a flat file.
    """
    raw = (user_id or "").strip()
    if not raw:
        raw = f"u-{uuid.uuid4().hex[:12]}"
    out = []
    for ch in raw:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or f"u-{uuid.uuid4().hex[:12]}"


def _load_user(user_id: str) -> dict | None:
    f = _user_file(user_id)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_user(data: dict) -> None:
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _user_file(data["id"]).write_text(body, encoding="utf-8")


def _find_user_id_by_email(email: str) -> str | None:
    """Locate an existing user by email (case-insensitive).

    The proxy gives us a new id for the same email when the IdP rotates
    subject ids. Matching by email keeps memberships sticky across rotations.
    """
    if not email or not USERS_DIR.exists():
        return None
    needle = email.strip().lower()
    for f in USERS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("email", "")).strip().lower() == needle:
            return data.get("id") or f.stem
    return None


def _coerce_memberships(raw) -> list[Membership]:
    out: list[Membership] = []
    for m in raw or []:
        slug = str(m.get("tenant_slug", "")).strip()
        role = str(m.get("role", "")).strip().lower()
        if slug and role in {"owner", "editor", "viewer"}:
            out.append(Membership(tenant_slug=slug, role=role))
    return out


def get_or_create(user_id: str, email: str) -> User:
    """Resolve header identity to a stored User, creating on first sight.

    Order of resolution:
    1. Look up ``users/<user_id>.json`` directly.
    2. If absent, look up by email — IdPs sometimes rotate subject ids and
       we don't want to lose the user's memberships when that happens.
    3. If still absent, create a fresh record. The first user ever created
       is also recorded as the bootstrap platform admin.

    Email is refreshed from headers on every call so a user changing their
    profile in the IdP is reflected immediately.
    """
    raw_id = _safe_id(user_id)
    raw_email = (email or "").strip()

    record = _load_user(raw_id)
    if record is None and raw_email:
        existing_id = _find_user_id_by_email(raw_email)
        if existing_id:
            record = _load_user(existing_id)

    platform = _load_platform()
    if record is None:
        now = int(time.time())
        display = raw_email.split("@", 1)[0] if raw_email else raw_id
        record = {
            "id": raw_id,
            "email": raw_email,
            "display_name": display,
            "memberships": [],
            "platform_admin": False,
            "created_at": now,
        }
        # First user on a fresh platform becomes the admin. Otherwise the
        # migration script seeded one and we leave it alone.
        if not platform.get("admins"):
            record["platform_admin"] = True
            platform["admins"] = [raw_id]
            platform.setdefault("schema_version", 1)
            platform.setdefault("created_at", now)
            _save_platform(platform)
        _save_user(record)
    else:
        # Keep email fresh; if .platform.json says this id is admin, honor it
        # so a manually-edited platform record propagates without a restart.
        dirty = False
        if raw_email and record.get("email") != raw_email:
            record["email"] = raw_email
            dirty = True
        admin_now = record["id"] in (platform.get("admins") or [])
        if bool(record.get("platform_admin")) != admin_now:
            record["platform_admin"] = admin_now
            dirty = True
        if dirty:
            _save_user(record)

    return User(
        id=record["id"],
        email=record.get("email", ""),
        display_name=record.get("display_name") or record["id"],
        memberships=_coerce_memberships(record.get("memberships")),
        platform_admin=bool(record.get("platform_admin")),
        created_at=int(record.get("created_at") or 0),
    )


def add_membership(user_id: str, tenant_slug: str, role: str) -> User | None:
    """Append (or update) a membership on an existing user record.

    Returns the refreshed User, or None if the user file doesn't exist.
    Used by tenant invite flows in later phases; exported here so the
    callers have one place to mutate user state.
    """
    record = _load_user(user_id)
    if record is None:
        return None
    role = role.strip().lower()
    if role not in {"owner", "editor", "viewer"}:
        raise ValueError(f"invalid role: {role!r}")
    memberships = list(record.get("memberships") or [])
    found = False
    for i, m in enumerate(memberships):
        if m.get("tenant_slug") == tenant_slug:
            memberships[i] = {"tenant_slug": tenant_slug, "role": role}
            found = True
            break
    if not found:
        memberships.append({"tenant_slug": tenant_slug, "role": role})
    record["memberships"] = memberships
    _save_user(record)
    return get_or_create(record["id"], record.get("email", ""))
