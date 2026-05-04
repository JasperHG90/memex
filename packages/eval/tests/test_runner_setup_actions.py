"""Tests for _run_setup_actions and _resolve_unit_ids in the runner."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from memex_eval.internal.runner import _resolve_unit_ids, _run_setup_actions
from memex_eval.internal.scenarios import SetupAction
from memex_common.schemas import MemoryUnitDTO


def _make_action(**overrides) -> SetupAction:
    defaults = {
        'kind': 'record_outcome',
        'search_query': None,
        'unit_ids': None,
        'success': True,
        'reason': None,
        'kv_key': None,
        'kv_value': None,
        'count': 1,
    }
    defaults.update(overrides)
    return SetupAction(**defaults)


# ---------------------------------------------------------------------------
# _resolve_unit_ids
# ---------------------------------------------------------------------------


class TestResolveUnitIds:
    @pytest.mark.asyncio
    async def test_returns_explicit_ids_when_provided(self) -> None:
        api = AsyncMock()
        ids = [str(uuid4()), str(uuid4())]
        action = _make_action(unit_ids=ids)
        result = await _resolve_unit_ids(api, uuid4(), action)
        assert result == ids
        api.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_searches_when_no_explicit_ids(self) -> None:
        unit_id = uuid4()
        api = AsyncMock()
        api.search.return_value = [
            MemoryUnitDTO(id=unit_id, text='test', fact_type='world', status='active'),
        ]
        action = _make_action(search_query='Project Alpha')
        vault_id = uuid4()
        result = await _resolve_unit_ids(api, vault_id, action)
        api.search.assert_called_once_with(
            query='Project Alpha',
            limit=5,
            vault_ids=[vault_id],
        )
        assert result == [str(unit_id)]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_query_or_ids(self) -> None:
        api = AsyncMock()
        action = _make_action(search_query=None, unit_ids=None)
        result = await _resolve_unit_ids(api, uuid4(), action)
        assert result == []
        api.search.assert_not_called()


# ---------------------------------------------------------------------------
# _run_setup_actions — record_outcome
# ---------------------------------------------------------------------------


class TestRunSetupActionsRecordOutcome:
    @pytest.mark.asyncio
    async def test_records_outcome_with_explicit_ids(self) -> None:
        api = AsyncMock()
        uid = str(uuid4())
        action = _make_action(kind='record_outcome', unit_ids=[uid], success=True, count=2)
        vault_id = uuid4()
        await _run_setup_actions(api, vault_id, [action])
        assert api.record_outcome.call_count == 2
        api.record_outcome.assert_called_with(
            unit_ids=[uid],
            success=True,
            vault_id=str(vault_id),
            reason=None,
        )

    @pytest.mark.asyncio
    async def test_records_outcome_with_search_discovery(self) -> None:
        unit_id = uuid4()
        api = AsyncMock()
        api.search.return_value = [
            MemoryUnitDTO(id=unit_id, text='achievement', fact_type='world', status='active'),
        ]
        action = _make_action(
            kind='record_outcome',
            search_query='achievement',
            success=True,
            count=1,
        )
        vault_id = uuid4()
        await _run_setup_actions(api, vault_id, [action])
        api.record_outcome.assert_called_once_with(
            unit_ids=[str(unit_id)],
            success=True,
            vault_id=str(vault_id),
            reason=None,
        )

    @pytest.mark.asyncio
    async def test_skips_when_no_units_found(self) -> None:
        api = AsyncMock()
        api.search.return_value = []
        action = _make_action(
            kind='record_outcome',
            search_query='nonexistent',
            success=True,
        )
        await _run_setup_actions(api, uuid4(), [action])
        api.record_outcome.assert_not_called()


# ---------------------------------------------------------------------------
# _run_setup_actions — deprioritize
# ---------------------------------------------------------------------------


class TestRunSetupActionsDeprioritize:
    @pytest.mark.asyncio
    async def test_deprioritize_with_explicit_ids(self) -> None:
        api = AsyncMock()
        uid_str = str(uuid4())
        action = _make_action(kind='deprioritize', unit_ids=[uid_str], reason='test')
        vault_id = uuid4()
        await _run_setup_actions(api, vault_id, [action])
        api.deprioritize_memory_unit.assert_called_once_with(
            unit_id=UUID(uid_str),
            reason='test',
            vault_id=vault_id,
        )

    @pytest.mark.asyncio
    async def test_deprioritize_with_search_discovery(self) -> None:
        unit_id = uuid4()
        api = AsyncMock()
        api.search.return_value = [
            MemoryUnitDTO(id=unit_id, text='Widget Lite', fact_type='world', status='active'),
        ]
        action = _make_action(
            kind='deprioritize',
            search_query='Widget Lite discontinued',
            reason='deprecated product',
        )
        vault_id = uuid4()
        await _run_setup_actions(api, vault_id, [action])
        api.deprioritize_memory_unit.assert_called_once_with(
            unit_id=unit_id,
            reason='deprecated product',
            vault_id=vault_id,
        )


# ---------------------------------------------------------------------------
# _run_setup_actions — kv_write
# ---------------------------------------------------------------------------


class TestRunSetupActionsKvWrite:
    @pytest.mark.asyncio
    async def test_kv_write(self) -> None:
        api = AsyncMock()
        action = _make_action(
            kind='kv_write',
            kv_key='procedure:deploy:staging',
            kv_value='Use --no-migrate flag',
        )
        await _run_setup_actions(api, uuid4(), [action])
        api.kv_put.assert_called_once_with(
            value='Use --no-migrate flag',
            key='procedure:deploy:staging',
        )


# ---------------------------------------------------------------------------
# _run_setup_actions — consolidation_tick
# ---------------------------------------------------------------------------


class TestRunSetupActionsConsolidationTick:
    @pytest.mark.asyncio
    async def test_consolidation_tick(self) -> None:
        api = AsyncMock()
        action = _make_action(kind='consolidation_tick')
        vault_id = uuid4()
        await _run_setup_actions(api, vault_id, [action])
        api.consolidation_tick.assert_called_once_with(vault_id=vault_id)


# ---------------------------------------------------------------------------
# _run_setup_actions — unknown kind
# ---------------------------------------------------------------------------


class TestRunSetupActionsUnknownKind:
    @pytest.mark.asyncio
    async def test_unknown_kind_logs_warning(self) -> None:
        api = AsyncMock()
        action = _make_action(kind='unknown_action')
        await _run_setup_actions(api, uuid4(), [action])
        api.record_outcome.assert_not_called()


# ---------------------------------------------------------------------------
# _run_setup_actions — error handling
# ---------------------------------------------------------------------------


class TestRunSetupActionsErrorHandling:
    @pytest.mark.asyncio
    async def test_continues_after_error(self) -> None:
        api = AsyncMock()
        api.record_outcome.side_effect = Exception('API error')
        api.kv_put = AsyncMock()
        uid = str(uuid4())
        actions = [
            _make_action(kind='record_outcome', unit_ids=[uid], success=True),
            _make_action(kind='kv_write', kv_key='test', kv_value='value'),
        ]
        await _run_setup_actions(api, uuid4(), actions)
        api.kv_put.assert_called_once()
