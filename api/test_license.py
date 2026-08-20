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
