"""eval_import_state table — tracks V12 snapshot imports.

Owned by ``memex_eval``. Lives outside the alembic chain because it is
eval-only state — production servers don't need it on disk. The eval
runner calls ``ensure_eval_import_state_table`` against its DB
connection before invoking ``SnapshotImporter``; the DDL is idempotent
so concurrent eval processes don't race.

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

Import is only "done" once ``state='complete'``. A sweep helper deletes
rows in any non-complete state older than a configurable threshold.
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

EVAL_IMPORT_STATE_DDL_STATEMENTS = (
    EVAL_IMPORT_STATE_DDL_TABLE,
    EVAL_IMPORT_STATE_DDL_INDEX,
)


VALID_STATES = ('staging', 'db_committed', 'assets_committed', 'embedded', 'complete')


async def ensure_eval_import_state_table(conn: AsyncConnection) -> None:
    """Idempotently create the eval_import_state table + unique index.

    Called by the eval runner before its first ``SnapshotImporter`` run
    against a given DB.
    """
    await conn.execute(text(EVAL_IMPORT_STATE_DDL_TABLE))
    await conn.execute(text(EVAL_IMPORT_STATE_DDL_INDEX))
