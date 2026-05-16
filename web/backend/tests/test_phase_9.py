"""Phase 9 verification — hardening + nice-to-haves.

Per §13 Phase 9 of web/MULTITENANT_REARCHITECTURE.md:

- Append-only audit log at ``platform/audit.log`` (JSONL), readable via
  ``GET /api/platform/audit`` (platform-admin only).
- Cost-aware per-tenant rate limiting (req/min + cost/min USD) with 429
  responses, ``Retry-After`` header, and audit ``rate_limit.block`` row.
- Tenant quotas — ``max_projects`` and ``max_storage_bytes`` — enforced at
  every write boundary (project create, from-template, shared upload,
  image-gen pre-flight).
- Soft delete: ``DELETE /api/tenants/{slug}/projects/{name}`` moves the dir
  to ``.trash/`` so accidental deletes are reversible. List / restore /
  purge endpoints round-trip the entry and honour the §6 RBAC matrix.
- Monthly invoice cron for stripe_auto tenants — idempotent on
  (tenant, period), raises 503 without ``STRIPE_TEST_SECRET_KEY``.

The fixture mirrors Phases 7+8 — three tenants (default, acme, globex) and
five users (alice platform-admin, bob acme-editor, carol acme-viewer, dave
globex-owner, erin globex-viewer) — so every RBAC + cross-tenant edge case
maps to a real header set.
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import audit
import billing
import files as files_mod
import main
import metering as metering_mod
import quotas
import rate_limit
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
    audit_log_path = platform_dir / "audit.log"

    users_dir.mkdir()
    tenants_dir.mkdir()
    platform_dir.mkdir()
    platform_skills_dir.mkdir()
    pricing_file.write_text(
        json.dumps({
            "version": "phase9-test",
            "margin": 0.20,
            "providers": {
                "openai": {
                    "models": {
                        "gpt-image-1": {
                            "image": {
                                "1024": {"per_image_usd": 0.04},
                                "2048": {"per_image_usd": 0.08},
                            }
                        }
                    }
                }
            },
        }),
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
    monkeypatch.setattr(metering_mod, "PLATFORM_DIR", platform_dir)
    monkeypatch.setattr(metering_mod, "BILLING_DB", billing_db)
    monkeypatch.setattr(metering_mod, "PRICING_FILE", pricing_file)
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", audit_log_path)

    # In-process rate-limit buckets persist across tests; reset between runs.
    rate_limit.reset()

    # Phase 9 tests should never hit Stripe live, even on a dev machine that
    # happens to have STRIPE_TEST_SECRET_KEY exported.
    monkeypatch.delenv("STRIPE_TEST_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

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

    # Seed one project on acme so trash + share + project-level tests have a
    # real directory to mutate.
    proj = tenants_dir / "acme" / "projects" / "alpha"
    proj.mkdir()
    (proj / "SKILL.md").write_text("# Alpha project\n", encoding="utf-8")
    (proj / "images").mkdir()

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


def _read_audit(isolated_root: Path) -> list[dict]:
    """Read all audit events for assertions, newest-first."""
    return audit.read(limit=10_000)


def _set_quota(isolated_root: Path, slug: str, **fields) -> None:
    """Patch a tenant's quota block in config.json."""
    cfg_path = isolated_root / "tenants" / slug / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["quota"] = {**(cfg.get("quota") or {}), **fields}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


def _set_rate_limit(isolated_root: Path, slug: str, **fields) -> None:
    """Patch a tenant's rate_limit block in config.json and rebuild buckets."""
    cfg_path = isolated_root / "tenants" / slug / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["rate_limit"] = {**(cfg.get("rate_limit") or {}), **fields}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    rate_limit.reset(slug)


# ─── audit log ─────────────────────────────────────────────────────────────


def test_audit_records_tenant_create(client: TestClient, isolated_root: Path) -> None:
    r = client.post(
        "/api/tenants",
        headers=hdr("u-alice"),
        json={"slug": "newco", "name": "New Co"},
    )
    assert r.status_code == 200, r.text
    events = _read_audit(isolated_root)
    create = [e for e in events if e["action"] == "tenant.create"]
    assert create, "expected tenant.create event"
    assert create[0]["tenant"] == "newco"
    assert create[0]["actor"]["user_id"] == "u-alice"
    assert create[0]["actor"]["platform_admin"] is True


