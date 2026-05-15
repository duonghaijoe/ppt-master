"""Usage event ledger — Phase 5.

Every billable provider call funnels through one of two capture points
that both end up appending a row to ``platform/billing.db``:

- ``record_chat_usage(session, model, usage)`` — called by ``agent.py``
  when a Claude SDK event surfaces a ``usage`` block. ``usage`` is the
  Anthropic-shaped record (``input_tokens``, ``output_tokens``,
  ``cache_creation_input_tokens``, ``cache_read_input_tokens``).
- ``record_provider_call(session, provider, model, kind, units, request_id)``
  — called by ``providers/*`` wrappers (today: OpenAI image gen) after
  the upstream call returns. ``units`` is provider-specific.

The pricing table lives at ``platform/pricing.json``. Each event records
the ``pricing_version`` it priced against, so historical bills stay
stable when prices change.

The ledger is intentionally a thin SQLite schema. We can swap in Postgres
when scale demands it without changing the call sites.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from files import REPO_ROOT

# Resolved lazily so tests can monkeypatch via ``metering.BILLING_DB``.
PLATFORM_DIR = REPO_ROOT / "platform"
BILLING_DB = PLATFORM_DIR / "billing.db"
PRICING_FILE = PLATFORM_DIR / "pricing.json"


@dataclass
class UsageEvent:
    tenant: str
    project: str
    user_id: str
    session_id: Optional[str]
    provider: str
    model: str
    kind: str  # "chat", "image", ...
    units: dict[str, Any]
    upstream_cost_usd: Decimal
    billed_cost_usd: Decimal
    pricing_version: str
    request_id: Optional[str] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: int = field(default_factory=lambda: int(time.time()))


# ─── pricing table ─────────────────────────────────────────────────────────


def _load_pricing() -> dict:
    """Read the active pricing table.

    Falls back to a permissive empty table if the file is missing so the
    ledger doesn't refuse writes when ops forgets to seed pricing — we'd
    rather record the call with zero cost than drop it on the floor.
    """
    try:
        return json.loads(PRICING_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": "unset", "margin": 0.0, "providers": {}}
    except Exception:
        return {"version": "unset", "margin": 0.0, "providers": {}}


def _model_row(provider: str, model: str) -> tuple[dict, str, float]:
    pricing = _load_pricing()
    margin = float(pricing.get("margin") or 0.0)
    version = str(pricing.get("version") or "unset")
    providers = pricing.get("providers") or {}
    by_model = providers.get(provider) or {}
    row = by_model.get(model) or {}
    return row, version, margin


# ─── cost compute ──────────────────────────────────────────────────────────


def _quant(x: Decimal) -> Decimal:
    """Round to micro-USD. Anything finer is meaningless for ledger entries."""
    return x.quantize(Decimal("0.000001"))


def price_chat(usage: dict[str, Any], row: dict[str, Any]) -> Decimal:
    """Compute upstream USD cost for one Anthropic chat usage block."""
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    mtok = Decimal("1000000")
    cost = Decimal("0")
    cost += Decimal(input_tokens) * Decimal(str(row.get("input_per_mtok") or 0)) / mtok
    cost += Decimal(output_tokens) * Decimal(str(row.get("output_per_mtok") or 0)) / mtok
    cost += Decimal(cache_creation) * Decimal(str(row.get("cache_creation_per_mtok") or 0)) / mtok
    cost += Decimal(cache_read) * Decimal(str(row.get("cache_read_per_mtok") or 0)) / mtok
    return _quant(cost)


def price_image(units: dict[str, Any], row: dict[str, Any]) -> Decimal:
    """Compute upstream USD cost for one OpenAI image-gen call.

    ``units`` carries ``images`` (count) and ``size`` (one of "1024", "2048").
    """
    n = int(units.get("images") or 1)
    size = str(units.get("size") or "1024")
    key = f"per_image_{size}"
    per = Decimal(str(row.get(key) or row.get("per_image_1024") or 0))
    return _quant(Decimal(n) * per)


# ─── sqlite plumbing ───────────────────────────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id              TEXT PRIMARY KEY,
    tenant          TEXT NOT NULL,
    project         TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    session_id      TEXT,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    units_json      TEXT NOT NULL,
    upstream_cost   TEXT NOT NULL,
    billed_cost     TEXT NOT NULL,
    pricing_version TEXT NOT NULL,
    request_id      TEXT,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_tenant_time ON usage_events (tenant, created_at);
CREATE INDEX IF NOT EXISTS ix_usage_tenant_project_time ON usage_events (tenant, project, created_at);
"""


