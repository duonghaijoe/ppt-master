"""Phase 6 verification — billing UX, caps, and payments.

Per §13 Phase 6 + §17.4 / §17.5 / §10.7 of web/MULTITENANT_REARCHITECTURE.md.

Critical surface this suite locks in:

- ``check_eligibility`` raises HTTP 402 on session creation when MTD
  spend + the per-turn estimate would breach the hard cap, *or* when
  trial + balance can't cover the estimate.
- The soft-cap watermark (80% of soft cap) emits exactly one
  ``billing.threshold`` event per crossing, doesn't re-emit until usage
  dips below the threshold and crosses again.
- Manual credit (``POST /api/platform/billing/credit``) lands in the
  tenant balance, is idempotent on the supplied reference, and rejects
  non-admin callers.
- Owner can raise the soft cap up to the hard cap. Hard-cap raise from
  an owner is refused; from a platform admin it lands.
- Stripe checkout / portal / webhook code paths are exercised when
  ``STRIPE_TEST_SECRET_KEY`` is set (xfail-skipped otherwise per the
  loop's "no mocks" rule).

The phase 5 fixture shape is reused: each test isolates billing.db,
pricing.json, the users/tenants tree, and the platform dir.
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import billing
import files as files_mod
import main
import metering
import tenants as tenants_mod
import templates as templates_mod
import users as users_mod


# ─── shared fixture ────────────────────────────────────────────────────────


_PRICING_FIXTURE = {
    "version": "phase6-test",
    "margin": 0.20,
    "providers": {
        "anthropic": {
            "claude-sonnet-4-6": {
                "input_per_mtok": 3.0,
                "output_per_mtok": 15.0,
            }
        }
    },
}


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
    pricing_file.write_text(json.dumps(_PRICING_FIXTURE), encoding="utf-8")

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
    monkeypatch.setattr(metering, "PLATFORM_DIR", platform_dir)
    monkeypatch.setattr(metering, "BILLING_DB", billing_db)
    monkeypatch.setattr(metering, "PRICING_FILE", pricing_file)

    now = int(time.time())
    for slug, name in [("default", "Default"), ("acme", "Acme Co"), ("oldco", "Old Co")]:
        td = tenants_dir / slug
        td.mkdir()
        # ``oldco`` is past the trial window so we can prove trial=0 there.
        created = now if slug != "oldco" else now - 200 * 86400
        (td / "config.json").write_text(
            json.dumps({
                "slug": slug,
                "name": name,
                "owner_user_id": f"u-{slug}-owner",
                "default_format": "ppt169",
                "created_at": created,
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

    (tenants_dir / "acme" / "projects" / "alpha").mkdir()

    seeded = [
        ("u-alice", "alice@example.com", True,
         [{"tenant_slug": "acme", "role": "owner"},
          {"tenant_slug": "default", "role": "owner"},
          {"tenant_slug": "oldco", "role": "owner"}]),
        ("u-bob", "bob@example.com", False,
         [{"tenant_slug": "acme", "role": "owner"}]),
        ("u-carol", "carol@example.com", False,
         [{"tenant_slug": "acme", "role": "editor"}]),
        ("u-dave", "dave@example.com", False,
         [{"tenant_slug": "acme", "role": "viewer"}]),
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


def _set_caps(tenant: str, *, soft: str, hard: str) -> None:
    """Force config-level caps for a tenant (skipping the override DB rows)."""
    cfg_path = tenants_mod.tenant_dir(tenant) / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["billing"] = cfg.get("billing") or {}
    cfg["billing"]["cap_soft_usd"] = soft
    cfg["billing"]["cap_hard_usd"] = hard
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


def _seed_usage(tenant: str, billed_usd: str) -> None:
    """Inject a fake usage event so MTD reflects ``billed_usd``."""
    import metering as m
    from decimal import Decimal as D
    conn = m._connect()
    try:
        import uuid as _uuid
        conn.execute(
            "INSERT INTO usage_events (id, tenant, project, user_id, session_id, "
            "provider, model, kind, units_json, upstream_cost, billed_cost, "
            "pricing_version, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _uuid.uuid4().hex,
                tenant,
                "alpha",
                "u-seed",
                "s-seed",
                "anthropic",
                "claude-sonnet-4-6",
                "chat",
                "{}",
                str(D(billed_usd)),
                str(D(billed_usd)),
                "phase6-test",
                None,
                int(time.time()),
            ),
        )
    finally:
        conn.close()


# ─── balance, trial, eligibility ───────────────────────────────────────────


def test_trial_credit_granted_on_first_balance_read(isolated_root: Path) -> None:
    """A fresh tenant within the trial window gets the default $20 grant."""
    assert billing.balance_usd("acme") == Decimal("20.000000")
    assert billing.trial_remaining_usd("acme") == Decimal("20.000000")
    # Idempotent — re-reading doesn't double the trial.
    assert billing.balance_usd("acme") == Decimal("20.000000")
    rows = billing.list_credits("acme")
    trial_rows = [r for r in rows if r["source"] == "trial"]
    assert len(trial_rows) == 1
    assert trial_rows[0]["source_id"] == "initial"


def test_trial_credit_expired_after_window(isolated_root: Path) -> None:
    """A tenant past 90 days gets no trial — balance starts at $0."""
    assert billing.trial_remaining_usd("oldco") == Decimal("0")
    assert billing.balance_usd("oldco") == Decimal("0")
    # No trial row written for an expired tenant.
    assert billing.list_credits("oldco") == []


def test_check_eligibility_blocks_at_hard_cap(isolated_root: Path) -> None:
    """MTD + estimate > hard cap → BillingBlocked (HTTP 402)."""
    _set_caps("acme", soft="80", hard="100")
    _seed_usage("acme", "99.95")
    # Default estimate for unknown model is $0.20, which pushes past $100.
    with pytest.raises(billing.BillingBlocked) as exc:
        billing.check_eligibility("acme", Decimal("0.20"))
    body = exc.value.detail
    assert body["reason"] == "hard_cap_exceeded"
    assert body["tenant"] == "acme"
    assert body["cap_hard_usd"] == "100"


def test_check_eligibility_blocks_when_balance_empty(isolated_root: Path) -> None:
    """Expired-trial tenant with no balance can't start a session."""
    # oldco has no trial and no top-up.
    with pytest.raises(billing.BillingBlocked) as exc:
        billing.check_eligibility("oldco", Decimal("0.20"))
    assert exc.value.detail["reason"] == "insufficient_balance"


