import asyncio
import logging
from pathlib import Path

import asyncpg

from app.config import DATABASE_URL

log = logging.getLogger("app.startup")

_pool: asyncpg.Pool | None = None
# On a Docker engine start (a laptop reboot) the app and postgres containers start together, with no ordering
# (restart policies ignore depends_on), and postgres may still be in crash recovery: the pool is retried instead of
# crashing the process into a restart loop. 60 attempts 2 s apart: two minutes.
CONNECT_ATTEMPTS = 60
CONNECT_RETRY_SECONDS = 2.0
# What a postgres still starting raises: refused connections, timeouts, and its own "the database system is
# starting up" (asyncpg.PostgresError; the tests stub asyncpg, hence getattr)
RETRY_ERRORS = (OSError, ConnectionError, asyncio.TimeoutError, getattr(asyncpg, "PostgresError", OSError))


async def init_pool(attempts: int = CONNECT_ATTEMPTS, retry_seconds: float = CONNECT_RETRY_SECONDS,
                    sleep=asyncio.sleep) -> int:
    """Connect (postgres may not be ready yet: retried) and apply sql/init.sql; returns the attempt that connected."""
    global _pool
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, timeout=10)
            break
        except RETRY_ERRORS as error:
            last = error
            log.warning("[STARTUP] database: not ready (attempt %d/%d: %s: %s); retrying in %.0f s", attempt, attempts,
                        type(error).__name__, str(error)[:120] or "no answer", retry_seconds)
            await sleep(retry_seconds)
    else:
        raise RuntimeError(f"postgres did not answer in {attempts} attempts: {type(last).__name__}: {last}")
    sql = (Path(__file__).parent.parent / "sql" / "init.sql").read_text(encoding="utf-8")
    async with _pool.acquire() as conn:
        await conn.execute(sql)
    return attempt


async def close_pool() -> None:
    if _pool:
        await _pool.close()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool
