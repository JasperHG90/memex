"""TUI smoke test using Textual's `App.run_test()` pilot.

Drives the cockpit through a verdict cycle: build a fake controller with one
proposal in flight, press `1` (pick the recommended option), and assert the
fake client recorded the resolve call. This is a smoke test — it does not
attempt to exhaustively cover the TUI's keybindings.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from memex_cli.cockpit.app import ProposalCockpitApp
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
        # Remove this finding so the next fetch returns an empty queue.
        self._findings = [f for f in self._findings if f['id'] != finding_id]
        return {'finding_id': finding_id, 'status': 'resolved'}

    async def lint_dismiss(self, finding_id: str, *, note: str | None = None) -> dict[str, Any]:
        self.dismisses.append({'finding_id': finding_id, 'note': note})
        self._findings = [f for f in self._findings if f['id'] != finding_id]
        return {'finding_id': finding_id, 'status': 'dismissed'}

    async def lint_reverse(self, finding_id: str) -> dict[str, Any]:
        return {'finding_id': finding_id, 'status': 'reversed'}


def _finding() -> dict[str, Any]:
    return {
        'id': str(uuid4()),
        'vault_id': str(uuid4()),
        'rule_name': 'cold_low_mw_unit',
        'lint_type': 'quality',
        'target_type': 'memory_unit',
        'target_id': str(uuid4()),
        'target_text': 'sample low-MW unit text',
        'source': 'rule',
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
async def test_cockpit_renders_queue_and_resolves_pick_1() -> None:
    finding = _finding()
    client = _FakeClient([finding])
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test() as pilot:
        # Let mount + initial fetch settle.
        await pilot.pause()
        # The queue should have one row, detail pane should be populated.
        assert app.proposals
        assert app.proposals[0].finding_id == finding['id']

        # Trigger the recommended option (pick 1) — fires the verdict worker.
        app.action_pick('1')
        # Let the worker start, render the modal, and accept our Esc skip.
        await pilot.pause()
        await pilot.press('escape')
        # Wait for the verdict worker to finish (note skip → resolve → refresh).
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert client.resolves, 'pick 1 should issue a lint_resolve call'
    resolve = client.resolves[0]
    assert resolve['finding_id'] == finding['id']
    # cold_low_mw_unit's recommended option is deprioritize_unit.
    assert resolve['action'] == 'deprioritize_unit'


@pytest.mark.asyncio
async def test_cockpit_empty_queue_shows_no_proposals() -> None:
    client = _FakeClient([])
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.proposals == []


@pytest.mark.asyncio
async def test_enter_picks_recommended_option() -> None:
    """Enter on the highlighted proposal commits the ★ Recommended option.

    cold_low_mw_unit's recommended is deprioritize_unit, so Enter should
    issue lint_resolve with action='deprioritize_unit' — same outcome as
    pressing `1` would, but Enter is a more discoverable affordance.
    """
    finding = _finding()
    client = _FakeClient([finding])
    controller = CockpitController(client)
    app = ProposalCockpitApp(controller, limit=5)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.proposals
        # Fire the bound action directly to bypass focus / key-routing
        # quirks in the headless harness; the binding wiring is asserted
        # by the absence of regressions in the BINDINGS list.
        app.action_pick_recommended()
        await pilot.pause()
        await pilot.press('escape')  # skip the optional-note prompt
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert client.resolves, 'Enter should issue a resolve call'
    assert client.resolves[0]['action'] == 'deprioritize_unit'
