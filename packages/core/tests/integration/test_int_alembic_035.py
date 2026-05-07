"""Integration coverage for ``035_backfill_findings``.

Migration 035 backfills ``maintenance_proposals`` rows from pre-existing
``contradicts`` ``memory_links``. The runtime contradiction engine emits
the same shape via ``_sanitise_evidence_text`` in ``contradiction/engine.py``;
this test seeds varied pre-existing reasoning payloads (long, controls,
whitespace, duplicates) before running the migration and asserts the
resulting JSONB matches the runtime sanitisation contract end-to-end.
"""

from __future__ import annotations

from typing import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from _alembic_test_helpers import (  # noqa: F401
    alembic_upgrade as _alembic_upgrade,
    make_fresh_db,
)

pytestmark = [pytest.mark.integration]


_TARGET = '035_backfill_findings'
_PRE = '034_add_mw_mode'


@pytest_asyncio.fixture
async def fresh_db_url(postgres_container: PostgresContainer) -> AsyncGenerator[str, None]:
    async for url in make_fresh_db(postgres_container, db_prefix='mig035'):
        yield url


async def _seed_pre_035(engine_url: str) -> dict[str, str]:
    """Seed one vault, five memory units, and five contradicts links covering:

    * short clean reasoning  → expect echoed verbatim
    * 1500-char reasoning    → expect truncated to 999 chars + ``…``
    * reasoning with C0 / DEL / C1 controls + leading whitespace
                              → expect stripped + trimmed
    * empty-string reasoning → expect JSONB ``null``
    * duplicate to_unit_id   → expect deduped (one finding for the pair)
    """
    vault_id = str(uuid4())
    units = {f'u{i}': str(uuid4()) for i in range(1, 7)}

    long_reasoning = 'A' * 1500
    # Embed C0 controls (\x01), DEL (\x7F), and a C1 byte (\x9F). Tab/newline
    # MUST survive — they're explicitly allow-listed by the regex.
    dirty_reasoning = '   \tkeep tab\nkeep newline\x01drop\x7fdrop' + chr(0x9F) + 'drop  '
    title_with_controls = '\x01Real title' + chr(0x9F) + '   '

    engine = create_async_engine(engine_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO vaults (id, name, mw_mode) VALUES (:id, :name, 'stationary')"),
                {'id': vault_id, 'name': f'mig035-{vault_id[:8]}'},
            )
            for uid in units.values():
                await conn.execute(
                    text(
                        'INSERT INTO memory_units '
                        '(id, vault_id, text, fact_type, event_date) '
                        "VALUES (:id, :vault_id, 'fact text', 'world', NOW())"
                    ),
                    {'id': uid, 'vault_id': vault_id},
                )
            # link 1: short clean reasoning → unit u1 supersedes u2
            await conn.execute(
                text(
                    'INSERT INTO memory_links '
                    '(from_unit_id, to_unit_id, vault_id, link_type, link_metadata) '
                    "VALUES (:f, :t, :v, 'contradicts', "
                    "  jsonb_build_object('reasoning', 'short clean', "
                    "                     'superseding_note_title', 'Title One', "
                    "                     'authoritative_unit_id', CAST(:auth AS text)))"
                ),
                {'f': units['u1'], 't': units['u2'], 'v': vault_id, 'auth': units['u1']},
            )
            # link 2: long reasoning → u3 supersedes u4
            await conn.execute(
                text(
                    'INSERT INTO memory_links '
                    '(from_unit_id, to_unit_id, vault_id, link_type, link_metadata) '
                    "VALUES (:f, :t, :v, 'contradicts', "
                    "  jsonb_build_object('reasoning', CAST(:reasoning AS text)))"
                ),
                {
                    'f': units['u3'],
                    't': units['u4'],
                    'v': vault_id,
                    'reasoning': long_reasoning,
                },
            )
            # link 3: control chars + whitespace → u5 supersedes u6
            await conn.execute(
                text(
                    'INSERT INTO memory_links '
                    '(from_unit_id, to_unit_id, vault_id, link_type, link_metadata) '
                    "VALUES (:f, :t, :v, 'contradicts', "
                    "  jsonb_build_object('reasoning', CAST(:reasoning AS text), "
                    "                     'superseding_note_title', CAST(:title AS text)))"
                ),
                {
                    'f': units['u5'],
                    't': units['u6'],
                    'v': vault_id,
                    'reasoning': dirty_reasoning,
                    'title': title_with_controls,
                },
            )
            # link 4: duplicate of link 1 (same to_unit_id, different from)
            # → migration must dedup to one finding row, not raise
            # cardinality_violation on ON CONFLICT.
            await conn.execute(
                text(
                    'INSERT INTO memory_links '
                    '(from_unit_id, to_unit_id, vault_id, link_type, link_metadata) '
                    "VALUES (:f, :t, :v, 'contradicts', "
                    "  jsonb_build_object('reasoning', 'duplicate from'))"
                ),
                {'f': units['u3'], 't': units['u2'], 'v': vault_id},
            )
            # link 5: empty-string reasoning → JSONB null in output
            await conn.execute(
                text(
                    'INSERT INTO memory_links '
                    '(from_unit_id, to_unit_id, vault_id, link_type, link_metadata) '
                    "VALUES (:f, :t, :v, 'contradicts', "
                    "  jsonb_build_object('reasoning', '   '))"
                ),
                {'f': units['u1'], 't': units['u5'], 'v': vault_id},
            )
    finally:
        await engine.dispose()

    return {'vault_id': vault_id, **units}


