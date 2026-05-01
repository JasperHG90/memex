"""F8 — Hermes parity for memex_get_lint_flags (AC-F8-4).

Schema mirrors the MCP tool surface; the handler dispatches to
``api.lint_get_flags`` via ``run_sync`` (matches the diagnostics-summary
pattern).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from memex_hermes_plugin.memex.tools import (
    GET_LINT_FLAGS_SCHEMA,
    HANDLERS,
    handle_get_lint_flags,
)


def test_schema_is_registered_with_correct_handler() -> None:
    """Both the schema and handler MUST be in the dispatch table."""
    assert GET_LINT_FLAGS_SCHEMA['name'] == 'memex_get_lint_flags'
    assert HANDLERS['memex_get_lint_flags'] is handle_get_lint_flags


def test_schema_advertises_documented_parameters() -> None:
    """The Hermes schema mirrors the MCP signature exactly."""
    props = GET_LINT_FLAGS_SCHEMA['parameters']['properties']
    assert set(props.keys()) == {'vault_id', 'lint_type', 'status', 'limit', 'cursor'}
    assert props['lint_type']['enum'] == ['structural', 'quality', 'governance', 'schema']
    assert props['status']['enum'] == ['pending', 'resolved', 'dismissed']
    assert props['status']['default'] == 'pending'
    assert props['limit']['default'] == 20
    assert props['limit']['maximum'] == 200
    assert GET_LINT_FLAGS_SCHEMA['parameters']['required'] == []


def test_handler_passes_through_to_api(monkeypatch) -> None:
    """The handler dispatches to api.lint_get_flags with the documented kwargs."""
    expected_payload = {
        'findings': [
            {
                'finding_id': str(uuid4()),
                'target_id': 'unit-a',
                'lint_type': 'quality',
                'evidence': {'mw_score': 0.21},
                'suggested_action': 'deprioritize',
                'status': 'pending',
            }
        ],
        'next_cursor': None,
    }
    api = Mock()
    api.lint_get_flags = AsyncMock(return_value=expected_payload)
    api.resolve_vault_identifier = AsyncMock(
        return_value=UUID('00000000-0000-0000-0000-000000000123')
    )
    config = Mock()

    result = handle_get_lint_flags(
        api, config, vault_id=None, args={'vault_id': 'my-vault', 'lint_type': 'quality'}
    )
    parsed = json.loads(result)
    assert parsed == expected_payload
    # The handler must pass the resolved vault UUID, not the raw name.
    api.lint_get_flags.assert_awaited_once()
    await_args = api.lint_get_flags.await_args
    assert await_args is not None
    kwargs = await_args.kwargs
    assert kwargs['vault_id'] == '00000000-0000-0000-0000-000000000123'
    assert kwargs['lint_type'] == 'quality'
    assert kwargs['status'] == 'pending'
    assert kwargs['limit'] == 20


def test_handler_uses_session_bound_vault_when_no_arg() -> None:
    """No vault_id arg + session-bound vault → handler passes the bound UUID."""
    api = Mock()
    api.lint_get_flags = AsyncMock(return_value={'findings': [], 'next_cursor': None})
    config = Mock()
    bound = UUID('11111111-1111-1111-1111-111111111111')

    handle_get_lint_flags(api, config, vault_id=bound, args={})

    await_args = api.lint_get_flags.await_args
    assert await_args is not None
    kwargs = await_args.kwargs
    assert kwargs['vault_id'] == str(bound)


def test_handler_returns_tool_error_on_api_failure() -> None:
    """An API failure surfaces as a tool_error JSON envelope, not a raise."""
    api = Mock()
    api.lint_get_flags = AsyncMock(side_effect=RuntimeError('boom'))
    api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
    config = Mock()

    result = handle_get_lint_flags(api, config, vault_id=None, args={'vault_id': 'x'})
    parsed = json.loads(result)
    assert 'error' in parsed
    assert 'boom' in parsed.get('message', '') or 'boom' in parsed.get('error', '')
