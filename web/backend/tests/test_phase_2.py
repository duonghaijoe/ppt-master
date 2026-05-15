"""Phase 2 verification — identity + tenant directory.

Per §13 Phase 2 of web/MULTITENANT_REARCHITECTURE.md:

- ``/api/me`` resolves a header identity to a stored User (creating on first
  sight) and includes memberships + platform_admin in the response.
- The dev shim (``DEV_USER_ID`` env) substitutes for the proxy headers in
  local dev; unauthenticated requests fail 401 otherwise.
- IdP subject-id rotation is survivable — a new ``X-User-Id`` with a known
  email reattaches to the original record so memberships don't vanish.
- ``/api/tenants`` returns the caller's tenants with their role; platform
  admins see the full directory.
- ``/api/tenants/{slug}`` is readable for members + admins, forbidden
  otherwise, 404 for non-existent slugs.

Tests run against an isolated on-disk root (monkeypatched ``USERS_DIR``,
``PLATFORM_FILE``, ``TENANTS_ROOT``) so the real repo's tenants/users files
stay untouched. No mocks — the FastAPI ``app`` is exercised end-to-end via
TestClient with real header values.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import tenants as tenants_mod
import users as users_mod


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point users/tenants storage at a scratch directory.

    Each test gets a fresh slate. The fixture seeds two tenants and two users:

    - ``acme`` (alice = owner)
    - ``globex`` (bob = owner)
    - ``alice`` is a platform admin; ``bob`` is not

    so we can exercise membership filtering and admin override paths without
    standing up a full migration.
    """
    users_dir = tmp_path / "users"
    tenants_dir = tmp_path / "tenants"
    platform_file = tmp_path / ".platform.json"
    users_dir.mkdir()
    tenants_dir.mkdir()

    monkeypatch.setattr(users_mod, "USERS_DIR", users_dir)
    monkeypatch.setattr(users_mod, "PLATFORM_FILE", platform_file)
    monkeypatch.setattr(tenants_mod, "TENANTS_ROOT", tenants_dir)

    # Seed: two tenants on disk.
    for slug, name in [("acme", "Acme Co"), ("globex", "Globex Inc")]:
        td = tenants_dir / slug
        td.mkdir()
        (td / "config.json").write_text(
            json.dumps(
                {
                    "slug": slug,
                    "name": name,
                    "owner_user_id": f"u-{slug}-owner",
                    "default_format": "ppt169",
                    "created_at": 1700000000,
                }
            ),
            encoding="utf-8",
        )
        (td / "members.json").write_text("[]", encoding="utf-8")

    # Seed: two users. alice is platform admin + acme owner; bob owns globex.
    (users_dir / "u-alice.json").write_text(
        json.dumps(
            {
                "id": "u-alice",
                "email": "alice@example.com",
                "display_name": "alice",
                "memberships": [{"tenant_slug": "acme", "role": "owner"}],
                "platform_admin": True,
                "created_at": 1700000000,
            }
        ),
        encoding="utf-8",
    )
    (users_dir / "u-bob.json").write_text(
        json.dumps(
            {
                "id": "u-bob",
                "email": "bob@example.com",
                "display_name": "bob",
                "memberships": [{"tenant_slug": "globex", "role": "owner"}],
                "platform_admin": False,
                "created_at": 1700000000,
            }
        ),
        encoding="utf-8",
    )
    platform_file.write_text(
        json.dumps({"admins": ["u-alice"], "schema_version": 1, "created_at": 1700000000}),
        encoding="utf-8",
    )

    # Make sure the dev shim doesn't leak in from the developer's shell.
    monkeypatch.delenv("DEV_USER_ID", raising=False)
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)

    return tmp_path


@pytest.fixture
def client(isolated_root: Path) -> TestClient:
    return TestClient(main.app)


# ─── /api/me ────────────────────────────────────────────────────────────────

def test_me_requires_identity(client: TestClient) -> None:
    """No headers, no dev shim → 401 (fails closed)."""
    r = client.get("/api/me")
    assert r.status_code == 401, r.text


