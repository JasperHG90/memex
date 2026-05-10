"""Direct Postgres access for eval-suite teardowns.

Public memex-core APIs only expose ``+1`` increments on Memory-Worth
counters; they cannot reset to a prior state. Rather than introduce
test-only endpoints in core, the eval framework opens a direct
connection to the same database the server uses and runs cleanup SQL
itself.

Connection config:
- ``MEMEX_EVAL_DATABASE_URL`` env var if set (asyncpg DSN form,
  e.g. ``postgresql://user:pass@host:port/db``)
- Else default to localhost/postgres/postgres on the standard port —
  the developer-machine convention used in the project's local config.

Each call to ``eval_db_session()`` opens a short-lived connection,
yields it, and closes on exit. No pool — teardown SQL is rare and
the cost of a fresh connection per teardown is acceptable next to
the latency of the scenario itself.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

import asyncpg

if TYPE_CHECKING:
    from asyncpg import Connection

logger = logging.getLogger('memex_eval.suite.db_teardown')

_DEFAULT_DSN = 'postgresql://postgres:postgres@localhost:5432/postgres'


def resolve_database_dsn() -> str:
    """Return the asyncpg DSN to connect to. Env override, else default."""
    return os.environ.get('MEMEX_EVAL_DATABASE_URL') or _DEFAULT_DSN


@asynccontextmanager
async def eval_db_session() -> AsyncIterator['Connection']:
    """Yield an asyncpg connection to the memex Postgres instance.

    Caller-scoped lifecycle: opens on enter, closes on exit. Failures
    inside the ``with`` block are NOT swallowed — teardown handlers
    catch their own exceptions and log; surfacing here would mask the
    underlying SQL error from the per-handler logger.
    """
    dsn = resolve_database_dsn()
    conn: Connection | None = None
    try:
        conn = await asyncpg.connect(dsn)
        yield conn
    finally:
        if conn is not None:
            await conn.close()
