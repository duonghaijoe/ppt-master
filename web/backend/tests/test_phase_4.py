"""Phase 4 verification — platform scope.

Per §13 Phase 4 of web/MULTITENANT_REARCHITECTURE.md:

- ``skills/`` has moved to ``platform/skills/``. Agent sandbox reads from
  the new location continue to work for every tenant.
- ``/api/platform/skills`` endpoints expose the skill packages and their
  files to any authenticated user (read-only).
- ``/api/platform/admins`` is platform-admin-only — tenants cannot
  enumerate who can bypass tenant role checks.
- Reading ``.platform.json`` (the legacy admin registry) from the agent
  sandbox is denied via both file-Read and Bash paths.

Tests stand up an isolated tree with a seeded platform/skills/ppt-master/
package so we can exercise the read paths end-to-end without depending on
the real on-disk skill content.
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
    """Tree with one tenant + one platform skill package."""
    users_dir = tmp_path / "users"
    tenants_dir = tmp_path / "tenants"
    platform_file = tmp_path / ".platform.json"
    platform_skills_dir = tmp_path / "platform" / "skills"
    users_dir.mkdir()
    tenants_dir.mkdir()
    platform_skills_dir.mkdir(parents=True)

    monkeypatch.setattr(users_mod, "USERS_DIR", users_dir)
    monkeypatch.setattr(users_mod, "PLATFORM_FILE", platform_file)
    monkeypatch.setattr(tenants_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(files_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(files_mod, "DEFAULT_TENANT_ROOT", tenants_dir / "default")
    monkeypatch.setattr(files_mod, "PROJECTS_DIR", tenants_dir / "default" / "projects")
    monkeypatch.setattr(files_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(main, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(main, "PLATFORM_DIR", tmp_path / "platform")
    monkeypatch.setattr(main, "PLATFORM_SKILLS_DIR", platform_skills_dir)
    monkeypatch.setattr(templates_mod, "TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(templates_mod, "DEFAULT_TENANT_ROOT", tenants_dir / "default")
    monkeypatch.setattr(templates_mod, "TEMPLATES_DIR", tenants_dir / "default" / "templates")
    monkeypatch.setattr(templates_mod, "PROJECTS_DIR", tenants_dir / "default" / "projects")

    # Default tenant + an acme tenant so we can confirm cross-tenant reads of
    # platform/skills/ still go through (skill content is platform-scope).
    for slug, name in [("default", "Default"), ("acme", "Acme Co")]:
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
                {"user_id": f"u-{slug}-owner", "role": "owner", "added_at": 1700000000}
            ]),
            encoding="utf-8",
        )
        (td / "projects").mkdir()
        (td / "templates").mkdir()
        (td / "shared").mkdir()
        (td / "design_system").mkdir()

    (tenants_dir / "acme" / "projects" / "alpha").mkdir()
    (tenants_dir / "acme" / "projects" / "alpha" / "README.md").write_text(
        "acme alpha", encoding="utf-8"
    )

    # Seed a fake skill package.
    skill_root = platform_skills_dir / "ppt-master"
    (skill_root / "references").mkdir(parents=True)
    (skill_root / "templates").mkdir()
    (skill_root / "SKILL.md").write_text("# PPT Master\n\nworkflow doc\n", encoding="utf-8")
    (skill_root / "references" / "executor-base.md").write_text(
        "executor rules", encoding="utf-8"
    )
    # Second skill so the list endpoint returns more than one entry.
    (platform_skills_dir / "design-system" / "references").mkdir(parents=True)
    (platform_skills_dir / "design-system" / "SKILL.md").write_text(
        "# Design System\n", encoding="utf-8"
    )
    # Hidden dir should never appear in list/tree output.
    (platform_skills_dir / ".cache").mkdir()

    seeded = [
        ("u-alice", "alice@example.com", True,
         [{"tenant_slug": "acme", "role": "owner"},
          {"tenant_slug": "default", "role": "owner"}]),
        ("u-bob", "bob@example.com", False,
         [{"tenant_slug": "acme", "role": "viewer"},
          {"tenant_slug": "default", "role": "viewer"}]),
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
    email = uid.removeprefix("u-") + "@example.com"
    return {"X-User-Id": uid, "X-User-Email": email}


# ─── /api/platform/skills ──────────────────────────────────────────────────


def test_platform_skills_list_requires_auth(client: TestClient) -> None:
    r = client.get("/api/platform/skills")
    assert r.status_code == 401, r.text


def test_platform_skills_list_returns_packages(client: TestClient) -> None:
    r = client.get("/api/platform/skills", headers=hdr("u-bob"))
    assert r.status_code == 200, r.text
    names = {s["name"] for s in r.json()["skills"]}
    assert names == {"ppt-master", "design-system"}
    pm = next(s for s in r.json()["skills"] if s["name"] == "ppt-master")
    assert pm["has_skill_md"] is True


def test_platform_skills_list_skips_hidden_dirs(client: TestClient) -> None:
    r = client.get("/api/platform/skills", headers=hdr("u-bob"))
    names = [s["name"] for s in r.json()["skills"]]
    assert ".cache" not in names


def test_platform_skill_get_returns_skill_md(client: TestClient) -> None:
    r = client.get("/api/platform/skills/ppt-master", headers=hdr("u-bob"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "ppt-master"
    assert body["has_skill_md"] is True
    assert body["skill_md"].startswith("# PPT Master")


def test_platform_skill_get_404_for_unknown(client: TestClient) -> None:
    r = client.get("/api/platform/skills/nope", headers=hdr("u-bob"))
    assert r.status_code == 404


def test_platform_skill_get_rejects_path_traversal(client: TestClient) -> None:
    # Starlette resolves `..` segments in the path before routing, so the
    # request collapses to ``/api/platform/skills/`` and never reaches the
    # parameterised route — a 404 from FastAPI's router is the expected
    # outcome. The point of the assertion is just to confirm that no
    # traversal request can read a sibling directory.
    r = client.get("/api/platform/skills/..", headers=hdr("u-bob"))
    assert r.status_code in (400, 404)


def test_platform_skill_tree_lists_dir(client: TestClient) -> None:
    r = client.get(
        "/api/platform/skills/ppt-master/tree?path=references",
        headers=hdr("u-bob"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "references"
    names = {e["name"] for e in body["entries"]}
    assert "executor-base.md" in names


def test_platform_skill_tree_blocks_escape(client: TestClient) -> None:
    r = client.get(
        "/api/platform/skills/ppt-master/tree?path=../design-system",
        headers=hdr("u-bob"),
    )
    assert r.status_code == 400


def test_platform_skill_file_returns_content(client: TestClient) -> None:
    r = client.get(
        "/api/platform/skills/ppt-master/file?path=references/executor-base.md",
        headers=hdr("u-bob"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "references/executor-base.md"
    assert "executor rules" in body["content"]


def test_platform_skill_file_404_for_missing(client: TestClient) -> None:
    r = client.get(
        "/api/platform/skills/ppt-master/file?path=does-not-exist.md",
        headers=hdr("u-bob"),
    )
    assert r.status_code == 404


# ─── /api/platform/admins (platform-admin only) ────────────────────────────


def test_platform_admins_requires_admin(client: TestClient) -> None:
    """A tenant-only user cannot enumerate platform admins (verify gate)."""
    r = client.get("/api/platform/admins", headers=hdr("u-bob"))
    assert r.status_code == 403, r.text


def test_platform_admins_denies_non_member(client: TestClient) -> None:
    r = client.get("/api/platform/admins", headers=hdr("u-frank"))
    assert r.status_code == 403


def test_platform_admins_returns_for_admin(client: TestClient) -> None:
    r = client.get("/api/platform/admins", headers=hdr("u-alice"))
    assert r.status_code == 200, r.text
    assert r.json()["admins"] == ["u-alice"]


# ─── Agent sandbox — platform skill reads + admin denial ───────────────────


def _acme_session(root: Path) -> Session:
    return Session(
        id="t-phase4",
        project_path=(root / "tenants" / "acme" / "projects" / "alpha"),
        tenant_slug="acme",
        user_id="u-bob",
    )


def test_sandbox_allows_platform_skill_read(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Read",
        {"file_path": str(isolated_root / "platform" / "skills" / "ppt-master" / "SKILL.md")},
    )
    assert reason is None


def test_sandbox_denies_platform_admin_read(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the platform admin registry from the agent sandbox is denied."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Read",
        {"file_path": str(isolated_root / ".platform.json")},
    )
    assert reason is not None
    assert "platform admin" in reason


def test_sandbox_denies_platform_admins_json_read(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Future-shape admin registry at platform/admins.json is also denied."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    (isolated_root / "platform" / "admins.json").write_text(
        '{"admins": []}', encoding="utf-8"
    )
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Read",
        {"file_path": str(isolated_root / "platform" / "admins.json")},
    )
    assert reason is not None


def test_sandbox_bash_denies_cat_platform_json(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bash routes around file Read still hit the platform-admin guard."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Bash",
        {"command": "cat .platform.json"},
    )
    assert reason is not None
    assert "platform admin" in reason


def test_sandbox_bash_denies_platform_admins_listing(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Bash",
        {"command": "ls platform/admins/"},
    )
    assert reason is not None
    assert "platform admin" in reason


def test_sandbox_bash_allows_skill_read(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cat platform/skills/...` continues to work — that's a platform read."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Bash",
        {"command": "cat platform/skills/ppt-master/SKILL.md"},
    )
    assert reason is None
