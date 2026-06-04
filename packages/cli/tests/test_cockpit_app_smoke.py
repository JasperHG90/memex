"""TUI smoke test using Textual's ``App.run_test()`` pilot.

Drives the cockpit through the three-mode flow: LIST -> REVIEW -> NOTE.
The fake client records resolve/dismiss calls so assertions can verify
the expected actions fired.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from textual.widgets import Static

from memex_cli.cockpit.app import ProposalCockpitApp, _ProposalQueueItem
from memex_cli.cockpit.controller import CockpitController, CockpitProposal


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
        legacy_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.resolves.append(
            {
                'finding_id': finding_id,
                'action': action,
                'params': params,
                'note': note,
                'legacy_params': legacy_params,
            }
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


def _entity_collapse_finding() -> dict[str, Any]:
    winner, loser = 'a' * 36, 'b' * 36
    return {
        'id': str(uuid4()),
        'vault_id': None,
        'rule_name': 'entity_collapse_cluster',
        'lint_type': 'quality',
        'target_type': 'entity',
        'target_id': winner,
        'target_label': 'Governance team',
        'source': 'rule',
        'created_at': '2026-06-03T00:00:00Z',
        'evidence': {
            'cluster_members': [winner, loser],
            'member_canonical_names': {winner: 'Governance team', loser: 'governance team'},
            'suggested_winner_id': winner,
            'vaults_affected': ['v1'],
            'pair_min_similarity': 1.0,
            'pair_max_similarity': 1.0,
        },
        'suggested_action': 'Cluster of near-duplicate entities detected.',
    }


def test_entity_collapse_body_lists_every_member_and_marks_winner() -> None:
    """A reviewer MUST see which entities merge and which one wins."""
    app = ProposalCockpitApp(CockpitController(_FakeClient([])), limit=5)
    proposal = CockpitProposal.from_finding(_entity_collapse_finding())
    body = '\n'.join(app._build_entity_collapse_body(proposal))
    assert 'MERGE 2 entities' in body
    assert 'Governance team' in body  # the winner
    assert 'governance team' in body  # the member merged away
    assert 'winner' in body
    assert 'merged into winner' in body


@pytest.mark.asyncio
async def test_render_note_detail_panel_shows_title_text_and_error() -> None:
    """Note-target findings render the NOTE (title + body), never a unit 404."""
    app = ProposalCockpitApp(CockpitController(_FakeClient([])), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._render_note_detail_panel('n0deadbee', ('Quarterly Planning', 'The note body.'))
        body = str(app.query_one('#detail-body', Static).render())
        assert 'Quarterly Planning' in body
        assert 'The note body.' in body
        assert 'Could not load unit' not in body
        # Failure path resolves to a note error, not a unit error.
        app._render_note_detail_panel('n0deadbee', None)
        assert 'Could not load note' in str(app.query_one('#detail-body', Static).render())


@pytest.mark.asyncio
async def test_note_target_enter_detail_targets_note_not_unit() -> None:
    """Pressing Detail on a note-target finding records a note id, no unit lookup."""
    finding = _finding(rule='inbox_vault_no_fit', target_type='note')
    app = ProposalCockpitApp(CockpitController(_FakeClient([finding])), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press('d')
        await pilot.pause()
        assert app.mode == 'detail'
        assert app._detail_note_id == finding['target_id']
        assert app._detail_unit_ids == []


def test_queue_item_label_shows_human_target_not_uuid() -> None:
    """The queue row renders the human target_display, not the raw UUID."""
    proposal = CockpitProposal.from_finding(_entity_collapse_finding())
    item = _ProposalQueueItem(proposal)
    label = item._render_label()
    assert 'Governance team' in label
    assert proposal.target_id not in label  # no full bare UUID


def _entity_collapse_finding_3() -> dict[str, Any]:
    w, l1, l2 = 'a' * 36, 'b' * 36, 'c' * 36
    return {
        'id': str(uuid4()),
        'vault_id': None,
        'rule_name': 'entity_collapse_cluster',
        'lint_type': 'quality',
        'target_type': 'entity',
        'target_id': w,
        'target_label': 'Governance team',
        'source': 'rule',
        'created_at': '2026-06-03T00:00:00Z',
        'evidence': {
            'cluster_members': [w, l1, l2],
            'member_canonical_names': {w: 'Governance team', l1: 'governance team', l2: 'Gov Team'},
            'suggested_winner_id': w,
            'vaults_affected': ['v1'],
        },
        'suggested_action': 'Cluster of near-duplicate entities detected.',
    }


@pytest.mark.asyncio
async def test_collapse_mode_select_deselect_and_apply_subset() -> None:
    """Enter collapse mode on an entity cluster, exclude one member, apply the
    chosen winner + subset via the carveout."""
    finding = _entity_collapse_finding_3()
    client = _FakeClient([finding])
    app = ProposalCockpitApp(CockpitController(client), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # Enter review → collapse mode (entity_collapse routes here).
        await pilot.press('enter')
        await pilot.pause()
        assert app.mode == 'collapse'
        items = app._collapse_items()
        assert len(items) == 3
        # All included by default; first member is the suggested winner.
        assert all(it.included for it in items)
        assert items[0].is_winner

        # Exclude the third member (navigate down twice, toggle).
        await pilot.press('down')
        await pilot.press('down')
        await pilot.pause()
        await pilot.press('space')
        await pilot.pause()
        excluded_id = items[2].member_id
        assert not items[2].included
        assert app._collapse_included_ids() == ['a' * 36, 'b' * 36]

        # Apply.
        await pilot.press('a')
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert client.resolves, 'apply should issue a resolve carveout call'
    call = client.resolves[0]
    assert call['action'] is None  # carveout (no canned action)
    assert call['legacy_params'] == {
        'winner_id': 'a' * 36,
        'member_ids': ['a' * 36, 'b' * 36],
    }
    assert excluded_id not in call['legacy_params']['member_ids']


@pytest.mark.asyncio
async def test_collapse_mode_set_winner_then_apply() -> None:
    """'w' reassigns the winner; apply sends the chosen winner."""
    finding = _entity_collapse_finding_3()
    client = _FakeClient([finding])
    app = ProposalCockpitApp(CockpitController(client), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert app.mode == 'collapse'
        # Move to the 2nd member and make it the winner.
        await pilot.press('down')
        await pilot.pause()
        await pilot.press('w')
        await pilot.pause()
        assert app._collapse_winner_id == 'b' * 36
        items = app._collapse_items()
        assert items[1].is_winner and not items[0].is_winner
        await pilot.press('a')
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert client.resolves[0]['legacy_params']['winner_id'] == 'b' * 36


@pytest.mark.asyncio
async def test_collapse_apply_rejects_single_member() -> None:
    """Excluding down to <2 members blocks apply (no resolve call)."""
    finding = _entity_collapse_finding_3()
    client = _FakeClient([finding])
    app = ProposalCockpitApp(CockpitController(client), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        # Exclude both losers (members 2 and 3), leaving only the winner.
        await pilot.press('down')
        await pilot.press('space')
        await pilot.press('down')
        await pilot.press('space')
        await pilot.pause()
        assert app._collapse_included_ids() == ['a' * 36]
        await pilot.press('a')
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert client.resolves == [], 'apply must be blocked with fewer than 2 members'


@pytest.mark.asyncio
async def test_note_detail_escapes_rich_markup_in_content() -> None:
    """Note titles/bodies containing [..] must render literally, not as tags."""
    app = ProposalCockpitApp(CockpitController(_FakeClient([])), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._render_note_detail_panel(
            'n0deadbee', ('[ALERT] Q3 plan', 'see [section 2] for detail')
        )
        body = str(app.query_one('#detail-body', Static).render())
        # Bracketed body content survives as visible text — if it were parsed as
        # markup, "[section 2]" would be eaten as an (invalid) tag and dropped.
        assert '[section 2]' in body
        assert 'Q3 plan' in body


@pytest.mark.asyncio
async def test_collapse_mode_empty_cluster_bounces_to_list() -> None:
    """A finding with no members must not strand the user in an empty selector."""
    finding: dict[str, Any] = {
        'id': str(uuid4()),
        'vault_id': None,
        'rule_name': 'entity_collapse_cluster',
        'lint_type': 'quality',
        'target_type': 'entity',
        'target_id': 'a' * 36,
        'source': 'rule',
        'created_at': '2026-06-03T00:00:00Z',
        'evidence': {'cluster_members': [], 'member_canonical_names': {}},
        'suggested_action': 'x',
    }
    app = ProposalCockpitApp(CockpitController(_FakeClient([finding])), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert app.mode == 'list'  # bounced back, not stuck in 'collapse'
