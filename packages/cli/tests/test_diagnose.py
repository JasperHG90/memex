"""F32 CLI — `memex diagnostics ...` smoke tests (Test 10).

Verifies:
- `--help` lists the four F32 subcommands (manifold, retrieval, summary, lint).
- `manifold --help` / `retrieval --help` / `lint --help` emit usable help text.
- Each command emits JSON shape when invoked against a mocked api.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import json

from memex_cli.diagnose import app as diagnose_app


def test_help_lists_subcommands(runner):
    result = runner.invoke(diagnose_app, ['--help'])
    assert result.exit_code == 0
    assert 'manifold' in result.output
    assert 'retrieval' in result.output
    assert 'summary' in result.output
    # F26: lint dashboard subcommand is now part of the diagnostics surface.
    assert 'lint' in result.output


def test_manifold_help_and_json_shape(runner, mock_api, monkeypatch, mock_config):
    """`memex diagnostics manifold --help` works, and the command emits JSON."""
    help_result = runner.invoke(diagnose_app, ['manifold', '--help'])
    assert help_result.exit_code == 0
    assert '--vault' in help_result.output
    assert '--force-refresh' in help_result.output

    fake_vault = uuid4()
    payload = {
        'vault_id': str(fake_vault),
        'task_id': 'cafef00d',
    }
    mock_api.resolve_vault_identifier = AsyncMock(return_value=fake_vault)
    mock_api.get_diagnostics_manifold = AsyncMock(return_value=(202, payload))

    monkeypatch.setattr('memex_cli.diagnose.get_api_context', lambda config: mock_api)

    result = runner.invoke(diagnose_app, ['manifold', '--vault', str(fake_vault)], obj=mock_config)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed['vault_id'] == str(fake_vault)
    assert parsed['_http_status'] == 202


def test_retrieval_help_and_json_shape(runner, mock_api, monkeypatch, mock_config):
    """`memex diagnostics retrieval --help` works, and the command emits JSON."""
    help_result = runner.invoke(diagnose_app, ['retrieval', '--help'])
    assert help_result.exit_code == 0
    assert '--vault' in help_result.output
    assert '--top-n' in help_result.output

    fake_vault = uuid4()
    payload = {
        'vault_id': str(fake_vault),
        'top_n': 50,
        'entities': [],
        'as_of': '2026-04-30T00:00:00Z',
    }
    mock_api.resolve_vault_identifier = AsyncMock(return_value=fake_vault)
    mock_api.get_diagnostics_retrieval = AsyncMock(return_value=payload)

    monkeypatch.setattr('memex_cli.diagnose.get_api_context', lambda config: mock_api)

    result = runner.invoke(diagnose_app, ['retrieval', '--vault', str(fake_vault)], obj=mock_config)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed['vault_id'] == str(fake_vault)
    assert parsed['top_n'] == 50


def test_lint_help_and_json_shape(runner, mock_api, monkeypatch, mock_config):
    """F26: `memex diagnostics lint --help` works, and the command emits the
    aggregator pivot JSON 1:1 from the API."""
    help_result = runner.invoke(diagnose_app, ['lint', '--help'])
    assert help_result.exit_code == 0
    assert '--vault' in help_result.output

    fake_vault = uuid4()
    payload = {
        'vault_id': str(fake_vault),
        'counts_by_type_status_source': [
            {'lint_type': 'structural', 'status': 'pending', 'source': 'rule', 'count': 3},
        ],
        'pending_by_type': {'structural': 3},
        'top_5_pending': [
            {'id': str(uuid4()), 'lint_type': 'structural', 'rule_name': 'rule_x'},
        ],
    }
    mock_api.resolve_vault_identifier = AsyncMock(return_value=fake_vault)
    mock_api.get_diagnostics_lint = AsyncMock(return_value=payload)

    monkeypatch.setattr('memex_cli.diagnose.get_api_context', lambda config: mock_api)

    result = runner.invoke(diagnose_app, ['lint', '--vault', str(fake_vault)], obj=mock_config)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed['vault_id'] == str(fake_vault)
    assert parsed['pending_by_type'] == {'structural': 3}
    assert len(parsed['top_5_pending']) == 1
