import os

import pytest


GUMROAD_SALE = {
    "email": "buyer@example.com",
    "full_name": "Buyer Person",
    "seller_id": "test-seller",
    "sale_id": "sale_new_1",
    "license_key": "EEEE-FFFF",
    "country_code": "IN",
}


@pytest.mark.asyncio
async def test_webhook_creates_licence(client, migrated_conn):
    r = await client.post("/webhooks/gumroad/test-secret", data=GUMROAD_SALE)
    assert r.status_code == 200
    assert r.json()["success"] is True

    row = await migrated_conn.fetchrow(
        "select email, full_name, country, is_active from licenses "
        "where license_key = 'EEEE-FFFF'"
    )
    assert row["email"] == "buyer@example.com"
    assert row["full_name"] == "Buyer Person"
    assert row["country"] == "IN"
    assert row["is_active"] is True


@pytest.mark.asyncio
async def test_webhook_replay_creates_exactly_one_licence(client, migrated_conn):
    """Gumroad retries on non-2xx. The original handler inserted
    unconditionally, so a retry duplicated the licence.

    A naive unconditional-INSERT handler would 500 on the second post and
    still leave exactly one row, so the row count alone doesn't prove
    idempotency — the second response must also succeed, since that's the
    2xx Gumroad needs to see to stop retrying.
    """
    first = await client.post("/webhooks/gumroad/test-secret", data=GUMROAD_SALE)
    second = await client.post("/webhooks/gumroad/test-secret", data=GUMROAD_SALE)

    assert first.status_code == 200
    assert first.json()["success"] is True
    assert second.status_code == 200
    assert second.json()["success"] is True

    count = await migrated_conn.fetchval(
        "select count(*) from licenses where sale_id = 'sale_new_1'"
    )
    assert count == 1


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_seller(client, migrated_conn):
    payload = dict(GUMROAD_SALE, seller_id="impostor")
    r = await client.post("/webhooks/gumroad/test-secret", data=payload)
    assert r.json()["success"] is False

    count = await migrated_conn.fetchval("select count(*) from licenses")
    assert count == 0


@pytest.mark.asyncio
async def test_webhook_rejects_empty_seller_when_env_unset(client, migrated_conn, monkeypatch):
    """A missing GUMROAD_SELLER_ID must fail closed, not compare "" == ""
    against an attacker-supplied empty seller_id field."""
    monkeypatch.setenv("GUMROAD_SELLER_ID", "")
    payload = dict(GUMROAD_SALE, seller_id="")
    r = await client.post("/webhooks/gumroad/test-secret", data=payload)
    assert r.json()["success"] is False

    count = await migrated_conn.fetchval("select count(*) from licenses")
    assert count == 0


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_secret_path(client):
    r = await client.post("/webhooks/gumroad/wrong-secret", data=GUMROAD_SALE)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_webhook_rejects_non_ascii_secret_path(client):
    """compare_digest raises TypeError on non-ASCII str input. That must not
    escape as a 500 -- a 500 here versus a 404 for other bad paths would be a
    one-request oracle revealing that the route exists."""
    r = await client.post("/webhooks/gumroad/%C3%A9", data=GUMROAD_SALE)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_webhook_rejects_missing_sale_id(client, migrated_conn):
    payload = dict(GUMROAD_SALE)
    del payload["sale_id"]
    r = await client.post("/webhooks/gumroad/test-secret", data=payload)
    assert r.json()["success"] is False

    count = await migrated_conn.fetchval("select count(*) from licenses")
    assert count == 0


@pytest.mark.asyncio
async def test_webhook_reuse_of_license_key_under_new_sale_returns_200_failure(
    client, migrated_conn
):
    """sale_id is the ON CONFLICT arbiter, but license_key carries its own
    unique constraint. A different sale reusing a license_key must not raise
    -- Gumroad only stops retrying on a 2xx, so a genuine data conflict must
    still be reported as success=False on a 200, not surfaced as a 500."""
    await client.post("/webhooks/gumroad/test-secret", data=GUMROAD_SALE)

    conflicting = dict(GUMROAD_SALE, sale_id="sale_new_2")
    r = await client.post("/webhooks/gumroad/test-secret", data=conflicting)

    assert r.status_code == 200
    assert r.json()["success"] is False

    count = await migrated_conn.fetchval(
        "select count(*) from licenses where license_key = 'EEEE-FFFF'"
    )
    assert count == 1


