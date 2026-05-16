"""Cost-aware per-tenant rate limiting — Phase 9.

Two parallel token buckets per tenant:

- **req_per_min** — absolute request-count ceiling (defends against bursty
  clients regardless of cost).
- **cost_per_min_usd** — dollars of billed_cost_usd debited per minute.
  Cheap requests (chat) drain it slowly; image gen drains it fast.

Both buckets refill at their configured rate. ``check_and_consume`` is the
hot path: it returns the bucket status; if any bucket is empty, raise 429.

In-process state. Process restarts wipe buckets — acceptable for v1, since
caps + check_eligibility() still gate true overspend.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException

import tenants as tenants_mod
import audit


DEFAULT_REQ_PER_MIN = 120
DEFAULT_COST_PER_MIN_USD = Decimal("1.00")


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.tokens == 0.0:
            self.tokens = float(self.capacity)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now

    def try_consume(self, amount: float) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        self._refill()
        if self.tokens + 1e-9 >= amount:
            self.tokens -= amount
            return True, 0.0
        deficit = amount - self.tokens
        wait = deficit / self.refill_per_sec if self.refill_per_sec > 0 else 60.0
        return False, max(wait, 0.001)


_buckets_lock = threading.Lock()
_req_buckets: dict[str, _Bucket] = {}
_cost_buckets: dict[str, _Bucket] = {}
# Bucket config snapshot — invalidates when the tenant config changes.
_config_snapshot: dict[str, tuple[int, float]] = {}


def _read_config(tenant: str) -> tuple[int, Decimal]:
    cfg = tenants_mod.read_tenant_config(tenant) or {}
    rl = cfg.get("rate_limit") or {}
    req = rl.get("req_per_min")
    cost = rl.get("cost_per_min_usd")
    try:
        req_int = int(req) if req is not None else DEFAULT_REQ_PER_MIN
    except Exception:
        req_int = DEFAULT_REQ_PER_MIN
    try:
        cost_dec = Decimal(str(cost)) if cost is not None else DEFAULT_COST_PER_MIN_USD
    except Exception:
        cost_dec = DEFAULT_COST_PER_MIN_USD
    if req_int < 1:
        req_int = 1
    if cost_dec <= 0:
        cost_dec = Decimal("0.01")
    return req_int, cost_dec


def _get_buckets(tenant: str) -> tuple[_Bucket, _Bucket]:
    req_per_min, cost_per_min = _read_config(tenant)
    key = (req_per_min, float(cost_per_min))
    with _buckets_lock:
        snap = _config_snapshot.get(tenant)
        if snap != key:
            # Rebuild on config change so admin edits take effect on next call.
            _req_buckets[tenant] = _Bucket(
                capacity=float(req_per_min),
                refill_per_sec=req_per_min / 60.0,
            )
            _cost_buckets[tenant] = _Bucket(
                capacity=float(cost_per_min),
                refill_per_sec=float(cost_per_min) / 60.0,
            )
            _config_snapshot[tenant] = key
        return _req_buckets[tenant], _cost_buckets[tenant]


def reset(tenant: Optional[str] = None) -> None:
    """Drop in-memory bucket state. Used by tests; safe in prod."""
    with _buckets_lock:
        if tenant is None:
            _req_buckets.clear()
            _cost_buckets.clear()
            _config_snapshot.clear()
        else:
            _req_buckets.pop(tenant, None)
            _cost_buckets.pop(tenant, None)
            _config_snapshot.pop(tenant, None)


def check_and_consume(
    tenant: str,
    *,
    estimated_cost_usd: Decimal = Decimal("0"),
    actor_id: str = "",
) -> dict:
    """Charge 1 request + ``estimated_cost_usd`` cost units against tenant buckets.

    Returns a status dict on success. Raises 429 (with ``Retry-After``-friendly
    detail) on bucket exhaustion. Always emits an audit row when blocked so
    operators can see throttling without scraping logs.
    """
    req_bucket, cost_bucket = _get_buckets(tenant)
    cost_amount = float(estimated_cost_usd or 0)
    allowed_req, retry_req = req_bucket.try_consume(1.0)
    allowed_cost = True
    retry_cost = 0.0
    if allowed_req:
        allowed_cost, retry_cost = cost_bucket.try_consume(cost_amount) if cost_amount > 0 else (True, 0.0)
        if not allowed_cost:
            # Refund the request we just took so the next retry sees the cost
            # bucket as the bottleneck, not a doubly-debited request bucket.
            req_bucket.tokens = min(req_bucket.capacity, req_bucket.tokens + 1.0)
    retry = max(retry_req, retry_cost)
    if not allowed_req or not allowed_cost:
        reason = "req_per_min" if not allowed_req else "cost_per_min_usd"
        audit.record(
            "rate_limit.block",
            actor_id=actor_id,
            tenant=tenant,
            target=None,
            reason=reason,
            retry_after_seconds=round(retry, 3),
            estimated_cost_usd=str(estimated_cost_usd),
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "reason": reason,
                "retry_after_seconds": round(retry, 3),
            },
            headers={"Retry-After": str(max(int(retry), 1))},
        )
    return {
        "tenant": tenant,
        "req_tokens_remaining": round(req_bucket.tokens, 3),
        "cost_tokens_remaining_usd": round(cost_bucket.tokens, 6),
    }


def status(tenant: str) -> dict:
    req_bucket, cost_bucket = _get_buckets(tenant)
    return {
        "tenant": tenant,
        "req_per_min": int(req_bucket.capacity),
        "cost_per_min_usd": round(cost_bucket.capacity, 6),
        "req_tokens_remaining": round(req_bucket.tokens, 3),
        "cost_tokens_remaining_usd": round(cost_bucket.tokens, 6),
    }
