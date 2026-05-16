"""Tenant billing — Phase 6.

Adds the financial layer that sits on top of the Phase 5 ``UsageEvent``
ledger:

- ``credit_transactions`` rows fund a tenant's ``balance_usd`` (trial
  grants, Stripe top-ups, manual wires).
- ``tenant_billing_state`` rows track per-tenant payment mode, billing
  email, soft/hard cap overrides, and the last soft-cap notification
  watermark.
- ``check_eligibility`` walks balance + caps before a billable call.

The same SQLite database used by metering (``platform/billing.db``) is
re-used so the ledger and the credit ledger share a transaction-able
read view. The new tables are additive — metering's ``usage_events``
table is untouched.

Defaults (cap_soft=$80, cap_hard=$100, trial=$20/90d) come from each
tenant's ``config.json`` first; this module supplies fallback constants
only if the per-tenant config doesn't override them, per the multi-
tenant spec (§17.4).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

import metering
import tenants as tenants_mod


# ─── defaults (overridable per tenant in tenants/<slug>/config.json) ────────

DEFAULT_TRIAL_USD = Decimal("20.00")
DEFAULT_TRIAL_DAYS = 90
DEFAULT_CAP_SOFT_USD = Decimal("80.00")
DEFAULT_CAP_HARD_USD = Decimal("100.00")
DEFAULT_SESSION_CEILING_USD = Decimal("5.00")
SOFT_THRESHOLD_FRACTION = Decimal("0.80")  # spec §17.4: warn at 80% of soft cap


class BillingBlocked(HTTPException):
    """Raised when a request would push the tenant past its hard cap.

    Maps to HTTP 402 per §17.4. The detail body is a structured dict so
    the UI can surface a useful banner (cap, mtd, remaining).
    """

    def __init__(self, *, tenant: str, reason: str, **extra: Any) -> None:
        body = {"error": "billing_blocked", "tenant": tenant, "reason": reason}
        body.update(extra)
        super().__init__(status_code=402, detail=body)


@dataclass
class CreditTransaction:
    tenant: str
    source: str  # "trial", "stripe", "manual", "adjustment", "invoice"
    source_id: str  # idempotency key (checkout_session_id, wire reference, etc.)
    amount_usd: Decimal
    note: str = ""
    id: str = ""
    created_at: int = 0


# ─── sqlite schema (extends metering's billing.db) ─────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_transactions (
    id           TEXT PRIMARY KEY,
    tenant       TEXT NOT NULL,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    amount_usd   TEXT NOT NULL,
    note         TEXT,
    created_at   INTEGER NOT NULL,
    UNIQUE (tenant, source, source_id)
);
CREATE INDEX IF NOT EXISTS ix_credit_tenant_time
    ON credit_transactions (tenant, created_at);

CREATE TABLE IF NOT EXISTS tenant_billing_state (
    tenant                      TEXT PRIMARY KEY,
    payment_mode                TEXT NOT NULL DEFAULT 'stripe_prepaid',
    billing_email               TEXT,
    cap_soft_override_usd       TEXT,
    cap_hard_override_usd       TEXT,
    last_soft_threshold_at      INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id          TEXT,
    updated_at                  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_runs (
    tenant       TEXT NOT NULL,
    period       TEXT NOT NULL,   -- "YYYYMM" of the billed period
    invoice_id   TEXT,            -- Stripe invoice id (NULL on dry-run / no-charge)
    amount_usd   TEXT NOT NULL,
    status       TEXT NOT NULL,   -- "created", "skipped_zero", "skipped_no_customer"
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (tenant, period)
);
"""


def _connect() -> sqlite3.Connection:
    """Open the shared platform billing DB and ensure schemas exist.

    Re-using ``metering._connect`` would also ensure the usage_events
    table is created, but it's a private API; we duplicate the same WAL
    setup here so both modules can be tested in isolation.
    """
    metering.BILLING_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(metering.BILLING_DB), isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    return conn