@pytest.mark.asyncio
async def test_webhook_logs_every_payload_including_rejected(client, migrated_conn):
    """Two rows, not just >=1: the rejection branch writes its own row, so a
    loose >=1 assertion would also pass for a handler that only logs on
    failure and never logs before validating."""
    await client.post(
        "/webhooks/gumroad/test-secret", data=dict(GUMROAD_SALE, seller_id="impostor")
    )
    count = await migrated_conn.fetchval("select count(*) from webhook_logs")
    assert count == 2

    pre_validation_email = await migrated_conn.fetchval(
        "select payload->>'email' from webhook_logs order by received_at asc limit 1"
    )
    assert pre_validation_email == GUMROAD_SALE["email"]


@pytest.mark.asyncio
async def test_migrations_create_licenses_table(migrated_conn):
    exists = await migrated_conn.fetchval(
        "select to_regclass('public.licenses') is not null"
    )
    assert exists is True


@pytest.mark.asyncio
async def test_migrations_are_idempotent(migrated_conn, migrations_dir):
    import migrate

    applied = await migrate.apply(migrated_conn, migrations_dir)
    assert applied == [], "re-running migrations should apply nothing"


@pytest.mark.asyncio
async def test_normalize_license_key_strips_case_and_separators(migrated_conn):
    result = await migrated_conn.fetchval(
        "select normalize_license_key($1)", "ab-cd_ef"
    )
    assert result == "ABCDEF"


@pytest.mark.asyncio
async def test_app_role_cannot_delete_licenses(migrated_conn):
    """Asserts the grants are in force rather than assuming the migration ran.

    A DELETE grant slipping in is exactly the kind of regression that is
    invisible until it matters.
    """
    import asyncpg

    await migrated_conn.execute(
        "insert into licenses (email, license_key) values ($1, $2)",
        "a@example.com",
        "KEY-1",
    )

    app_conn = await asyncpg.connect(
        "postgresql://dropbeats_app:test_password@localhost/dropbeats_test"
    )
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute("delete from licenses")
    finally:
        await app_conn.close()


@pytest.mark.asyncio
async def test_app_role_cannot_update_webhook_logs(migrated_conn):
    import asyncpg

    app_conn = await asyncpg.connect(
        "postgresql://dropbeats_app:test_password@localhost/dropbeats_test"
    )
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute("update webhook_logs set success = false")
    finally:
        await app_conn.close()


@pytest.mark.asyncio
async def test_get_pool_before_init_raises():
    import db

    await db.close_pool()
    with pytest.raises(RuntimeError, match="not initialised"):
        db.get_pool()


@pytest.mark.asyncio
async def test_init_pool_is_idempotent(migrated_conn):
    import db

    await db.close_pool()
    first = await db.init_pool("postgresql://localhost/dropbeats_test")
    second = await db.init_pool("postgresql://localhost/dropbeats_test")
    assert first is second
    await db.close_pool()


@pytest.mark.asyncio
async def test_acquire_pool_initialises_and_is_idempotent(migrated_conn, monkeypatch):
    """acquire_pool() builds a pool when there is none, then reuses it.

    It reads DATABASE_URL rather than taking a dsn, because the endpoints that
    call it have no dsn to pass.
    """
    import db

    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/dropbeats_test")
    await db.close_pool()
    with pytest.raises(RuntimeError, match="not initialised"):
        db.get_pool()

    first = await db.acquire_pool()
    second = await db.acquire_pool()
    assert first is second
    # The lazily created pool is the module's pool, not a private one.
    assert db.get_pool() is first
    await db.close_pool()


