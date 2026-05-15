"""Phase 3 verification — role enforcement + sandbox isolation.

Per §13 Phase 3 of web/MULTITENANT_REARCHITECTURE.md:

- Every API route requires identity. The legacy default-tenant aliases keep
  working but enforce role rank on the ``default`` tenant.
- The tenant-prefixed routes use ``require_tenant_role`` so:
    * viewer can read but never write,
    * editor can write projects/templates but cannot manage members,
    * owner has full control,
    * non-members get 403 (404 on the tenant itself when nonexistent).
- Sandbox (``Session._sandbox_deny``):
    * cross-tenant reads denied,
    * cross-tenant writes denied,
    * platform paths (skills/, assets/, examples/) remain readable,
    * bash deny-list covers tenants/<other>/, repo templates/, and skill
      templates/ as write destinations.

Tests stand up an isolated tree (acme + globex tenants, alice/bob/carol/dave
users covering every role) so we can exercise every membership combination
without touching the real users/tenants on disk.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import files as files_mod
import main
import tenants as tenants_mod
import templates as templates_mod
import users as users_mod
from agent import Session


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scratch users/tenants tree with every role covered.

    Seeded users:
      - ``u-alice`` — platform admin, owner of acme
      - ``u-bob``   — owner of globex (no admin)
      - ``u-carol`` — editor on acme
      - ``u-dave``  — viewer on acme
      - ``u-erin``  — owner of acme (so demoting carol/dave doesn't strand it)
      - ``u-frank`` — no memberships (used for non-member 403 path)
    """
    users_dir = tmp_path / "users"
    tenants_dir = tmp_path / "tenants"
    platform_file = tmp_path / ".platform.json"
    users_dir.mkdir()
    tenants_dir.mkdir()

    # Patch every module-level copy of the storage roots. tenants.py,
    # users.py, templates.py and files.py each cache their own reference, so
    # one ``monkeypatch.setattr`` per module is required. ``REPO_ROOT`` is
    # repointed too because main.py uses it to compute
    # ``proj.relative_to(REPO_ROOT)`` in session-create responses.
    monkeypatch.setattr(users_mod, "USERS_DIR", users_dir)
    monkeypatch.setattr(users_mod, "PLATFORM_FILE", platform_file)
    monkeypatch.setattr(tenants_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(files_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(files_mod, "DEFAULT_TENANT_ROOT", tenants_dir / "default")
    monkeypatch.setattr(
        files_mod, "PROJECTS_DIR", tenants_dir / "default" / "projects"
    )
    monkeypatch.setattr(files_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(main, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(templates_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(
        templates_mod, "DEFAULT_TENANT_ROOT", tenants_dir / "default"
    )
    monkeypatch.setattr(
        templates_mod, "TEMPLATES_DIR", tenants_dir / "default" / "templates"
    )
    monkeypatch.setattr(
        templates_mod, "PROJECTS_DIR", tenants_dir / "default" / "projects"
    )

    # Seed tenants on disk.
    for slug, name in [("acme", "Acme Co"), ("globex", "Globex Inc"), ("default", "Default")]:
        td = tenants_dir / slug
        td.mkdir()
        (td / "config.json").write_text(
            json.dumps({
                "slug": slug,
                "name": name,
                "owner_user_id": f"u-{slug}-owner",
                "default_format": "ppt169",
                "created_at": 1700000000,
            }),
            encoding="utf-8",
        )
        (td / "members.json").write_text(
            json.dumps([
                {
                    "user_id": f"u-{slug}-owner",
                    "role": "owner",
                    "added_at": 1700000000,
                }
            ]),
            encoding="utf-8",
        )
        (td / "projects").mkdir()
        (td / "templates").mkdir()
        (td / "shared").mkdir()
        (td / "design_system").mkdir()

    # Seed a project per tenant so cross-tenant read deny has something to
    # actually point at.
    (tenants_dir / "acme" / "projects" / "alpha").mkdir()
    (tenants_dir / "acme" / "projects" / "alpha" / "README.md").write_text(
        "acme alpha", encoding="utf-8"
    )
    (tenants_dir / "globex" / "projects" / "beta").mkdir()
    (tenants_dir / "globex" / "projects" / "beta" / "README.md").write_text(
        "globex beta", encoding="utf-8"
    )

    # Seed users with overlapping roles so every membership branch is exercised.
    seeded = [
        ("u-alice", "alice@example.com", True,
         [{"tenant_slug": "acme", "role": "owner"}, {"tenant_slug": "default", "role": "owner"}]),
        ("u-bob", "bob@example.com", False,
         [{"tenant_slug": "globex", "role": "owner"}]),
        ("u-carol", "carol@example.com", False,
         [{"tenant_slug": "acme", "role": "editor"}, {"tenant_slug": "default", "role": "editor"}]),
        ("u-dave", "dave@example.com", False,
         [{"tenant_slug": "acme", "role": "viewer"}, {"tenant_slug": "default", "role": "viewer"}]),
        ("u-erin", "erin@example.com", False,
         [{"tenant_slug": "acme", "role": "owner"}]),
        ("u-frank", "frank@example.com", False, []),
    ]
    for uid, email, admin, memberships in seeded:
        (users_dir / f"{uid}.json").write_text(
            json.dumps({
                "id": uid,
                "email": email,
                "display_name": uid.removeprefix("u-"),
                "memberships": memberships,
                "platform_admin": admin,
                "created_at": 1700000000,
            }),
            encoding="utf-8",
        )
    platform_file.write_text(
        json.dumps({
            "admins": ["u-alice"],
            "schema_version": 1,
            "created_at": 1700000000,
        }),
        encoding="utf-8",
    )

    monkeypatch.delenv("DEV_USER_ID", raising=False)
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)

    return tmp_path


@pytest.fixture
def client(isolated_root: Path) -> TestClient:
    return TestClient(main.app)


def hdr(uid: str) -> dict:
    """Build the header pair for an isolated-root user."""
    email = uid.removeprefix("u-") + "@example.com"
    return {"X-User-Id": uid, "X-User-Email": email}


# ─── Role enforcement: tenant-prefixed routes ──────────────────────────────

def test_viewer_can_read_tenant_projects(client: TestClient) -> None:
    r = client.get("/api/tenants/acme/projects", headers=hdr("u-dave"))
    assert r.status_code == 200, r.text
    names = {p["name"] for p in r.json()["projects"]}
    assert "alpha" in names


def test_viewer_cannot_create_template(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/templates",
        headers=hdr("u-dave"),
        json={
            "source_project": "alpha",
            "name": "viewer-block",
            "description": "should fail",
            "include_images": [],
        },
    )
    assert r.status_code == 403, r.text


def test_viewer_cannot_session_create(client: TestClient) -> None:
    """Viewers cannot run agent chats — session-create requires editor+."""
    r = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-dave"),
        json={"name": "no-go", "format": "ppt169"},
    )
    assert r.status_code == 403, r.text


def test_editor_can_session_create(client: TestClient, isolated_root: Path) -> None:
    """Editor + on the tenant is enough to start a session."""
    r = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-carol"),
        json={"name": "carol-deck", "format": "ppt169"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_slug"] == "acme"
    # ``init_project`` decorates the slug with format + date; the prefix
    # is the only stable part so we anchor on that.
    assert body["project"].startswith("carol-deck_")


def test_editor_cannot_add_member(client: TestClient) -> None:
    """Member CRUD is owner-only."""
    r = client.post(
        "/api/tenants/acme/members",
        headers=hdr("u-carol"),
        json={"user": "frank@example.com", "role": "viewer"},
    )
    assert r.status_code == 403, r.text


def test_owner_can_add_member(client: TestClient) -> None:
    r = client.post(
        "/api/tenants/acme/members",
        headers=hdr("u-alice"),
        json={"user": "frank@example.com", "role": "viewer"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"user_id": "u-frank", "email": "frank@example.com", "role": "viewer"}
    # Confirm the roster now lists frank.
    r2 = client.get("/api/tenants/acme/members", headers=hdr("u-alice"))
    assert r2.status_code == 200
    assert any(m["user_id"] == "u-frank" for m in r2.json()["members"])


def test_non_member_forbidden(client: TestClient) -> None:
    """frank has no memberships — every acme route should 403."""
    r = client.get("/api/tenants/acme/projects", headers=hdr("u-frank"))
    assert r.status_code == 403, r.text


def test_platform_admin_bypasses_membership(client: TestClient) -> None:
    """alice is platform admin; she can read globex even without membership."""
    r = client.get("/api/tenants/globex/projects", headers=hdr("u-alice"))
    assert r.status_code == 200, r.text


def test_missing_tenant_returns_404(client: TestClient) -> None:
    r = client.get("/api/tenants/no-such/projects", headers=hdr("u-alice"))
    assert r.status_code == 404, r.text


# ─── Legacy default-tenant routes — role enforcement ───────────────────────

def test_legacy_projects_requires_identity(client: TestClient) -> None:
    r = client.get("/api/projects")
    assert r.status_code == 401, r.text


def test_legacy_templates_requires_default_membership(client: TestClient) -> None:
    """frank is not on default — listing templates should 403."""
    r = client.get("/api/templates", headers=hdr("u-frank"))
    assert r.status_code == 403, r.text


def test_legacy_templates_viewer_can_list(client: TestClient) -> None:
    r = client.get("/api/templates", headers=hdr("u-dave"))
    assert r.status_code == 200, r.text


def test_legacy_session_create_requires_editor(client: TestClient) -> None:
    """Viewer on default should be denied the legacy /api/sessions create path."""
    r = client.post(
        "/api/sessions",
        headers=hdr("u-dave"),
        json={"name": "dave-block", "format": "ppt169"},
    )
    assert r.status_code == 403, r.text


# ─── Sandbox: cross-tenant + bash patterns ─────────────────────────────────

def _acme_session(isolated_root: Path) -> Session:
    """Build a Session pinned to acme/alpha for sandbox tests.

    We patch ``REPO_ROOT`` in :mod:`agent` for the duration of the test so the
    tenant-path helpers resolve against the scratch tree, not the real repo.
    """
    import agent as agent_mod

    proj = isolated_root / "tenants" / "acme" / "projects" / "alpha"
    session = Session(
        id="test-sid",
        project_path=proj,
        tenant_slug="acme",
        user_id="u-carol",
    )
    return session


def test_sandbox_denies_cross_tenant_read(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Read",
        {"file_path": str(isolated_root / "tenants" / "globex" / "projects" / "beta" / "README.md")},
    )
    assert reason == "cross-tenant reads are not allowed"


def test_sandbox_denies_cross_tenant_write(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Write",
        {"file_path": str(isolated_root / "tenants" / "globex" / "projects" / "beta" / "evil.md")},
    )
    assert reason == "cross-tenant writes are not allowed"


def test_sandbox_allows_own_tenant_read(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Read",
        {"file_path": str(isolated_root / "tenants" / "acme" / "projects" / "alpha" / "README.md")},
    )
    assert reason is None


def test_sandbox_allows_platform_skills_read(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads under skills/ (above tenants/) remain allowed for every tenant.

    The platform-scope tree is still in the real repo for now; we just need
    to verify the cross-tenant check doesn't catch paths that aren't under
    tenants/<x>/ at all.
    """
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    (isolated_root / "skills" / "ppt-master").mkdir(parents=True)
    (isolated_root / "skills" / "ppt-master" / "SKILL.md").write_text(
        "platform skill", encoding="utf-8"
    )
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Read",
        {"file_path": str(isolated_root / "skills" / "ppt-master" / "SKILL.md")},
    )
    assert reason is None


def test_sandbox_bash_denies_cross_tenant_path(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Bash",
        {"command": "cat tenants/globex/projects/beta/README.md"},
    )
    assert reason is not None and "tenants/globex/" in reason


def test_sandbox_bash_allows_own_tenant_path(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    # Own tenant path should pass — the bash deny scan only fires for OTHER slugs.
    reason = session._sandbox_deny(
        "Bash",
        {"command": "ls tenants/acme/projects/alpha"},
    )
    assert reason is None


def test_sandbox_bash_denies_skill_template_write(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Bash",
        {"command": "mkdir -p skills/ppt-master/templates/badtpl"},
    )
    assert reason is not None and "skills/ppt-master/templates" in reason


def test_sandbox_bash_denies_repo_template_write(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Bash",
        {"command": "cp foo.md tenants/acme/templates/sneak.md"},
    )
    assert reason is not None and "templates/" in reason


def test_sandbox_denies_output_dirs_direct_write(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agents must register output dirs via the API, not by writing the JSON file."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Write",
        {"file_path": str(session.project_path / "output_dirs.json")},
    )
    assert reason is not None and "output-dirs/register" in reason


def test_sandbox_denies_py_edit(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Edit",
        {"file_path": str(session.project_path / "tools" / "make_deck.py")},
    )
    assert reason is not None and ".py" in reason
