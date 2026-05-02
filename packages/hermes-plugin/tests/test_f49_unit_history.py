"""F49 — Hermes plugin tests for memex_get_unit_history.

Verifies:
- Schema is registered in ALL_SCHEMAS exactly once.
- Handler is wired into HANDLERS.
- MemexAPIProtocol declares get_unit_history (so structural typing works).
- Schema shape matches MCP signature parity (CLAUDE.md tool-surface parity rule).
- Dispatch passes the resolved session-bound vault_id (no agent-supplied vault).
- Dispatch validates the unit_id UUID and max_depth.
- Dispatch refuses to run when no vault is bound.
- The handler serializes UnitHistoryNodeDTO (model_dump) into JSON.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from memex_common.schemas import UnitHistoryNodeDTO
from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.tools import (
    ALL_SCHEMAS,
    GET_UNIT_HISTORY_SCHEMA,
    HANDLERS,
    MemexAPIProtocol,
    dispatch,
    handle_get_unit_history,
)


@pytest.fixture
def config() -> HermesMemexConfig:
    return HermesMemexConfig()


@pytest.fixture
def vault_id():
    return uuid4()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_schema_registered_in_all_schemas():
    """memex_get_unit_history must appear in ALL_SCHEMAS exactly once."""
    matches = [s for s in ALL_SCHEMAS if s['name'] == 'memex_get_unit_history']
    assert len(matches) == 1, f'expected 1 get_unit_history schema, got {len(matches)}'
    assert matches[0] is GET_UNIT_HISTORY_SCHEMA


def test_handler_dispatchable():
    """HANDLERS routing entry exists and points at the right function."""
    assert HANDLERS.get('memex_get_unit_history') is handle_get_unit_history


def test_protocol_method_present():
    """MemexAPIProtocol must declare get_unit_history."""
    assert hasattr(MemexAPIProtocol, 'get_unit_history')


def test_schema_shape():
    """Schema mirrors the MCP signature: unit_id required, max_depth optional.

    vault_id is intentionally NOT exposed in the schema — the handler injects
    it from the Hermes session binding (per CLAUDE.md vault routing pattern).
    """
    props = GET_UNIT_HISTORY_SCHEMA['parameters']['properties']
    assert 'unit_id' in props
    assert 'max_depth' in props
    assert 'vault_id' not in props, (
        'vault_id must not be in schema — handler injects from session binding'
    )
    assert GET_UNIT_HISTORY_SCHEMA['parameters']['required'] == ['unit_id']
    assert props['max_depth']['type'] == 'integer'


def test_schema_description_calls_out_supersession_history():
    """Docstring must explicitly note the v1 limitation (supersession only)."""
    desc = GET_UNIT_HISTORY_SCHEMA['description'].lower()
    assert 'supersession history' in desc
    assert 'forward=true' in desc or 'forward' in desc


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _make_history(root_id) -> UnitHistoryNodeDTO:
    pred = UnitHistoryNodeDTO(
        unit_id=uuid4(),
        text='older',
        confidence=0.5,
        event_date=datetime(2026, 4, 25, tzinfo=timezone.utc),
        link_type='contradicts',
        link_metadata={'reasoning': 'overrides'},
        depth=1,
    )
    return UnitHistoryNodeDTO(
        unit_id=root_id,
        text='newer',
        confidence=1.0,
        event_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
        link_type=None,
        link_metadata={},
        depth=0,
        predecessors=[pred],
    )


def test_dispatch_passes_session_vault_id(config, vault_id):
    """Handler injects the session-bound vault_id into the api call."""
    root_id = uuid4()
    api = Mock()
    api.get_unit_history = AsyncMock(return_value=_make_history(root_id))

    out = dispatch(
        'memex_get_unit_history',
        {'unit_id': str(root_id), 'max_depth': 5},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['unit_id'] == str(root_id)
    assert data['depth'] == 0
    assert len(data['predecessors']) == 1
    api.get_unit_history.assert_awaited_once()
    call = api.get_unit_history.await_args
    assert call is not None
    assert call.args == (root_id,)
    assert call.kwargs == {'vault_id': vault_id, 'max_depth': 5}


def test_dispatch_default_max_depth(config, vault_id):
    """Omitted max_depth uses the default of 10."""
    root_id = uuid4()
    api = Mock()
    api.get_unit_history = AsyncMock(return_value=_make_history(root_id))

    dispatch(
        'memex_get_unit_history',
        {'unit_id': str(root_id)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    api.get_unit_history.assert_awaited_once()
    call = api.get_unit_history.await_args
    assert call is not None
    assert call.kwargs['max_depth'] == 10


def test_dispatch_rejects_invalid_unit_id(config, vault_id):
    api = Mock()
    api.get_unit_history = AsyncMock()
    out = dispatch(
        'memex_get_unit_history',
        {'unit_id': 'not-a-uuid'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'invalid' in data['error'].lower() or 'uuid' in data['error'].lower()
    api.get_unit_history.assert_not_awaited()


def test_dispatch_requires_session_vault(config):
    """No session-bound vault -> tool_error and no api call."""
    api = Mock()
    api.get_unit_history = AsyncMock()
    out = dispatch(
        'memex_get_unit_history',
        {'unit_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=None,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'vault' in data['error'].lower()
    api.get_unit_history.assert_not_awaited()


def test_dispatch_rejects_negative_max_depth(config, vault_id):
    api = Mock()
    api.get_unit_history = AsyncMock()
    out = dispatch(
        'memex_get_unit_history',
        {'unit_id': str(uuid4()), 'max_depth': -1},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'max_depth' in data['error'].lower()
    api.get_unit_history.assert_not_awaited()


def test_dispatch_surfaces_api_error(config, vault_id):
    """Underlying api errors become tool_error JSON envelopes."""
    api = Mock()
    api.get_unit_history = AsyncMock(side_effect=RuntimeError('boom'))
    out = dispatch(
        'memex_get_unit_history',
        {'unit_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'boom' in data['error']