def _quant(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.000001"))


# ─── per-tenant config helpers ─────────────────────────────────────────────


def _tenant_config(tenant: str) -> dict:
    cfg = tenants_mod.read_tenant_config(tenant) or {}
    return cfg


def _billing_cfg(tenant: str) -> dict:
    """Read the tenant's ``billing`` block from config.json.

    Returns an empty dict when unset so callers can ``.get(...)`` with
    defaults without writing the block at first read.
    """
    return _tenant_config(tenant).get("billing") or {}


def trial_grant_usd(tenant: str) -> Decimal:
    """Configured trial grant. Defaults to $20 when not set in config.json."""
    val = _billing_cfg(tenant).get("trial_grant_usd")
    if val is None:
        return DEFAULT_TRIAL_USD
    return Decimal(str(val))


def trial_days(tenant: str) -> int:
    val = _billing_cfg(tenant).get("trial_days")
    if val is None:
        return DEFAULT_TRIAL_DAYS
    try:
        return int(val)
    except Exception:
        return DEFAULT_TRIAL_DAYS


def cap_soft_usd(tenant: str) -> Decimal:
    """Soft cap with admin/owner override precedence.

    Read order:
      1. ``tenant_billing_state.cap_soft_override_usd`` (owner can raise)
      2. ``config.json.billing.cap_soft_usd``
      3. DEFAULT_CAP_SOFT_USD
    """
    override = _read_state(tenant).get("cap_soft_override_usd")
    if override is not None:
        return Decimal(str(override))
    val = _billing_cfg(tenant).get("cap_soft_usd")
    if val is None:
        return DEFAULT_CAP_SOFT_USD
    return Decimal(str(val))


def cap_hard_usd(tenant: str) -> Decimal:
    """Hard cap — platform-admin override > config.json > DEFAULT."""
    override = _read_state(tenant).get("cap_hard_override_usd")
    if override is not None:
        return Decimal(str(override))
    val = _billing_cfg(tenant).get("cap_hard_usd")
    if val is None:
        return DEFAULT_CAP_HARD_USD
    return Decimal(str(val))


def session_ceiling_usd(tenant: str) -> Decimal:
    """Per-session billing ceiling. Spec default $5."""
    val = _billing_cfg(tenant).get("session_ceiling_usd")
    if val is None:
        return DEFAULT_SESSION_CEILING_USD
    return Decimal(str(val))


def payment_mode(tenant: str) -> str:
    """`stripe_prepaid` (default), `stripe_auto`, or `manual`."""
    return _read_state(tenant).get("payment_mode") or "stripe_prepaid"


def billing_email(tenant: str) -> Optional[str]:
    state = _read_state(tenant)
    return state.get("billing_email") or _billing_cfg(tenant).get("billing_email") or None


# ─── state row helpers ─────────────────────────────────────────────────────


def _read_state(tenant: str) -> dict:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT payment_mode, billing_email, cap_soft_override_usd, "
            "cap_hard_override_usd, last_soft_threshold_at, stripe_customer_id "
            "FROM tenant_billing_state WHERE tenant = ?",
            (tenant,),
        ).fetchone()
        if not row:
            return {}
        return {
            "payment_mode": row[0],
            "billing_email": row[1],
            "cap_soft_override_usd": row[2],
            "cap_hard_override_usd": row[3],
            "last_soft_threshold_at": int(row[4] or 0),
            "stripe_customer_id": row[5],
        }
    finally:
        conn.close()


