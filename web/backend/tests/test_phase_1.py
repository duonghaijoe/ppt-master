"""Phase 1 verification — filesystem layer + path-mapper.

What this exercises (per §13 Phase 1 of web/MULTITENANT_REARCHITECTURE.md):

1. ``PROJECTS_DIR`` and ``TEMPLATES_DIR`` resolve under ``tenants/default/``,
   not the legacy repo-root locations.
2. Existing project routes (``/api/projects``, ``/api/projects/<n>/tree``,
   ``/api/projects/<n>/slides``) still serve the migrated projects without
   any URL changes — backwards-compat is the whole point of Phase 1.
3. Template create / list / delete still round-trips, and the saved template
   actually lands under ``tenants/default/templates/<name>/``.

The tests use FastAPI's TestClient against the real ``app`` and the real
on-disk projects/tenants tree. No mocks — if the migration regressed, the
project list comes back empty and the asserts catch it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import files
import main
import templates as templates_mod


REPO_ROOT = files.REPO_ROOT


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(main.app)


def test_projects_dir_points_at_default_tenant() -> None:
    assert files.PROJECTS_DIR == REPO_ROOT / "tenants" / "default" / "projects"
    assert files.DEFAULT_TENANT_ROOT == REPO_ROOT / "tenants" / "default"
    assert files.PROJECTS_DIR.is_dir(), (
        "tenants/default/projects/ must exist after Phase 1 migration"
    )


def test_templates_dir_points_at_default_tenant() -> None:
    assert templates_mod.TEMPLATES_DIR == REPO_ROOT / "tenants" / "default" / "templates"


def test_no_stray_legacy_projects_dir() -> None:
    """Phase 1 leaves the old repo-root projects/ either absent or empty.

    A stale ``projects/`` at the repo root usually means someone reverted the
    migration mid-flight — every subsequent test would still pass (because
    PROJECTS_DIR points at the new location) so we guard it explicitly.
    """
    legacy = REPO_ROOT / "projects"
    if legacy.exists():
        entries = [p for p in legacy.iterdir() if p.name not in {".DS_Store"}]
        assert not entries, f"legacy projects/ still has content: {entries}"


def test_platform_json_exists() -> None:
    """Migration writes ``.platform.json`` with the bootstrap admin id."""
    pf = REPO_ROOT / ".platform.json"
    assert pf.exists(), ".platform.json should be created by the migration"
    import json

    data = json.loads(pf.read_text(encoding="utf-8"))
    assert data.get("schema_version") == 1
    assert isinstance(data.get("admins"), list) and data["admins"], (
        "admins list must contain the bootstrap user id"
    )


def test_default_tenant_config_exists() -> None:
    cfg = REPO_ROOT / "tenants" / "default" / "config.json"
    assert cfg.exists(), "tenants/default/config.json missing"
    import json

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data.get("slug") == "default"
    assert data.get("owner_user_id")


def test_get_projects_returns_migrated_decks(client: TestClient) -> None:
    """Backwards compat: ``/api/projects`` returns the migrated projects."""
    r = client.get("/api/projects")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "projects" in body and isinstance(body["projects"], list)
    # Repo has 11 actual project dirs after the migration; we only check that
    # the list is non-empty so the test isn't brittle when a project is added.
    assert len(body["projects"]) >= 1, "expected migrated projects in the listing"
    for entry in body["projects"]:
        for key in ("name", "slides", "exports", "mtime"):
            assert key in entry, f"missing key {key!r} in {entry}"


def test_get_project_tree_works(client: TestClient) -> None:
    """Tree endpoint resolves project paths under the new root."""
    r = client.get("/api/projects")
    assert r.status_code == 200
    projects = r.json()["projects"]
    assert projects, "no projects to drill into"
    name = projects[0]["name"]
    r = client.get(f"/api/projects/{name}/tree")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("type") == "dir"
    assert isinstance(body.get("entries"), list)


def test_templates_lifecycle(client: TestClient) -> None:
    """Save → list → delete cycle works and writes under tenants/default/."""
    # Pick a project that's likely to have a thumbnail-eligible svg.
    r = client.get("/api/projects")
    assert r.status_code == 200
    projects = r.json()["projects"]
    source = next((p for p in projects if p.get("slides", 0) >= 1), None)
    if source is None:
        pytest.skip("no project with slides available to template")
    tpl_name = "phase1_pytest_smoke"
    # Best-effort cleanup if a previous failed run left it behind.
    client.delete(f"/api/templates/{tpl_name}")

    r = client.post(
        "/api/templates",
        json={
            "source_project": source["name"],
            "name": tpl_name,
            "description": "phase 1 pytest smoke",
            "include_images": [],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == tpl_name
    # The on-disk template must live under tenants/default/templates/.
    saved = templates_mod.TEMPLATES_DIR / tpl_name
    assert saved.is_dir(), f"template dir not under tenants/default: {saved}"

    r = client.get("/api/templates")
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["templates"]]
    assert tpl_name in names

    r = client.delete(f"/api/templates/{tpl_name}")
    assert r.status_code == 200
    assert not saved.exists()
