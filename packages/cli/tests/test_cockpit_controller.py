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
    DISMISS_OPTION,
    options_for_rule,
)


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
