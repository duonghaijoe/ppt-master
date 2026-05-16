"""Phase 8 verification — cross-tenant project sharing.

Per §13 Phase 8 of web/MULTITENANT_REARCHITECTURE.md:

- ``GET/POST/DELETE /api/tenants/{slug}/projects/{name}/share`` — owner-only
  writes; the share dialog lists current grants.
- ``.project.json`` carries ``acl`` (per-user grants) and
  ``shared_with_tenants`` (whole-tenant grants capped at editor) per §6.2.
- Project-scoped routes (slides/exports/recent/tree/file/output-dirs/
  output-dirs.put/output-dirs.register/images.generate) gate on the new
  ``require_project_role`` dependency so a cross-tenant shared user can
  actually open the deck.
- Session attach honours the same project-level resolver.

The fixture mirrors Phase 7 — three tenants (default, acme, globex) and four
users (alice platform-admin + acme/default owner, bob acme-editor, carol
acme-viewer, dave globex-owner) — so we can cover the home-tenant role,
direct ACL grant, tenant grant, and no-grant 403 cases without touching
the real users/tenants on disk.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import files as files_mod
import main
import tenants as tenants_mod
import templates as templates_mod
import users as users_mod


# ─── shared fixture ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    users_dir = tmp_path / "users"
    tenants_dir = tmp_path / "tenants"
    platform_dir = tmp_path / "platform"
    platform_skills_dir = platform_dir / "skills"
    billing_db = platform_dir / "billing.db"
    pricing_file = platform_dir / "pricing.json"
    platform_file = tmp_path / ".platform.json"

    users_dir.mkdir()
    tenants_dir.mkdir()
    platform_dir.mkdir()
    platform_skills_dir.mkdir()
    pricing_file.write_text(
        json.dumps({"version": "phase8-test", "margin": 0.20, "providers": {}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(users_mod, "USERS_DIR", users_dir)
    monkeypatch.setattr(users_mod, "PLATFORM_FILE", platform_file)
    monkeypatch.setattr(tenants_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(files_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(files_mod, "DEFAULT_TENANT_ROOT", tenants_dir / "default")
    monkeypatch.setattr(files_mod, "PROJECTS_DIR", tenants_dir / "default" / "projects")
    monkeypatch.setattr(files_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(main, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(main, "PLATFORM_DIR", platform_dir)
    monkeypatch.setattr(main, "PLATFORM_SKILLS_DIR", platform_skills_dir)
    monkeypatch.setattr(templates_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(templates_mod, "DEFAULT_TENANT_ROOT", tenants_dir / "default")
    monkeypatch.setattr(templates_mod, "TEMPLATES_DIR", tenants_dir / "default" / "templates")
    monkeypatch.setattr(templates_mod, "PROJECTS_DIR", tenants_dir / "default" / "projects")

    import metering as metering_mod
    monkeypatch.setattr(metering_mod, "PLATFORM_DIR", platform_dir)
    monkeypatch.setattr(metering_mod, "BILLING_DB", billing_db)
    monkeypatch.setattr(metering_mod, "PRICING_FILE", pricing_file)

    now = int(time.time())
    for slug, name in [("default", "Default"), ("acme", "Acme Co"), ("globex", "Globex Inc")]:
        td = tenants_dir / slug
        td.mkdir()
        (td / "config.json").write_text(
            json.dumps({
                "slug": slug,
                "name": name,
                "owner_user_id": f"u-{slug}-owner",
                "default_format": "ppt169",
                "created_at": now,
            }),
            encoding="utf-8",
        )
        (td / "members.json").write_text(
            json.dumps([{"user_id": f"u-{slug}-owner", "role": "owner", "added_at": now}]),
            encoding="utf-8",
        )
        (td / "projects").mkdir()
        (td / "templates").mkdir()
        (td / "shared").mkdir()
        (td / "design_system").mkdir()

    # Seed the project that will receive shares.
    proj = tenants_dir / "acme" / "projects" / "alpha"
    proj.mkdir()
    (proj / "SKILL.md").write_text("# Alpha project\n", encoding="utf-8")
    (proj / "svg_final").mkdir()
    (proj / "svg_final" / "001.svg").write_text(
        '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"/>',
        encoding="utf-8",
    )
    # Output-dirs descriptor so /output-dirs returns 200 (not 404 from a missing file).
    (proj / "output-dirs.json").write_text(
        json.dumps({"current": "svg_final", "dirs": [{"dir": "svg_final", "label": "Final"}]}),
        encoding="utf-8",
    )

    seeded = [
        ("u-alice", "alice@example.com", True,
         [{"tenant_slug": "acme", "role": "owner"},
          {"tenant_slug": "default", "role": "owner"}]),
        ("u-bob", "bob@example.com", False,
         [{"tenant_slug": "acme", "role": "editor"}]),
        ("u-carol", "carol@example.com", False,
         [{"tenant_slug": "acme", "role": "viewer"}]),
        ("u-dave", "dave@example.com", False,
         [{"tenant_slug": "globex", "role": "owner"}]),
        ("u-erin", "erin@example.com", False,
         [{"tenant_slug": "globex", "role": "viewer"}]),
    ]
    for uid, email, admin, memberships in seeded:
        (users_dir / f"{uid}.json").write_text(
            json.dumps({
                "id": uid,
                "email": email,
                "display_name": uid.removeprefix("u-"),
                "memberships": memberships,
                "platform_admin": admin,
                "created_at": now,
            }),
            encoding="utf-8",
        )
    platform_file.write_text(
        json.dumps({"admins": ["u-alice"], "schema_version": 1, "created_at": now}),
        encoding="utf-8",
    )

    monkeypatch.delenv("DEV_USER_ID", raising=False)
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    return tmp_path


@pytest.fixture
def client(isolated_root: Path) -> TestClient:
    return TestClient(main.app)


def hdr(uid: str) -> dict:
    email = uid.removeprefix("u-") + "@example.com"
    return {"X-User-Id": uid, "X-User-Email": email}


# ─── share endpoints ────────────────────────────────────────────────────────


def test_share_list_default_empty(client: TestClient) -> None:
    """A freshly seeded project has no grants yet but the GET still 200s."""
    r = client.get("/api/tenants/acme/projects/alpha/share", headers=hdr("u-alice"))
    assert r.status_code == 200, r.text
    assert r.json() == {"acl": [], "shared_with_tenants": []}


def test_share_list_404_when_project_missing(client: TestClient) -> None:
    """Platform admins resolve to owner for any project (existing or not), so
    the handler reaches read_project_meta, which raises FileNotFoundError → 404.
    A regular tenant member would 403 first via require_project_role — the
    no-grant-no-access test below covers that path."""
    r = client.get(
        "/api/tenants/acme/projects/no-such/share", headers=hdr("u-alice")
    )
    assert r.status_code == 404


def test_share_add_user_by_id_owner_succeeds(client: TestClient, isolated_root: Path) -> None:
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "editor"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["acl"] == [{"user_id": "u-dave", "role": "editor"}]

    # Persisted to disk in .project.json.
    meta = json.loads(
        (isolated_root / "tenants" / "acme" / "projects" / "alpha" / ".project.json")
        .read_text(encoding="utf-8")
    )
    assert {"user_id": "u-dave", "role": "editor"} in meta["acl"]


def test_share_add_user_by_email_resolves_user_id(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"email": "dave@example.com", "role": "viewer"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["acl"] == [{"user_id": "u-dave", "role": "viewer"}]


def test_share_add_user_email_unknown_returns_404(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"email": "ghost@nowhere.test", "role": "editor"},
    )
    assert r.status_code == 404, r.text


def test_share_add_tenant_grant(client: TestClient, isolated_root: Path) -> None:
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"tenant_slug": "globex"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["shared_with_tenants"] == ["globex"]
    meta = json.loads(
        (isolated_root / "tenants" / "acme" / "projects" / "alpha" / ".project.json")
        .read_text(encoding="utf-8")
    )
    assert "globex" in meta["shared_with_tenants"]


def test_share_add_rejects_unknown_tenant(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"tenant_slug": "no-such-tenant"},
    )
    assert r.status_code == 404, r.text


def test_share_add_rejects_self_tenant(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"tenant_slug": "acme"},
    )
    assert r.status_code == 400, r.text


def test_share_add_rejects_multiple_targets(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "tenant_slug": "globex", "role": "editor"},
    )
    assert r.status_code == 400, r.text


def test_share_add_rejects_invalid_role(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "superuser"},
    )
    assert r.status_code == 400, r.text


def test_share_add_editor_rejected(client: TestClient) -> None:
    """Only home-tenant owners can manage shares — bob is editor, blocked."""
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-bob"),
        json={"user_id": "u-dave", "role": "editor"},
    )
    assert r.status_code == 403, r.text


def test_share_add_viewer_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-carol"),
        json={"user_id": "u-dave", "role": "editor"},
    )
    assert r.status_code == 403, r.text


def test_share_add_non_member_rejected(client: TestClient) -> None:
    """A user with no membership in the home tenant 403s on share writes."""
    r = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-dave"),
        json={"user_id": "u-erin", "role": "editor"},
    )
    assert r.status_code == 403, r.text


def test_share_idempotent_replace_role(client: TestClient) -> None:
    """Re-sharing the same user with a different role overwrites in place."""
    r1 = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "viewer"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "editor"},
    )
    assert r2.status_code == 200
    acl = r2.json()["acl"]
    assert len(acl) == 1
    assert acl[0] == {"user_id": "u-dave", "role": "editor"}


def test_share_remove_user(client: TestClient) -> None:
    client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "editor"},
    )
    r = client.delete(
        "/api/tenants/acme/projects/alpha/share/user:u-dave",
        headers=hdr("u-alice"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["acl"] == []


def test_share_remove_tenant(client: TestClient) -> None:
    client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"tenant_slug": "globex"},
    )
    r = client.delete(
        "/api/tenants/acme/projects/alpha/share/tenant:globex",
        headers=hdr("u-alice"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["shared_with_tenants"] == []


def test_share_remove_unknown_principal_404(client: TestClient) -> None:
    r = client.delete(
        "/api/tenants/acme/projects/alpha/share/user:u-nobody",
        headers=hdr("u-alice"),
    )
    assert r.status_code == 404, r.text


def test_share_remove_malformed_principal_400(client: TestClient) -> None:
    r = client.delete(
        "/api/tenants/acme/projects/alpha/share/just-a-string",
        headers=hdr("u-alice"),
    )
    assert r.status_code == 400, r.text


def test_share_remove_editor_rejected(client: TestClient) -> None:
    client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "editor"},
    )
    r = client.delete(
        "/api/tenants/acme/projects/alpha/share/user:u-dave",
        headers=hdr("u-bob"),
    )
    assert r.status_code == 403, r.text


# ─── resolution chain ──────────────────────────────────────────────────────


def test_shared_user_can_read_project_file(client: TestClient) -> None:
    """A direct user ACL grant lets a cross-tenant user fetch the SKILL.md."""
    # Before the share: dave (globex-only) cannot reach acme's alpha.
    r = client.get(
        "/api/tenants/acme/projects/alpha/file?path=SKILL.md",
        headers=hdr("u-dave"),
    )
    assert r.status_code == 403, r.text

    # Grant editor.
    grant = client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "editor"},
    )
    assert grant.status_code == 200, grant.text

    r = client.get(
        "/api/tenants/acme/projects/alpha/file?path=SKILL.md",
        headers=hdr("u-dave"),
    )
    assert r.status_code == 200, r.text
    assert r.text.startswith("# Alpha project")


def test_shared_viewer_cannot_write_output_dirs(client: TestClient) -> None:
    """Direct ACL grant of viewer reads but doesn't write."""
    client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "viewer"},
    )
    r = client.put(
        "/api/tenants/acme/projects/alpha/output-dirs",
        headers=hdr("u-dave"),
        json={"current": "svg_final", "dirs": [{"dir": "svg_final", "label": "F"}]},
    )
    assert r.status_code == 403, r.text


