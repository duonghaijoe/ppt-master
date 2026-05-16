"""Phase 7 verification — tenant management UI surface.

Per §13 Phase 7 of web/MULTITENANT_REARCHITECTURE.md:

- ``GET/PUT /api/tenants/{slug}/design-system`` — viewer reads, owner writes
  palette / typography / spec.md slices independently and atomically.
- ``GET/POST/DELETE /api/tenants/{slug}/shared`` — list / upload / delete
  shared assets (logos, fonts, etc) with size cap, extension denylist,
  and path-traversal guard.
- ``GET /api/tenants/{slug}/shared/file`` — viewer can fetch the bytes.
- ``GET /api/tenants/{slug}/templates/{name}/file`` — viewer can stream
  thumbnails and design specs out of a template.
- ``POST /api/tenants/{slug}/projects/from-template`` — editor materialises
  a tenant-scoped template into a new project.

The fixture stands up an isolated tree (acme + globex tenants, alice
platform-admin + acme-owner, bob acme-editor, carol acme-viewer, dave
globex-only) so every membership branch can be exercised without
touching the real users/tenants on disk.
"""
from __future__ import annotations

import io
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
    # Minimal pricing so check_eligibility doesn't blow up if anything reaches it.
    pricing_file.write_text(
        json.dumps({"version": "phase7-test", "margin": 0.20, "providers": {}}),
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
            json.dumps([
                {"user_id": f"u-{slug}-owner", "role": "owner", "added_at": now}
            ]),
            encoding="utf-8",
        )
        (td / "projects").mkdir()
        (td / "templates").mkdir()
        (td / "shared").mkdir()
        (td / "design_system").mkdir()

    # Seed a project that we can save as a template + then create-from-template.
    # Use svg_final/ so the template thumbnail picker has something to copy.
    proj = tenants_dir / "acme" / "projects" / "alpha"
    proj.mkdir()
    (proj / "SKILL.md").write_text("# Alpha project\n", encoding="utf-8")
    (proj / "svg_final").mkdir()
    (proj / "svg_final" / "001.svg").write_text(
        '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"/>',
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


# ─── design system ─────────────────────────────────────────────────────────


def test_design_system_default_empty(client: TestClient) -> None:
    """A fresh tenant returns empty palette/typography/spec — not a 404."""
    r = client.get("/api/tenants/acme/design-system", headers=hdr("u-alice"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"palette": {}, "typography": {}, "spec": ""}


def test_design_system_viewer_can_read(client: TestClient) -> None:
    r = client.get("/api/tenants/acme/design-system", headers=hdr("u-carol"))
    assert r.status_code == 200
    assert "palette" in r.json()


def test_design_system_non_member_403(client: TestClient, isolated_root: Path) -> None:
    r = client.get("/api/tenants/acme/design-system", headers=hdr("u-dave"))
    assert r.status_code == 403, r.text


def test_design_system_owner_writes_all_slices(client: TestClient, isolated_root: Path) -> None:
    payload = {
        "palette": {"primary": "#E8A78A", "ink": "#1A1A1A"},
        "typography": {"heading": "Inter", "body": "Inter"},
        "spec": "## Header pattern\n- stripe at y=520\n",
    }
    r = client.put(
        "/api/tenants/acme/design-system",
        headers=hdr("u-alice"),
        json=payload,
    )
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["palette"] == payload["palette"]
    assert saved["typography"] == payload["typography"]
    assert saved["spec"] == payload["spec"]

    # On disk: palette.json, typography.json, spec.md are written atomically.
    ds_dir = isolated_root / "tenants" / "acme" / "design_system"
    assert json.loads((ds_dir / "palette.json").read_text(encoding="utf-8")) == payload["palette"]
    assert json.loads((ds_dir / "typography.json").read_text(encoding="utf-8")) == payload["typography"]
    assert (ds_dir / "spec.md").read_text(encoding="utf-8") == payload["spec"]


def test_design_system_partial_update_preserves_other_slices(
    client: TestClient, isolated_root: Path,
) -> None:
    """PUT with only `spec` keeps the existing palette + typography."""
    base = {"palette": {"primary": "#E8A78A"}, "typography": {"heading": "Inter"}, "spec": "before"}
    r = client.put("/api/tenants/acme/design-system", headers=hdr("u-alice"), json=base)
    assert r.status_code == 200

    r = client.put(
        "/api/tenants/acme/design-system",
        headers=hdr("u-alice"),
        json={"spec": "after"},
    )
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["palette"] == {"primary": "#E8A78A"}
    assert saved["typography"] == {"heading": "Inter"}
    assert saved["spec"] == "after"


def test_design_system_viewer_cannot_write(client: TestClient) -> None:
    r = client.put(
        "/api/tenants/acme/design-system",
        headers=hdr("u-carol"),
        json={"spec": "viewer shouldn't be able to do this"},
    )
    assert r.status_code == 403, r.text


def test_design_system_editor_cannot_write(client: TestClient) -> None:
    """Design system is owner-only — bob is editor and is rejected."""
    r = client.put(
        "/api/tenants/acme/design-system",
        headers=hdr("u-bob"),
        json={"spec": "editor shouldn't either"},
    )
    assert r.status_code == 403, r.text


def test_design_system_rejects_invalid_palette_type(client: TestClient) -> None:
    """Palette must be a JSON object — not a list / string. Pydantic raises
    422 on schema-mismatched bodies; either 400 or 422 satisfies the rule
    that the malformed write doesn't land."""
    r = client.put(
        "/api/tenants/acme/design-system",
        headers=hdr("u-alice"),
        json={"palette": ["not", "an", "object"]},
    )
    assert r.status_code in (400, 422), r.text


# ─── shared assets ─────────────────────────────────────────────────────────


def test_shared_assets_empty_on_fresh_tenant(client: TestClient) -> None:
    r = client.get("/api/tenants/acme/shared", headers=hdr("u-alice"))
    assert r.status_code == 200
    assert r.json() == {"assets": []}


def test_shared_assets_upload_and_list(client: TestClient, isolated_root: Path) -> None:
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    r = client.post(
        "/api/tenants/acme/shared",
        headers=hdr("u-alice"),
        files={"file": ("dark.png", blob, "image/png")},
        data={"path": "logos/dark.png"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "logos/dark.png"
    assert body["size"] == len(blob)

    on_disk = isolated_root / "tenants" / "acme" / "shared" / "logos" / "dark.png"
    assert on_disk.read_bytes() == blob

    r = client.get("/api/tenants/acme/shared", headers=hdr("u-alice"))
    paths = [a["path"] for a in r.json()["assets"]]
    assert "logos/dark.png" in paths


def test_shared_assets_viewer_can_list_but_not_write(client: TestClient) -> None:
    r = client.get("/api/tenants/acme/shared", headers=hdr("u-carol"))
    assert r.status_code == 200

    r = client.post(
        "/api/tenants/acme/shared",
        headers=hdr("u-carol"),
        files={"file": ("logo.png", b"data", "image/png")},
        data={"path": "logo.png"},
    )
    assert r.status_code == 403, r.text


def test_shared_assets_editor_can_write(client: TestClient) -> None:
    """Shared uploads require editor (not just owner). Per spec: tenant
    assets are content, not config — editors curate them like projects."""
    r = client.post(
        "/api/tenants/acme/shared",
        headers=hdr("u-bob"),
        files={"file": ("hero.svg", b"<svg/>", "image/svg+xml")},
        data={"path": "hero.svg"},
    )
    assert r.status_code == 200, r.text


def test_shared_upload_blocks_executable_extension(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/shared",
        headers=hdr("u-alice"),
        files={"file": ("evil.exe", b"MZ\x00\x00", "application/x-msdownload")},
        data={"path": "evil.exe"},
    )
    assert r.status_code == 400, r.text
    # Common scripty types are also rejected.
    for forbidden in ("evil.js", "evil.html", "evil.py", "evil.sh"):
        r = client.post(
            "/api/tenants/acme/shared",
            headers=hdr("u-alice"),
            files={"file": (forbidden, b"// payload", "text/plain")},
            data={"path": forbidden},
        )
        assert r.status_code == 400, f"{forbidden}: {r.status_code} {r.text}"


def test_shared_upload_blocks_path_traversal(client: TestClient, isolated_root: Path) -> None:
    """A relative path that escapes the shared/ root must be rejected.

    Leading ``/`` is stripped (not rejected) — the frontend does the same
    so users can paste ``/logos/dark.png`` without an error.
    """
    for bad_path in ("../escape.png", "logos/../../escape.png", "/../escape.png"):
        r = client.post(
            "/api/tenants/acme/shared",
            headers=hdr("u-alice"),
            files={"file": ("x.png", b"data", "image/png")},
            data={"path": bad_path},
        )
        assert r.status_code in (400, 422), f"{bad_path}: {r.status_code} {r.text}"
    # Nothing wrote outside of shared/ — the tenant directory contains no
    # stray ``escape.png`` and the parent dir is untouched.
    shared_root = isolated_root / "tenants" / "acme" / "shared"
    for entry in shared_root.rglob("escape.png"):
        raise AssertionError(f"escape.png landed under shared/: {entry}")
    assert not (isolated_root / "tenants" / "acme" / "escape.png").exists()
    assert not (isolated_root / "escape.png").exists()


def test_shared_upload_blocks_oversize(client: TestClient) -> None:
    """The 25 MB cap kicks in on byte length — we send 26 MB."""
    blob = io.BytesIO(b"\x00" * (26 * 1024 * 1024))
    r = client.post(
        "/api/tenants/acme/shared",
        headers=hdr("u-alice"),
        files={"file": ("big.png", blob, "image/png")},
        data={"path": "big.png"},
    )
    assert r.status_code == 400, r.text


def test_shared_asset_delete_clears_file(client: TestClient, isolated_root: Path) -> None:
    """Editor can delete an asset they uploaded; the file goes away."""
    client.post(
        "/api/tenants/acme/shared",
        headers=hdr("u-bob"),
        files={"file": ("small.png", b"data", "image/png")},
        data={"path": "small.png"},
    )
    r = client.delete(
        "/api/tenants/acme/shared",
        headers=hdr("u-bob"),
        params={"path": "small.png"},
    )
    assert r.status_code == 200, r.text
    on_disk = isolated_root / "tenants" / "acme" / "shared" / "small.png"
    assert not on_disk.exists()


def test_shared_asset_file_fetch_returns_bytes(client: TestClient) -> None:
    blob = b"original-svg-bytes"
    client.post(
        "/api/tenants/acme/shared",
        headers=hdr("u-alice"),
        files={"file": ("logo.svg", blob, "image/svg+xml")},
        data={"path": "logo.svg"},
    )
    r = client.get(
        "/api/tenants/acme/shared/file",
        headers=hdr("u-carol"),
        params={"path": "logo.svg"},
    )
    assert r.status_code == 200, r.text
    assert r.content == blob


def test_shared_file_non_member_403(client: TestClient) -> None:
    """Non-members can't fetch via the file route either."""
    client.post(
        "/api/tenants/acme/shared",
        headers=hdr("u-alice"),
        files={"file": ("logo.svg", b"x", "image/svg+xml")},
        data={"path": "logo.svg"},
    )
    r = client.get(
        "/api/tenants/acme/shared/file",
        headers=hdr("u-dave"),
        params={"path": "logo.svg"},
    )
    assert r.status_code == 403, r.text


# ─── templates: tenant-scoped file fetch + from-template ───────────────────


def test_tenant_template_save_and_thumbnail_fetch(
    client: TestClient, isolated_root: Path,
) -> None:
    """Save acme/alpha as a template, then fetch its thumbnail via the
    tenant-scoped file route."""
    # Seed a fake thumbnail so the file route has something to serve.
    (isolated_root / "tenants" / "acme" / "projects" / "alpha" / "thumbnail.svg").write_text(
        '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="64" height="36"/>',
        encoding="utf-8",
    )
    r = client.post(
        "/api/tenants/acme/templates",
        headers=hdr("u-bob"),
        json={
            "source_project": "alpha",
            "name": "alpha-tmpl",
            "description": "phase 7 fixture",
            "include_images": [],
        },
    )
    assert r.status_code == 200, r.text

    r = client.get(
        "/api/tenants/acme/templates/alpha-tmpl/file",
        headers=hdr("u-carol"),
        params={"path": "thumbnail.svg"},
    )
    assert r.status_code == 200, r.text
    assert "svg" in r.headers["content-type"]


def test_tenant_template_file_path_escape_rejected(
    client: TestClient, isolated_root: Path,
) -> None:
    """The path query param can't break out of the template directory."""
    (isolated_root / "tenants" / "acme" / "projects" / "alpha" / "thumbnail.svg").write_text(
        '<svg/>', encoding="utf-8",
    )
    client.post(
        "/api/tenants/acme/templates",
        headers=hdr("u-bob"),
        json={"source_project": "alpha", "name": "esc-tmpl",
              "description": "", "include_images": []},
    )
    r = client.get(
        "/api/tenants/acme/templates/esc-tmpl/file",
        headers=hdr("u-bob"),
        params={"path": "../../../etc/passwd"},
    )
    assert r.status_code in (400, 404), r.text


def test_tenant_template_file_non_member_403(
    client: TestClient, isolated_root: Path,
) -> None:
    (isolated_root / "tenants" / "acme" / "projects" / "alpha" / "thumbnail.svg").write_text(
        '<svg/>', encoding="utf-8",
    )
    client.post(
        "/api/tenants/acme/templates",
        headers=hdr("u-bob"),
        json={"source_project": "alpha", "name": "non-member-tmpl",
              "description": "", "include_images": []},
    )
    r = client.get(
        "/api/tenants/acme/templates/non-member-tmpl/file",
        headers=hdr("u-dave"),
        params={"path": "thumbnail.svg"},
    )
    assert r.status_code == 403, r.text


def test_create_project_from_template_editor_succeeds(
    client: TestClient, isolated_root: Path,
) -> None:
    """Editor can materialize a tenant template into a new project."""
    # Save the alpha project as a template first.
    r = client.post(
        "/api/tenants/acme/templates",
        headers=hdr("u-bob"),
        json={"source_project": "alpha", "name": "alpha-tmpl",
              "description": "", "include_images": []},
    )
    assert r.status_code == 200, r.text

    r = client.post(
        "/api/tenants/acme/projects/from-template",
        headers=hdr("u-bob"),
        json={
            "template": "alpha-tmpl",
            "project_name": "alpha_copy_phase7",
            "format": "ppt169",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant"] == "acme"
    # project_manager appends ``_<fmt>_<YYYYMMDD>`` for uniqueness — match
    # the prefix rather than the final canonicalised name.
    assert body["project"].startswith("alpha_copy_phase7"), body
    assert body["template"] == "alpha-tmpl"
    assert (
        isolated_root / "tenants" / "acme" / "projects" / body["project"]
    ).is_dir()


def test_create_project_from_template_viewer_blocked(
    client: TestClient, isolated_root: Path,
) -> None:
    """Viewer cannot create projects from templates."""
    # Seed a template the viewer can see.
    client.post(
        "/api/tenants/acme/templates",
        headers=hdr("u-bob"),
        json={"source_project": "alpha", "name": "viewer-block-tmpl",
              "description": "", "include_images": []},
    )
    r = client.post(
        "/api/tenants/acme/projects/from-template",
        headers=hdr("u-carol"),
        json={
            "template": "viewer-block-tmpl",
            "project_name": "alpha_viewer_blocked",
            "format": "ppt169",
        },
    )
    assert r.status_code == 403, r.text


def test_create_project_from_template_missing_template_404(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/projects/from-template",
        headers=hdr("u-bob"),
        json={"template": "does-not-exist", "project_name": "newproj", "format": "ppt169"},
    )
    assert r.status_code == 404, r.text


# ─── members: regression — phase 3 owner-only writes still hold ────────────


def test_members_owner_only_write_regression(client: TestClient) -> None:
    """Owners can manage; editor + viewer get 403."""
    r = client.post(
        "/api/tenants/acme/members",
        headers=hdr("u-alice"),
        json={"user": "u-dave", "role": "viewer"},
    )
    assert r.status_code == 200, r.text

    r = client.post(
        "/api/tenants/acme/members",
        headers=hdr("u-bob"),
        json={"user": "u-dave", "role": "editor"},
    )
    assert r.status_code == 403, r.text

    r = client.delete(
        "/api/tenants/acme/members/u-dave",
        headers=hdr("u-carol"),
    )
    assert r.status_code == 403, r.text
