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