def test_audit_records_member_add(client: TestClient, isolated_root: Path) -> None:
    r = client.post(
        "/api/tenants/acme/members",
        headers=hdr("u-alice"),
        json={"user": "dave@example.com", "role": "viewer"},
    )
    assert r.status_code == 200, r.text
    events = _read_audit(isolated_root)
    add = [e for e in events if e["action"] == "member.add"]
    assert add and add[0]["target"] == "u-dave"
    assert add[0]["tenant"] == "acme"


def test_audit_records_manual_credit(client: TestClient, isolated_root: Path) -> None:
    r = client.post(
        "/api/platform/billing/credit",
        headers=hdr("u-alice"),
        json={
            "tenant": "acme",
            "amount_usd": "25.00",
            "reference": "wire-2026-05-01",
            "note": "initial wire",
        },
    )
    assert r.status_code == 200, r.text
    events = _read_audit(isolated_root)
    creds = [e for e in events if e["action"] == "billing.manual_credit"]
    assert creds and creds[0]["tenant"] == "acme"
    assert creds[0]["target"] == "wire-2026-05-01"
    assert creds[0]["amount_usd"] == "25.000000"


def test_audit_records_cap_change(client: TestClient, isolated_root: Path) -> None:
    r = client.patch(
        "/api/tenants/acme/billing/caps",
        headers=hdr("u-alice"),
        json={"cap_soft_usd": "60.00"},
    )
    assert r.status_code == 200, r.text
    events = _read_audit(isolated_root)
    caps = [e for e in events if e["action"] == "billing.cap_change"]
    assert caps and caps[0]["target"] == "cap_soft_usd"


def test_audit_records_payment_change(client: TestClient, isolated_root: Path) -> None:
    r = client.patch(
        "/api/tenants/acme/billing/payment",
        headers=hdr("u-alice"),
        json={"payment_mode": "stripe_auto"},
    )
    assert r.status_code == 200, r.text
    events = _read_audit(isolated_root)
    pmt = [e for e in events if e["action"] == "billing.payment_change"]
    assert pmt and pmt[0]["target"] == "payment_mode"


def test_audit_endpoint_platform_admin_only(client: TestClient) -> None:
    """Non-admins (even tenant owners) cannot read the platform audit log."""
    r = client.get("/api/platform/audit", headers=hdr("u-dave"))
    assert r.status_code == 403, r.text


def test_audit_endpoint_returns_newest_first(client: TestClient) -> None:
    # Trigger two events with a tiny gap to give them distinct timestamps.
    client.post(
        "/api/tenants/acme/members",
        headers=hdr("u-alice"),
        json={"user": "dave@example.com", "role": "editor"},
    )
    time.sleep(1)
    client.patch(
        "/api/tenants/acme/members/u-dave",
        headers=hdr("u-alice"),
        json={"role": "viewer"},
    )
    r = client.get(
        "/api/platform/audit?tenant=acme&limit=10", headers=hdr("u-alice")
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "events" in body
    relevant = [e for e in body["events"] if e["action"] in {"member.add", "member.role_change"}]
    assert relevant, "expected audit events"
    # Newest first → role_change before add.
    assert relevant[0]["action"] == "member.role_change"


# ─── rate limiting ─────────────────────────────────────────────────────────


def test_rate_limit_blocks_at_threshold(client: TestClient, isolated_root: Path) -> None:
    """3 req/min ceiling: 4th call within the same window 429s."""
    _set_rate_limit(isolated_root, "acme", req_per_min=3, cost_per_min_usd=100)
    # The 4th caps-PATCH should 429 — first 3 succeed.
    for i in range(3):
        r = client.patch(
            "/api/tenants/acme/billing/caps",
            headers=hdr("u-alice"),
            json={"cap_soft_usd": f"{50 + i}.00"},
        )
        # tenant_billing_caps doesn't itself rate-limit; use design-system PUT
        # which lands an audit row + can be replayed. Drop this loop in favour
        # of session-create — that's the actual rate-limited path.
        break

    # Use session create — actively rate-limited.
    _set_rate_limit(isolated_root, "acme", req_per_min=3, cost_per_min_usd=100)
    successes = 0
    for i in range(3):
        r = client.post(
            "/api/tenants/acme/sessions",
            headers=hdr("u-alice"),
            json={"name": f"rl_proj_{i}", "format": "ppt169"},
        )
        if r.status_code == 200:
            successes += 1
        else:
            # Stop on first non-success; we want to assert on the 4th.
            break
    assert successes == 3, f"expected 3 successes before 429, got {successes}"
    blocked = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-alice"),
        json={"name": "rl_proj_blocked", "format": "ppt169"},
    )
    assert blocked.status_code == 429, blocked.text
    detail = blocked.json()["detail"]
    assert detail["error"] == "rate_limited"
    assert detail["reason"] == "req_per_min"
    assert detail["retry_after_seconds"] > 0
    assert "Retry-After" in blocked.headers


