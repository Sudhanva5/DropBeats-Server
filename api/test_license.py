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
