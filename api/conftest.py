import os
import sys
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent))

# Owner connection: used by fixtures to migrate and to seed rows.
TEST_DSN = os.getenv(
    "TEST_DATABASE_URL", "postgresql://localhost/dropbeats_test"
)
# Maintenance connection: used to drop and recreate the test database.
ADMIN_DSN = os.getenv("TEST_ADMIN_URL", "postgresql://localhost/postgres")
# Least-privilege connection: what the endpoints themselves run under, mirroring
# production. Password matches ensure_app_role() in the migrated_conn fixture.
APP_DSN = os.getenv(
    "TEST_APP_URL",
    "postgresql://dropbeats_app:test_password@localhost/dropbeats_test",
)


@pytest.fixture(scope="session")
def migrations_dir() -> Path:
    return Path(__file__).parent / "migrations"


@pytest_asyncio.fixture
async def migrated_conn(migrations_dir):
    """A connection to a freshly created, freshly migrated test database.

    The database is dropped and recreated per test so that no test can see
    another's rows. At 39 production rows this is cheap and worth the
    isolation.
    """
    import migrate

    admin = await asyncpg.connect(ADMIN_DSN)
    await admin.execute("drop database if exists dropbeats_test with (force)")
    await admin.execute("create database dropbeats_test")
    await admin.close()

    conn = await asyncpg.connect(TEST_DSN)
    # Role first: 002_roles_rls.sql grants TO dropbeats_app, so applying
    # migrations before the role exists fails with "role does not exist".
    await migrate.ensure_app_role(conn, "test_password")
    await migrate.apply(conn, migrations_dir)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def client(migrated_conn, monkeypatch):
    """An httpx client bound to a FastAPI app carrying only the licence router.

    main.py is not imported: it constructs a YTMusic client at import time and
    would make these tests depend on YouTube being reachable.
    """
    import httpx
    from fastapi import FastAPI

    import db
    import license as license_module

    monkeypatch.setenv("GUMROAD_SELLER_ID", "test-seller")
    monkeypatch.setenv("GUMROAD_WEBHOOK_SECRET", "test-secret")

    # The limiters are module globals, so their buckets outlive a test and
    # would otherwise leak spent tokens into whatever runs next -- a surprise
    # 429 in an unrelated test, appearing only once the suite grows past the
    # capacity. Every test starts with a full bucket.
    for limiter in (
        license_module._validate_limiter,
        license_module._deactivate_limiter,
        license_module._onboarding_limiter,
    ):
        limiter.reset()

    await db.close_pool()
    # Connect as dropbeats_app, not as the owner. Production runs under this
    # role, so tests that ran as owner would silently pass while a missing
    # grant broke the deployed service.
    await db.init_pool(APP_DSN)

    app = FastAPI()
    app.include_router(license_module.router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c

    await db.close_pool()


@pytest_asyncio.fixture
async def seeded(migrated_conn):
    """One active and one deactivated licence."""
    await migrated_conn.execute(
        """
        insert into licenses (email, full_name, country, license_key, sale_id, is_active)
        values ('active@example.com', 'Active User', 'IN', 'AAAA-BBBB', 'sale_1', true),
               ('gone@example.com',   'Gone User',   'US', 'CCCC-DDDD', 'sale_2', false)
        """
    )
