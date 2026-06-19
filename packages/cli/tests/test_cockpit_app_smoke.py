"""TUI smoke test using Textual's ``App.run_test()`` pilot.

Drives the cockpit through the three-mode flow: LIST -> REVIEW -> NOTE.
The fake client records resolve/dismiss calls so assertions can verify
the expected actions fired.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from textual.widgets import ListView, Static

from memex_cli.cockpit.app import FilterScreen, ProposalCockpitApp, _ProposalQueueItem
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


@pytest.mark.asyncio
async def test_s_views_source_note_in_detail() -> None:
    """In DETAIL mode, [s] (not [n]) triggers the source-note fetch."""
    finding = _finding()
    client = _FakeClient([finding])
    app = ProposalCockpitApp(CockpitController(client), limit=5)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press('d')
        await pilot.pause()
        assert app.mode == 'detail'
        assert app._viewing_source_note is False

        await pilot.press('s')
        await app.workers.wait_for_complete()
        await pilot.pause()
        # [s] triggers the source-note fetch handler. The fake unit has no
        # linked note, so the handler reports that via the status bar — proving
        # [s] (not [n]) is wired to the fetch in DETAIL mode.
        status = str(app.query_one('#status-bar', Static).render())
        assert 'source note' in status.lower()


@pytest.mark.asyncio
async def test_f5_refreshes_queue() -> None:
    """F5 re-fetches the queue from the server."""
    finding = _finding()

    class _CountingClient(_FakeClient):
        def __init__(self, findings: list[dict[str, Any]]) -> None:
            super().__init__(findings)
            self.fetch_count = 0

        async def lint_findings(self, **kwargs: Any) -> dict[str, Any]:
            self.fetch_count += 1
            return await super().lint_findings(**kwargs)

    client = _CountingClient([finding])
    app = ProposalCockpitApp(CockpitController(client), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        first = client.fetch_count
        await pilot.press('f5')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert client.fetch_count > first


@pytest.mark.asyncio
async def test_queue_load_failure_surfaces_error_not_crash() -> None:
    """A failing fetch renders an error state with a retry hint, not a traceback."""

    class _BrokenClient(_FakeClient):
        async def lint_findings(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError('boom: server down')

    app = ProposalCockpitApp(CockpitController(_BrokenClient([])), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.mode == 'list'
        header = str(app.query_one('#detail-header', Static).render())
        assert 'Could not load the proposal queue' in header
        assert 'boom: server down' in header
        status = str(app.query_one('#status-bar', Static).render())
        assert 'Retry' in status


@pytest.mark.asyncio
async def test_reverse_screen_rejects_non_uuid() -> None:
    """ReverseScreen keeps a malformed finding_id from reaching the server."""
    from memex_cli.cockpit.app import ReverseScreen
    from textual.widgets import Input, Label

    app = ProposalCockpitApp(CockpitController(_FakeClient([])), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = ReverseScreen()
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one('#reverse-input', Input).value = 'not-a-uuid'
        await pilot.press('enter')
        await pilot.pause()
        # Still on the modal; an inline error is shown rather than dismissing.
        assert isinstance(app.screen, ReverseScreen)
        assert 'Not a valid finding_id' in str(screen.query_one('#reverse-help', Label).render())


@pytest.mark.asyncio
async def test_refresh_failure_from_detail_keeps_retry_hint() -> None:
    """A refresh that fails while in DETAIL still surfaces the retry affordance.

    Regression guard: dropping to LIST must happen before the error is painted,
    or watch_mode's footer update would clobber the '[F5] Retry' status hint.
    """

    class _FailAfterFirst(_FakeClient):
        def __init__(self, findings: list[dict[str, Any]]) -> None:
            super().__init__(findings)
            self.calls = 0

        async def lint_findings(self, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError('server went away')
            return await super().lint_findings(**kwargs)

    finding = _finding()
    app = ProposalCockpitApp(CockpitController(_FailAfterFirst([finding])), limit=5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press('d')
        await pilot.pause()
        assert app.mode == 'detail'

        await pilot.press('f5')
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.mode == 'list'
        status = str(app.query_one('#status-bar', Static).render())
        assert 'Retry' in status
        header = str(app.query_one('#detail-header', Static).render())
        assert 'Could not load the proposal queue' in header


@pytest.mark.asyncio
async def test_flag_one_returns_result_then_error() -> None:
    """_flag_one returns (result, None) on success and (None, error) on failure —
    the contract both verdict paths branch on. Guards the (result, error) order."""
    proposal_finding = _finding()

    ok_app = ProposalCockpitApp(CockpitController(_FakeClient([proposal_finding])), limit=5)
    async with ok_app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        proposal = ok_app.proposals[0]
        result, error = await ok_app._flag_one(proposal)
        assert error is None
        assert result is not None and result.get('flagged') is True
        assert proposal.flagged_at == '2026-05-26T00:00:00Z'

    class _FlagBoom(_FakeClient):
        async def lint_flag(self, finding_id: str) -> dict[str, Any]:
            raise RuntimeError('flag endpoint down')

    err_app = ProposalCockpitApp(CockpitController(_FlagBoom([proposal_finding])), limit=5)
    async with err_app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        result, error = await err_app._flag_one(err_app.proposals[0])
        assert result is None
        assert error is not None and 'flag endpoint down' in error


@pytest.mark.asyncio
async def test_batch_flag_counts_success_and_failure() -> None:
    """_submit_batch_async flag branch counts ok on error-None, fail otherwise."""

    class _FlagOddFails(_FakeClient):
        def __init__(self, findings: list[dict[str, Any]]) -> None:
            super().__init__(findings)
            self._n = 0

        async def lint_flag(self, finding_id: str) -> dict[str, Any]:
            self._n += 1
            if self._n == 2:
                raise RuntimeError('transient')
            return {'finding_id': finding_id, 'flagged': True, 'flagged_at': 'x'}

    findings = [_finding(), _finding(), _finding()]
    app = ProposalCockpitApp(CockpitController(_FlagOddFails(findings)), limit=5)
    from memex_cli.cockpit.controller import FLAG_OPTION

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._submit_batch_async(list(app.proposals), FLAG_OPTION, None)
        await app.workers.wait_for_complete()
        await pilot.pause()
        status = str(app.query_one('#status-bar', Static).render())
        assert '2 flagged' in status
        assert '1 failed' in status


# ---------------------------------------------------------------------------
# Rule-name filter + select-all
# ---------------------------------------------------------------------------


def _mixed_findings() -> list[dict[str, Any]]:
    # Sorted distinct rules: cold_low_mw_unit, llm_schema_drift,
    # llm_semantic_contradiction.
    return [
        _finding(rule='llm_schema_drift'),
        _finding(rule='llm_schema_drift'),
        _finding(rule='llm_semantic_contradiction'),
        _finding(rule='cold_low_mw_unit'),
    ]


async def _open_filter_and_pick(pilot: Any, app: ProposalCockpitApp, index: int) -> None:
    """Open the filter picker and select the entry at ``index`` (0 = All rules)."""
    await pilot.press('slash')
    await pilot.pause()
    assert isinstance(app.screen, FilterScreen)
    app.screen.query_one('#filter-list', ListView).index = index
    await pilot.press('enter')
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.asyncio
async def test_rule_filter_narrows_and_clears() -> None:
    """`/` -> pick a rule narrows the queue; 'All rules' restores it."""
    client = _FakeClient(_mixed_findings())
    app = ProposalCockpitApp(CockpitController(client), limit=50)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert len(app.proposals) == 4
        assert len(app._all_proposals) == 4

        # Entry 2 in the picker = llm_schema_drift (after 'All rules' at 0,
        # cold_low_mw_unit at 1).
        await _open_filter_and_pick(pilot, app, 2)
        assert app._rule_filter == 'llm_schema_drift'
        assert len(app.proposals) == 2
        assert all(p.rule_name == 'llm_schema_drift' for p in app.proposals)
        # Label reflects shown/loaded under a filter.
        label = str(app.query_one('#queue-label').render())
        assert '2/4' in label and 'llm_schema_drift' in label

        # _current_proposal indexes the filtered list correctly.
        assert app._current_proposal() is app.proposals[0]

        # Select 'All rules' (entry 0) -> full list returns.
        await _open_filter_and_pick(pilot, app, 0)
        assert app._rule_filter is None
        assert len(app.proposals) == 4


@pytest.mark.asyncio
async def test_select_all_respects_filter_and_filter_clears_selection() -> None:
    """'a' selects every filtered row; changing the filter drops the selection."""
    client = _FakeClient(_mixed_findings())
    app = ProposalCockpitApp(CockpitController(client), limit=50)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open_filter_and_pick(pilot, app, 2)  # llm_schema_drift -> 2 rows
        assert len(app.proposals) == 2

        await pilot.press('a')
        await pilot.pause()
        assert app._count_selected() == 2

        # Switching the filter rebuilds the queue and clears the checkboxes.
        await _open_filter_and_pick(pilot, app, 0)  # All rules
        assert app._count_selected() == 0


@pytest.mark.asyncio
async def test_filter_survives_refresh_then_resets_when_rule_gone() -> None:
    """F5 keeps a still-present filter; resets to All when the rule vanishes."""
    findings = _mixed_findings()
    client = _FakeClient(findings)
    app = ProposalCockpitApp(CockpitController(client), limit=50)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open_filter_and_pick(pilot, app, 2)  # llm_schema_drift
        assert app._rule_filter == 'llm_schema_drift'

        # Refresh with the rule still present -> filter retained.
        await pilot.press('f5')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._rule_filter == 'llm_schema_drift'
        assert len(app.proposals) == 2

        # Drop the filtered rule from the backing data, refresh -> filter resets.
        client._findings = [f for f in findings if f['rule_name'] != 'llm_schema_drift']
        await pilot.press('f5')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._rule_filter is None
        assert len(app.proposals) == 2  # contradiction + cold_low_mw_unit


@pytest.mark.asyncio
async def test_filter_select_all_batch_dismiss_targets_exactly_filtered_set() -> None:
    """Headline flow: / -> pick rule -> a -> Enter (batch) -> dismiss the filtered set."""
    client = _FakeClient(_mixed_findings())  # 2 schema_drift + contradiction + cold
    app = ProposalCockpitApp(CockpitController(client), limit=50)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open_filter_and_pick(pilot, app, 2)  # llm_schema_drift
        schema_ids = {p.finding_id for p in app.proposals}
        assert len(schema_ids) == 2

        await pilot.press('a')  # select all (filtered)
        await pilot.pause()
        assert app._count_selected() == 2
        # LIST subtitle echoes the selection count.
        assert '2 selected' in app.sub_title

        await pilot.press('enter')  # >1 selected -> BATCH over the filtered rows
        await pilot.pause()
        assert app.mode == 'batch'
        assert {p.finding_id for p in app._batch_targets} == schema_ids

        from memex_cli.cockpit.controller import DISMISS_OPTION

        await app._submit_batch_async(app._batch_targets, DISMISS_OPTION, None)
        await app.workers.wait_for_complete()
        await pilot.pause()

    # Exactly the filtered set was dismissed — no contradiction/cold rows touched.
    assert {d['finding_id'] for d in client.dismisses} == schema_ids


@pytest.mark.asyncio
async def test_select_all_key_is_inert_in_collapse_mode() -> None:
    """'a' applies the merge in COLLAPSE mode; it must NOT trigger select-all."""
    client = _FakeClient([_entity_collapse_finding()])
    app = ProposalCockpitApp(CockpitController(client), limit=5)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press('enter')  # entity_collapse_cluster -> COLLAPSE mode
        await pilot.pause()
        assert app.mode == 'collapse'

        await pilot.press('a')  # apply merge — not select-all
        await app.workers.wait_for_complete()
        await pilot.pause()

    # The collapse 'a' resolved the finding (an apply), and select-all never ran
    # (it is mode-guarded to 'list'); the resolve fired, not a no-op.
    assert client.resolves, "'a' in collapse mode should apply the merge"