def _connect() -> sqlite3.Connection:
    BILLING_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BILLING_DB), isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    return conn


def _insert(event: UsageEvent) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO usage_events (
                id, tenant, project, user_id, session_id, provider, model,
                kind, units_json, upstream_cost, billed_cost,
                pricing_version, request_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.tenant,
                event.project,
                event.user_id,
                event.session_id,
                event.provider,
                event.model,
                event.kind,
                json.dumps(event.units, default=str),
                str(event.upstream_cost_usd),
                str(event.billed_cost_usd),
                event.pricing_version,
                event.request_id,
                event.created_at,
            ),
        )
    finally:
        conn.close()


# ─── public capture API ────────────────────────────────────────────────────


def _project_name_from_path(project_path: Path) -> str:
    """Project name as the directory basename (matches the rest of the stack)."""
    return Path(project_path).name


def record_chat_usage(
    *,
    tenant: str,
    project: str,
    user_id: str,
    session_id: Optional[str],
    model: str,
    usage: dict[str, Any],
    request_id: Optional[str] = None,
) -> UsageEvent:
    """Append a chat ``UsageEvent`` for one Anthropic SDK ``usage`` block.

    ``usage`` is the dict the SDK emits on ``assistant`` / ``result`` events.
    Zero-token blocks are still recorded — a turn that hits cache produces
    a cheap-but-real event.
    """
    row, version, margin = _model_row("anthropic", model)
    upstream = price_chat(usage or {}, row)
    billed = _quant(upstream * (Decimal("1") + Decimal(str(margin))))
    event = UsageEvent(
        tenant=tenant,
        project=project,
        user_id=user_id,
        session_id=session_id,
        provider="anthropic",
        model=model,
        kind="chat",
        units={
            "input_tokens": int((usage or {}).get("input_tokens") or 0),
            "output_tokens": int((usage or {}).get("output_tokens") or 0),
            "cache_creation_input_tokens": int(
                (usage or {}).get("cache_creation_input_tokens") or 0
            ),
            "cache_read_input_tokens": int(
                (usage or {}).get("cache_read_input_tokens") or 0
            ),
        },
        upstream_cost_usd=upstream,
        billed_cost_usd=billed,
        pricing_version=version,
        request_id=request_id,
    )
    _insert(event)
    return event


def record_provider_call(
    *,
    tenant: str,
    project: str,
    user_id: str,
    session_id: Optional[str],
    provider: str,
    model: str,
    kind: str,
    units: dict[str, Any],
    request_id: Optional[str] = None,
) -> UsageEvent:
    """Append a non-chat ``UsageEvent`` (image-gen today, more later)."""
    row, version, margin = _model_row(provider, model)
    if kind == "image":
        upstream = price_image(units or {}, row)
    else:
        # Future-proof: unknown kinds price at zero; the call is still
        # recorded so reconciliation can flag the gap.
        upstream = Decimal("0")
    billed = _quant(upstream * (Decimal("1") + Decimal(str(margin))))
    event = UsageEvent(
        tenant=tenant,
        project=project,
        user_id=user_id,
        session_id=session_id,
        provider=provider,
        model=model,
        kind=kind,
        units=dict(units or {}),
        upstream_cost_usd=upstream,
        billed_cost_usd=billed,
        pricing_version=version,
        request_id=request_id,
    )
    _insert(event)
    return event


# ─── readers (used by tests and the future billing UI) ─────────────────────


def list_events(tenant: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Most recent events first. ``tenant=None`` lists across all tenants."""
    conn = _connect()
    try:
        if tenant is None:
            rows = conn.execute(
                "SELECT * FROM usage_events ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM usage_events
                WHERE tenant = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (tenant, limit),
            ).fetchall()
        cols = [c[0] for c in conn.execute("SELECT * FROM usage_events LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def mtd_billed_usd(tenant: str) -> Decimal:
    """Sum ``billed_cost`` for this tenant, for the current calendar month.

    Lightweight implementation — fine for the per-request eligibility check
    at expected scale. Index ``ix_usage_tenant_time`` keeps it fast.
    """
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    month_start = int(
        _dt.datetime(now.year, now.month, 1, tzinfo=_dt.timezone.utc).timestamp()
    )
    conn = _connect()
    try:
        total = Decimal("0")
        for (billed,) in conn.execute(
            "SELECT billed_cost FROM usage_events WHERE tenant = ? AND created_at >= ?",
            (tenant, month_start),
        ):
            try:
                total += Decimal(str(billed))
            except Exception:
                continue
        return _quant(total)
    finally:
        conn.close()
