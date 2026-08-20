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
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(dsn)
    try:
        password = os.environ.get("APP_DB_PASSWORD")
        if password:
            await ensure_app_role(conn, password)
        applied = await apply(conn, MIGRATIONS_DIR)
    finally:
        await conn.close()

    print(f"applied: {applied}" if applied else "already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
