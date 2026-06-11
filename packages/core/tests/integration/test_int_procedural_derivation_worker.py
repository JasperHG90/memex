"""Integration: the derivation worker turns cases → procedure → strategy.

This exercises ``ProceduralDerivationService`` (via ``api.procedural.
derive_pending``) end-to-end against real Postgres, with the two LLM
distillation passes patched to canned outputs so the test asserts the
*worker contract* deterministically:

* a procedure draft with >=1 provenance case gets its body/trigger/summary
  filled by distillation (a version bump) — a SINGLE case is enough
  (§9 amended, JG 2026-06-11); the guard only leaves a stub at 0 cases.
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


async def test_single_case_distills_procedure(api, monkeypatch):
    """A SINGLE case is enough to derive a procedure (§9 amended, JG
    2026-06-11) — the draft body is distilled, not left a stub."""
    _patch_distillers(monkeypatch)
    vault = await _mk_vault(api)
    entry = await _make_procedure_with_cases(api, vault, verb='deploy', context='nomad', n_cases=1)

    completed = await api.procedural.derive_pending(limit=10)

    assert len(completed) == 1
    reloaded = await api.procedural.get(entry.id)
    assert '10%' in reloaded.body  # distilled body written, not a stub
    assert reloaded.title.startswith('Distilled')


async def test_derive_procedure_fills_draft_then_rolls_up_strategy(api, monkeypatch):
    """Cases → procedure body distilled; ≥2 procedures → strategy derived."""
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


async def test_distillation_files_activation_proposal_then_activate_publishes(api, monkeypatch):
    """§18.6.1: a distilled draft is NOT auto-published — derivation files a
    governance lint proposal, and the activate_procedural_entry action
    confirms it (draft → published), reversibly."""
    from sqlmodel import col, select

    from memex_core.memory.sql_models import MaintenanceProposal
    from memex_core.services.proposal_actions import get_action

    _patch_distillers(monkeypatch)
    vault = await _mk_vault(api)
    p1 = await _make_procedure_with_cases(api, vault, verb='deploy', context='nomad', n_cases=3)
    await api.procedural.derive_pending(limit=10)

    # 1. The draft was distilled but stays draft (not auto-published).
    before = await api.procedural.get(p1.id)
    assert before.status == 'draft'
    assert before.body  # distillation ran

    # 2. Derivation filed a governance activation proposal targeting the entry.
    async with api.metastore.session() as session:
        props = (
            await session.exec(
                select(MaintenanceProposal)
                .where(col(MaintenanceProposal.target_type) == 'procedural_entry')
                .where(col(MaintenanceProposal.target_id) == str(p1.id))
            )
        ).all()
    assert len(props) == 1
    prop = props[0]
    assert prop.rule_name == 'procedural_distillation'
    assert str(prop.status).endswith('pending')
    assert str(prop.lint_type).endswith('governance')

    # 3. Confirm via the activate action → published.
    action = get_action('activate_procedural_entry')
    action.validate({}, target_type='procedural_entry', target_id=str(p1.id))
    result = await action.execute(api, {}, target_id=str(p1.id), vault_id=vault, actor='reviewer')
    after = await api.procedural.get(p1.id)
    assert after.status == 'published'

    # 4. Reverse → back to draft (non-destructive).
    await action.reverse(
        api,
        {},
        result.applied_state,
        result.prior_state,
        target_id=str(p1.id),
        vault_id=vault,
        actor='reviewer',
    )
    reverted = await api.procedural.get(p1.id)
    assert reverted.status == 'draft'


async def test_hand_edit_authors_entry_and_derivation_proposes_not_overwrites(api, monkeypatch):
    """§18.6.4: a hand edit flips origin→authored (sticky); derivation then
    PROPOSES (apply_derivation) rather than overwriting; the action applies
    the diff (origin stays authored) and reverse restores the prior content."""
    from memex_common.procedural_schemas import ProceduralEntryUpdate
    from memex_core.services.proposal_actions import get_action

    _patch_distillers(monkeypatch)
    vault = await _mk_vault(api)
    p1 = await _make_procedure_with_cases(api, vault, verb='deploy', context='nomad', n_cases=3)
    await api.procedural.derive_pending(limit=10)
    assert (await api.procedural.get(p1.id)).origin == 'derived'

    # Hand edit (non-system actor, content change) → authored, sticky.
    edited = await api.procedural.update(
        p1.id, ProceduralEntryUpdate(body='HAND EDITED BODY', edited_by='jasper')
    )
    assert edited.origin == 'authored'
    assert edited.body == 'HAND EDITED BODY'

    # A 4th case + re-derive. Authored → NOT overwritten.
    case_id = await _mk_case(api, vault, 'Case 4 — rolled the canary at 10%.')
    await api._procedural_repo.add_source(
        p1.id, ProceduralSourceCreate(source_note_id=case_id, role='provenance')
    )
    await api._procedural_repo.enqueue_derivation(
        vault_id=vault,
        source_entry_ids=[p1.id],
        target_kind='procedure',
        target_scope='global',
        target_verb='deploy',
        target_context='nomad',
    )
    await api.procedural.derive_pending(limit=10)
    after = await api.procedural.get(p1.id)
    assert after.body == 'HAND EDITED BODY'  # derivation did NOT overwrite
    assert after.origin == 'authored'

    # apply_derivation applies the diff (origin stays authored)…
    action = get_action('apply_derivation')
    params = {'body': 'DISTILLED BODY', 'title': 'D', 'summary': 's', 'trigger': 't'}
    action.validate(params, target_type='procedural_entry', target_id=str(p1.id))
    result = await action.execute(
        api, params, target_id=str(p1.id), vault_id=vault, actor='reviewer'
    )
    applied = await api.procedural.get(p1.id)
    assert applied.body == 'DISTILLED BODY'
    assert applied.origin == 'authored'

    # …and reverse restores the prior hand-edited content.
    await action.reverse(
        api,
        params,
        result.applied_state,
        result.prior_state,
        target_id=str(p1.id),
        vault_id=vault,
        actor='reviewer',
    )
    assert (await api.procedural.get(p1.id)).body == 'HAND EDITED BODY'
