"""Integration tests for InboxRouterService against real Postgres.

The router tables + views are SQLModel models, so the shared ``create_all``
harness provisions them like any other table; the NB prior is seeded by the
service (``ensure_prior_seeded`` via ``refresh_anchors``). Distinct per-vault
chunk embeddings give a real ranking signal (the mock embedding model returns a
constant vector for narratives).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from memex_core.memory.sql_models import Chunk, ContentStatus, Note, Vault
from memex_core.services.inbox_router.service import ROUTE_RULE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Two orthogonal embedding directions so cosine cleanly separates the vaults.
_VEC_A = [1.0] * 192 + [0.0] * 192
_VEC_B = [0.0] * 192 + [1.0] * 192


async def _seed_vault_with_note(session, name: str, embedding: list[float]) -> Vault:
    vault = Vault(id=uuid4(), name=name, description=f'{name} vault')
    session.add(vault)
    # autoflush is off on the integration session and ORM batched inserts can
    # reorder across classes (SA_UUID + asyncpg insertmanyvalues), so flush the
    # vault before the note/chunk that FK to it — otherwise notes_vault_id_fkey.
    await session.flush()
    note_id = uuid4()
    session.add(
        Note(
            id=note_id,
            vault_id=vault.id,
            original_text=f'note {uuid4().hex}',
            content_hash=uuid4().hex,
            title=f'{name} note',
        )
    )
    await session.flush()
    session.add(
        Chunk(
            id=uuid4(),
            vault_id=vault.id,
            note_id=note_id,
            text=f'{name} body content alpha beta',
            content_hash=uuid4().hex,
            embedding=embedding,
            chunk_index=0,
            status=ContentStatus.ACTIVE,
        )
    )
    await session.commit()
    return vault


async def _seed_empty_vault(session, name: str) -> Vault:
    """Create a vault with no notes — used for the inbox itself, whose only
    notes must be the ones seeded explicitly via _seed_inbox_note (otherwise a
    stray note inflates the triage scored-count)."""
    vault = Vault(id=uuid4(), name=name, description=f'{name} vault')
    session.add(vault)
    await session.flush()
    return vault


async def _seed_inbox_note(session, inbox: Vault, embedding: list[float]) -> UUID:
    note_id = uuid4()
    session.add(
        Note(
            id=note_id,
            vault_id=inbox.id,
            original_text=f'inbox note {uuid4().hex}',
            content_hash=uuid4().hex,
            title='inbox note',
        )
    )
    # Flush the note before the chunk that FKs to it (see _seed_vault_with_note).
    await session.flush()
    session.add(
        Chunk(
            id=uuid4(),
            vault_id=inbox.id,
            note_id=note_id,
            text='inbox body content alpha beta',
            content_hash=uuid4().hex,
            embedding=embedding,
            chunk_index=0,
            status=ContentStatus.ACTIVE,
        )
    )
    await session.commit()
    return note_id


async def test_score_ranks_topically_closest_vault_first(api, session):
    """An inbox note embedded like vault-a should rank vault-a above vault-b."""
    inbox = await _seed_empty_vault(session, 'inbox')
    await _seed_vault_with_note(session, 'vault-a', _VEC_A)
    await _seed_vault_with_note(session, 'vault-b', _VEC_B)
    note_id = await _seed_inbox_note(session, inbox, _VEC_A)

    await api.inbox_router.refresh_anchors()
    await api.inbox_router.populate_note_cache(note_id)
    scored = await api.inbox_router.score_notes([note_id])

    cands = scored[note_id]
    names = [c.vault_name for c in sorted(cands, key=lambda c: -c.p_match)]
    assert 'vault-a' in names and 'vault-b' in names
    # vault-a shares the note's embedding direction; it must outrank vault-b.
    assert names.index('vault-a') < names.index('vault-b')


async def test_triage_tick_emits_routing_proposal(api, session):
    """A full tick scores the inbox note and records a routing proposal."""
    inbox = await _seed_empty_vault(session, 'inbox')
    await _seed_vault_with_note(session, 'vault-a', _VEC_A)
    await _seed_vault_with_note(session, 'vault-b', _VEC_B)
    note_id = await _seed_inbox_note(session, inbox, _VEC_A)

    result = await api.inbox_router.triage_tick()
    assert result.scored == 1
    assert result.errors == 0

    async with api.metastore.session() as s:
        n = (
            await s.execute(
                text(
                    'SELECT COUNT(*) FROM maintenance_proposals '
                    "WHERE lint_type = 'routing' AND target_id = :tid"
                ),
                {'tid': str(note_id)},
            )
        ).scalar()
    # Cold start (seed match-count=1 < 50) → a pending route proposal, not auto-route.
    assert n >= 1


async def test_blocked_backoff_counts_suppressed_no_fit(api, session):
    """A no-fit emission suppressed by the backoff window (``_emit_no_fit``
    returns False) is counted in ``blocked_backoff`` — not silently dropped."""
    from unittest.mock import AsyncMock

    inbox = await _seed_empty_vault(session, 'inbox')
    await _seed_vault_with_note(session, 'vault-a', _VEC_A)
    await _seed_inbox_note(session, inbox, _VEC_A)

    # Force PROPOSE_NO_FIT regardless of the cold-start score.
    api.config.server.memory.inbox_router.t_low = 0.99
    # Simulate a not-yet-due backoff window: the upsert lands 0 rows.
    api.inbox_router._emit_no_fit = AsyncMock(return_value=False)

    result = await api.inbox_router.triage_tick()
    assert result.scored == 1
    assert result.no_fit == 0
    assert result.blocked_backoff == 1
    assert result.errors == 0


async def test_reproposal_cooldown_blocks_then_bootstrap_unblocks(api, session):
    """A recently-dismissed route is not re-proposed (counted, not silently
    dropped); setting the cooldown to 0 re-evaluates it immediately."""
    inbox = await _seed_empty_vault(session, 'inbox')
    await _seed_vault_with_note(session, 'vault-a', _VEC_A)
    await _seed_vault_with_note(session, 'vault-b', _VEC_B)
    note_id = await _seed_inbox_note(session, inbox, _VEC_A)

    cfg = api.config.server.memory.inbox_router
    # Drop the no-fit floor so the note routes (PROPOSE_CANDIDATES) rather than
    # falling to no-fit under the cold-start NB scores — this test is about the
    # route re-proposal cooldown specifically.
    cfg.t_low = 0.0

    # First tick emits a pending route proposal; dismiss it (status flip +
    # resolved_at=now) so the note is eligible again but inside the cooldown.
    first = await api.inbox_router.triage_tick()
    assert first.proposed >= 1, f'expected a route proposal on first tick; got {first.as_dict()}'
    async with api.metastore.session() as s:
        await s.execute(
            text(
                "UPDATE maintenance_proposals SET status = 'dismissed', resolved_at = now() "
                "WHERE lint_type = 'routing' AND target_id = :tid"
            ),
            {'tid': str(note_id)},
        )
        await s.commit()

    # Default cooldown (30d) → the re-proposal is suppressed and ACCOUNTED.
    cfg.reproposal_cooldown_days = 30
    blocked = await api.inbox_router.triage_tick()
    assert blocked.proposed == 0
    assert blocked.blocked_cooldown >= 1, (
        f'cooldown-suppressed note must be counted, not dropped; got {blocked.as_dict()}'
    )

    # Bootstrap: cooldown 0 → re-evaluated immediately.
    cfg.reproposal_cooldown_days = 0
    unblocked = await api.inbox_router.triage_tick()
    assert unblocked.proposed >= 1, (
        f'cooldown=0 must re-propose the dismissed note; got {unblocked.as_dict()}'
    )
    assert unblocked.blocked_cooldown == 0


async def test_record_feedback_updates_sufficient_stats(api, session):
    """An online update increments the match class count and a feature's n."""
    inbox = await _seed_empty_vault(session, 'inbox')
    vault_a = await _seed_vault_with_note(session, 'vault-a', _VEC_A)
    note_id = await _seed_inbox_note(session, inbox, _VEC_A)
    await api.inbox_router.refresh_anchors()
    await api.inbox_router.populate_note_cache(note_id)

    async with api.metastore.session() as s:
        before = (
            await s.execute(text('SELECT n FROM inbox_router_nb_class_counts WHERE label = 1'))
        ).scalar()

    await api.inbox_router.record_feedback(note_id, vault_a.id, 1)

    async with api.metastore.session() as s:
        after = (
            await s.execute(text('SELECT n FROM inbox_router_nb_class_counts WHERE label = 1'))
        ).scalar()
    # With EWMA gamma<1: n_after = gamma*n_before + 1.
    gamma = api.config.server.memory.inbox_router.ewma_gamma
    assert after == pytest.approx(gamma * float(before) + 1.0, rel=1e-6)