def test_shared_editor_can_write_output_dirs(client: TestClient) -> None:
    client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "editor"},
    )
    r = client.put(
        "/api/tenants/acme/projects/alpha/output-dirs",
        headers=hdr("u-dave"),
        json={"current": "svg_final", "dirs": [{"dir": "svg_final", "label": "F-by-dave"}]},
    )
    assert r.status_code == 200, r.text


def test_tenant_grant_caps_at_editor_for_owners(client: TestClient) -> None:
    """``shared_with_tenants`` grants give every tenant member their home role
    *capped at editor* — a tenant owner becomes a project editor, viewers stay
    viewers."""
    client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"tenant_slug": "globex"},
    )
    # dave is globex owner — should read AND write (capped at editor, which is enough).
    r = client.get(
        "/api/tenants/acme/projects/alpha/file?path=SKILL.md",
        headers=hdr("u-dave"),
    )
    assert r.status_code == 200, r.text
    r = client.put(
        "/api/tenants/acme/projects/alpha/output-dirs",
        headers=hdr("u-dave"),
        json={"current": "svg_final", "dirs": [{"dir": "svg_final", "label": "g"}]},
    )
    assert r.status_code == 200, r.text


def test_tenant_grant_viewer_stays_viewer(client: TestClient) -> None:
    """A globex-viewer (erin) on a tenant-grant share keeps viewer access — the
    cap only lowers owner→editor, it doesn't raise viewer→editor."""
    client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"tenant_slug": "globex"},
    )
    r = client.get(
        "/api/tenants/acme/projects/alpha/file?path=SKILL.md",
        headers=hdr("u-erin"),
    )
    assert r.status_code == 200, r.text
    r = client.put(
        "/api/tenants/acme/projects/alpha/output-dirs",
        headers=hdr("u-erin"),
        json={"current": "svg_final", "dirs": [{"dir": "svg_final", "label": "v"}]},
    )
    assert r.status_code == 403, r.text