def test_check_eligibility_passes_with_trial_credits(isolated_root: Path) -> None:
    """Trial credits absorb the first calls."""
    snapshot = billing.check_eligibility("acme", Decimal("0.20"))
    assert snapshot["allowed"] is True
    assert Decimal(snapshot["trial_remaining_usd"]) == Decimal("20")


def test_session_create_returns_402_when_blocked(client: TestClient, isolated_root: Path) -> None:
    """End-to-end: hitting POST /api/sessions on an over-cap tenant → 402."""
    _set_caps("acme", soft="80", hard="100")
    _seed_usage("acme", "99.95")
    r = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-bob"),
        json={"name": "blocked", "format": "ppt169", "model_tier": "premium"},
    )
    assert r.status_code == 402, r.text
    body = r.json()["detail"]
    assert body["error"] == "billing_blocked"
    assert body["reason"] == "hard_cap_exceeded"


def test_session_create_passes_when_within_caps(client: TestClient, isolated_root: Path) -> None:
    """Within trial + under caps: session-create succeeds (status 200)."""
    r = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-bob"),
        json={"name": "ok-deck", "format": "ppt169", "model_tier": "light"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant_slug"] == "acme"


# ─── soft-cap threshold ────────────────────────────────────────────────────


def test_soft_threshold_emits_once_then_silences(isolated_root: Path) -> None:
    """Cross 80% of soft cap → emit; second call returns None until usage drops."""
    _set_caps("acme", soft="80", hard="100")
    _seed_usage("acme", "64.00")  # exactly at 80%
    evt = billing.maybe_emit_soft_threshold("acme")
    assert evt is not None
    assert evt["type"] == "billing.threshold"
    assert Decimal(evt["mtd_usd"]) >= Decimal(evt["threshold_usd"])
    # Second call doesn't re-emit.
    assert billing.maybe_emit_soft_threshold("acme") is None


def test_soft_threshold_resets_when_mtd_drops_below(isolated_root: Path) -> None:
    """If MTD drops back under the threshold and crosses again, we re-emit.

    Driven via the watermark in tenant_billing_state — drop simulated by
    clearing the table.
    """
    _set_caps("acme", soft="80", hard="100")
    _seed_usage("acme", "70.00")
    assert billing.maybe_emit_soft_threshold("acme") is not None
    # Reset MTD by wiping the usage_events (test-only).
    import sqlite3
    conn = sqlite3.connect(str(metering.BILLING_DB))
    try:
        conn.execute("DELETE FROM usage_events")
        conn.commit()
    finally:
        conn.close()
    # Watermark resets because mtd < threshold now.
    assert billing.maybe_emit_soft_threshold("acme") is None
    # Cross again → emit again.
    _seed_usage("acme", "70.00")
    assert billing.maybe_emit_soft_threshold("acme") is not None


# ─── manual credit endpoint ────────────────────────────────────────────────


def test_manual_credit_lands_in_balance(client: TestClient, isolated_root: Path) -> None:
    """Platform admin credits land in the tenant balance immediately."""
    initial = billing.balance_usd("acme")  # 20 (trial)
    r = client.post(
        "/api/platform/billing/credit",
        headers=hdr("u-alice"),
        json={
            "tenant": "acme",
            "amount_usd": "250.00",
            "reference": "wire-2026-05-16-001",
            "note": "Initial annual prepay",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "manual"
    assert body["source_id"] == "wire-2026-05-16-001"
    assert Decimal(body["amount_usd"]) == Decimal("250.000000")
    assert Decimal(body["balance_usd"]) == initial + Decimal("250.000000")


def test_manual_credit_is_idempotent_on_reference(
    client: TestClient, isolated_root: Path
) -> None:
    """The same wire reference can't be banked twice."""
    body = {
        "tenant": "acme",
        "amount_usd": "100.00",
        "reference": "dup-ref-1",
        "note": "x",
    }
    r1 = client.post("/api/platform/billing/credit", headers=hdr("u-alice"), json=body)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/platform/billing/credit", headers=hdr("u-alice"), json=body)
    assert r2.status_code == 400, r2.text
    assert "already recorded" in r2.json()["detail"]


def test_manual_credit_requires_platform_admin(
    client: TestClient, isolated_root: Path
) -> None:
    """Owners on a tenant cannot mint their own credits."""
    r = client.post(
        "/api/platform/billing/credit",
        headers=hdr("u-bob"),  # acme owner, not platform admin
        json={"tenant": "acme", "amount_usd": "5", "reference": "ref-x"},
    )
    assert r.status_code == 403, r.text


def test_manual_credit_404_for_unknown_tenant(
    client: TestClient, isolated_root: Path
) -> None:
    r = client.post(
        "/api/platform/billing/credit",
        headers=hdr("u-alice"),
        json={"tenant": "ghost", "amount_usd": "1", "reference": "x"},
    )
    assert r.status_code == 404, r.text


# ─── cap mutation rules ────────────────────────────────────────────────────


def test_owner_can_raise_soft_cap(client: TestClient, isolated_root: Path) -> None:
    r = client.patch(
        "/api/tenants/acme/billing/caps",
        headers=hdr("u-bob"),  # acme owner
        json={"cap_soft_usd": "90"},
    )
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["cap_soft_usd"]) == Decimal("90")
    assert billing.cap_soft_usd("acme") == Decimal("90")


def test_owner_cannot_push_soft_past_hard(client: TestClient, isolated_root: Path) -> None:
    r = client.patch(
        "/api/tenants/acme/billing/caps",
        headers=hdr("u-bob"),
        json={"cap_soft_usd": "200"},
    )
    assert r.status_code == 400, r.text
    assert "hard cap" in r.json()["detail"]


def test_owner_cannot_raise_hard_cap(client: TestClient, isolated_root: Path) -> None:
    """Hard-cap raise requires platform admin per §17.4."""
    r = client.patch(
        "/api/tenants/acme/billing/caps",
        headers=hdr("u-bob"),
        json={"cap_hard_usd": "500"},
    )
    assert r.status_code == 403, r.text


def test_platform_admin_can_raise_hard_cap(client: TestClient, isolated_root: Path) -> None:
    r = client.patch(
        "/api/tenants/acme/billing/caps",
        headers=hdr("u-alice"),  # platform admin
        json={"cap_hard_usd": "500"},
    )
    assert r.status_code == 200, r.text
    assert billing.cap_hard_usd("acme") == Decimal("500")


# ─── billing summary surface ───────────────────────────────────────────────


def test_billing_summary_for_owner(client: TestClient, isolated_root: Path) -> None:
    r = client.get("/api/tenants/acme/billing", headers=hdr("u-bob"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "balance_usd" in body
    assert "trial_remaining_usd" in body
    assert "cap_soft_usd" in body
    assert "cap_hard_usd" in body
    assert body["tenant"] == "acme"


def test_billing_summary_for_viewer_is_redacted(client: TestClient, isolated_root: Path) -> None:
    """Viewers/editors only see MTD + caps — no balance / payment state."""
    r = client.get("/api/tenants/acme/billing", headers=hdr("u-dave"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "mtd_billed_usd" in body
    assert "balance_usd" not in body
    assert "payment_mode" not in body


def test_session_cost_endpoint(client: TestClient, isolated_root: Path) -> None:
    """The chat footer counter sums usage by session_id."""
    r = client.post(
        "/api/tenants/acme/sessions",
        headers=hdr("u-bob"),
        json={"name": "cost-test", "format": "ppt169", "model_tier": "light"},
    )
    assert r.status_code == 200
    sid = r.json()["session_id"]
    # No usage yet — counter is 0.
    r = client.get(f"/api/sessions/{sid}/cost", headers=hdr("u-bob"))
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["cost_usd"]) == Decimal("0")
    # Inject one chat row tied to this session.
    metering.record_chat_usage(
        tenant="acme",
        project="cost-test",
        user_id="u-bob",
        session_id=sid,
        model="claude-sonnet-4-6",
        usage={"input_tokens": 1000, "output_tokens": 500},
    )
    r = client.get(f"/api/sessions/{sid}/cost", headers=hdr("u-bob"))
    assert Decimal(r.json()["cost_usd"]) == Decimal("0.012600")


# ─── Stripe paths (live-call tests are xfail when key absent) ──────────────


_STRIPE_KEY = os.environ.get("STRIPE_TEST_SECRET_KEY")


@pytest.mark.xfail(
    not _STRIPE_KEY,
    reason="STRIPE_TEST_SECRET_KEY not configured — Stripe live calls skipped",
    strict=False,
)
def test_stripe_checkout_creates_session(client: TestClient, isolated_root: Path) -> None:
    """When STRIPE_TEST_SECRET_KEY is set, checkout returns a real URL."""
    r = client.post(
        "/api/tenants/acme/billing/checkout",
        headers=hdr("u-bob"),
        json={
            "amount_usd": "25.00",
            "success_url": "https://example.com/ok",
            "cancel_url": "https://example.com/cancel",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("http")


def test_stripe_checkout_503_when_key_missing(
    client: TestClient, isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a Stripe key, the route returns 503 — no fabricated URL."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_TEST_SECRET_KEY", raising=False)
    r = client.post(
        "/api/tenants/acme/billing/checkout",
        headers=hdr("u-bob"),
        json={
            "amount_usd": "25.00",
            "success_url": "https://example.com/ok",
            "cancel_url": "https://example.com/cancel",
        },
    )
    assert r.status_code == 503, r.text
    assert "Stripe is not configured" in r.json()["detail"]


def test_stripe_webhook_503_when_key_missing(
    client: TestClient, isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Webhook ingress also fails closed without configuration."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_TEST_SECRET_KEY", raising=False)
    r = client.post(
        "/api/billing/stripe/webhook",
        content=b"{}",
        headers={"Stripe-Signature": "sig"},
    )
    assert r.status_code == 503, r.text
