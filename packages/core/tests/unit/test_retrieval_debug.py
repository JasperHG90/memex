"""Tests for TEMPR strategy debugging tools (P2-03)."""

import json
import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from memex_common.schemas import (
    MemoryUnitDTO,
    RetrievalRequest,
    StrategyDebugInfo,
)
from memex_common.types import FactTypes
from memex_core.memory.retrieval.engine import (
    DebugContext,
    StrategyContribution,
)
from memex_core.memory.retrieval.models import (
    RetrievalRequest as InternalRetrievalRequest,
)
from memex_core.server import app
from memex_core.server.common import get_api
from memex_core.server.common import build_memory_unit_dto


# ---------------------------------------------------------------------------
# Schema / Model Tests
# ---------------------------------------------------------------------------


class TestStrategyDebugInfoSchema:
    def test_strategy_debug_info_fields(self):
        info = StrategyDebugInfo(
            strategy_name='semantic',
            rank=1,
            rrf_score=0.016129,
            raw_score=0.85,
            timing_ms=12.5,
        )
        assert info.strategy_name == 'semantic'
        assert info.rank == 1
        assert info.rrf_score == 0.016129
        assert info.raw_score == 0.85
        assert info.timing_ms == 12.5

    def test_strategy_debug_info_optional_fields(self):
        info = StrategyDebugInfo(
            strategy_name='keyword',
            rank=3,
            rrf_score=0.01,
        )
        assert info.raw_score is None
        assert info.timing_ms is None

    def test_strategy_debug_info_serialization(self):
        info = StrategyDebugInfo(
            strategy_name='temporal',
            rank=2,
            rrf_score=0.012,
            raw_score=0.7,
            timing_ms=5.3,
        )
        data = info.model_dump()
        assert data == {
            'strategy_name': 'temporal',
            'rank': 2,
            'rrf_score': 0.012,
            'raw_score': 0.7,
            'timing_ms': 5.3,
        }


class TestRetrievalRequestDebugField:
    def test_http_request_debug_default_false(self):
        req = RetrievalRequest(query='test query')
        assert req.debug is False

    def test_http_request_debug_true(self):
        req = RetrievalRequest(query='test query', debug=True)
        assert req.debug is True

    def test_internal_request_debug_default_false(self):
        req = InternalRetrievalRequest(query='test query')
        assert req.debug is False

    def test_internal_request_debug_true(self):
        req = InternalRetrievalRequest(query='test query', debug=True)
        assert req.debug is True


class TestMemoryUnitDTODebugInfo:
    def test_dto_debug_info_none_by_default(self):
        dto = MemoryUnitDTO(
            id=uuid4(),
            text='test fact',
            fact_type=FactTypes.WORLD,
            status='active',
            vault_id=uuid4(),
        )
        assert dto.debug_info is None

    def test_dto_debug_info_populated(self):
        info = [
            StrategyDebugInfo(
                strategy_name='semantic',
                rank=1,
                rrf_score=0.016,
                raw_score=0.9,
                timing_ms=10.0,
            ),
            StrategyDebugInfo(
                strategy_name='keyword',
                rank=5,
                rrf_score=0.008,
                raw_score=None,
                timing_ms=3.0,
            ),
        ]
        dto = MemoryUnitDTO(
            id=uuid4(),
            text='test fact',
            fact_type=FactTypes.WORLD,
            status='active',
            vault_id=uuid4(),
            debug_info=info,
        )
        assert len(dto.debug_info) == 2
        assert dto.debug_info[0].strategy_name == 'semantic'
        assert dto.debug_info[1].strategy_name == 'keyword'

    def test_dto_debug_info_serialization(self):
        info = [
            StrategyDebugInfo(
                strategy_name='graph',
                rank=2,
                rrf_score=0.014,
            ),
        ]
        dto = MemoryUnitDTO(
            id=uuid4(),
            text='test',
            fact_type=FactTypes.WORLD,
            status='active',
            vault_id=uuid4(),
            debug_info=info,
        )
        data = dto.model_dump()
        assert data['debug_info'] is not None
        assert len(data['debug_info']) == 1
        assert data['debug_info'][0]['strategy_name'] == 'graph'


# ---------------------------------------------------------------------------
# Engine Dataclass Tests
# ---------------------------------------------------------------------------


