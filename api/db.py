"""asyncpg pool lifecycle. Imported only when DATABASE_URL is set.

Kept separate from license.py so that the bundled macOS build never pulls a
database driver into a process that has no database.
"""

import os
from typing import Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


async def init_pool(dsn: Optional[str] = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn or os.environ["DATABASE_URL"],
            min_size=1,
            max_size=5,
            command_timeout=10,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool is not initialised")
    return _pool