@pytest.mark.asyncio
async def test_endpoint_heals_itself_when_startup_init_never_ran(
    client, seeded, monkeypatch
):
    """A request succeeds even though no pool was initialised at startup.

    main.py's startup pool init is deliberately non-fatal, so a database that
    was down at boot leaves a healthy process with no pool and no restart
    coming. This is the path that makes licensing recover anyway: the first
    request after the database returns initialises the pool itself.
    """
    import db

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://dropbeats_app:test_password@localhost/dropbeats_test",
    )
    # Undo the fixture's init_pool to reproduce a failed startup init.
    await db.close_pool()
    with pytest.raises(RuntimeError, match="not initialised"):
        db.get_pool()

    r = await client.post("/license/validate", json={"key": "AAAA-BBBB"})

    assert r.status_code == 200
    assert r.json()["valid"] is True
    # The endpoint left a usable pool behind, so later requests are cheap.
    assert db.get_pool() is not None


@pytest.mark.asyncio
async def test_validate_accepts_known_active_key(client, seeded):
    r = await client.post("/license/validate", json={"key": "AAAA-BBBB"})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["name"] == "Active User"
    assert body["email"] == "active@example.com"
    assert body["country"] == "IN"
    assert body["has_completed_onboarding"] is False
    assert body["created_at"] is not None


@pytest.mark.asyncio
async def test_validate_created_at_has_no_fractional_seconds(client, seeded, migrated_conn):
    """The shipped Swift decoder has no fractional-seconds formatter, so a
    microsecond component makes the whole response undecodable."""
    from datetime import datetime

    # Force a created_at with a non-zero microsecond component so this test
    # doesn't pass by luck if now() happens to land on a whole second.
    await migrated_conn.execute(
        "update licenses set created_at = '2025-01-20 09:47:01.123456+00' "
        "where license_key = 'AAAA-BBBB'"
    )

    r = await client.post("/license/validate", json={"key": "AAAA-BBBB"})
    created_at = r.json()["created_at"]

    assert "." not in created_at, f"fractional seconds present: {created_at}"
    # Must round-trip through a strict parser.
    assert datetime.fromisoformat(created_at) is not None


@pytest.mark.asyncio
async def test_validate_rejects_unknown_key(client, seeded):
    r = await client.post("/license/validate", json={"key": "NOPE-NOPE"})
    assert r.status_code == 200
    assert r.json() == {
        "valid": False,
        "error": "Invalid license key",
        "name": None,
        "email": None,
        "country": None,
        "created_at": None,
        "has_completed_onboarding": None,
    }


@pytest.mark.asyncio
async def test_validate_rejects_deactivated_key(client, seeded):
    r = await client.post("/license/validate", json={"key": "CCCC-DDDD"})
    assert r.json()["valid"] is False
    assert r.json()["error"] == "License is not active"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["aaaa-bbbb", "AAAABBBB", "aa_aa_bbbb", "AaAa-BbBb"])
async def test_validate_normalises_key_variants(client, seeded, variant):
    r = await client.post("/license/validate", json={"key": variant})
    assert r.json()["valid"] is True, f"{variant} should resolve to the same licence"


@pytest.mark.asyncio
async def test_validate_updates_last_login(client, seeded, migrated_conn):
    before = await migrated_conn.fetchval(
        "select last_login from licenses where license_key = 'AAAA-BBBB'"
    )
    assert before is None

    await client.post("/license/validate", json={"key": "AAAA-BBBB"})

    after = await migrated_conn.fetchval(
        "select last_login from licenses where license_key = 'AAAA-BBBB'"
    )
    assert after is not None


@pytest.mark.asyncio
async def test_deactivate_marks_licence_inactive(client, seeded, migrated_conn):
    r = await client.post(
        "/license/deactivate",
        json={"key": "AAAA-BBBB", "email": "active@example.com"},
    )
    assert r.status_code == 200
    assert r.json()["success"] is True

    still_active = await migrated_conn.fetchval(
        "select is_active from licenses where license_key = 'AAAA-BBBB'"
    )
    assert still_active is False


