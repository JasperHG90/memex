"""Tests for RemoteMemexAPI.record_outcome() — F29 HTTP wrapper.

Verifies the wrapper preserves the F14 ADD-2 contract (positional
``unit_ids`` + ``success``, keyword-only ``target_type`` / ``kv_key``)
and forwards body params to /api/v1/outcomes/record correctly.
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
    """Default memory_unit mode: unit_ids + success forwarded; target_type defaults."""
    api = RemoteMemexAPI(mock_client)
    captured: dict = {}
    mock_client.post = _capture_post(captured)

    await api.record_outcome(['unit-1', 'unit-2'], True, vault_id='vault-uuid')

    assert captured['path'] == 'outcomes/record'
    assert captured['json']['unit_ids'] == ['unit-1', 'unit-2']
    assert captured['json']['success'] is True
    assert captured['json']['vault_id'] == 'vault-uuid'
    assert captured['json']['target_type'] == 'memory_unit'
    assert captured['json']['outcome_confidence'] == 1.0
    assert 'kv_key' not in captured['json']
    assert 'reason' not in captured['json']


@pytest.mark.asyncio
async def test_kv_key_mode(mock_client):
    """kv_key mode: keyword-only target_type='kv_key' + kv_key forwarded; unit_ids omitted."""
    api = RemoteMemexAPI(mock_client)
    captured: dict = {}
    mock_client.post = _capture_post(captured)

    await api.record_outcome(
        None,
        False,
        vault_id='vault-uuid',
        target_type='kv_key',
        kv_key='procedure:run-tests:default',
    )

    assert captured['json']['success'] is False
    assert captured['json']['target_type'] == 'kv_key'
    assert captured['json']['kv_key'] == 'procedure:run-tests:default'
    assert 'unit_ids' not in captured['json']


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


# ---------------------------------------------------------------------------
# F14 ADD-2 contract preservation
# ---------------------------------------------------------------------------


def test_signature_matches_in_process_api():
    """ADD-2 invariant: RemoteMemexAPI.record_outcome must mirror MemexAPI.record_outcome.

    Specifically: unit_ids + success are positional-or-keyword, target_type
    and kv_key are keyword-only. A drift here means a Hermes call that works
    in-process could fail (or worse, silently misbehave) over the wire.
    """
    sig = inspect.signature(RemoteMemexAPI.record_outcome)
    params = sig.parameters

    # Positional-or-keyword (ADD-2: success has no default — required).
    assert params['unit_ids'].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params['success'].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params['success'].default is inspect.Parameter.empty, (
        'success must have no default — kwargless calls cannot silently record FAILURE'
    )

    # Keyword-only — drift here would let a positional 7th-arg call collide.
    assert params['target_type'].kind == inspect.Parameter.KEYWORD_ONLY
    assert params['kv_key'].kind == inspect.Parameter.KEYWORD_ONLY
