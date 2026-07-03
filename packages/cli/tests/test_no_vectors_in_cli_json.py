"""Pin: CLI ``--json`` output never carries an ``embedding`` key.

Vector exposure is an HTTP/Python-caller capability; the CLI is an
operator/agent surface and must stay byte-identical to its pre-field
shape. The DTOs gained an ``embedding`` field, so a bare ``model_dump()``
would emit ``embedding: null`` — every CLI JSON render excludes it.
These tests feed vector-laden DTOs through the commands and walk the
parsed output, so a future render path that forgets the exclusion fails
loudly.
"""

import datetime as dt
import json
from uuid import uuid4

from memex_cli.kv import app as kv_app
from memex_cli.memory import app as memory_app
from memex_cli.vaults import app as vaults_app
from memex_common.schemas import KVEntryDTO, MemoryUnitDTO, VaultSummaryDTO


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _assert_no_embedding_key(data) -> None:
    for node in _walk(data):
        assert 'embedding' not in node, f'embedding key leaked into CLI JSON: {sorted(node)}'


def _unit(text: str) -> MemoryUnitDTO:
    return MemoryUnitDTO(id=uuid4(), text=text, fact_type='world', embedding=[0.1] * 8)


def _kv_entry(key: str) -> KVEntryDTO:
    return KVEntryDTO(
        id=uuid4(),
        key=key,
        value='neovim',
        embedding=[0.5] * 8,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        updated_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
    )


def test_memory_view_json_has_no_embedding(runner, mock_api, mock_config, monkeypatch):
    uid = uuid4()
    mock_api.get_memory_unit.return_value = _unit('A fact with a vector')
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(memory_app, ['view', str(uid), '--json'], obj=mock_config)
    assert result.exit_code == 0
    _assert_no_embedding_key(json.loads(result.stdout))


def test_memory_view_multi_json_has_no_embedding(runner, mock_api, mock_config, monkeypatch):
    u1, u2 = uuid4(), uuid4()
    mock_api.get_memory_unit.side_effect = [_unit('A'), _unit('B')]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(memory_app, ['view', str(u1), str(u2), '--json'], obj=mock_config)
    assert result.exit_code == 0
    _assert_no_embedding_key(json.loads(result.stdout))


def test_memory_search_json_has_no_embedding(runner, mock_api, mock_config, monkeypatch):
    mock_api.search.return_value = [_unit('search hit')]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(memory_app, ['search', 'anything', '--json'], obj=mock_config)
    assert result.exit_code == 0
    # A `Searching: <query>` banner precedes the JSON payload.
    payload = result.stdout[result.stdout.index('[') :]
    _assert_no_embedding_key(json.loads(payload))


def test_kv_search_json_has_no_embedding(runner, mock_api, mock_config, monkeypatch):
    mock_api.kv_search_text.return_value = [_kv_entry('user:editor')]
    monkeypatch.setattr('memex_cli.kv.get_api_context', lambda config: mock_api)

    result = runner.invoke(kv_app, ['search', 'editor', '--json'], obj=mock_config)
    assert result.exit_code == 0
    _assert_no_embedding_key(json.loads(result.stdout))


def test_kv_list_json_has_no_embedding(runner, mock_api, mock_config, monkeypatch):
    mock_api.kv_list.return_value = [_kv_entry('user:editor'), _kv_entry('global:lang')]
    monkeypatch.setattr('memex_cli.kv.get_api_context', lambda config: mock_api)

    result = runner.invoke(kv_app, ['list', '--json'], obj=mock_config)
    assert result.exit_code == 0
    _assert_no_embedding_key(json.loads(result.stdout))


def test_vault_summary_json_has_no_embedding(runner, mock_api, mock_config, monkeypatch):
    vault_id = uuid4()
    mock_api.resolve_vault_identifier.return_value = vault_id
    mock_api.get_vault_summary.return_value = VaultSummaryDTO(
        id=uuid4(),
        vault_id=vault_id,
        narrative='A vault with a stored vector.',
        themes=[],
        inventory={},
        key_entities=[],
        embedding=[0.25] * 8,
        version=2,
        notes_incorporated=4,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        updated_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
    )
    monkeypatch.setattr('memex_cli.vaults.get_api_context', lambda config: mock_api)

    result = runner.invoke(vaults_app, ['summary', 'my-vault', '--json'], obj=mock_config)
    assert result.exit_code == 0
    _assert_no_embedding_key(json.loads(result.stdout))
