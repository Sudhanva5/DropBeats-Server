"""asyncpg pool lifecycle. Imported only when DATABASE_URL is set.

Kept separate from license.py so that the bundled macOS build never pulls a
database driver into a process that has no database.
"""

import asyncio
import os

import asyncpg

_pool: asyncpg.Pool | None = None

# Guards pool creation. Startup init is non-fatal, so the first requests after
# the database recovers can arrive concurrently, all seeing _pool is None; the
# lock stops them from each creating a pool and leaking all but one.
_pool_lock = asyncio.Lock()


async def init_pool(dsn: str | None = None) -> asyncpg.Pool:
    """Create the pool if it does not exist yet, and return it.

    The sole home of the connection settings: acquire_pool() delegates here so
    the two entry points cannot drift apart.
    """
    global _pool
    if _pool is None:
        async with _pool_lock:
            # Re-checked under the lock: another coroutine may have created the
            # pool while this one was waiting for it.
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    dsn or os.environ["DATABASE_URL"],
                    min_size=1,
                    max_size=5,
                    command_timeout=10,
                )
    return _pool


async def acquire_pool() -> asyncpg.Pool:
    """Return the pool, initialising it if a previous attempt failed.

    Startup init is deliberately non-fatal so a database problem cannot take
    /search down with it. That leaves a process that would otherwise stay
    healthy forever with no pool, so the first request after the database
    comes back is what heals it.

    If the database is still down this raises, and the calling endpoint fails
    -- which is the correct outcome, and no worse than before.
    """
    return await init_pool()


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the pool, or raise if it was never initialised.

    Retained for callers that must not trigger a connection attempt. Request
    handlers should use acquire_pool() so they can self-heal.
    """
    if _pool is None:
        raise RuntimeError("database pool is not initialised")
    return _pool
