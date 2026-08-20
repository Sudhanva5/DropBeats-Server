"""Applies numbered .sql files once each, tracked in schema_migrations.

Deliberately tiny. Alembic would bring a migration DSL, autogeneration and a
config file for what is two tables that change roughly never.
"""

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def apply(conn: asyncpg.Connection, migrations_dir: Path) -> list[str]:
    """Apply every unapplied migration in filename order. Returns their names."""
    await conn.execute(
        """
        create table if not exists schema_migrations (
          name       text primary key,
          applied_at timestamptz not null default now()
        )
        """
    )
    done = {r["name"] for r in await conn.fetch("select name from schema_migrations")}

    applied = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in done:
            continue
        # Each migration runs in its own transaction: a failure half way
        # through leaves the database on the last good migration rather than
        # in an undefined state.
        async with conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute(
                "insert into schema_migrations (name) values ($1)", path.name
            )
        applied.append(path.name)
    return applied


async def ensure_app_role(conn: asyncpg.Connection, password: str) -> None:
    """Create or update the dropbeats_app login role.

    Kept out of the .sql files because the password comes from the
    environment and must never be committed.
    """
    exists = await conn.fetchval(
        "select 1 from pg_roles where rolname = 'dropbeats_app'"
    )
    quoted = quote_literal(password)
    if exists:
        await conn.execute(f"alter role dropbeats_app with login password {quoted}")
    else:
        await conn.execute(f"create role dropbeats_app with login password {quoted}")


def quote_literal(value: str) -> str:
    """Postgres single-quoted literal. Doubling quotes is the escape."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


async def main() -> int:
    # Migrations run as the OWNER, not as the application role. On Railway
    # DATABASE_URL is deliberately the least-privilege dropbeats_app DSN, which
    # owns nothing and cannot do DDL, so running migrations through it would
    # fail. ADMIN_DATABASE_URL carries the superuser DSN. The fallback keeps
    # local development working, where the developer is already superuser and
    # only sets DATABASE_URL.
    dsn = os.environ.get("ADMIN_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "neither ADMIN_DATABASE_URL nor DATABASE_URL is set",
            file=sys.stderr,
        )
        return 1

    password = os.environ.get("APP_DB_PASSWORD")
    if not password:
        # Checked before connecting, because the failure it prevents is
        # ugly: 002_roles_rls.sql grants TO dropbeats_app, so skipping the
        # role and pressing on dies with "role does not exist" only after
        # 001 has committed and been recorded as applied.
        print(
            "APP_DB_PASSWORD is not set. It is required: the role it creates "
            "(dropbeats_app) is granted to by 002_roles_rls.sql, so migrating "
            "without it fails part-way through.",
            file=sys.stderr,
        )
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        await ensure_app_role(conn, password)
        applied = await apply(conn, MIGRATIONS_DIR)
    finally:
        await conn.close()

    print(f"applied: {applied}" if applied else "already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