def _write_state(tenant: str, fields: dict) -> dict:
    """Upsert a partial state row, returning the full state after the write."""
    existing = _read_state(tenant) or {}
    merged = dict(existing)
    merged.update(fields)
    merged.setdefault("payment_mode", "stripe_prepaid")
    merged.setdefault("last_soft_threshold_at", 0)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO tenant_billing_state (
                tenant, payment_mode, billing_email,
                cap_soft_override_usd, cap_hard_override_usd,
                last_soft_threshold_at, stripe_customer_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant) DO UPDATE SET
                payment_mode = excluded.payment_mode,
                billing_email = excluded.billing_email,
                cap_soft_override_usd = excluded.cap_soft_override_usd,
                cap_hard_override_usd = excluded.cap_hard_override_usd,
                last_soft_threshold_at = excluded.last_soft_threshold_at,
                stripe_customer_id = excluded.stripe_customer_id,
                updated_at = excluded.updated_at
            """,
            (
                tenant,
                merged.get("payment_mode") or "stripe_prepaid",
                merged.get("billing_email"),
                merged.get("cap_soft_override_usd"),
                merged.get("cap_hard_override_usd"),
                int(merged.get("last_soft_threshold_at") or 0),
                merged.get("stripe_customer_id"),
                int(time.time()),
            ),
        )
    finally:
        conn.close()
    return merged


# ─── credit / trial ────────────────────────────────────────────────────────


def _tenant_created_at(tenant: str) -> int:
    return int(_tenant_config(tenant).get("created_at") or 0)


def _trial_expired(tenant: str) -> bool:
    created = _tenant_created_at(tenant)
    if created <= 0:
        # No tenant config / no timestamp — be safe, don't grant trial.
        return True
    expiry = created + trial_days(tenant) * 86400
    return int(time.time()) > expiry


def _ensure_trial_credit(tenant: str) -> None:
    """Idempotently insert the one-shot trial credit row for a new tenant.

    Skipped silently when the tenant doesn't exist (caller will raise),
    when the trial window already lapsed, or when the row is already there.
    """
    cfg = _tenant_config(tenant)
    if not cfg:
        return
    grant = trial_grant_usd(tenant)
    if grant <= 0:
        return
    if _trial_expired(tenant):
        return
    # UNIQUE(tenant, source, source_id) makes this safe under races.
    try:
        record_credit(
            tenant=tenant,
            source="trial",
            source_id="initial",
            amount_usd=grant,
            note=f"Trial credit ({trial_days(tenant)}d)",
        )
    except ValueError:
        # Already present.
        return


def record_credit(
    *,
    tenant: str,
    source: str,
    source_id: str,
    amount_usd: Decimal,
    note: str = "",
) -> CreditTransaction:
    """Append a credit row. Idempotent on (tenant, source, source_id).

    Raises:
        ValueError: amount_usd is non-positive, or the (source, source_id)
            pair is already on file for this tenant.
    """
    amt = Decimal(str(amount_usd))
    if amt <= 0:
        raise ValueError("amount_usd must be positive")
    src = (source or "").strip()
    sid = (source_id or "").strip()
    if not src or not sid:
        raise ValueError("source and source_id are required")
    txn = CreditTransaction(
        tenant=tenant,
        source=src,
        source_id=sid,
        amount_usd=_quant(amt),
        note=note or "",
        id=uuid.uuid4().hex,
        created_at=int(time.time()),
    )
    conn = _connect()
    try:
        try:
            conn.execute(
                """
                INSERT INTO credit_transactions (
                    id, tenant, source, source_id, amount_usd, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    txn.id,
                    txn.tenant,
                    txn.source,
                    txn.source_id,
                    str(txn.amount_usd),
                    txn.note,
                    txn.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"credit already recorded for {tenant}/{source}/{source_id}"
            ) from exc
    finally:
        conn.close()
    return txn


