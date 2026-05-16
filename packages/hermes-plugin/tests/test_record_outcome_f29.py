"""Hermes-side tests for ``memex_record_outcome`` schema + handler.

Covers the agent-facing surface: schema registration, dispatch routing,
dual-shape parameter handling (per-unit ``units`` + legacy
``unit_ids``+``success``), and the ADD-2 invariant (``success`` has no
default).
"""

from __future__ import annotations

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


def test_schema_registered_in_all_schemas():
    """memex_record_outcome must appear in ALL_SCHEMAS exactly once."""
    matches = [s for s in ALL_SCHEMAS if s['name'] == 'memex_record_outcome']
    assert len(matches) == 1, f'expected 1 record_outcome schema, got {len(matches)}'
    assert matches[0] is RECORD_OUTCOME_SCHEMA


def test_handler_dispatchable():
    """HANDLERS routing entry exists and points at the right function."""
    assert HANDLERS.get('memex_record_outcome') is handle_record_outcome


def test_schema_shape():
    """Schema covers both shapes (per-unit `units` + legacy `unit_ids`/`success`)."""
    props = RECORD_OUTCOME_SCHEMA['parameters']['properties']
    assert {
        'success',
        'units',
        'unit_ids',
        'vault_id',
        'caller_id',
        'turn_outcome',
        'exploration_tagged',
    } <= set(props)
    assert 'target_type' not in props, 'target_type was removed with procedure MW'
    assert 'kv_key' not in props, 'kv_key was removed with procedure MW'
    assert props['outcome_confidence']['minimum'] == 0.0
    assert props['outcome_confidence']['maximum'] == 1.0
    assert props['caller_id']['type'] == 'string'
    assert props['turn_outcome']['type'] == 'string'
    assert props['exploration_tagged']['type'] == 'boolean'
    units_items = props['units']['items']
    assert units_items['properties']['verb']['enum'] == [
        'helpful',
        'not_helpful',
        'not_used',
    ]


def test_protocol_method_present():
    """MemexAPIProtocol must declare record_outcome so static type tools and
    structural-type asserts (Mock(spec=MemexAPIProtocol)) work."""
    assert hasattr(MemexAPIProtocol, 'record_outcome')


def test_dispatch_memory_unit_passthrough(config, vault_id):
    """dispatch routes memex_record_outcome through handle_record_outcome,
    which calls api.record_outcome with positional unit_ids/success."""
    api = Mock()
    api.record_outcome = AsyncMock(
        return_value={'units_updated': 2, 'entities_updated': 0, 'models_updated': 0}
    )

    out = dispatch(
        'memex_record_outcome',
        {'success': True, 'unit_ids': ['u1', 'u2']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['units_updated'] == 2
    api.record_outcome.assert_awaited_once()
    call = api.record_outcome.await_args
    assert call is not None
    assert call.args == (
        ['u1', 'u2'],
        True,
        str(vault_id),
        1.0,
        None,
    )
    assert call.kwargs == {
        'units': None,
        'caller_id': None,
        'turn_outcome': None,
        'retrieved_set_size': None,
        'exploration_tagged': False,
    }


def test_dispatch_audit_fields_passthrough(config, vault_id):
    """caller_id, turn_outcome, and exploration_tagged must flow to api.record_outcome
    so Hermes-driven outcomes carry the same audit context the HTTP route does."""
    api = Mock()
    api.record_outcome = AsyncMock(return_value={'units_updated': 1})
    dispatch(
        'memex_record_outcome',
        {
            'success': True,
            'unit_ids': ['u1'],
            'caller_id': 'hermes-session-abc',
            'turn_outcome': 'success',
            'exploration_tagged': True,
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call = api.record_outcome.await_args
    assert call is not None
    assert call.kwargs['caller_id'] == 'hermes-session-abc'
    assert call.kwargs['turn_outcome'] == 'success'
    assert call.kwargs['exploration_tagged'] is True


def test_dispatch_audit_fields_type_validation(config, vault_id):
    """Invalid types for the audit fields must error before the api call."""
    api = Mock(spec=[])
    for bad in (
        {'success': True, 'unit_ids': ['u1'], 'caller_id': 123},
        {'success': True, 'unit_ids': ['u1'], 'turn_outcome': 99},
        {'success': True, 'unit_ids': ['u1'], 'exploration_tagged': 'yes'},
    ):
        out = dispatch(
            'memex_record_outcome',
            bad,
            api=api,
            config=config,
            vault_id=vault_id,
        )
        assert 'error' in json.loads(out)


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


def test_missing_required_fields_returns_tool_error(config, vault_id):
    """record_outcome requires either `units` or legacy `unit_ids`+`success`."""
    api = Mock(spec=[])
    out = dispatch(
        'memex_record_outcome',
        {},
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


@pytest.mark.parametrize(
    'extras',
    [
        {'target_type': 'kv_key'},
        {'kv_key': 'procedure:foo:bar'},
        {'target_type': 'kv_key', 'kv_key': 'procedure:foo:bar'},
    ],
    ids=['target_type_only', 'kv_key_only', 'both'],
)
def test_legacy_kv_key_args_rejected(config, vault_id, extras):
    """Stale agents passing the removed target_type='kv_key' / kv_key must be
    rejected with a tool_error, not silently routed as a memory_unit call."""
    api = Mock(spec=[])
    out = dispatch(
        'memex_record_outcome',
        {'success': True, 'unit_ids': ['u1'], **extras},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    payload = json.loads(out)
    assert 'error' in payload
    msg = payload.get('message', payload.get('error', ''))
    assert 'target_type' in msg or 'kv_key' in msg or 'unknown' in msg


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