def test_me_returns_existing_user_by_id(client: TestClient) -> None:
    r = client.get(
        "/api/me",
        headers={"X-User-Id": "u-alice", "X-User-Email": "alice@example.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "u-alice"
    assert body["email"] == "alice@example.com"
    assert body["platform_admin"] is True
    assert {"tenant_slug": "acme", "role": "owner"} in body["memberships"]


def test_me_creates_user_on_first_sight(client: TestClient, isolated_root: Path) -> None:
    """Unknown id + email → record is created (non-admin by default)."""
    r = client.get(
        "/api/me",
        headers={"X-User-Id": "u-new", "X-User-Email": "new@example.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "u-new"
    assert body["email"] == "new@example.com"
    assert body["memberships"] == []
    # Not admin: alice is already the seeded admin so the bootstrap path
    # doesn't fire.
    assert body["platform_admin"] is False
    # Persisted to disk.
    assert (isolated_root / "users" / "u-new.json").exists()


def test_me_first_user_on_fresh_platform_becomes_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``.platform.json`` has no admins yet, the first caller is one.

    We can't reuse ``isolated_root`` because it pre-seeds alice as admin —
    this test specifically exercises the empty-admins bootstrap path.
    """
    users_dir = tmp_path / "users"
    tenants_dir = tmp_path / "tenants"
    users_dir.mkdir()
    tenants_dir.mkdir()
    monkeypatch.setattr(users_mod, "USERS_DIR", users_dir)
    monkeypatch.setattr(users_mod, "PLATFORM_FILE", tmp_path / ".platform.json")
    monkeypatch.setattr(tenants_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.delenv("DEV_USER_ID", raising=False)

    client = TestClient(main.app)
    r = client.get(
        "/api/me",
        headers={"X-User-Id": "u-founder", "X-User-Email": "founder@example.com"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["platform_admin"] is True

    pf = json.loads((tmp_path / ".platform.json").read_text(encoding="utf-8"))
    assert pf["admins"] == ["u-founder"]


def test_me_resolves_by_email_when_subject_rotates(
    client: TestClient, isolated_root: Path
) -> None:
    """IdP rotates the subject id; lookup by email keeps memberships sticky."""
    r = client.get(
        "/api/me",
        headers={"X-User-Id": "u-alice-rotated", "X-User-Email": "alice@example.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Resolved to the original record, not a new one.
    assert body["id"] == "u-alice"
    assert any(m["tenant_slug"] == "acme" for m in body["memberships"])


def test_me_dev_shim_substitutes_for_headers(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``DEV_USER_ID`` env lets local dev work without the reverse proxy."""
    monkeypatch.setenv("DEV_USER_ID", "u-alice")
    monkeypatch.setenv("DEV_USER_EMAIL", "alice@example.com")
    client = TestClient(main.app)
    r = client.get("/api/me")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "u-alice"


# ─── /api/tenants ──────────────────────────────────────────────────────────

def test_list_tenants_platform_admin_sees_all(client: TestClient) -> None:
    r = client.get(
        "/api/tenants",
        headers={"X-User-Id": "u-alice", "X-User-Email": "alice@example.com"},
    )
    assert r.status_code == 200, r.text
    slugs = {t["slug"] for t in r.json()["tenants"]}
    assert {"acme", "globex"} <= slugs
    # Alice's role on her own tenant should be "owner"; on globex she has no
    # explicit membership so the admin label kicks in.
    by_slug = {t["slug"]: t for t in r.json()["tenants"]}
    assert by_slug["acme"]["role"] == "owner"
    assert by_slug["globex"]["role"] == "admin"


def test_list_tenants_non_admin_sees_only_memberships(client: TestClient) -> None:
    r = client.get(
        "/api/tenants",
        headers={"X-User-Id": "u-bob", "X-User-Email": "bob@example.com"},
    )
    assert r.status_code == 200, r.text
    slugs = {t["slug"] for t in r.json()["tenants"]}
    assert slugs == {"globex"}, f"bob should only see globex, got {slugs}"


def test_tenant_detail_member_can_read(client: TestClient) -> None:
    r = client.get(
        "/api/tenants/acme",
        headers={"X-User-Id": "u-alice", "X-User-Email": "alice@example.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "acme"
    assert body["name"] == "Acme Co"
    assert body["role"] == "owner"


def test_tenant_detail_non_member_forbidden(client: TestClient) -> None:
    r = client.get(
        "/api/tenants/acme",
        headers={"X-User-Id": "u-bob", "X-User-Email": "bob@example.com"},
    )
    assert r.status_code == 403, r.text


def test_tenant_detail_admin_can_read_any(client: TestClient) -> None:
    """Platform admins bypass membership for read access."""
    r = client.get(
        "/api/tenants/globex",
        headers={"X-User-Id": "u-alice", "X-User-Email": "alice@example.com"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "globex"


def test_tenant_detail_missing_returns_404(client: TestClient) -> None:
    r = client.get(
        "/api/tenants/no-such-tenant",
        headers={"X-User-Id": "u-alice", "X-User-Email": "alice@example.com"},
    )
    assert r.status_code == 404, r.text
