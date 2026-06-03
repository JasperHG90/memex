"""Unit tests for the entity-collapse resolve carveout's member-subset support.

The cockpit lets a reviewer deselect cluster members; the server must merge
ONLY the selected members into the winner and leave the rest as separate
entities. These tests mock the DB collapse so they assert the loser-set
computation + validation deterministically.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from memex_core.server import lint as lint_mod


def _finding(members: list[str], winner: str) -> dict:
    return {
        'id': str(uuid4()),
        'rule_name': 'entity_collapse_cluster',
        'target_id': winner,
        'evidence': {
            'cluster_members': members,
            'suggested_winner_id': winner,
            'vaults_affected': [str(uuid4())],
        },
    }


def _api() -> MagicMock:
    api = MagicMock()
    api.entities.collapse_cluster = AsyncMock(return_value={'cluster_size': 2})
    api.lint.set_status = AsyncMock(return_value=True)
    return api


@pytest.mark.asyncio
async def test_subset_merges_only_selected_members():
    w, l1, l2 = str(uuid4()), str(uuid4()), str(uuid4())
    api = _api()
    with patch.object(lint_mod, 'check_vault_access', AsyncMock(return_value=None)):
        result = await lint_mod._resolve_entity_collapse_cluster(
            finding=_finding([w, l1, l2], w),
            api=api,
            auth=None,
            params={'winner_id': w, 'member_ids': [w, l1]},
        )
    kwargs = api.entities.collapse_cluster.await_args.kwargs
    assert kwargs['winner_id'] == UUID(w)
    assert kwargs['loser_ids'] == [UUID(l1)]  # l2 was deselected → left separate
    assert result['status'] == 'resolved'


@pytest.mark.asyncio
async def test_resolve_endpoint_routes_top_level_member_ids_to_subset():
    """Full route path: the cockpit sends winner_id/member_ids as TOP-LEVEL body
    keys (via the client's legacy_params); the ``lint_resolve`` endpoint passes
    the whole body as ``params`` to the carveout, so the subset reaches
    collapse_cluster. Closes the client→route→function glue gap.
    """
    w, l1, l2 = str(uuid4()), str(uuid4()), str(uuid4())
    finding = _finding([w, l1, l2], w)
    api = _api()
    with (
        patch.object(lint_mod, '_load_finding_or_404', AsyncMock(return_value=finding)),
        patch.object(lint_mod, 'check_vault_access', AsyncMock(return_value=None)),
    ):
        result = await lint_mod.lint_resolve(
            finding_id=UUID(finding['id']),
            api=api,
            auth=None,
            # Mirrors the on-the-wire body: no 'action', top-level winner/members.
            payload={'winner_id': w, 'member_ids': [w, l1]},
        )
    assert result['status'] == 'resolved'
    assert api.entities.collapse_cluster.await_args.kwargs['loser_ids'] == [UUID(l1)]


@pytest.mark.asyncio
async def test_no_subset_merges_whole_cluster():
    w, l1, l2 = str(uuid4()), str(uuid4()), str(uuid4())
    api = _api()
    with patch.object(lint_mod, 'check_vault_access', AsyncMock(return_value=None)):
        await lint_mod._resolve_entity_collapse_cluster(
            finding=_finding([w, l1, l2], w), api=api, auth=None, params={'winner_id': w}
        )
    assert api.entities.collapse_cluster.await_args.kwargs['loser_ids'] == [UUID(l1), UUID(l2)]


@pytest.mark.asyncio
async def test_subset_with_non_cluster_member_rejected():
    w, l1 = str(uuid4()), str(uuid4())
    stranger = str(uuid4())
    api = _api()
    with patch.object(lint_mod, 'check_vault_access', AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await lint_mod._resolve_entity_collapse_cluster(
                finding=_finding([w, l1], w),
                api=api,
                auth=None,
                params={'winner_id': w, 'member_ids': [w, stranger]},
            )
    assert exc.value.status_code == 400
    api.entities.collapse_cluster.assert_not_called()


@pytest.mark.asyncio
async def test_subset_excluding_winner_rejected():
    w, l1 = str(uuid4()), str(uuid4())
    api = _api()
    with patch.object(lint_mod, 'check_vault_access', AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await lint_mod._resolve_entity_collapse_cluster(
                finding=_finding([w, l1], w),
                api=api,
                auth=None,
                params={'winner_id': w, 'member_ids': [l1]},
            )
    assert exc.value.status_code == 400
    api.entities.collapse_cluster.assert_not_called()


@pytest.mark.asyncio
async def test_no_subset_single_member_cluster_message():
    """No-subset path on a degenerate ≤1-member cluster reports the cluster has
    no members — not the subset-selection message."""
    w = str(uuid4())
    api = _api()
    with patch.object(lint_mod, 'check_vault_access', AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await lint_mod._resolve_entity_collapse_cluster(
                finding=_finding([w], w), api=api, auth=None, params={'winner_id': w}
            )
    assert exc.value.status_code == 400
    assert 'no members' in exc.value.detail
    assert 'select at least two' not in exc.value.detail
    api.entities.collapse_cluster.assert_not_called()