def list_credits(tenant: str, limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, tenant, source, source_id, amount_usd, note, created_at "
            "FROM credit_transactions WHERE tenant = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (tenant, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "tenant": r[1],
            "source": r[2],
            "source_id": r[3],
            "amount_usd": r[4],
            "note": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def total_credits_usd(tenant: str) -> Decimal:
    conn = _connect()
    try:
        total = Decimal("0")
        for (amt,) in conn.execute(
            "SELECT amount_usd FROM credit_transactions WHERE tenant = ?",
            (tenant,),
        ):
            try:
                total += Decimal(str(amt))
            except Exception:
                continue
        return _quant(total)
    finally:
        conn.close()


def total_usage_usd(tenant: str) -> Decimal:
    """All-time billed cost for a tenant (across months)."""
    conn = metering._connect()
    try:
        total = Decimal("0")
        for (billed,) in conn.execute(
            "SELECT billed_cost FROM usage_events WHERE tenant = ?",
            (tenant,),
        ):
            try:
                total += Decimal(str(billed))
            except Exception:
                continue
        return _quant(total)
    finally:
        conn.close()


def balance_usd(tenant: str) -> Decimal:
    """Total credits (including trial) minus total billed usage.

    May be negative if a tenant runs past their hard cap during an open
    session — eligibility blocks future calls but doesn't claw back the
    in-flight ones.
    """
    _ensure_trial_credit(tenant)
    return _quant(total_credits_usd(tenant) - total_usage_usd(tenant))


def trial_remaining_usd(tenant: str) -> Decimal:
    """How much of the trial grant is unspent and still in-window.

    Returns 0 after expiry regardless of unspent grant — trial does not
    roll into balance per §17.4. Active trial spend is debited by usage
    first; non-trial credits absorb everything past that.
    """
    if _trial_expired(tenant):
        return Decimal("0")
    _ensure_trial_credit(tenant)
    conn = _connect()
    try:
        total = Decimal("0")
        for (amt,) in conn.execute(
            "SELECT amount_usd FROM credit_transactions "
            "WHERE tenant = ? AND source = 'trial'",
            (tenant,),
        ):
            try:
                total += Decimal(str(amt))
            except Exception:
                continue
    finally:
        conn.close()
    used = total_usage_usd(tenant)
    remaining = total - used
    if remaining < 0:
        return Decimal("0")
    return _quant(remaining)


# ─── eligibility + soft-cap watermark ──────────────────────────────────────


def estimate_cost_usd(model: Optional[str], kind: str = "chat") -> Decimal:
    """Conservative per-turn cost estimate used by the eligibility gate.

    Spec §17.4 calls out that a flat number is too coarse, so we bucket
    by model family. Numbers err high — under-estimates eat into margin.
    """
    if kind == "image":
        return Decimal("0.10")
    m = (model or "").lower()
    if "opus" in m:
        return Decimal("0.50")
    if "sonnet" in m:
        return Decimal("0.15")
    if "haiku" in m:
        return Decimal("0.03")
    # Unknown model → assume an Opus-shaped session so we fail-safe high.
    return Decimal("0.20")


def billing_summary(tenant: str) -> dict:
    """Owner-facing snapshot for the billing header card."""
    mtd = metering.mtd_billed_usd(tenant)
    return {
        "tenant": tenant,
        "balance_usd": str(balance_usd(tenant)),
        "trial_remaining_usd": str(trial_remaining_usd(tenant)),
        "trial_grant_usd": str(trial_grant_usd(tenant)),
        "trial_expires_at": _tenant_created_at(tenant) + trial_days(tenant) * 86400,
        "trial_expired": _trial_expired(tenant),
        "mtd_billed_usd": str(mtd),
        "cap_soft_usd": str(cap_soft_usd(tenant)),
        "cap_hard_usd": str(cap_hard_usd(tenant)),
        "session_ceiling_usd": str(session_ceiling_usd(tenant)),
        "payment_mode": payment_mode(tenant),
        "billing_email": billing_email(tenant),
        "soft_threshold_crossed": mtd >= cap_soft_usd(tenant) * SOFT_THRESHOLD_FRACTION,
        "hard_cap_blocked": mtd >= cap_hard_usd(tenant),
    }


def check_eligibility(
    tenant: str, estimated_cost_usd: Optional[Decimal] = None
) -> dict:
    """Hard-cap gate. Raises ``BillingBlocked`` if the call cannot proceed.

    Returns a dict snapshot of the relevant numbers on success — useful for
    callers that want to attach them to a session-start log line.
    """
    if not tenants_mod.tenant_exists(tenant):
        # No tenant → nothing to gate; the higher routes already 404.
        return {"tenant": tenant, "allowed": True, "reason": "no-tenant"}
    est = (
        estimated_cost_usd
        if estimated_cost_usd is not None
        else estimate_cost_usd(None)
    )
    est = Decimal(str(est))
    mtd = metering.mtd_billed_usd(tenant)
    hard = cap_hard_usd(tenant)
    trial = trial_remaining_usd(tenant)
    bal = balance_usd(tenant)
    effective = trial + max(bal, Decimal("0"))
    # §17.4 math
    if mtd + est > hard:
        raise BillingBlocked(
            tenant=tenant,
            reason="hard_cap_exceeded",
            mtd_usd=str(mtd),
            cap_hard_usd=str(hard),
            estimated_cost_usd=str(est),
        )
    if est > effective:
        raise BillingBlocked(
            tenant=tenant,
            reason="insufficient_balance",
            balance_usd=str(bal),
            trial_remaining_usd=str(trial),
            estimated_cost_usd=str(est),
        )
    return {
        "tenant": tenant,
        "allowed": True,
        "mtd_usd": str(mtd),
        "balance_usd": str(bal),
        "trial_remaining_usd": str(trial),
        "estimated_cost_usd": str(est),
        "cap_soft_usd": str(cap_soft_usd(tenant)),
        "cap_hard_usd": str(hard),
    }


def maybe_emit_soft_threshold(tenant: str) -> Optional[dict]:
    """If MTD just crossed 80% of soft cap, return an event payload.

    Idempotent within a single soft-cap window: once we emit, we stamp
    ``last_soft_threshold_at`` and won't re-emit until usage drops back
    below the threshold (the watermark resets when it does).
    """
    mtd = metering.mtd_billed_usd(tenant)
    soft = cap_soft_usd(tenant)
    threshold = _quant(soft * SOFT_THRESHOLD_FRACTION)
    state = _read_state(tenant)
    last = int(state.get("last_soft_threshold_at") or 0)
    if mtd < threshold:
        if last:
            _write_state(tenant, {"last_soft_threshold_at": 0})
        return None
    if last:
        return None
    now = int(time.time())
    _write_state(tenant, {"last_soft_threshold_at": now})
    return {
        "type": "billing.threshold",
        "tenant": tenant,
        "mtd_usd": str(mtd),
        "cap_soft_usd": str(soft),
        "threshold_usd": str(threshold),
        "billing_email": billing_email(tenant),
        "emitted_at": now,
    }


def session_cost_usd(session_id: str) -> Decimal:
    """Sum billed_cost for one session — drives the chat footer counter."""
    if not session_id:
        return Decimal("0")
    conn = metering._connect()
    try:
        total = Decimal("0")
        for (billed,) in conn.execute(
            "SELECT billed_cost FROM usage_events WHERE session_id = ?",
            (session_id,),
        ):
            try:
                total += Decimal(str(billed))
            except Exception:
                continue
        return _quant(total)
    finally:
        conn.close()


# ─── caps mutation ─────────────────────────────────────────────────────────


def set_soft_cap(tenant: str, amount_usd: Decimal) -> Decimal:
    """Owner-callable: raise the soft cap. Lowering below current MTD is fine.

    Owners may not exceed their hard cap — pushing soft past hard is a
    platform-admin action because raising the hard cap is too.
    """
    amt = Decimal(str(amount_usd))
    if amt <= 0:
        raise ValueError("soft cap must be positive")
    if amt > cap_hard_usd(tenant):
        raise ValueError(
            "soft cap cannot exceed hard cap; ask a platform admin to raise the hard cap first"
        )
    _write_state(tenant, {"cap_soft_override_usd": str(_quant(amt))})
    return amt


def set_hard_cap(tenant: str, amount_usd: Decimal) -> Decimal:
    """Platform-admin-callable: raise (or lower) the hard cap."""
    amt = Decimal(str(amount_usd))
    if amt <= 0:
        raise ValueError("hard cap must be positive")
    _write_state(tenant, {"cap_hard_override_usd": str(_quant(amt))})
    return amt


def set_payment_mode(tenant: str, mode: str) -> str:
    """Owner-callable: switch between stripe_prepaid / stripe_auto / manual."""
    m = (mode or "").strip().lower()
    if m not in {"stripe_prepaid", "stripe_auto", "manual"}:
        raise ValueError(f"unknown payment_mode {mode!r}")
    _write_state(tenant, {"payment_mode": m})
    return m


def set_billing_email(tenant: str, email: str) -> str:
    e = (email or "").strip()
    _write_state(tenant, {"billing_email": e or None})
    return e


# ─── Stripe glue ───────────────────────────────────────────────────────────
# We don't import stripe at module load — the package is optional in the
# dev environment, and we want the rest of billing.py usable without it.


def _stripe_secret_key() -> Optional[str]:
    """Prefer ``STRIPE_TEST_SECRET_KEY`` in dev; fall back to ``STRIPE_SECRET_KEY``."""
    return (
        os.environ.get("STRIPE_TEST_SECRET_KEY")
        or os.environ.get("STRIPE_SECRET_KEY")
        or None
    )


def stripe_configured() -> bool:
    return bool(_stripe_secret_key())


def _require_stripe():
    key = _stripe_secret_key()
    if not key:
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured on this platform "
            "(set STRIPE_TEST_SECRET_KEY)",
        )
    try:
        import stripe  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"stripe SDK is not installed: {exc}",
        ) from exc
    stripe.api_key = key
    return stripe