@pytest.mark.asyncio
async def test_deactivate_requires_matching_email(client, seeded, migrated_conn):
    """The email is the authorisation check: holding the key alone must not be
    enough to deactivate someone else's licence."""
    r = await client.post(
        "/license/deactivate",
        json={"key": "AAAA-BBBB", "email": "attacker@example.com"},
    )
    assert r.json()["success"] is False
    assert r.json()["error"] == "License not found"

    still_active = await migrated_conn.fetchval(
        "select is_active from licenses where license_key = 'AAAA-BBBB'"
    )
    assert still_active is True


@pytest.mark.asyncio
async def test_onboarding_flag_round_trips(client, seeded):
    r = await client.post(
        "/license/onboarding", json={"key": "AAAA-BBBB", "completed": True}
    )
    assert r.json()["success"] is True

    check = await client.post("/license/validate", json={"key": "AAAA-BBBB"})
    assert check.json()["has_completed_onboarding"] is True


@pytest.mark.asyncio
async def test_onboarding_unknown_key_reports_failure(client, seeded):
    r = await client.post(
        "/license/onboarding", json={"key": "NOPE", "completed": True}
    )
    assert r.json()["success"] is False
    assert r.json()["error"] == "License not found"


def test_rate_limiter_allows_then_blocks():
    import license as license_module

    limiter = license_module.RateLimiter(capacity=3, refill_per_second=0.0)
    assert [limiter.allow("1.2.3.4") for _ in range(3)] == [True, True, True]
    assert limiter.allow("1.2.3.4") is False


def test_rate_limiter_is_per_key():
    import license as license_module

    limiter = license_module.RateLimiter(capacity=1, refill_per_second=0.0)
    assert limiter.allow("1.1.1.1") is True
    assert limiter.allow("1.1.1.1") is False
    assert limiter.allow("2.2.2.2") is True


def test_rate_limiter_refills_over_time():
    import license as license_module

    clock = {"now": 1000.0}
    limiter = license_module.RateLimiter(
        capacity=1, refill_per_second=1.0, clock=lambda: clock["now"]
    )
    assert limiter.allow("1.1.1.1") is True
    assert limiter.allow("1.1.1.1") is False
    clock["now"] += 2.0
    assert limiter.allow("1.1.1.1") is True


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    """Minimal stand-in for fastapi.Request: just enough surface for
    _client_key (headers.get and client.host)."""

    def __init__(self, headers=None, client_host="9.9.9.9"):
        self.headers = headers or {}
        self.client = _FakeClient(client_host) if client_host is not None else None


def test_client_key_prefers_leftmost_x_forwarded_for():
    import license as license_module

    request = _FakeRequest(
        headers={
            "x-forwarded-for": "203.0.113.7, 70.41.3.18, 150.172.238.178"
        },
        client_host="10.0.0.1",
    )
    assert license_module._client_key(request) == "203.0.113.7"


def test_client_key_falls_back_to_peer_address_without_header():
    import license as license_module

    request = _FakeRequest(headers={}, client_host="10.0.0.1")
    assert license_module._client_key(request) == "10.0.0.1"


def test_client_key_falls_back_to_unknown_without_client_or_header():
    import license as license_module

    request = _FakeRequest(headers={}, client_host=None)
    assert license_module._client_key(request) == "unknown"


def test_different_x_forwarded_for_values_get_independent_buckets():
    """Two callers behind different X-Forwarded-For values must not share a
    bucket -- exhausting one caller's tokens must not deny the other.

    Drives the same key-derivation the endpoint uses, against a small,
    dedicated RateLimiter instance rather than the real module-level
    _validate_limiter (capacity 30), so the test stays fast and does not
    pollute shared state used by other tests.
    """
    import license as license_module

    limiter = license_module.RateLimiter(capacity=1, refill_per_second=0.0)

    request_a = _FakeRequest(headers={"x-forwarded-for": "1.1.1.1"})
    request_b = _FakeRequest(headers={"x-forwarded-for": "2.2.2.2"})

    key_a = license_module._client_key(request_a)
    key_b = license_module._client_key(request_b)

    assert limiter.allow(key_a) is True
    assert limiter.allow(key_a) is False
    # A different X-Forwarded-For value must still be allowed: its bucket is
    # untouched by request_a's exhaustion.
    assert limiter.allow(key_b) is True