async def test_ensure_inbox_vault_is_idempotent(api, session):
    """Calling ensure_inbox_vault twice returns the same vault id."""
    first = await api.inbox_router.ensure_inbox_vault()
    second = await api.inbox_router.ensure_inbox_vault()
    assert first is not None
    assert first == second


async def test_daily_cap_falls_through_to_proposal(api, session):
    """When the daily auto-apply budget is exhausted, an otherwise-auto-routable
    note falls through to a proposal (skipped_cap increments, not auto_routed)."""
    inbox = await _seed_empty_vault(session, 'inbox')
    await _seed_vault_with_note(session, 'vault-a', _VEC_A)
    await _seed_vault_with_note(session, 'vault-b', _VEC_B)
    await _seed_inbox_note(session, inbox, _VEC_A)

    # Seed the NB prior FIRST so the UPDATE below targets an existing row.
    # ensure_prior_seeded uses INSERT ... ON CONFLICT DO NOTHING, so a later
    # refresh_anchors call from triage_tick is a no-op and leaves n=100 alone.
    await api.inbox_router.ensure_prior_seeded()

    # Warm up the model and relax the gates so the decision is AUTO_ROUTE...
    async with api.metastore.session() as s:
        await s.execute(text('UPDATE inbox_router_nb_class_counts SET n = 100 WHERE label = 1'))
        await s.commit()
    cfg = api.config.server.memory.inbox_router
    cfg.auto_apply_min_p_match = 0.0
    cfg.t_margin = 0.0
    cfg.t_low = 0.0
    # ...but zero the daily budget so nothing may actually auto-route.
    cfg.max_auto_applies_per_day = 0

    result = await api.inbox_router.triage_tick()
    assert result.errors == 0
    assert result.auto_routed == 0
    assert result.skipped_cap >= 1, (
        f'expected an AUTO_ROUTE capped to a proposal; got {result.as_dict()}'
    )


async def test_route_rule_name_constant():
    assert ROUTE_RULE == 'inbox_vault_route'
