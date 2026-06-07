import json
from memex_cli.memory import app
from memex_common.schemas import MemoryUnitDTO
from uuid import uuid4


# ---------------------------------------------------------------------------
# memory view
# ---------------------------------------------------------------------------


def test_memory_view_single(runner, mock_api, mock_config, monkeypatch):
    uid = uuid4()
    mock_api.get_memory_unit.return_value = MemoryUnitDTO(
        id=uid, text='Python is a language', fact_type='world'
    )
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(uid)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Python is a language' in result.stdout
    assert str(uid) in result.stdout


def test_memory_view_single_json(runner, mock_api, mock_config, monkeypatch):
    uid = uuid4()
    mock_api.get_memory_unit.return_value = MemoryUnitDTO(id=uid, text='A fact', fact_type='world')
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(uid), '--json'], obj=mock_config)
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data['id'] == str(uid)


def test_memory_view_multi(runner, mock_api, mock_config, monkeypatch):
    u1, u2 = uuid4(), uuid4()
    mock_api.get_memory_unit.side_effect = [
        MemoryUnitDTO(id=u1, text='Fact one', fact_type='world'),
        MemoryUnitDTO(id=u2, text='Fact two', fact_type='event'),
    ]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(u1), str(u2)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Fact one' in result.stdout
    assert 'Fact two' in result.stdout


def test_memory_view_multi_json(runner, mock_api, mock_config, monkeypatch):
    u1, u2 = uuid4(), uuid4()
    mock_api.get_memory_unit.side_effect = [
        MemoryUnitDTO(id=u1, text='A', fact_type='world'),
        MemoryUnitDTO(id=u2, text='B', fact_type='event'),
    ]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(u1), str(u2), '--json'], obj=mock_config)
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2


def test_memory_view_multi_partial_error(runner, mock_api, mock_config, monkeypatch):
    u1, u2 = uuid4(), uuid4()
    mock_api.get_memory_unit.side_effect = [
        MemoryUnitDTO(id=u1, text='Good one', fact_type='world'),
        RuntimeError('not found'),
    ]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['view', str(u1), str(u2)], obj=mock_config)
    assert result.exit_code == 0
    assert 'Good one' in result.stdout
    assert 'Error' in result.stdout


# ---------------------------------------------------------------------------
# memory search — server-side intent / risk filter (issue #92)
# ---------------------------------------------------------------------------


def test_memory_search_intent_forwarded_to_api(runner, mock_api, mock_config, monkeypatch):
    """`--intent ephemeral` must be forwarded to api.search; no client-side filtering."""
    from memex_common.schemas import IntentClass

    uid = uuid4()
    mock_api.search.return_value = [
        MemoryUnitDTO(id=uid, text='hit', fact_type='world', intent_class='ephemeral'),
    ]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['search', 'query', '--intent', 'ephemeral'], obj=mock_config)
    assert result.exit_code == 0
    mock_api.search.assert_called_once()
    kwargs = mock_api.search.call_args.kwargs
    # CLI coerces validated string to the typed enum at the boundary so the
    # RemoteMemexAPI.search signature (IntentClass | None) is satisfied.
    assert kwargs.get('intent_class') == IntentClass.EPHEMERAL
    assert kwargs.get('risk_class') is None


def test_memory_search_risk_forwarded_to_api(runner, mock_api, mock_config, monkeypatch):
    """`--risk sensitive` must be forwarded to api.search; no client-side filtering."""
    from memex_common.schemas import RiskClass

    uid = uuid4()
    mock_api.search.return_value = [
        MemoryUnitDTO(id=uid, text='hit', fact_type='world', risk_class='sensitive'),
    ]
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['search', 'query', '--risk', 'sensitive'], obj=mock_config)
    assert result.exit_code == 0
    mock_api.search.assert_called_once()
    kwargs = mock_api.search.call_args.kwargs
    # CLI coerces validated string to the typed enum at the boundary so the
    # RemoteMemexAPI.search signature (RiskClass | None) is satisfied.
    assert kwargs.get('risk_class') == RiskClass.SENSITIVE
    assert kwargs.get('intent_class') is None


def test_memory_search_intent_invalid_rejected_locally(runner, mock_api, mock_config, monkeypatch):
    """Bad --intent values must be rejected before the API roundtrip with exit code 2."""
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['search', 'q', '--intent', 'bogus'], obj=mock_config)
    assert result.exit_code == 2
    assert 'Invalid --intent' in result.stdout
    mock_api.search.assert_not_called()


def test_memory_search_risk_invalid_rejected_locally(runner, mock_api, mock_config, monkeypatch):
    """Bad --risk values must be rejected before the API roundtrip with exit code 2."""
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['search', 'q', '--risk', 'bogus'], obj=mock_config)
    assert result.exit_code == 2
    assert 'Invalid --risk' in result.stdout
    mock_api.search.assert_not_called()


def test_memory_search_no_client_side_filter_warning(runner, mock_api, mock_config, monkeypatch):
    """Server-side filter means we never emit the "filter applied client-side" warning."""
    mock_api.search.return_value = []  # server returned 0 hits
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['search', 'query', '--intent', 'ephemeral'], obj=mock_config)
    assert result.exit_code == 0
    # Old behavior printed a warning about client-side filtering — must be gone.
    assert 'filter applied client-side' not in result.stdout
    assert 'filter applied client-side' not in (result.stderr or '')
    assert 'No results found' in result.stdout