def create_checkout_session(
    *, tenant: str, amount_usd: Decimal, success_url: str, cancel_url: str
) -> str:
    """Build a Stripe Checkout session URL for a prepaid top-up.

    Returns the redirect URL the frontend opens in a new tab. The
    webhook ``checkout.session.completed`` lands the matching credit row.
    """
    if not tenants_mod.tenant_exists(tenant):
        raise HTTPException(status_code=404, detail="tenant not found")
    amt = Decimal(str(amount_usd))
    if amt <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    stripe = _require_stripe()
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": int(amt * Decimal("100")),
                    "product_data": {
                        "name": f"Awesome Deck credits — {tenant}",
                    },
                },
                "quantity": 1,
            }
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "tenant": tenant,
            "amount_usd": str(_quant(amt)),
            "purpose": "credit_topup",
        },
    )
    return session.url


def create_portal_session(*, tenant: str, return_url: str) -> str:
    """Stripe Customer Portal session — owner manages saved cards."""
    state = _read_state(tenant)
    customer_id = state.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(
            status_code=400,
            detail="tenant has no Stripe customer yet — make a top-up first",
        )
    stripe = _require_stripe()
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return session.url


def handle_stripe_webhook(*, payload: bytes, signature: Optional[str]) -> dict:
    """Verify + dispatch a Stripe webhook event.

    Currently handles ``checkout.session.completed`` → credit the tenant's
    balance with ``metadata.amount_usd`` and stash the customer id for the
    portal route.
    """
    stripe = _require_stripe()
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="STRIPE_WEBHOOK_SECRET is not configured",
        )
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"signature invalid: {exc}") from exc
    etype = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    data = (
        event.get("data", {}).get("object", {})
        if isinstance(event, dict)
        else getattr(event.data, "object", {})
    )
    if etype == "checkout.session.completed":
        meta = (data.get("metadata") or {}) if isinstance(data, dict) else {}
        tenant = meta.get("tenant")
        amount = meta.get("amount_usd")
        if not tenant or not amount:
            return {"ignored": True, "reason": "missing metadata"}
        customer = data.get("customer") if isinstance(data, dict) else None
        if customer:
            _write_state(tenant, {"stripe_customer_id": customer})
        try:
            record_credit(
                tenant=tenant,
                source="stripe",
                source_id=str(data.get("id") or event.get("id")),
                amount_usd=Decimal(str(amount)),
                note=f"Stripe Checkout — {data.get('id')}",
            )
        except ValueError:
            return {"ignored": True, "reason": "already credited"}
        return {"credited": True, "tenant": tenant, "amount_usd": str(amount)}
    if etype == "invoice.payment_succeeded":
        # Phase 9 — monthly auto-invoice settlement. Idempotent on the
        # Stripe invoice id; the cron writes invoice_runs but credit only
        # lands here when the customer actually pays.
        meta = (data.get("metadata") or {}) if isinstance(data, dict) else {}
        tenant = meta.get("tenant")
        period = meta.get("period")
        invoice_id = data.get("id") if isinstance(data, dict) else None
        amount_paid_cents = data.get("amount_paid") if isinstance(data, dict) else None
        if not tenant or not invoice_id or amount_paid_cents in (None, 0):
            return {"ignored": True, "reason": "missing metadata or zero pay"}
        amount = (Decimal(int(amount_paid_cents)) / Decimal(100)).quantize(Decimal("0.01"))
        try:
            record_credit(
                tenant=tenant,
                source="invoice",
                source_id=str(invoice_id),
                amount_usd=amount,
                note=f"Monthly auto-invoice for {period or 'unknown'}",
            )
        except ValueError:
            return {"ignored": True, "reason": "already credited"}
        return {"invoice_paid": True, "tenant": tenant, "amount_usd": str(amount)}
    return {"ignored": True, "reason": f"unhandled event {etype}"}