def test_rate_limit_emits_audit_row(client: TestClient, isolated_root: Path) -> None:
    _set_rate_limit(isolated_root, "acme", req_per_min=1, cost_per_min_usd=100)
    # First call succeeds; second is blocked + audited.
    client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-alice"),
        json={"name": "rl_audit_first", "format": "ppt169"},
    )
    r = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-alice"),
        json={"name": "rl_audit_second", "format": "ppt169"},
    )
    assert r.status_code == 429
    events = _read_audit(isolated_root)
    blocks = [e for e in events if e["action"] == "rate_limit.block"]
    assert blocks, "expected rate_limit.block audit row"
    assert blocks[0]["tenant"] == "acme"
    assert blocks[0]["reason"] in {"req_per_min", "cost_per_min_usd"}


def test_rate_limit_cost_bucket_independent(client: TestClient, isolated_root: Path) -> None:
    """req_per_min headroom but cost ceiling exhausted → 429 on cost bucket."""
    # Each chat-session create estimates the model's cost; set the cost bucket
    # so the second call exceeds it but req bucket has plenty of room.
    _set_rate_limit(
        isolated_root, "acme", req_per_min=100, cost_per_min_usd=0.000001
    )
    # First call drains both buckets minimally; second should land on cost.
    r1 = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-alice"),
        json={"name": "cost_first", "format": "ppt169"},
    )
    r2 = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-alice"),
        json={"name": "cost_second", "format": "ppt169"},
    )
    # At least one of the two should be blocked on cost_per_min_usd.
    blocked = next(r for r in (r1, r2) if r.status_code == 429)
    assert blocked.json()["detail"]["reason"] == "cost_per_min_usd"


# ─── quotas ────────────────────────────────────────────────────────────────


def test_quota_usage_endpoint_shape(client: TestClient) -> None:
    r = client.get("/api/tenants/acme/quota", headers=hdr("u-bob"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) >= {"projects", "max_projects", "storage_bytes", "max_storage_bytes"}
    # One seeded project (alpha) at fixture time.
    assert body["projects"] >= 1


def test_quota_usage_non_member_403(client: TestClient) -> None:
    r = client.get("/api/tenants/acme/quota", headers=hdr("u-dave"))
    assert r.status_code == 403, r.text


def test_max_projects_blocks_create(client: TestClient, isolated_root: Path) -> None:
    """At-cap tenants get 403 quota_exceeded on session-create."""
    _set_quota(isolated_root, "acme", max_projects=2)
    # alpha already exists → 1 used. First call succeeds, second fills cap,
    # third is blocked. Use sessions endpoint (instantiates a real project dir).
    r1 = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-alice"),
        json={"name": "q_one", "format": "ppt169"},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-alice"),
        json={"name": "q_two", "format": "ppt169"},
    )
    assert r2.status_code == 403, r2.text
    detail = r2.json()["detail"]
    assert detail["error"] == "quota_exceeded"
    assert detail["reason"] == "max_projects"


