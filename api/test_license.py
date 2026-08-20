import pytest


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