def test_home_tenant_role_unchanged_by_acl(client: TestClient) -> None:
    """A direct ACL viewer entry does NOT downgrade a home-tenant editor —
    home-tenant role wins per §6.2 resolution order step 2."""
    # bob is an acme editor; pin him as ACL viewer; he should still be editor.
    client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-bob", "role": "viewer"},
    )
    r = client.put(
        "/api/tenants/acme/projects/alpha/output-dirs",
        headers=hdr("u-bob"),
        json={"current": "svg_final", "dirs": [{"dir": "svg_final", "label": "x"}]},
    )
    assert r.status_code == 200, r.text


def test_platform_admin_bypasses_acl(client: TestClient) -> None:
    """Platform admins always resolve to owner — no share needed."""
    r = client.put(
        "/api/tenants/acme/projects/alpha/output-dirs",
        headers=hdr("u-alice"),
        json={"current": "svg_final", "dirs": [{"dir": "svg_final", "label": "a"}]},
    )
    assert r.status_code == 200, r.text


def test_no_grant_no_access(client: TestClient) -> None:
    """A user with no home-tenant role, no ACL entry, and no tenant grant
    cannot reach the project at all."""
    r = client.get(
        "/api/tenants/acme/projects/alpha/slides",
        headers=hdr("u-dave"),
    )
    assert r.status_code == 403, r.text


def test_share_list_visible_to_shared_user(client: TestClient) -> None:
    """The share list is viewer-readable — once dave is added, he can see who
    else is on the project."""
    client.post(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-alice"),
        json={"user_id": "u-dave", "role": "viewer"},
    )
    r = client.get(
        "/api/tenants/acme/projects/alpha/share",
        headers=hdr("u-dave"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["acl"] == [{"user_id": "u-dave", "role": "viewer"}]
