#!/usr/bin/env python
"""Rebuild ``entity_cooccurrences`` per-vault from ground truth.

Migration ``052_entity_cooccurrence_vault_pk`` changed the cooccurrence grain
to ``(entity_id_1, entity_id_2, vault_id)`` and rebuilds the table on upgrade.
This is the standalone operational equivalent: an operator can re-derive the
table from the source of truth (``unit_entities`` ⋈ ``memory_units``) at any
time — e.g. after a bulk import, a suspected drift, or to repair a DB that
carried the old cross-vault-summed rows — without running a full Alembic cycle.

The cooccurrence count for a pair in a vault is the number of distinct units in
that vault that co-mention both entities (the ingest-time semantics). Canonical
ordering ``entity_id_1 < entity_id_2`` is enforced by the self-join, matching
the table CHECK constraint.

Defaults to a DRY RUN (reports current vs rebuilt row counts, writes nothing).
Pass ``--apply`` to perform the rebuild inside a single transaction (TRUNCATE +
repopulate; rolls back atomically on error). On a large corpus the self-join is
heavy and takes an ACCESS EXCLUSIVE lock for its duration — run in a maintenance
window.

Usage:
    uv run python scripts/rebuild_entity_cooccurrences.py            # dry run
    uv run python scripts/rebuild_entity_cooccurrences.py --apply
    uv run python scripts/rebuild_entity_cooccurrences.py --dsn postgresql://... --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg
from sqlalchemy.engine.url import make_url

# Per-vault rebuild SELECT — identical semantics to migration 052's
# _REBUILD_PER_VAULT (count = distinct units in the vault co-mentioning the pair).
_REBUILD_SELECT = """
SELECT a.entity_id, b.entity_id, mu.vault_id,
       COUNT(DISTINCT a.unit_id), now(), MIN(mu.event_date)
FROM unit_entities a
JOIN unit_entities b ON a.unit_id = b.unit_id AND a.entity_id < b.entity_id
JOIN memory_units mu ON mu.id = a.unit_id
GROUP BY a.entity_id, b.entity_id, mu.vault_id
"""

_REBUILD_SQL = (
    'INSERT INTO entity_cooccurrences '
    '(entity_id_1, entity_id_2, vault_id, cooccurrence_count, last_cooccurred, valid_from)\n'
    + _REBUILD_SELECT
)

_COUNT_REBUILD_SQL = f'SELECT COUNT(*) FROM ({_REBUILD_SELECT}) t'


def _resolve_dsn(explicit: str | None) -> str:
    """Return a plain ``postgresql://`` DSN (asyncpg wants no +driver suffix)."""
    if explicit:
        raw = explicit
    else:
        # Reuse the same config the server resolves (YAML + env).
        from memex_common.config import MemexConfig

        raw = MemexConfig().server.meta_store.instance.connection_string
    return make_url(raw).set(drivername='postgresql').render_as_string(hide_password=False)


async def _run(dsn: str, apply: bool) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        current = await conn.fetchval('SELECT COUNT(*) FROM entity_cooccurrences')
        rebuilt = await conn.fetchval(_COUNT_REBUILD_SQL)
        print(f'entity_cooccurrences: current rows = {current:,}; rebuilt rows = {rebuilt:,}')

        if not apply:
            delta = rebuilt - current
            print(
                f'DRY RUN — no changes made. Rebuild would result in {rebuilt:,} rows '
                f'({delta:+,} vs current). Re-run with --apply to perform it.'
            )
            return 0

        print('Applying rebuild (TRUNCATE + repopulate) in a single transaction...')
        async with conn.transaction():
            await conn.execute('TRUNCATE entity_cooccurrences')
            await conn.execute(_REBUILD_SQL)
            final = await conn.fetchval('SELECT COUNT(*) FROM entity_cooccurrences')
        print(f'Done. entity_cooccurrences now has {final:,} rows.')
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--dsn',
        default=None,
        help='Postgres DSN. Defaults to the server config (YAML + env).',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Perform the rebuild. Without this flag the script only reports (dry run).',
    )
    args = parser.parse_args()

    try:
        dsn = _resolve_dsn(args.dsn)
    except Exception as exc:  # noqa: BLE001 - surface config errors plainly
        print(f'Failed to resolve DSN: {exc}', file=sys.stderr)
        return 2

    return asyncio.run(_run(dsn, apply=args.apply))


if __name__ == '__main__':
    raise SystemExit(main())
