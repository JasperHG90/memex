"""Tests for RemoteMemexAPI.record_outcome() — HTTP wrapper.

Verifies the wrapper preserves the ADD-2 contract (positional ``unit_ids``
+ ``success``, keyword-only ``units``) and forwards body params to
/api/v1/outcomes/record correctly.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from memex_common.client import RemoteMemexAPI


@pytest.fixture
def mock_client():
    """httpx.AsyncClient stub that captures the POST body."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


def _capture_post(captured: dict):
    async def _post(path, json=None, **kwargs):
        captured['path'] = path
        captured['json'] = json
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = {
            'units_updated': 1,
            'entities_updated': 0,
            'models_updated': 0,
        }
        response.raise_for_status = MagicMock()
        return response

    return _post


@pytest.mark.asyncio
async def test_memory_unit_passthrough(mock_client):
    """memory_unit shape: unit_ids + success forwarded."""
    api = RemoteMemexAPI(mock_client)
    captured: dict = {}
    mock_client.post = _capture_post(captured)

    await api.record_outcome(['unit-1', 'unit-2'], True, vault_id='vault-uuid')

    assert captured['path'] == 'outcomes/record'
    assert captured['json']['unit_ids'] == ['unit-1', 'unit-2']
    assert captured['json']['success'] is True
    assert captured['json']['vault_id'] == 'vault-uuid'
    assert captured['json']['outcome_confidence'] == 1.0
    assert 'reason' not in captured['json']


@pytest.mark.asyncio
async def test_optional_fields_only_sent_when_set(mock_client):
    """outcome_confidence and reason flow through when supplied."""
    api = RemoteMemexAPI(mock_client)
    captured: dict = {}
    mock_client.post = _capture_post(captured)

    await api.record_outcome(
        ['u1'],
        True,
        vault_id='v',
        outcome_confidence=0.5,
        reason='partial — only one unit was actually used',
    )

    assert captured['json']['outcome_confidence'] == 0.5
    assert captured['json']['reason'] == 'partial — only one unit was actually used'


def test_signature_matches_in_process_api():
    """RemoteMemexAPI.record_outcome must mirror MemexAPI.record_outcome.

    Preferred shape is ``units=[UnitOutcome, ...]``; legacy
    ``(unit_ids, success)`` shape is still accepted and emits FutureWarning
    server-side. ``units`` must stay keyword-only so positional drift cannot
    collide with ``unit_ids``.
    """
    sig = inspect.signature(RemoteMemexAPI.record_outcome)
    params = sig.parameters

    assert params['unit_ids'].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params['success'].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params['units'].kind == inspect.Parameter.KEYWORD_ONLY