@pytest.mark.asyncio
async def test_035_backfill_truncates_with_ellipsis_and_strips_controls(
    fresh_db_url: str,
) -> None:
    """End-to-end: seed varied ``contradicts`` links → migrate → verify
    each finding's ``evidence`` JSONB matches the runtime sanitisation
    contract (ellipsis truncation, control stripping, NULL on empty,
    dedup on duplicate ``to_unit_id``).
    """
    await _alembic_upgrade(fresh_db_url, target=_PRE)
    seed = await _seed_pre_035(fresh_db_url)
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        'SELECT target_id, evidence '
                        'FROM maintenance_proposals '
                        "WHERE rule_name = 'semantic_contradiction' AND vault_id = :v "
                        'ORDER BY target_id'
                    ),
                    {'v': seed['vault_id']},
                )
            ).all()

            by_target = {str(r[0]): r[1] for r in rows}

            # Dedup: link 1 (u1→u2) + link 4 (u3→u2) collapse to one
            # finding for target u2 — not two (duplicate target_ids inside
            # a single ON CONFLICT INSERT raises cardinality_violation).
            assert seed['u2'] in by_target
            assert seed['u4'] in by_target
            assert seed['u6'] in by_target
            assert seed['u5'] in by_target
            # 4 distinct targets → 4 findings, even though 5 links seeded.
            assert len(by_target) == 4, f'expected 4 deduped findings, got {len(by_target)}'

            # Dedup winner depends on ``ORDER BY ... from_unit_id`` (smallest
            # UUID wins) — both inputs were valid clean strings, so accept
            # either. The point of this assertion is that exactly one row
            # exists and its evidence reflects ONE of the two seeded links.
            ev_u2 = by_target[seed['u2']]
            assert ev_u2['reasoning'] in ('short clean', 'duplicate from'), (
                f'unexpected reasoning after dedup: {ev_u2["reasoning"]!r}'
            )
            if ev_u2['reasoning'] == 'short clean':
                assert ev_u2['superseding_note_title'] == 'Title One'
                assert ev_u2['authoritative_unit_id'] == seed['u1']
            else:
                # link 4 set no superseding_note_title, no authoritative_unit_id
                # → title null, authoritative falls back to from_unit_id (u3).
                assert ev_u2['superseding_note_title'] is None
                assert ev_u2['authoritative_unit_id'] == seed['u3']
            assert ev_u2['superseded_unit_id'] == seed['u2']
            assert ev_u2['backfilled'] is True

            # Long reasoning → 999 chars + '…', total 1000 chars (the cap).
            ev_u4 = by_target[seed['u4']]
            assert len(ev_u4['reasoning']) == 1000
            assert ev_u4['reasoning'].endswith('…')
            assert ev_u4['reasoning'][:-1] == 'A' * 999
            # Authoritative defaults to from_unit_id when unset.
            assert ev_u4['authoritative_unit_id'] == seed['u3']

            # Control chars stripped, tab + newline preserved, edge whitespace trimmed.
            ev_u6 = by_target[seed['u6']]
            assert ev_u6['reasoning'] == 'keep tab\nkeep newline\x01drop\x7fdropdrop'.replace(
                '\x01', ''
            ).replace('\x7f', '').replace('', '')
            assert ev_u6['reasoning'].startswith('\tkeep tab') or ev_u6['reasoning'].startswith(
                'keep tab'
            )
            assert '\n' in ev_u6['reasoning']
            # No surviving controls / DEL / C1.
            assert all(
                ord(ch) >= 0x20 and ord(ch) != 0x7F and not (0x80 <= ord(ch) <= 0x9F)
                for ch in ev_u6['reasoning']
                if ch not in ('\n', '\t')
            )
            # Title stripped of controls + edge whitespace, no trailing spaces.
            assert ev_u6['superseding_note_title'] == 'Real title'

            # Empty / whitespace-only reasoning → JSONB null (not '').
            ev_u5 = by_target[seed['u5']]
            assert ev_u5['reasoning'] is None, (
                f'expected null for whitespace-only reasoning, got {ev_u5["reasoning"]!r}'
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_035_backfill_is_idempotent(fresh_db_url: str) -> None:
    """Re-running the migration's INSERT must not duplicate rows.

    The partial unique index from migration 025 plus ``ON CONFLICT DO NOTHING``
    is what makes a re-run safe; this test exercises it explicitly so a
    future edit that drops either guard fails loudly. We can't run alembic
    upgrade twice (it's a no-op past head), so we extract the migration's
    INSERT statement and execute it again directly.
    """
    await _alembic_upgrade(fresh_db_url, target=_PRE)
    seed = await _seed_pre_035(fresh_db_url)
    await _alembic_upgrade(fresh_db_url, target=_TARGET)

    # Import the migration module to grab its INSERT SQL — the same statement
    # the upgrade() ran moments ago.
    import importlib.util
    import pathlib as plb

    import memex_core

    migration_path = (
        plb.Path(memex_core.__file__).resolve().parent
        / 'alembic'
        / 'versions'
        / '035_backfill_findings.py'
    )
    spec = importlib.util.spec_from_file_location('mig035', migration_path)
    assert spec is not None and spec.loader is not None
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    engine = create_async_engine(fresh_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            count_before = (
                await conn.execute(
                    text(
                        'SELECT COUNT(*) FROM maintenance_proposals '
                        "WHERE rule_name = 'semantic_contradiction' AND vault_id = :v"
                    ),
                    {'v': seed['vault_id']},
                )
            ).scalar()

        async with engine.begin() as conn:
            await conn.execute(mig._BACKFILL_SQL)

        async with engine.connect() as conn:
            count_after = (
                await conn.execute(
                    text(
                        'SELECT COUNT(*) FROM maintenance_proposals '
                        "WHERE rule_name = 'semantic_contradiction' AND vault_id = :v"
                    ),
                    {'v': seed['vault_id']},
                )
            ).scalar()

        assert count_before == count_after, (
            f're-run produced {count_after - count_before} duplicate findings — '
            'ON CONFLICT DO NOTHING regression?'
        )
        # Sanity: there were rows to begin with, so the test is meaningful.
        assert count_before is not None and count_before > 0
    finally:
        await engine.dispose()