# ─── Monthly invoice cron (Phase 9) ────────────────────────────────────────
# Sums each ``payment_mode="stripe_auto"`` tenant's billed_cost_usd for the
# requested calendar month and creates a Stripe invoice. Idempotent on
# ``(tenant, period)`` via the ``invoice_runs`` table — a second cron run
# for the same period is a no-op. When Stripe is unconfigured the route
# raises 503 (no mock invoice ever lands in the ledger).


def _previous_period_yyyymm(now: Optional[_dt.datetime] = None) -> str:
    """``YYYYMM`` for the calendar month immediately before ``now``."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    first_of_this_month = _dt.datetime(now.year, now.month, 1, tzinfo=_dt.timezone.utc)
    last_of_prev = first_of_this_month - _dt.timedelta(days=1)
    return f"{last_of_prev.year:04d}{last_of_prev.month:02d}"


def _period_bounds(period_yyyymm: str) -> tuple[int, int]:
    """Return ``(start_ts, end_ts_exclusive)`` epoch seconds for a YYYYMM string."""
    if len(period_yyyymm) != 6 or not period_yyyymm.isdigit():
        raise ValueError(f"period must be YYYYMM, got {period_yyyymm!r}")
    year = int(period_yyyymm[:4])
    month = int(period_yyyymm[4:6])
    if not (1 <= month <= 12):
        raise ValueError(f"invalid month in period {period_yyyymm!r}")
    start = _dt.datetime(year, month, 1, tzinfo=_dt.timezone.utc)
    if month == 12:
        end = _dt.datetime(year + 1, 1, 1, tzinfo=_dt.timezone.utc)
    else:
        end = _dt.datetime(year, month + 1, 1, tzinfo=_dt.timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def _sum_billed_for_period(tenant: str, period_yyyymm: str) -> Decimal:
    start, end = _period_bounds(period_yyyymm)
    conn = _connect()
    try:
        total = Decimal("0")
        for (billed,) in conn.execute(
            "SELECT billed_cost FROM usage_events "
            "WHERE tenant = ? AND created_at >= ? AND created_at < ?",
            (tenant, start, end),
        ):
            try:
                total += Decimal(str(billed))
            except Exception:
                continue
        return _quant(total)
    finally:
        conn.close()


def _stripe_auto_tenants() -> list[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT tenant FROM tenant_billing_state WHERE payment_mode = 'stripe_auto'"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _record_invoice_run(
    *, tenant: str, period: str, invoice_id: Optional[str], amount_usd: Decimal, status: str
) -> bool:
    """Insert an invoice_runs row. Returns False when the row already exists."""
    conn = _connect()
    try:
        try:
            conn.execute(
                "INSERT INTO invoice_runs (tenant, period, invoice_id, amount_usd, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tenant,
                    period,
                    invoice_id,
                    str(_quant(amount_usd)),
                    status,
                    int(time.time()),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False
    finally:
        conn.close()


def list_invoice_runs(*, tenant: Optional[str] = None, limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        if tenant:
            rows = conn.execute(
                "SELECT tenant, period, invoice_id, amount_usd, status, created_at "
                "FROM invoice_runs WHERE tenant = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT tenant, period, invoice_id, amount_usd, status, created_at "
                "FROM invoice_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [
        {
            "tenant": r[0],
            "period": r[1],
            "invoice_id": r[2],
            "amount_usd": r[3],
            "status": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]


def _create_stripe_invoice(
    *, tenant: str, period: str, amount_usd: Decimal, customer_id: str
) -> str:
    """Create + finalize a Stripe invoice for the given amount. Returns the id."""
    stripe = _require_stripe()
    cents = int((amount_usd * Decimal(100)).quantize(Decimal("1")))
    stripe.InvoiceItem.create(
        customer=customer_id,
        amount=cents,
        currency="usd",
        description=f"Awesome Deck usage — {tenant} — {period}",
        metadata={"tenant": tenant, "period": period},
    )
    invoice = stripe.Invoice.create(
        customer=customer_id,
        collection_method="charge_automatically",
        auto_advance=True,
        metadata={"tenant": tenant, "period": period},
    )
    finalized = stripe.Invoice.finalize_invoice(invoice.id)
    return finalized.id if hasattr(finalized, "id") else invoice.id


def run_monthly_invoices(period_yyyymm: Optional[str] = None) -> dict:
    """Iterate every ``stripe_auto`` tenant and invoice the requested period.

    ``period_yyyymm`` defaults to the previous calendar month. Returns a
    summary with the per-tenant outcome. Raises 503 if Stripe isn't
    configured — no mock invoices ever land in the ledger.
    """
    if not stripe_configured():
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured — cannot run monthly invoices",
        )
    period = period_yyyymm or _previous_period_yyyymm()
    _period_bounds(period)  # validate shape early
    results: list[dict] = []
    for tenant in _stripe_auto_tenants():
        amount = _sum_billed_for_period(tenant, period)
        # Idempotency: if invoice_runs already has (tenant, period), skip.
        existing = list_invoice_runs(tenant=tenant)
        if any(r["period"] == period for r in existing):
            results.append({"tenant": tenant, "status": "already_invoiced", "amount_usd": str(amount)})
            continue
        if amount <= 0:
            _record_invoice_run(
                tenant=tenant,
                period=period,
                invoice_id=None,
                amount_usd=Decimal("0"),
                status="skipped_zero",
            )
            results.append({"tenant": tenant, "status": "skipped_zero", "amount_usd": "0"})
            continue
        customer_id = _read_state(tenant).get("stripe_customer_id")
        if not customer_id:
            _record_invoice_run(
                tenant=tenant,
                period=period,
                invoice_id=None,
                amount_usd=amount,
                status="skipped_no_customer",
            )
            results.append({
                "tenant": tenant,
                "status": "skipped_no_customer",
                "amount_usd": str(amount),
            })
            continue
        try:
            invoice_id = _create_stripe_invoice(
                tenant=tenant, period=period, amount_usd=amount, customer_id=customer_id
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            results.append({"tenant": tenant, "status": "error", "error": str(exc)})
            continue
        _record_invoice_run(
            tenant=tenant,
            period=period,
            invoice_id=invoice_id,
            amount_usd=amount,
            status="created",
        )
        results.append({
            "tenant": tenant,
            "status": "created",
            "invoice_id": invoice_id,
            "amount_usd": str(amount),
        })
    return {"period": period, "results": results}