class TestDebugContext:
    def test_debug_context_initial_state(self):
        ctx = DebugContext()
        assert ctx.strategy_timings == {}
        assert len(ctx.per_result) == 0

    def test_debug_context_records_timings(self):
        ctx = DebugContext()
        ctx.strategy_timings['semantic'] = 15.3
        ctx.strategy_timings['keyword'] = 5.1
        assert ctx.strategy_timings['semantic'] == 15.3
        assert ctx.strategy_timings['keyword'] == 5.1

    def test_debug_context_records_contributions(self):
        ctx = DebugContext()
        uid = uuid4()
        contrib = StrategyContribution(
            strategy_name='semantic',
            rank=1,
            rrf_score=0.016,
            raw_score=0.9,
            timing_ms=10.0,
        )
        ctx.per_result[uid].append(contrib)
        assert len(ctx.per_result[uid]) == 1
        assert ctx.per_result[uid][0].strategy_name == 'semantic'

    def test_debug_context_multiple_contributions(self):
        ctx = DebugContext()
        uid = uuid4()
        ctx.per_result[uid].append(
            StrategyContribution(
                strategy_name='semantic',
                rank=1,
                rrf_score=0.016,
            )
        )
        ctx.per_result[uid].append(
            StrategyContribution(
                strategy_name='keyword',
                rank=3,
                rrf_score=0.012,
            )
        )
        assert len(ctx.per_result[uid]) == 2


class TestStrategyContribution:
    def test_contribution_fields(self):
        c = StrategyContribution(
            strategy_name='semantic',
            rank=1,
            rrf_score=0.016129,
            raw_score=0.85,
            timing_ms=12.5,
        )
        assert c.strategy_name == 'semantic'
        assert c.rank == 1
        assert c.rrf_score == 0.016129
        assert c.raw_score == 0.85
        assert c.timing_ms == 12.5

    def test_contribution_optional_defaults(self):
        c = StrategyContribution(
            strategy_name='keyword',
            rank=2,
            rrf_score=0.01,
        )
        assert c.raw_score is None
        assert c.timing_ms is None


# ---------------------------------------------------------------------------
# DTO Builder Tests
# ---------------------------------------------------------------------------


class TestBuildRetrievalDtosDebug:
    def _make_unit(self, uid=None, debug_info=None):
        unit = SimpleNamespace(
            id=uid or uuid4(),
            note_id=uuid4(),
            text='test fact',
            fact_type=FactTypes.WORLD,
            status='active',
            mentioned_at=None,
            occurred_start=None,
            occurred_end=None,
            event_date=datetime.now(timezone.utc),
            vault_id=uuid4(),
            unit_metadata={},
            score=1.0,
        )
        if debug_info is not None:
            object.__setattr__(unit, '_debug_info', debug_info)
        return unit

    def test_no_debug_info_when_debug_false(self):
        unit = self._make_unit(
            debug_info=[
                StrategyContribution(
                    strategy_name='semantic',
                    rank=1,
                    rrf_score=0.016,
                )
            ]
        )
        dtos = [build_memory_unit_dto(unit, debug=False)]
        assert dtos[0].debug_info is None

    def test_debug_info_included_when_debug_true(self):
        contrib = StrategyContribution(
            strategy_name='semantic',
            rank=1,
            rrf_score=0.016,
            raw_score=0.9,
            timing_ms=10.0,
        )
        unit = self._make_unit(debug_info=[contrib])
        dtos = [build_memory_unit_dto(unit, debug=True)]
        assert dtos[0].debug_info is not None
        assert len(dtos[0].debug_info) == 1
        info = dtos[0].debug_info[0]
        assert info.strategy_name == 'semantic'
        assert info.rank == 1
        assert info.rrf_score == 0.016
        assert info.raw_score == 0.9
        assert info.timing_ms == 10.0

    def test_debug_info_none_when_no_debug_attr(self):
        unit = self._make_unit()  # no _debug_info attribute
        dtos = [build_memory_unit_dto(unit, debug=True)]
        assert dtos[0].debug_info is None

    def test_multiple_strategies_per_result(self):
        contribs = [
            StrategyContribution(
                strategy_name='semantic',
                rank=1,
                rrf_score=0.016,
            ),
            StrategyContribution(
                strategy_name='keyword',
                rank=3,
                rrf_score=0.012,
            ),
            StrategyContribution(
                strategy_name='graph',
                rank=2,
                rrf_score=0.014,
            ),
        ]
        unit = self._make_unit(debug_info=contribs)
        dtos = [build_memory_unit_dto(unit, debug=True)]
        assert len(dtos[0].debug_info) == 3
        strategy_names = {d.strategy_name for d in dtos[0].debug_info}
        assert strategy_names == {'semantic', 'keyword', 'graph'}


# ---------------------------------------------------------------------------
# Server Endpoint Integration Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_api():
    mock_api = AsyncMock()
    mock_api.config = SimpleNamespace(server=SimpleNamespace(active_vault='default-vault'))
    mock_api.resolve_vault_identifier.return_value = UUID('00000000-0000-0000-0000-000000000001')
    return mock_api


