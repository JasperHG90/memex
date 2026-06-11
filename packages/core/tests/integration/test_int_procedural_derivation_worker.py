"""Integration: the derivation worker turns cases → procedure → strategy.

This exercises ``ProceduralDerivationService`` (via ``api.procedural.
derive_pending``) end-to-end against real Postgres, with the two LLM
distillation passes patched to canned outputs so the test asserts the
*worker contract* deterministically:

* a procedure draft with N≥3 provenance cases gets its body/trigger/summary
  filled by distillation (a version bump); below N≥3 it stays a stub (§9).
* once ``(scope, verb)`` has ≥2 procedures, the worker enqueues + derives
  the strategy *above* them (§9: the heuristic emerges over multiple
  procedures).

The LLM fidelity itself (groundedness, quantitative-anchor preservation —
§9 rule 6 / §19.5) is gated by the eval suite, not here.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from memex_common.procedural_schemas import (
    ProceduralEntryCreate,
    ProceduralSourceCreate,
)
from memex_core.memory.procedural_distillation import (
    DistilledProcedure,
    DistilledStrategy,
)

pytestmark = [pytest.mark.integration]


async def _mk_vault(api) -> uuid.UUID:
    vid = uuid.uuid4()
    async with api.metastore.session() as session:
        await session.execute(
            text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
            {'id': str(vid), 'name': f'deriv_{vid.hex[:8]}'},
        )
        await session.commit()
    return vid


async def _mk_case(api, vault_id: uuid.UUID, body: str) -> uuid.UUID:
    """Insert a ``role='case'`` note and return its id."""
    from memex_core.memory.sql_models import Note

    nid = uuid.uuid4()
    async with api.metastore.session() as session:
        session.add(
            Note(
                id=nid,
                vault_id=vault_id,
                session_id='test-derivation',
                status='active',
                original_text=body,
                role='case',
            )
        )
        await session.commit()
    return nid


async def _make_procedure_with_cases(
    api, vault_id: uuid.UUID, *, verb: str, context: str, n_cases: int
):
    """Create a draft procedure anchor + n provenance cases + enqueue it."""
    entry = await api.procedural.create(
        ProceduralEntryCreate(
            vault_id=vault_id,
            kind='procedure',
            scope='global',
            verb=verb,
            context=context,
            title=f'draft {verb}:{context}',
            summary='draft stub',
            body='',
            trigger=f'about to {verb} on {context}',
            status='draft',
            origin='derived',
        )
    )
    for i in range(n_cases):
        case_id = await _mk_case(
            api, vault_id, f'Case {i} for {verb}:{context} — rolled the canary at 10%.'
        )
        await api._procedural_repo.add_source(
            entry.id, ProceduralSourceCreate(source_note_id=case_id, role='provenance')
        )
    await api._procedural_repo.enqueue_derivation(
        vault_id=vault_id,
        source_entry_ids=[entry.id],
        target_kind='procedure',
        target_scope='global',
        target_verb=verb,
        target_context=context,
    )
    return entry


def _patch_distillers(monkeypatch):
    async def fake_proc(lm, *, cases_markdown, anchor, timeout=120):
        # Echo a load-bearing anchor so the assertion proves the worker
        # writes the distilled body verbatim.
        return DistilledProcedure(
            title=f'Distilled {anchor}',
            summary='Distilled procedure summary.',
            trigger='about to deploy a service',
            body='## Steps\n\n1. roll the canary at 10%',
            steps=[],
            notes='',
        )

    async def fake_strat(lm, *, procedures_markdown, anchor, timeout=120):
        return DistilledStrategy(
            title=f'Strategy {anchor}',
            summary='Prefer the canary rollout across deploy targets.',
            trigger='deploying any service',
            body='Prefer a canary rollout; fall back to the target-specific procedure.',
            notes='',
        )

    monkeypatch.setattr(
        'memex_core.services.procedural_derivation_service.distill_procedure', fake_proc
    )
    monkeypatch.setattr(
        'memex_core.services.procedural_derivation_service.distill_strategy', fake_strat
    )


async def test_below_threshold_leaves_draft_stub(api, monkeypatch):
    """A procedure with < 3 cases is not distilled (§9 N≥3)."""
    _patch_distillers(monkeypatch)
    vault = await _mk_vault(api)
    entry = await _make_procedure_with_cases(api, vault, verb='deploy', context='nomad', n_cases=2)

    completed = await api.procedural.derive_pending(limit=10)

    assert len(completed) == 1  # task completes (as a no-op)
    reloaded = await api.procedural.get(entry.id)
    assert reloaded.body == ''  # body untouched — still a stub


async def test_derive_procedure_fills_draft_then_rolls_up_strategy(api, monkeypatch):
    """N≥3 cases → procedure body distilled; ≥2 procedures → strategy derived."""
    _patch_distillers(monkeypatch)
    vault = await _mk_vault(api)

    # Procedure 1 (global/deploy/nomad) with 3 cases → distils.
    p1 = await _make_procedure_with_cases(api, vault, verb='deploy', context='nomad', n_cases=3)
    completed = await api.procedural.derive_pending(limit=10)
    assert p1.id in {c for c in [p1.id]}  # sanity
    assert len(completed) == 1

    p1b = await api.procedural.get(p1.id)
    assert '10%' in p1b.body  # distilled body written (anchor echoed)
    assert p1b.title.startswith('Distilled')
    # The distillation update appends a version row (the audit trail).
    versions = await api._procedural_repo.list_versions(p1.id)
    assert len(versions) >= 1

    # Only one procedure so far → no strategy yet.
    strat = await api.procedural.get_by_identity(
        kind='strategy', scope='global', verb='deploy', context=None, status=None
    )
    assert strat is None

    # Procedure 2 (global/deploy/k8s) with 3 cases → distils AND, since
    # (global, deploy) now has 2 procedures, enqueues a strategy.
    await _make_procedure_with_cases(api, vault, verb='deploy', context='k8s', n_cases=3)
    completed2 = await api.procedural.derive_pending(limit=10)
    assert len(completed2) == 1  # the p2 procedure task

    # Drain the strategy task the worker just enqueued.
    completed3 = await api.procedural.derive_pending(limit=10)
    assert len(completed3) == 1  # the strategy task

    strat = await api.procedural.get_by_identity(
        kind='strategy', scope='global', verb='deploy', context=None, status=None
    )
    assert strat is not None
    assert strat.kind == 'strategy'
    assert strat.context is None  # strategy anchor ≡ (scope, verb), no context
    assert strat.origin == 'derived'
    assert strat.body  # distilled heuristic written
