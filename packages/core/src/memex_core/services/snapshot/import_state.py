"""eval_import_state table — eval-only tracking for V12 imports.

Lives outside the alembic chain on purpose: this table only exists on
servers running with ``server.eval_mode=True``. Production servers never
see it. Idempotent ``CREATE TABLE IF NOT EXISTS`` runs at server startup
when eval-mode is on.

Columns:

- ``target_vault_id`` (PK): the new vault UUID allocated at import-start.
- ``import_id``: opaque UUID generated alongside ``target_vault_id``;
  identifies the specific import attempt for retry/sweep semantics.
- ``source_snapshot_path``: absolute path to the snapshot dir.
- ``state``: lifecycle state of the import; transitions through phases.
- ``updated_at``: last state transition timestamp.

State machine:

::

    staging -> db_committed -> assets_committed -> embedded -> complete

Import is only "done" once ``state='complete'``. Sweep deletes rows in any
non-complete state older than a configurable threshold.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy import text

EVAL_IMPORT_STATE_DDL_TABLE = """
CREATE TABLE IF NOT EXISTS eval_import_state (
    target_vault_id UUID PRIMARY KEY,
    import_id UUID NOT NULL,
    source_snapshot_path TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT eval_import_state_state_check CHECK (
        state IN ('staging', 'db_committed', 'assets_committed', 'embedded', 'complete')
    )
)
"""

EVAL_IMPORT_STATE_DDL_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS eval_import_state_source_path_unique
    ON eval_import_state (source_snapshot_path)
"""

# Back-compat: keep the old constant name as a list of statements.
EVAL_IMPORT_STATE_DDL = (EVAL_IMPORT_STATE_DDL_TABLE, EVAL_IMPORT_STATE_DDL_INDEX)


VALID_STATES = ('staging', 'db_committed', 'assets_committed', 'embedded', 'complete')


async def ensure_eval_import_state_table(conn: AsyncConnection) -> None:
    """Idempotently create the eval_import_state table + unique index.

    Called from the server lifespan startup hook when ``eval_mode=True``.
    """
    await conn.execute(text(EVAL_IMPORT_STATE_DDL_TABLE))
    await conn.execute(text(EVAL_IMPORT_STATE_DDL_INDEX))