def test_client_key_rejects_over_long_x_forwarded_for():
    """An attacker-chosen key must not be attacker-sized.

    The header is spoofable by design, so the only thing standing between it
    and unbounded memory growth is that a multi-kilobyte value never becomes a
    dict key at all."""
    import license as license_module

    request = _FakeRequest(
        headers={"x-forwarded-for": "1" * 4096},
        client_host="10.0.0.1",
    )
    assert license_module._client_key(request) == "10.0.0.1"


@pytest.mark.parametrize(
    "forwarded",
    [
        "a" * 46,  # one over the IPv6 maximum
        "not an ip",  # spaces
        "<script>",  # arbitrary junk
        "host.example.com",  # a name, not an address
        "",  # empty after strip
    ],
)
def test_client_key_rejects_malformed_x_forwarded_for(forwarded):
    import license as license_module

    request = _FakeRequest(
        headers={"x-forwarded-for": forwarded}, client_host="10.0.0.1"
    )
    assert license_module._client_key(request) == "10.0.0.1"


def test_client_key_accepts_ipv6_with_zone():
    """The clamp must not reject addresses it is supposed to let through."""
    import license as license_module

    request = _FakeRequest(
        headers={"x-forwarded-for": "2001:db8::1%1"}, client_host="10.0.0.1"
    )
    assert license_module._client_key(request) == "2001:db8::1%1"


def test_client_key_falls_back_to_unknown_when_peer_is_not_an_address():
    import license as license_module

    request = _FakeRequest(
        headers={"x-forwarded-for": "garbage value"}, client_host="/tmp/uvicorn.sock"
    )
    assert license_module._client_key(request) == "unknown"


def test_rate_limiter_bucket_count_stays_bounded():
    """Many distinct keys must not mean many retained buckets.

    This is the memory exhaustion the clamp alone cannot stop: every key here
    is perfectly well formed."""
    import license as license_module

    limiter = license_module.RateLimiter(
        capacity=1, refill_per_second=0.0, max_buckets=10
    )
    for i in range(1000):
        limiter.allow(f"10.0.{i // 256}.{i % 256}")

    assert len(limiter._buckets) <= 10


def test_rate_limiter_evicts_full_buckets_first():
    """A bucket at capacity is indistinguishable from an unseen key, so it is
    the safe thing to drop -- and dropping it must not change that key's
    answer."""
    clock = {"now": 1000.0}
    import license as license_module

    limiter = license_module.RateLimiter(
        capacity=2,
        refill_per_second=1.0,
        clock=lambda: clock["now"],
        max_buckets=3,
    )

    # "1.1.1.1" spends a token, then refills all the way back to capacity.
    assert limiter.allow("1.1.1.1") is True
    clock["now"] += 100.0

    # Drive the limiter over its cap with keys that are mid-consumption.
    for i in range(20):
        limiter.allow(f"2.2.2.{i}")

    assert len(limiter._buckets) <= 3
    assert "1.1.1.1" not in limiter._buckets, "the full bucket should be evicted first"

    # Evicted or not, the answer for that key is unchanged: a full bucket's
    # worth of requests still succeeds.
    assert limiter.allow("1.1.1.1") is True
    assert limiter.allow("1.1.1.1") is True
    assert limiter.allow("1.1.1.1") is False


def test_rate_limiter_reset_clears_buckets():
    import license as license_module

    limiter = license_module.RateLimiter(capacity=1, refill_per_second=0.0)
    assert limiter.allow("1.1.1.1") is True
    assert limiter.allow("1.1.1.1") is False
    limiter.reset()
    assert limiter.allow("1.1.1.1") is True


