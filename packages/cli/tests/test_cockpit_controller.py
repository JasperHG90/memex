"""Controller-level tests for the Textual cockpit.

These tests exercise the data layer (`CockpitController`,
`options_for_rule`, sort order) without touching the Textual app — the
TUI is covered by a separate smoke test using `App.run_test`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from memex_cli.cockpit.controller import (
    CockpitController,
    CockpitOption,
    CockpitProposal,
    DISMISS_OPTION,
    options_for_proposal,
    options_for_rule,
    recommended_resolve_option,
)


def test_target_display_prefers_label_then_evidence_then_text_then_id():
    """Queue/detail rows must show a human label, never a bare UUID."""
    base = {'id': '1', 'rule_name': 'r', 'lint_type': 'quality'}
    # 1) Server-resolved label (note title / entity / mm name) wins.
    p = CockpitProposal.from_finding(
        {
            **base,
            'target_type': 'note',
            'target_id': 'a' * 36,
            'target_label': 'AlphaEvolve article',
        }
    )
    assert p.target_display == 'AlphaEvolve article'
    # 2) Entity-collapse member names from evidence when no server label.
    p2 = CockpitProposal.from_finding(
        {
            **base,
            'target_type': 'entity',
            'target_id': 'b' * 36,
            'evidence': {'member_canonical_names': {'b' * 36: 'Marc', 'c' * 36: 'Marc Haas'}},
        }
    )
    assert 'Marc' in p2.target_display and 'Marc Haas' in p2.target_display
    # 3) Unit text snippet when that's all there is.
    p3 = CockpitProposal.from_finding(
        {
            **base,
            'target_type': 'memory_unit',
            'target_id': 'd' * 36,
            'target_text': 'the fact body',
        }
    )
    assert p3.target_display == 'the fact body'
    # 4) Last resort: truncated id (never the full bare UUID).
    p4 = CockpitProposal.from_finding(
        {**base, 'target_type': 'memory_unit', 'target_id': 'abcdef1234567890'}
    )
    assert p4.target_display == 'abcdef12…'


@pytest.mark.asyncio
async def test_fetch_note_detail_returns_title_and_text():
    class _Note:
        title = 'Quarterly Planning'
        original_text = 'Body of the note.'

    class _Client:
        async def get_note(self, note_id: Any) -> Any:
            return _Note()

    ctrl = CockpitController(_Client())
    assert await ctrl.fetch_note_detail('n') == ('Quarterly Planning', 'Body of the note.')


@pytest.mark.asyncio
async def test_fetch_note_detail_none_on_error():
    class _Client:
        async def get_note(self, note_id: Any) -> Any:
            raise RuntimeError('boom')

    ctrl = CockpitController(_Client())
    assert await ctrl.fetch_note_detail('n') is None


@pytest.mark.asyncio
async def test_fetch_note_detail_handles_dict_note():
    """A client returning a dict (not a model) still yields title/text."""

    class _Client:
        async def get_note(self, note_id: Any) -> Any:
            return {'title': 'Quarterly Planning', 'original_text': 'Body.'}

    ctrl = CockpitController(_Client())
    assert await ctrl.fetch_note_detail('n') == ('Quarterly Planning', 'Body.')


@pytest.mark.asyncio
async def test_apply_entity_collapse_uses_carveout_with_member_subset():
    """The collapse apply hits the server carveout (action=None) and sends the
    chosen winner + member subset via top-level (legacy_params) body keys."""
    captured: dict[str, Any] = {}

    class _Client:
        async def lint_resolve(
            self,
            finding_id: str,
            *,
            action: Any = None,
            params: Any = None,
            note: Any = None,
            legacy_params: Any = None,
        ) -> dict[str, Any]:
            captured.update(
                finding_id=finding_id, action=action, params=params, legacy_params=legacy_params
            )
            return {'status': 'resolved'}

    ctrl = CockpitController(_Client())
    await ctrl.apply_entity_collapse('f1', winner_id='w', member_ids=['w', 'l1'])
    assert captured['action'] is None  # carveout, not a canned action
    assert captured['params'] is None
    assert captured['legacy_params'] == {'winner_id': 'w', 'member_ids': ['w', 'l1']}


def test_recommended_resolve_option_inbox_route_carries_vault_param():
    """An inbox route's recommended option must carry its target_vault_id.

    Regression pin for the batch-resolve bug: batch resolution fans out to this
    per-proposal option, so the params must be present or the note never moves.
    """
    vault_id = str(uuid4())
    proposal = CockpitProposal.from_finding(
        _finding(
            'inbox_vault_route',
            target_type='note',
            evidence={
                'top_candidates': [
                    {'vault_id': vault_id, 'vault_name': 'projects', 'p_match': 0.7},
                    {'vault_id': str(uuid4()), 'vault_name': 'archive', 'p_match': 0.3},
                ]
            },
        )
    )
    option = recommended_resolve_option(proposal)
    assert option is not None
    assert option.action_id == 'route_note_to_vault'
    assert option.params == {
        'target_vault_id': vault_id,
        'other_vault_ids': [proposal.raw_evidence['top_candidates'][1]['vault_id']],
    }


def test_options_for_proposal_dispatches_by_rule():
    """Contradiction and inbox-route rules get their dynamic builders, not the generic fallback."""
    contra = CockpitProposal.from_finding(_finding('llm_semantic_contradiction'))
    assert any(o.action_id == 'deprioritize_unit' for o in options_for_proposal(contra))

    route = CockpitProposal.from_finding(
        _finding(
            'inbox_vault_route',
            target_type='note',
            evidence={'top_candidates': [{'vault_id': str(uuid4()), 'vault_name': 'x'}]},
        )
    )
    assert any(o.action_id == 'route_note_to_vault' for o in options_for_proposal(route))


def _finding(
    rule: str,
    *,
    source: str = 'rule',
    target_type: str = 'memory_unit',
    created_at: str = '2026-05-20T00:00:00Z',
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        'id': str(uuid4()),
        'vault_id': str(uuid4()),
        'rule_name': rule,
        'lint_type': 'quality',
        'target_type': target_type,
        'target_id': str(uuid4()),
        'target_text': 'sample text',
        'source': source,
        'created_at': created_at,
        'evidence': evidence or {},
        'suggested_action': 'do the thing',
    }


class _FakeClient:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self._findings = findings
        self.resolved: list[tuple[str, dict[str, Any]]] = []
        self.dismissed: list[tuple[str, str | None]] = []
        self.reversed_ids: list[str] = []

    async def lint_findings(self, **kwargs: Any) -> dict[str, Any]:
        return {'count': len(self._findings), 'findings': self._findings}

    async def lint_resolve(
        self,
        finding_id: str,
        *,
        action: str | None = None,
        params: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        self.resolved.append((finding_id, {'action': action, 'params': params, 'note': note}))
        return {'finding_id': finding_id, 'status': 'resolved'}

    async def lint_dismiss(
        self,
        finding_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        self.dismissed.append((finding_id, note))
        return {'finding_id': finding_id, 'status': 'dismissed'}

    async def lint_reverse(self, finding_id: str) -> dict[str, Any]:
        self.reversed_ids.append(finding_id)
        return {'finding_id': finding_id, 'status': 'reversed'}


@pytest.mark.asyncio
async def test_fetch_pending_sorts_llm_first() -> None:
    findings = [
        _finding('cold_low_mw_unit', source='rule', created_at='2026-05-23T00:00:00Z'),
        _finding('llm_semantic_contradiction', source='llm', created_at='2026-05-20T00:00:00Z'),
        _finding(
            'orphan_mental_model',
            source='rule',
            target_type='mental_model',
            created_at='2026-05-22T00:00:00Z',
        ),
        _finding('llm_schema_drift', source='llm', created_at='2026-05-21T00:00:00Z'),
    ]
    client = _FakeClient(findings)
    controller = CockpitController(client)
    proposals = await controller.fetch_pending()
    sources = [p.source for p in proposals]
    assert sources == ['llm', 'llm', 'rule', 'rule']
    # Within each tier, newest first.
    llm_dates = [p.created_at for p in proposals if p.source == 'llm']
    rule_dates = [p.created_at for p in proposals if p.source == 'rule']
    assert llm_dates == sorted(llm_dates, reverse=True)
    assert rule_dates == sorted(rule_dates, reverse=True)


def test_options_for_rule_carries_dismiss_sentinel() -> None:
    options = options_for_rule('cold_low_mw_unit', 'memory_unit')
    assert any(opt.action_id == 'deprioritize_unit' for opt in options)
    assert any(opt is DISMISS_OPTION or opt.action_id == '' for opt in options)


def test_options_for_rule_filters_by_target_type() -> None:
    # cold_low_mw_unit canon includes `deprioritize_unit` (memory_unit-only).
    # When we ask the menu for a mental_model target, the canned deprio must
    # disappear; no_op (works for any target) and dismiss survive.
    options = options_for_rule('cold_low_mw_unit', 'mental_model')
    action_ids = [opt.action_id for opt in options]
    assert 'deprioritize_unit' not in action_ids
    assert 'no_op' in action_ids
    assert '' in action_ids  # dismiss sentinel


def test_options_for_rule_falls_back_for_unknown_rule() -> None:
    options = options_for_rule('not_a_real_rule', 'memory_unit')
    # Fallback offers no_op + dismiss; both should survive the target-type filter.
    ids = [opt.action_id for opt in options]
    assert 'no_op' in ids
    assert '' in ids


def test_orphan_mental_model_does_not_offer_delete_mental_model() -> None:
    """delete_mental_model is entity-only; a mental_model-typed orphan finding
    must never surface it.

    Pins fix #2's client-side half: the static action catalogue marks
    delete_mental_model ``applicable_target_types=('entity',)`` (was
    ``('entity', 'mental_model')``), so the target-type filter in
    options_for_rule withholds it from a mental_model target. A
    mental_model-typed finding carries target_id = mental_model.id, which the
    server's delete_mental_model (keyed on entity_id) cannot honour — offering
    it would dead-end the reviewer."""
    from memex_cli.cockpit.controller import _ACTION_CATALOGUE

    # Catalogue contract: the action is entity-only (the fix-#2 change).
    _, _, applicable_types, _ = _ACTION_CATALOGUE['delete_mental_model']
    assert applicable_types == ('entity',)
    assert 'mental_model' not in applicable_types

    # And the rule menu never offers it for a mental_model target.
    option_ids = [opt.action_id for opt in options_for_rule('orphan_mental_model', 'mental_model')]
    assert 'delete_mental_model' not in option_ids


def test_options_have_at_most_one_recommended() -> None:
    for rule in [
        'cold_low_mw_unit',
        'orphan_mental_model',
        'llm_schema_drift',
        'llm_semantic_contradiction',
        'composite_deprioritize_candidate',
        'claim_too_aggressive',
    ]:
        # Each rule's recommended is per-target-type; check the canonical case.
        target_type = 'mental_model' if rule == 'orphan_mental_model' else 'memory_unit'
        options = options_for_rule(rule, target_type)
        recommended = [opt for opt in options if opt.recommended]
        assert len(recommended) <= 1, f'{rule}: {len(recommended)} recommended options'


@pytest.mark.asyncio
async def test_resolve_dispatches_dismiss_for_dismiss_option() -> None:
    findings = [_finding('cold_low_mw_unit')]
    client = _FakeClient(findings)
    controller = CockpitController(client)
    proposals = await controller.fetch_pending()
    result = await controller.resolve(proposals[0], DISMISS_OPTION, note='tracked elsewhere')
    assert result['status'] == 'dismissed'
    assert client.dismissed == [(proposals[0].finding_id, 'tracked elsewhere')]
    assert client.resolved == []


@pytest.mark.asyncio
async def test_resolve_passes_action_to_client() -> None:
    findings = [_finding('cold_low_mw_unit')]
    client = _FakeClient(findings)
    controller = CockpitController(client)
    proposals = await controller.fetch_pending()
    deprio = CockpitOption(
        action_id='deprioritize_unit',
        label='Deprio',
        summary='',
        effect='',
        reversible=True,
    )
    result = await controller.resolve(
        proposals[0], deprio, note='because', params={'reason': 'stale'}
    )
    assert result['status'] == 'resolved'
    assert client.resolved == [
        (
            proposals[0].finding_id,
            {'action': 'deprioritize_unit', 'params': {'reason': 'stale'}, 'note': 'because'},
        )
    ]


@pytest.mark.asyncio
async def test_reverse_forwards_to_client() -> None:
    controller = CockpitController(_FakeClient([]))
    client = controller._client  # type: ignore[attr-defined]
    fid = str(uuid4())
    result = await controller.reverse(fid)
    assert result['status'] == 'reversed'
    assert client.reversed_ids == [fid]
