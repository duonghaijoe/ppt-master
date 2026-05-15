"""Phase 5 verification — metering capture + mediated image generation.

Per §13 Phase 5 and §17 of web/MULTITENANT_REARCHITECTURE.md:

- Every chat turn that lands a ``usage`` block in the agent loop writes
  one ``UsageEvent`` to ``platform/billing.db`` with a non-zero
  ``billed_cost_usd``.
- Image generation funnels through
  ``POST /api/tenants/{slug}/projects/{name}/images/generate``. The
  endpoint writes the bytes into ``<project>/images/`` and records a
  ``UsageEvent`` priced from ``platform/pricing.json``.
- The agent sandbox blocks direct OpenAI / ``image_gen.py`` calls from
  Bash so tenants can't bypass metering by shelling out.
"""
from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import files as files_mod
import main
import metering
import tenants as tenants_mod
import templates as templates_mod
import users as users_mod
from agent import Session


# ─── isolated tree + pricing ───────────────────────────────────────────────


_PRICING_FIXTURE = {
    "version": "2026-05-15-test",
    "margin": 0.20,
    "providers": {
        "anthropic": {
            "claude-sonnet-4-6": {
                "input_per_mtok": 3.0,
                "output_per_mtok": 15.0,
                "cache_read_per_mtok": 0.3,
                "cache_creation_per_mtok": 3.75,
            },
            "claude-opus-4-7": {
                "input_per_mtok": 15.0,
                "output_per_mtok": 75.0,
                "cache_read_per_mtok": 1.5,
                "cache_creation_per_mtok": 18.75,
            },
        },
        "openai": {
            "gpt-image-1": {
                "per_image_1024": 0.04,
                "per_image_2048": 0.08,
            }
        },
    },
}


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tree with one tenant + isolated billing.db + pricing.json."""
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
    (tenants_dir / "acme" / "projects" / "alpha" / "images").mkdir()

    seeded = [
        ("u-alice", "alice@example.com", True,
         [{"tenant_slug": "acme", "role": "owner"}]),
        ("u-bob", "bob@example.com", False,
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
                "created_at": 1700000000,
            }),
            encoding="utf-8",
        )
    platform_file.write_text(
        json.dumps({"admins": ["u-alice"], "schema_version": 1, "created_at": 1700000000}),
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


# ─── §17.2 capture point #1 — chat usage ───────────────────────────────────


def test_chat_usage_writes_nonzero_event(isolated_root: Path) -> None:
    """A chat turn's usage block produces a UsageEvent with non-zero cost.

    Pricing math: 1000 input + 500 output @ sonnet rates.
        upstream = 1000/1e6 * 3.0 + 500/1e6 * 15.0
                 = 0.003 + 0.0075 = 0.0105
        billed   = upstream * 1.20 = 0.0126
    """
    event = metering.record_chat_usage(
        tenant="acme",
        project="alpha",
        user_id="u-bob",
        session_id="s-test",
        model="claude-sonnet-4-6",
        usage={
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    )
    assert event.upstream_cost_usd == Decimal("0.010500")
    assert event.billed_cost_usd == Decimal("0.012600")
    assert event.billed_cost_usd > Decimal("0")
    # Round-trip through the SQLite ledger.
    events = metering.list_events(tenant="acme")
    assert any(e["id"] == event.id for e in events)
    row = next(e for e in events if e["id"] == event.id)
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["kind"] == "chat"
    assert Decimal(row["billed_cost"]) == Decimal("0.012600")
    assert row["pricing_version"] == "2026-05-15-test"


def test_chat_usage_with_cache_tokens(isolated_root: Path) -> None:
    """Cache-read tokens are priced at the cache rate, not the input rate."""
    event = metering.record_chat_usage(
        tenant="acme",
        project="alpha",
        user_id="u-bob",
        session_id="s-cache",
        model="claude-sonnet-4-6",
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 400,
        },
    )
    # 100/1e6*3 + 50/1e6*15 + 200/1e6*3.75 + 400/1e6*0.3
    # = 0.0003 + 0.00075 + 0.00075 + 0.00012 = 0.00192
    assert event.upstream_cost_usd == Decimal("0.001920")
    assert event.billed_cost_usd == Decimal("0.002304")


def test_chat_usage_unknown_model_priced_zero(isolated_root: Path) -> None:
    """Unknown models record at zero — never drop the event on the floor."""
    event = metering.record_chat_usage(
        tenant="acme",
        project="alpha",
        user_id="u-bob",
        session_id="s-unknown",
        model="claude-mystery-9",
        usage={"input_tokens": 10000, "output_tokens": 5000},
    )
    assert event.upstream_cost_usd == Decimal("0.000000")
    assert event.billed_cost_usd == Decimal("0.000000")
    # Still in the ledger.
    events = metering.list_events(tenant="acme")
    assert any(e["id"] == event.id for e in events)


def test_mtd_billed_aggregates(isolated_root: Path) -> None:
    """``mtd_billed_usd`` sums billed_cost across all rows for the tenant."""
    for _ in range(3):
        metering.record_chat_usage(
            tenant="acme",
            project="alpha",
            user_id="u-bob",
            session_id="s-mtd",
            model="claude-sonnet-4-6",
            usage={"input_tokens": 1000, "output_tokens": 500},
        )
    total = metering.mtd_billed_usd("acme")
    # 3 * 0.0126 = 0.0378
    assert total == Decimal("0.037800")


# ─── §17.2 capture point #2 — mediated image generation ────────────────────


class _FakeImageItem:
    """Stub that mimics ``client.images.generate(...).data[0]``."""
    def __init__(self, b64: str) -> None:
        self.b64_json = b64
        self.url = None


class _FakeImageResponse:
    def __init__(self, b64: str) -> None:
        self.data = [_FakeImageItem(b64)]
        self._request_id = "fake-req-42"


class _FakeImagesEndpoint:
    def __init__(self, b64: str) -> None:
        self._b64 = b64
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeImageResponse(self._b64)


class _FakeOpenAI:
    """Drop-in for the OpenAI SDK client.

    The provider wrapper imports ``OpenAI`` lazily inside ``generate_image``,
    so the test monkeypatches the symbol on ``providers.openai_images``
    before invoking the route.
    """
    instances: list["_FakeOpenAI"] = []

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url
        # 1x1 transparent PNG so the bytes we write are a real image.
        self.images = _FakeImagesEndpoint(
            base64.b64encode(
                bytes.fromhex(
                    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000a49444154789c6300010000000500010d0a2db40000000049454e44ae42"
                    "6082"
                )
            ).decode()
        )
        _FakeOpenAI.instances.append(self)


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> type[_FakeOpenAI]:
    """Replace ``providers.openai_images.OpenAI`` with the fake client.

    We're not mocking the *integration* — the route layer, the provider
    wrapper, the file write, and the ledger insert all run end-to-end.
    Only the network call is replaced so the suite is hermetic.
    """
    import providers.openai_images as oi

    _FakeOpenAI.instances.clear()
    monkeypatch.setattr(oi, "OpenAI", _FakeOpenAI, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    return _FakeOpenAI


def test_image_route_requires_editor_role(client: TestClient, isolated_root: Path) -> None:
    """Viewers cannot trigger billable image gen."""
    r = client.post(
        "/api/tenants/acme/projects/alpha/images/generate",
        headers=hdr("u-bob"),
        json={"prompt": "sunset"},
    )
    assert r.status_code == 403, r.text


def test_image_route_404_for_missing_project(
    client: TestClient, fake_openai: type[_FakeOpenAI]
) -> None:
    r = client.post(
        "/api/tenants/acme/projects/no-such/images/generate",
        headers=hdr("u-alice"),
        json={"prompt": "sunset"},
    )
    assert r.status_code == 404, r.text


def test_image_route_writes_file_and_records_event(
    client: TestClient,
    isolated_root: Path,
    fake_openai: type[_FakeOpenAI],
) -> None:
    """End-to-end: editor POSTs prompt → file lands in images/ → UsageEvent stored."""
    r = client.post(
        "/api/tenants/acme/projects/alpha/images/generate",
        headers=hdr("u-alice"),
        json={
            "prompt": "a misty harbour at dawn",
            "aspect_ratio": "16:9",
            "size": "1024",
            "session_id": "s-img-1",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"].startswith("images/")
    assert body["pixel_size"] == "1536x1024"
    assert body["bytes"] > 0

    file_on_disk = (
        isolated_root / "tenants" / "acme" / "projects" / "alpha" / body["path"]
    )
    assert file_on_disk.is_file()
    assert file_on_disk.read_bytes().startswith(b"\x89PNG"), "expected real PNG bytes"

    events = metering.list_events(tenant="acme")
    matched = [e for e in events if e["id"] == body["usage_event_id"]]
    assert len(matched) == 1, "image gen must produce exactly one UsageEvent"
    row = matched[0]
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-image-1"
    assert row["kind"] == "image"
    units = json.loads(row["units_json"])
    assert units == {"images": 1, "size": "1024"}
    # 1 image @ 0.04 * 1.20 margin = 0.048
    assert Decimal(row["billed_cost"]) == Decimal("0.048000")
    assert row["session_id"] == "s-img-1"
    assert row["user_id"] == "u-alice"

    # The OpenAI SDK fake was constructed with the env key.
    assert _FakeOpenAI.instances[0].api_key == "sk-test-key"
    # And the call used the resolved pixel size, not "1024".
    assert _FakeOpenAI.instances[0].images.calls[0]["size"] == "1536x1024"


def test_image_route_2k_pricing(
    client: TestClient, fake_openai: type[_FakeOpenAI]
) -> None:
    """The 2048 pricing tier costs twice as much per image."""
    r = client.post(
        "/api/tenants/acme/projects/alpha/images/generate",
        headers=hdr("u-alice"),
        json={"prompt": "x", "aspect_ratio": "1:1", "size": "2048"},
    )
    assert r.status_code == 200, r.text
    row = next(
        e for e in metering.list_events(tenant="acme")
        if e["id"] == r.json()["usage_event_id"]
    )
    # 0.08 * 1.20 = 0.096
    assert Decimal(row["billed_cost"]) == Decimal("0.096000")
    assert row["model"] == "gpt-image-1"


def test_image_route_missing_key_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, isolated_root: Path
) -> None:
    """No mock fallback when the key is missing — surface a real failure."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = client.post(
        "/api/tenants/acme/projects/alpha/images/generate",
        headers=hdr("u-alice"),
        json={"prompt": "fail closed"},
    )
    assert r.status_code == 503, r.text
    assert "OPENAI_API_KEY" in r.json()["detail"]