@pytest.mark.asyncio
async def test_validate_is_rate_limited(client, seeded, monkeypatch):
    """The limiter is wired into the endpoint, and exhausting it is a 429.

    Without this, deleting the two lines that call the limiter leaves the
    whole suite green.
    """
    import license as license_module

    monkeypatch.setattr(
        license_module,
        "_validate_limiter",
        license_module.RateLimiter(capacity=1, refill_per_second=0.0),
    )

    first = await client.post("/license/validate", json={"key": "AAAA-BBBB"})
    second = await client.post("/license/validate", json={"key": "AAAA-BBBB"})

    assert first.status_code == 200
    assert first.json()["valid"] is True
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_onboarding_is_rate_limited(client, seeded, monkeypatch):
    """/license/onboarding answers "does this key exist?" on a key alone --
    the same oracle validation is limited to protect."""
    import license as license_module

    monkeypatch.setattr(
        license_module,
        "_onboarding_limiter",
        license_module.RateLimiter(capacity=1, refill_per_second=0.0),
    )

    first = await client.post(
        "/license/onboarding", json={"key": "AAAA-BBBB", "completed": True}
    )
    second = await client.post(
        "/license/onboarding", json={"key": "AAAA-BBBB", "completed": True}
    )

    assert first.status_code == 200
    assert first.json()["success"] is True
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_deactivate_is_rate_limited(client, seeded, monkeypatch):
    import license as license_module

    monkeypatch.setattr(
        license_module,
        "_deactivate_limiter",
        license_module.RateLimiter(capacity=1, refill_per_second=0.0),
    )

    payload = {"key": "AAAA-BBBB", "email": "active@example.com"}
    first = await client.post("/license/deactivate", json=payload)
    second = await client.post("/license/deactivate", json=payload)

    assert first.status_code == 200
    assert first.json()["success"] is True
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_validate_returns_503_when_pool_unavailable(client, seeded, monkeypatch):
    """A database outage is "ask again later", not "your licence is invalid"
    and not a 500. The client has to be able to tell it apart from a verdict.
    """
    import db
    import license as license_module

    async def boom():
        raise OSError("connection refused")

    monkeypatch.setattr(db, "acquire_pool", boom)

    r = await client.post("/license/validate", json={"key": "AAAA-BBBB"})

    assert r.status_code == 503
    body = r.json()
    # Not a ValidateResponse: no "valid" field to mistake for a business answer.
    assert "valid" not in body
    assert body["detail"] == "Licensing temporarily unavailable"


@pytest.mark.asyncio
async def test_mutating_endpoints_return_503_when_pool_unavailable(
    client, seeded, monkeypatch
):
    import db

    async def boom():
        raise OSError("connection refused")

    monkeypatch.setattr(db, "acquire_pool", boom)

    onboarding = await client.post(
        "/license/onboarding", json={"key": "AAAA-BBBB", "completed": True}
    )
    deactivate = await client.post(
        "/license/deactivate",
        json={"key": "AAAA-BBBB", "email": "active@example.com"},
    )

    assert onboarding.status_code == 503
    assert deactivate.status_code == 503


