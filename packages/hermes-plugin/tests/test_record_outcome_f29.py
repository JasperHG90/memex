"""Hermes-side tests for F29 — memex_record_outcome schema + handler.

Pairs with packages/common/tests/test_client_record_outcome.py (HTTP wrapper)
and tests/test_e2e_f29_outcomes_route.py (HTTP route). This file covers the
agent-facing surface: schema registration, dispatch routing, dual-mode
parameter handling, and the F14 ADD-2 invariant (success has no default).
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.tools import (
    ALL_SCHEMAS,
    HANDLERS,
    RECORD_OUTCOME_SCHEMA,
    MemexAPIProtocol,
    dispatch,
    handle_record_outcome,
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
    """memex_record_outcome must appear in ALL_SCHEMAS exactly once."""
    matches = [s for s in ALL_SCHEMAS if s['name'] == 'memex_record_outcome']
    assert len(matches) == 1, f'expected 1 record_outcome schema, got {len(matches)}'
    assert matches[0] is RECORD_OUTCOME_SCHEMA


def test_handler_dispatchable():
    """HANDLERS routing entry exists and points at the right function."""
    assert HANDLERS.get('memex_record_outcome') is handle_record_outcome


def test_schema_shape():
    """Schema covers both modes; only success is required at the schema level
    (mode-specific requirements are validated in the handler/route since they
    depend on target_type — the JSON-schema ``required`` array can't express
    that conditionally without oneOf bloat)."""
    props = RECORD_OUTCOME_SCHEMA['parameters']['properties']
    assert {'success', 'unit_ids', 'kv_key', 'target_type', 'vault_id'} <= set(props)
    assert RECORD_OUTCOME_SCHEMA['parameters']['required'] == ['success']
    assert props['target_type']['enum'] == ['memory_unit', 'kv_key']
    assert props['outcome_confidence']['minimum'] == 0.0
    assert props['outcome_confidence']['maximum'] == 1.0


def test_protocol_method_present():
    """MemexAPIProtocol must declare record_outcome so static type tools and
    structural-type asserts (Mock(spec=MemexAPIProtocol)) work."""
    assert hasattr(MemexAPIProtocol, 'record_outcome')


# ---------------------------------------------------------------------------
# Dispatch — memory_unit mode
# ---------------------------------------------------------------------------


def test_dispatch_memory_unit_passthrough(config, vault_id):
    """dispatch routes memex_record_outcome through handle_record_outcome,
    which calls api.record_outcome with positional unit_ids/success and the
    keyword-only target_type/kv_key."""
    api = Mock()
    api.record_outcome = AsyncMock(
        return_value={'units_updated': 2, 'entities_updated': 0, 'models_updated': 0}
    )

    out = dispatch(
        'memex_record_outcome',
        {'success': True, 'unit_ids': ['u1', 'u2'], 'target_type': 'memory_unit'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['units_updated'] == 2
    api.record_outcome.assert_awaited_once()
    call = api.record_outcome.await_args
    assert call is not None
    # ADD-2: unit_ids + success are positional, target_type + kv_key kwargs.
    assert call.args == (
        ['u1', 'u2'],  # unit_ids
        True,  # success
        str(vault_id),  # vault_id (defaulted from session-bound)
        1.0,  # outcome_confidence
        None,  # reason
    )
    assert call.kwargs == {'target_type': 'memory_unit', 'kv_key': None}


def test_dispatch_kv_key_mode(config, vault_id):
    """kv_key mode: unit_ids omitted, kv_key passed via kwargs."""
    api = Mock()
    api.record_outcome = AsyncMock(
        return_value={
            'kv_key': 'procedure:run-tests:default',
            'vault_id': str(vault_id),
            'success_co_count': 1,
            'failure_co_count': 0,
            'last_outcome_at': '2026-05-01T05:59:00Z',
        }
    )

    out = dispatch(
        'memex_record_outcome',
        {
            'success': False,
            'kv_key': 'procedure:run-tests:default',
            'target_type': 'kv_key',
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['kv_key'] == 'procedure:run-tests:default'
    call = api.record_outcome.await_args
    assert call is not None
    assert call.args[0] is None  # unit_ids
    assert call.args[1] is False  # success
    assert call.kwargs['target_type'] == 'kv_key'
    assert call.kwargs['kv_key'] == 'procedure:run-tests:default'


def test_dispatch_explicit_vault_overrides_session(config, vault_id):
    """When args carry vault_id, it overrides the session-bound vault."""
    api = Mock()
    api.record_outcome = AsyncMock(return_value={'units_updated': 0})
    explicit = uuid4()
    dispatch(
        'memex_record_outcome',
        {'success': True, 'unit_ids': ['u1'], 'vault_id': str(explicit)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call = api.record_outcome.await_args
    assert call is not None
    assert call.args[2] == str(explicit)


# ---------------------------------------------------------------------------
# Bad inputs (handler-level validation)
# ---------------------------------------------------------------------------


def test_missing_success_returns_tool_error(config, vault_id):
    """ADD-2 invariant: success is required; absent → tool_error JSON.

    The handler must reject before calling api.record_outcome — a regression
    that defaulted success=False would silently train MW counters with
    failure signals on every kwargless agent call. The api mock is left
    un-stubbed so any call would raise AttributeError and surface here.
    """
    api = Mock(spec=[])  # spec=[] means no attributes; any access raises.
    out = dispatch(
        'memex_record_outcome',
        {'unit_ids': ['u1']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)


def test_invalid_target_type_returns_tool_error(config, vault_id):
    """target_type must be 'memory_unit' or 'kv_key'."""
    api = Mock()
    out = dispatch(
        'memex_record_outcome',
        {'success': True, 'target_type': 'something-else'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)


def test_non_bool_success_returns_tool_error(config, vault_id):
    """success must be a JSON boolean, not 0/1/'true'."""
    api = Mock()
    out = dispatch(
        'memex_record_outcome',
        {'success': 1, 'unit_ids': ['u1']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)


def test_api_error_surfaces_as_tool_error(config, vault_id):
    """Underlying api.record_outcome RuntimeError → tool_error envelope, not crash."""
    api = Mock()
    api.record_outcome = AsyncMock(side_effect=RuntimeError('vault not found'))
    out = dispatch(
        'memex_record_outcome',
        {'success': True, 'unit_ids': ['u1']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    payload = json.loads(out)
    assert 'error' in payload
    assert 'vault not found' in payload.get('message', payload.get('error', ''))


# ---------------------------------------------------------------------------
# F14 ADD-2 contract — handler signature lock
# ---------------------------------------------------------------------------


def test_handler_passes_keyword_only_args_as_kwargs():
    """Source-level lock: handle_record_outcome must call api.record_outcome
    with target_type and kv_key as keyword arguments, not positional.

    The api signature has those as keyword-only (per the in-process MemexAPI
    contract); a regression that passed them positionally would only fail
    against a real RemoteMemexAPI, not against AsyncMock — so we lock the
    call shape here."""
    src = inspect.getsource(handle_record_outcome)
    # The api call must use target_type=... and kv_key=... — never the
    # positional 6th/7th arg form. Line breaks accepted; rely on the keyword
    # token presence.
    assert 'target_type=target_type' in src, 'target_type must be passed as kwarg'
    assert 'kv_key=kv_key' in src, 'kv_key must be passed as kwarg'
