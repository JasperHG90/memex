"""TUI smoke test using Textual's ``App.run_test()`` pilot.

Drives the cockpit through the three-mode flow: LIST -> REVIEW -> NOTE.
The fake client records resolve/dismiss calls so assertions can verify
the expected actions fired.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from memex_cli.cockpit.app import ProposalCockpitApp, _ProposalQueueItem
from memex_cli.cockpit.controller import CockpitController


class _FakeClient:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self._findings = findings
        self.resolves: list[dict[str, Any]] = []
        self.dismisses: list[dict[str, Any]] = []

    async def lint_findings(self, **kwargs: Any) -> dict[str, Any]:
        return {'count': len(self._findings), 'findings': list(self._findings)}

    async def lint_resolve(
        self,
        finding_id: str,
        *,
        action: str | None = None,
        params: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        self.resolves.append(
            {'finding_id': finding_id, 'action': action, 'params': params, 'note': note}
        )
        self._findings = [f for f in self._findings if f['id'] != finding_id]
        return {'finding_id': finding_id, 'status': 'resolved'}

    async def lint_dismiss(self, finding_id: str, *, note: str | None = None) -> dict[str, Any]:
        self.dismisses.append({'finding_id': finding_id, 'note': note})
        self._findings = [f for f in self._findings if f['id'] != finding_id]
        return {'finding_id': finding_id, 'status': 'dismissed'}

    async def lint_reverse(self, finding_id: str) -> dict[str, Any]:
        return {'finding_id': finding_id, 'status': 'reversed'}

    async def lint_flag(self, finding_id: str) -> dict[str, Any]:
        return {'finding_id': finding_id, 'flagged': True, 'flagged_at': '2026-05-26T00:00:00Z'}

    async def get_memory_unit(self, unit_id: str) -> Any:
        return None

    async def get_note(self, note_id: Any) -> Any:
        return None

    async def get_note_page_index(self, note_id: Any) -> Any:
        return None

    async def get_lineage(
        self,
        entity_type: str,
        entity_id: Any,
        direction: Any = None,
        depth: int = 3,
        limit: int = 10,
    ) -> Any:
        return None

    async def list_vaults(self) -> list[Any]:
        return []


def _finding(
    *,
    rule: str = 'cold_low_mw_unit',
    source: str = 'rule',
    target_type: str = 'memory_unit',
) -> dict[str, Any]:
    return {
        'id': str(uuid4()),
        'vault_id': str(uuid4()),
        'rule_name': rule,
        'lint_type': 'quality',
        'target_type': target_type,
        'target_id': str(uuid4()),
        'target_text': 'sample low-MW unit text',
        'source': source,
        'created_at': '2026-05-23T00:00:00Z',
        'evidence': {
            'mw_score': 0.1,
            'success_co_count': 0,
            'failure_co_count': 5,
            'last_outcome_age_days': 60,
        },
        'suggested_action': 'Deprioritize candidate.',
    }


@pytest.mark.asyncio
async def test_cockpit_renders_queue_and_resolves_via_review() -> None:
    """Enter → REVIEW, then Enter on action → NOTE, then Enter submits."""
    finding = _finding()
    client = _FakeClient([finding])
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.proposals
        assert app.proposals[0].finding_id == finding['id']
        assert app.mode == 'list'

        await pilot.press('enter')
        await pilot.pause()
        assert app.mode == 'review'

        await pilot.press('enter')
        await pilot.pause()
        assert app.mode == 'note'

        await pilot.press('enter')
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert client.resolves, 'review flow should issue a lint_resolve call'
    resolve = client.resolves[0]
    assert resolve['finding_id'] == finding['id']
    assert resolve['action'] == 'deprioritize_unit'


@pytest.mark.asyncio
async def test_cockpit_empty_queue_shows_no_proposals() -> None:
    client = _FakeClient([])
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.proposals == []


@pytest.mark.asyncio
async def test_escape_returns_to_list_from_review() -> None:
    finding = _finding()
    client = _FakeClient([finding])
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.mode == 'list'

        await pilot.press('enter')
        await pilot.pause()
        assert app.mode == 'review'

        await pilot.press('escape')
        await pilot.pause()
        assert app.mode == 'list'


@pytest.mark.asyncio
async def test_space_toggles_multiselect() -> None:
    findings = [_finding(), _finding()]
    client = _FakeClient(findings)
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.mode == 'list'

        await pilot.press('space')
        await pilot.pause()

        queue = app.query_one('#queue-list')
        items = [c for c in queue.children if isinstance(c, _ProposalQueueItem)]
        assert items[0].checked

        await pilot.press('escape')
        await pilot.pause()
        assert not items[0].checked


@pytest.mark.asyncio
async def test_n_toggles_note_area_in_review() -> None:
    finding = _finding()
    client = _FakeClient([finding])
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        await pilot.press('enter')
        await pilot.pause()
        assert app.mode == 'review'

        await pilot.press('n')
        await pilot.pause()
        assert app.mode == 'note'
        assert app.query_one('#note-section').has_class('visible')


@pytest.mark.asyncio
async def test_d_enters_detail_mode_and_esc_returns() -> None:
    """Press d in LIST to enter DETAIL, Esc to return to LIST."""
    finding = _finding()
    client = _FakeClient([finding])
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.mode == 'list'

        await pilot.press('d')
        await pilot.pause()
        assert app.mode == 'detail'
        assert len(app._detail_unit_ids) == 1
        assert app._detail_unit_ids[0] == finding['target_id']

        await pilot.press('escape')
        await pilot.pause()
        assert app.mode == 'list'


@pytest.mark.asyncio
async def test_detail_mode_cycles_units_with_tab() -> None:
    """Tab cycles through target + related units in DETAIL mode."""
    target_id = str(uuid4())
    related_id = str(uuid4())
    finding = {
        'id': str(uuid4()),
        'vault_id': str(uuid4()),
        'rule_name': 'llm_semantic_contradiction',
        'lint_type': 'quality',
        'target_type': 'memory_unit',
        'target_id': target_id,
        'target_text': 'target text',
        'source': 'llm',
        'created_at': '2026-05-23T00:00:00Z',
        'evidence': {
            'related_unit_ids': [related_id],
            'explanation': 'contradicts',
        },
        'suggested_action': None,
    }
    client = _FakeClient([finding])
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.mode == 'list'

        await pilot.press('d')
        await pilot.pause()
        assert app.mode == 'detail'
        assert len(app._detail_unit_ids) == 2
        assert app._detail_unit_index == 0

        await pilot.press('tab')
        await pilot.pause()
        assert app._detail_unit_index == 1

        await pilot.press('tab')
        await pilot.pause()
        assert app._detail_unit_index == 0  # wraps around

        await pilot.press('escape')
        await pilot.pause()
        assert app.mode == 'list'