def test_image_route_provider_failure_propagates_as_503(
    client: TestClient,
    isolated_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real OpenAI exception bubbles up as 503, never as a fake success."""
    import providers.openai_images as oi

    class _BrokenOpenAI:
        def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
            class _Images:
                def generate(self, **_kwargs):
                    raise RuntimeError("boom")
            self.images = _Images()

    monkeypatch.setattr(oi, "OpenAI", _BrokenOpenAI, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    r = client.post(
        "/api/tenants/acme/projects/alpha/images/generate",
        headers=hdr("u-alice"),
        json={"prompt": "boom"},
    )
    assert r.status_code == 503, r.text
    assert "openai image gen failed" in r.json()["detail"]


# ─── §17.2 hard guarantee — sandbox blocks bypass paths ────────────────────


def _acme_session(root: Path) -> Session:
    return Session(
        id="t-phase5",
        project_path=(root / "tenants" / "acme" / "projects" / "alpha"),
        tenant_slug="acme",
        user_id="u-bob",
    )


def test_sandbox_bash_blocks_image_gen_script(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct ``image_gen.py`` invocation from Bash is denied."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Bash",
        {"command": "python3 platform/skills/ppt-master/scripts/image_gen.py 'cat'"},
    )
    assert reason is not None
    assert "image_gen" in reason


def test_sandbox_bash_blocks_direct_openai_curl(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``curl https://api.openai.com/...`` bypasses metering — denied."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Bash",
        {"command": "curl https://api.openai.com/v1/images/generations -d '{}'"},
    )
    assert reason is not None
    assert "openai" in reason.lower()


def test_sandbox_bash_allows_local_image_route_curl(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hitting the local mediated route from Bash is allowed (and is the
    only billable path)."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "REPO_ROOT", isolated_root)
    session = _acme_session(isolated_root)
    reason = session._sandbox_deny(
        "Bash",
        {
            "command": (
                "curl -sS -X POST http://127.0.0.1:8787/api/tenants/acme/projects/alpha/images/generate "
                "-H 'Content-Type: application/json' -d '{\"prompt\":\"x\"}'"
            )
        },
    )
    assert reason is None