@pytest.fixture
def client(mock_api):
    app.dependency_overrides[get_api] = lambda: mock_api
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestSearchEndpointDebug:
    def test_debug_false_no_debug_info_in_response(self, client, mock_api):
        uid = uuid4()
        unit = SimpleNamespace(
            id=uid,
            note_id=uuid4(),
            text='Test fact',
            fact_type=FactTypes.WORLD,
            status='active',
            mentioned_at=None,
            occurred_start=None,
            occurred_end=None,
            event_date=datetime.now(timezone.utc),
            vault_id=uuid4(),
            unit_metadata={},
            score=1.0,
        )
        mock_api.search.return_value = ([unit], None)
        mock_api.resolve_source_notes.return_value = {}

        response = client.post(
            '/api/v1/memories/search',
            json={'query': 'test', 'limit': 10, 'debug': False},
        )
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.strip().split('\n') if line]
        assert data[0]['debug_info'] is None

    def test_debug_true_passes_debug_to_search(self, client, mock_api):
        mock_api.search.return_value = ([], None)
        mock_api.resolve_source_notes.return_value = {}

        client.post(
            '/api/v1/memories/search',
            json={'query': 'test', 'limit': 10, 'debug': True},
        )

        # Verify debug=True was passed to api.search()
        mock_api.search.assert_called_once()
        call_kwargs = mock_api.search.call_args
        assert call_kwargs.kwargs.get('debug') is True

    def test_debug_true_includes_debug_info(self, client, mock_api):
        uid = uuid4()
        unit = SimpleNamespace(
            id=uid,
            note_id=uuid4(),
            text='Test fact',
            fact_type=FactTypes.WORLD,
            status='active',
            mentioned_at=None,
            occurred_start=None,
            occurred_end=None,
            event_date=datetime.now(timezone.utc),
            vault_id=uuid4(),
            unit_metadata={},
            score=1.0,
        )
        # Simulate engine attaching debug info
        object.__setattr__(
            unit,
            '_debug_info',
            [
                StrategyContribution(
                    strategy_name='semantic',
                    rank=1,
                    rrf_score=0.016,
                    raw_score=0.9,
                    timing_ms=10.0,
                ),
            ],
        )
        mock_api.search.return_value = ([unit], None)
        mock_api.resolve_source_notes.return_value = {}

        response = client.post(
            '/api/v1/memories/search',
            json={'query': 'test', 'limit': 10, 'debug': True},
        )
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.strip().split('\n') if line]
        assert data[0]['debug_info'] is not None
        assert len(data[0]['debug_info']) == 1
        assert data[0]['debug_info'][0]['strategy_name'] == 'semantic'
        assert data[0]['debug_info'][0]['rank'] == 1
        assert data[0]['debug_info'][0]['rrf_score'] == 0.016
        assert data[0]['debug_info'][0]['raw_score'] == 0.9
        assert data[0]['debug_info'][0]['timing_ms'] == 10.0

    def test_search_endpoint_schedules_resonance_background_task(self, client, mock_api):
        uid = uuid4()
        unit = SimpleNamespace(
            id=uid,
            note_id=uuid4(),
            text='Test fact',
            fact_type=FactTypes.WORLD,
            status='active',
            mentioned_at=None,
            occurred_start=None,
            occurred_end=None,
            event_date=datetime.now(timezone.utc),
            vault_id=uuid4(),
            unit_metadata={},
            score=1.0,
        )
        mock_resonance_fn = AsyncMock()
        mock_api.search.return_value = ([unit], mock_resonance_fn)
        mock_api.resolve_source_notes.return_value = {}

        response = client.post(
            '/api/v1/memories/search',
            json={'query': 'test', 'limit': 10},
        )
        assert response.status_code == 200
        # FastAPI TestClient runs background tasks synchronously
        mock_resonance_fn.assert_called_once()

    def test_search_endpoint_no_resonance_when_none(self, client, mock_api):
        uid = uuid4()
        unit = SimpleNamespace(
            id=uid,
            note_id=uuid4(),
            text='Test fact',
            fact_type=FactTypes.WORLD,
            status='active',
            mentioned_at=None,
            occurred_start=None,
            occurred_end=None,
            event_date=datetime.now(timezone.utc),
            vault_id=uuid4(),
            unit_metadata={},
            score=1.0,
        )
        mock_api.search.return_value = ([unit], None)
        mock_api.resolve_source_notes.return_value = {}

        response = client.post(
            '/api/v1/memories/search',
            json={'query': 'test', 'limit': 10},
        )
        assert response.status_code == 200