@pytest.mark.asyncio
async def test_cors_headers_absent_on_licensing_routes():
    """A wildcard CORS policy over /license/validate would let any web page
    read a customer's name and email cross-origin. The native client sends no
    Origin, so these routes need no CORS at all."""
    import httpx
    from fastapi import FastAPI

    from cors import ScopedCORSMiddleware

    app = FastAPI()

    @app.get("/search/{query}")
    async def search(query: str):
        return {"ok": True}

    @app.post("/license/validate")
    async def validate():
        return {"valid": True, "email": "customer@example.com"}

    app.add_middleware(
        ScopedCORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        headers = {"Origin": "https://evil.example"}

        music = await c.get("/search/anything", headers=headers)
        licensing = await c.post("/license/validate", headers=headers)

        # The music endpoints keep the behaviour they had.
        assert music.headers.get("access-control-allow-origin") is not None
        # The licensing route gets nothing, so a browser withholds the body.
        assert licensing.headers.get("access-control-allow-origin") is None
        assert licensing.headers.get("access-control-allow-credentials") is None


@pytest.mark.asyncio
async def test_cors_preflight_not_answered_for_licensing_routes():
    """The preflight must not be answered either, or a non-simple
    cross-origin request would be waved through."""
    import httpx
    from fastapi import FastAPI

    from cors import ScopedCORSMiddleware

    app = FastAPI()

    @app.post("/license/validate")
    async def validate():
        return {"valid": True}

    app.add_middleware(
        ScopedCORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.request(
            "OPTIONS",
            "/license/validate",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert r.status_code != 200, "CORS middleware answered a preflight it should skip"
    assert r.headers.get("access-control-allow-origin") is None


# main.py cannot be imported into this process: it builds a YTMusic client at
# module scope and pulls in uvicorn and dotenv, none of which belong in a
# database test run. It is still the file where the licensing kill-switch and
# the health report live, so it is exercised out of process with those three
# imports stubbed. One subprocess per scenario also means each gets a clean
# module-level LICENSING_ENABLED, which a same-process import could not.
_MAIN_PROBE = r'''
import json
import os
import sys
import types

ytmusicapi = types.ModuleType("ytmusicapi")


class _FakeYTMusic:
    def __init__(self, *args, **kwargs):
        pass

    def search(self, *args, **kwargs):
        return []


ytmusicapi.YTMusic = _FakeYTMusic
sys.modules["ytmusicapi"] = ytmusicapi

uvicorn = types.ModuleType("uvicorn")
uvicorn.run = lambda *a, **k: None
sys.modules["uvicorn"] = uvicorn

dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *a, **k: None
sys.modules["dotenv"] = dotenv

if os.environ.get("PROBE_BLOCK_ASYNCPG"):
    # None in sys.modules is what makes "import asyncpg" raise ImportError,
    # reproducing a machine where the driver was never installed.
    sys.modules["asyncpg"] = None

import main

if os.environ.get("PROBE_INIT_POOL"):
    import asyncio

    import db

    asyncio.run(db.init_pool(os.environ["PROBE_INIT_POOL"]))

import asyncio

health = asyncio.run(main.health_check())

print(json.dumps({
    "licensing": health["licensing"],
    "status": health["status"],
    "routes": sorted(
        r.path for r in main.app.routes if getattr(r, "path", "").startswith("/license")
    ),
    "enabled": main.LICENSING_ENABLED,
}))
'''


def _probe_main(**env):
    """Import main.py in a subprocess and report its licensing state."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    child_env = dict(os.environ)
    child_env.pop("DATABASE_URL", None)
    child_env.update({k: v for k, v in env.items() if v is not None})

    result = subprocess.run(
        [sys.executable, "-c", _MAIN_PROBE],
        cwd=str(Path(__file__).parent),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"importing main.py failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_health_reports_licensing_disabled_without_database_url():
    assert _probe_main()["licensing"] == "disabled"


def test_missing_asyncpg_disables_licensing_instead_of_killing_the_process():
    """The bundled macOS app hands this process its whole environment and
    main.py calls load_dotenv(), so a stray DATABASE_URL on a user's machine
    turns licensing on where asyncpg was never installed. An unguarded import
    would raise at module scope, uvicorn would exit, and the user would lose
    all playback while search kept working."""
    probe = _probe_main(
        DATABASE_URL="postgresql://localhost/nope", PROBE_BLOCK_ASYNCPG="1"
    )

    assert probe["enabled"] is False
    assert probe["routes"] == [], "licensing routes registered without a driver"
    assert probe["licensing"] == "disabled"


def test_health_reports_degraded_when_enabled_but_pool_is_missing():
    """The failure this names is invisible otherwise: startup pool init is
    deliberately non-fatal, so a process with completely broken licensing
    still answers /health as healthy."""
    probe = _probe_main(DATABASE_URL="postgresql://localhost/nope")

    assert probe["enabled"] is True
    assert "/license/validate" in probe["routes"]
    assert probe["licensing"] == "degraded"
    # Railway restarts a service whose healthcheck fails. Reporting the
    # degradation must not become a way of taking /search down.
    assert probe["status"] == "healthy"


def test_health_reports_ready_when_pool_exists(migrated_conn):
    from conftest import TEST_DSN

    probe = _probe_main(DATABASE_URL=TEST_DSN, PROBE_INIT_POOL=TEST_DSN)

    assert probe["licensing"] == "ready"