def test_max_projects_blocks_from_template(client: TestClient, isolated_root: Path) -> None:
    """Quota check runs *before* template lookup, so a missing template still
    surfaces the 403 when the tenant is at cap."""
    _set_quota(isolated_root, "acme", max_projects=1)
    r = client.post(
        "/api/tenants/acme/projects/from-template",
        headers=hdr("u-alice"),
        json={"template": "no-such-template", "project_name": "fromtmpl"},
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "quota_exceeded"
    assert detail["reason"] == "max_projects"


def test_max_storage_blocks_shared_upload(client: TestClient, isolated_root: Path) -> None:
    # quotas.max_storage_bytes floors at 1 MiB, so set the cap there and
    # send a >1 MiB payload to exercise the actual guard. Use .png so the
    # extension denylist doesn't 400 us before we hit the quota check.
    _set_quota(isolated_root, "acme", max_storage_bytes=1024 * 1024)
    big = b"x" * (1024 * 1024 + 1024)
    r = client.post(
        "/api/tenants/acme/shared",
        headers=hdr("u-alice"),
        files={"file": ("big.png", big, "image/png")},
        data={"path": "big.png"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["reason"] == "max_storage_bytes"


def test_max_storage_blocks_image_gen(client: TestClient, isolated_root: Path) -> None:
    """Image-gen reserves ~8 MB headroom; a 1 MB cap blocks before we hit
    the provider (so we never need a Stripe / OpenAI key in tests)."""
    _set_quota(isolated_root, "acme", max_storage_bytes=1024 * 1024)
    r = client.post(
        "/api/tenants/acme/projects/alpha/images/generate",
        headers=hdr("u-alice"),
        json={"prompt": "a sunset", "size": "1024", "model": "gpt-image-1"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["reason"] == "max_storage_bytes"


# ─── soft delete + trash ───────────────────────────────────────────────────


def test_project_soft_delete_moves_to_trash(client: TestClient, isolated_root: Path) -> None:
    r = client.delete("/api/tenants/acme/projects/alpha", headers=hdr("u-alice"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "trashed" in body
    trash_id = body["trashed"]["id"]
    # Live dir gone, trash dir present.
    assert not (isolated_root / "tenants" / "acme" / "projects" / "alpha").exists()
    assert (isolated_root / "tenants" / "acme" / ".trash" / trash_id).exists()
    # Audit row emitted.
    events = _read_audit(isolated_root)
    trash_evt = [e for e in events if e["action"] == "project.trash"]
    assert trash_evt and trash_evt[0]["target"] == "alpha"


def test_project_soft_delete_editor_rejected(client: TestClient) -> None:
    """Editors can't drop a deck — owner role on the project required."""
    r = client.delete("/api/tenants/acme/projects/alpha", headers=hdr("u-bob"))
    assert r.status_code == 403, r.text


def test_trash_list_editor_allowed(client: TestClient) -> None:
    # Soft-delete first.
    client.delete("/api/tenants/acme/projects/alpha", headers=hdr("u-alice"))
    r = client.get("/api/tenants/acme/trash", headers=hdr("u-bob"))
    assert r.status_code == 200, r.text
    assert len(r.json()["trash"]) == 1


def test_trash_list_non_member_403(client: TestClient) -> None:
    client.delete("/api/tenants/acme/projects/alpha", headers=hdr("u-alice"))
    r = client.get("/api/tenants/acme/trash", headers=hdr("u-dave"))
    assert r.status_code == 403, r.text


def test_trash_restore_brings_project_back(client: TestClient, isolated_root: Path) -> None:
    d = client.delete("/api/tenants/acme/projects/alpha", headers=hdr("u-alice"))
    trash_id = d.json()["trashed"]["id"]
    r = client.post(
        f"/api/tenants/acme/trash/{trash_id}/restore",
        headers=hdr("u-alice"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored_as"] == "alpha"
    assert (isolated_root / "tenants" / "acme" / "projects" / "alpha").exists()
    events = _read_audit(isolated_root)
    assert any(e["action"] == "project.restore" for e in events)


def test_trash_restore_collision_409(client: TestClient, isolated_root: Path) -> None:
    """Restoring on top of a live project of the same name → 409, never
    a silent overwrite."""
    d = client.delete("/api/tenants/acme/projects/alpha", headers=hdr("u-alice"))
    trash_id = d.json()["trashed"]["id"]
    # Recreate a live project with the same name.
    live = isolated_root / "tenants" / "acme" / "projects" / "alpha"
    live.mkdir()
    (live / "marker.txt").write_text("new-life\n", encoding="utf-8")
    r = client.post(
        f"/api/tenants/acme/trash/{trash_id}/restore",
        headers=hdr("u-alice"),
    )
    assert r.status_code == 409, r.text


def test_trash_purge_removes_entry(client: TestClient, isolated_root: Path) -> None:
    d = client.delete("/api/tenants/acme/projects/alpha", headers=hdr("u-alice"))
    trash_id = d.json()["trashed"]["id"]
    r = client.delete(
        f"/api/tenants/acme/trash/{trash_id}", headers=hdr("u-alice")
    )
    assert r.status_code == 200, r.text
    # Entry gone, list empty.
    listr = client.get("/api/tenants/acme/trash", headers=hdr("u-alice"))
    assert listr.json()["trash"] == []
    events = _read_audit(isolated_root)
    assert any(e["action"] == "project.purge" for e in events)


def test_trash_purge_editor_rejected(client: TestClient) -> None:
    d = client.delete("/api/tenants/acme/projects/alpha", headers=hdr("u-alice"))
    trash_id = d.json()["trashed"]["id"]
    r = client.delete(
        f"/api/tenants/acme/trash/{trash_id}", headers=hdr("u-bob")
    )
    assert r.status_code == 403, r.text


# ─── monthly invoice cron ──────────────────────────────────────────────────


def test_monthly_invoice_503_without_stripe_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripe absent → 503, never a fabricated invoice."""
    monkeypatch.delenv("STRIPE_TEST_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    r = client.post(
        "/api/platform/billing/invoices/run",
        headers=hdr("u-alice"),
        json={"period": "202604"},
    )
    assert r.status_code == 503, r.text


def test_monthly_invoice_non_admin_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/platform/billing/invoices/run",
        headers=hdr("u-dave"),
        json={"period": "202604"},
    )
    assert r.status_code == 403, r.text


def test_monthly_invoice_idempotent_skipped_zero(
    client: TestClient, isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stripe_auto tenant with zero billed usage → ``skipped_zero``; a
    second run for the same period returns ``already_invoiced`` without a
    second invoice_runs row."""
    monkeypatch.setenv("STRIPE_TEST_SECRET_KEY", "sk_test_dummy_not_called")
    # Ensure metering's usage_events table exists before the cron queries it.
    metering_mod._connect().close()
    billing.set_payment_mode("acme", "stripe_auto")

    r1 = client.post(
        "/api/platform/billing/invoices/run",
        headers=hdr("u-alice"),
        json={"period": "202604"},
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["period"] == "202604"
    acme1 = next(x for x in body1["results"] if x["tenant"] == "acme")
    assert acme1["status"] == "skipped_zero"

    r2 = client.post(
        "/api/platform/billing/invoices/run",
        headers=hdr("u-alice"),
        json={"period": "202604"},
    )
    assert r2.status_code == 200, r2.text
    acme2 = next(x for x in r2.json()["results"] if x["tenant"] == "acme")
    assert acme2["status"] == "already_invoiced"

    listr = client.get(
        "/api/platform/billing/invoices?tenant=acme", headers=hdr("u-alice")
    )
    runs = listr.json()["runs"]
    assert len(runs) == 1, f"expected exactly one invoice_run row, got {runs!r}"
    assert runs[0]["status"] == "skipped_zero"
    assert Decimal(runs[0]["amount_usd"]) == Decimal("0")


def test_monthly_invoice_skipped_no_customer(
    client: TestClient, isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stripe_auto with non-zero usage but no stripe_customer_id → skipped_no_customer
    (we never call Stripe.InvoiceItem.create without a customer)."""
    monkeypatch.setenv("STRIPE_TEST_SECRET_KEY", "sk_test_dummy_not_called")
    # Ensure metering's usage_events table exists before we insert into it.
    metering_mod._connect().close()
    billing.set_payment_mode("acme", "stripe_auto")

    # Insert a billed usage_event in period 202604 (Apr 2026).
    period_start = int(time.mktime(time.strptime("2026-04-15", "%Y-%m-%d")))
    conn = billing._connect()
    try:
        conn.execute(
            """
            INSERT INTO usage_events (
                id, tenant, project, user_id, session_id, provider, model,
                kind, units_json, upstream_cost, billed_cost,
                pricing_version, request_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt-1", "acme", "alpha", "u-alice", None, "anthropic",
                "claude-sonnet-4-6", "chat", "{}", "0.10", "0.12",
                "phase9-test", "req-1", period_start,
            ),
        )
    finally:
        conn.close()

    r = client.post(
        "/api/platform/billing/invoices/run",
        headers=hdr("u-alice"),
        json={"period": "202604"},
    )
    assert r.status_code == 200, r.text
    acme = next(x for x in r.json()["results"] if x["tenant"] == "acme")
    assert acme["status"] == "skipped_no_customer"
    assert Decimal(acme["amount_usd"]) > 0


def test_invoices_listing_platform_admin_only(client: TestClient) -> None:
    r = client.get("/api/platform/billing/invoices", headers=hdr("u-dave"))
    assert r.status_code == 403, r.text


# ─── audit endpoint smoke ──────────────────────────────────────────────────


def test_audit_endpoint_filters_by_tenant(client: TestClient) -> None:
    client.post(
        "/api/tenants",
        headers=hdr("u-alice"),
        json={"slug": "newco2", "name": "New Co Two"},
    )
    client.post(
        "/api/tenants/acme/members",
        headers=hdr("u-alice"),
        json={"user": "dave@example.com", "role": "viewer"},
    )
    r = client.get(
        "/api/platform/audit?tenant=acme", headers=hdr("u-alice")
    )
    assert r.status_code == 200, r.text
    for evt in r.json()["events"]:
        assert evt["tenant"] == "acme"
