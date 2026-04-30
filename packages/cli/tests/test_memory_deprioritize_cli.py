"""F4 — CLI tests for `memex memory deprioritize` and `memex memory restore`.

T5 (CLI half): the Typer commands exist, dispatch to the API client correctly,
and surface success messages.
"""

from __future__ import annotations

from uuid import uuid4

from memex_cli.memory import app
from memex_common.schemas import MemoryUnitDTO


def _fake_unit(unit_id: str, *, is_deprioritized: bool = True) -> MemoryUnitDTO:
    return MemoryUnitDTO(
        id=unit_id,
        text='example',
        fact_type='observation',
        confidence=1.0,
        is_deprioritized=is_deprioritized,
    )


def test_deprioritize_command_invokes_api(runner, mock_api, mock_config, monkeypatch):
    unit_id = str(uuid4())
    mock_api.deprioritize_memory_unit.return_value = _fake_unit(unit_id)
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(
        app, ['deprioritize', unit_id, '--reason', 'wrong about deploy'], obj=mock_config
    )
    assert result.exit_code == 0, result.stdout
    assert 'deprioritized' in result.stdout.lower()

    mock_api.deprioritize_memory_unit.assert_called_once()
    args, kwargs = mock_api.deprioritize_memory_unit.call_args
    assert str(args[0]) == unit_id
    assert kwargs.get('reason') == 'wrong about deploy'


def test_restore_command_invokes_api(runner, mock_api, mock_config, monkeypatch):
    unit_id = str(uuid4())
    mock_api.restore_memory_unit.return_value = _fake_unit(unit_id, is_deprioritized=False)
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['restore', unit_id], obj=mock_config)
    assert result.exit_code == 0, result.stdout
    assert 'restored' in result.stdout.lower()

    mock_api.restore_memory_unit.assert_called_once()


def test_deprioritize_default_reason_is_manual(runner, mock_api, mock_config, monkeypatch):
    """Sanity: when --reason is omitted the CLI sends 'manual' (not None)."""
    unit_id = str(uuid4())
    mock_api.deprioritize_memory_unit.return_value = _fake_unit(unit_id)
    monkeypatch.setattr('memex_cli.memory.get_api_context', lambda config: mock_api)

    result = runner.invoke(app, ['deprioritize', unit_id], obj=mock_config)
    assert result.exit_code == 0
    assert mock_api.deprioritize_memory_unit.call_args.kwargs.get('reason') == 'manual'


def test_deprioritize_help_lists_command():
    """`memex memory deprioritize --help` returns clean."""
    from typer.testing import CliRunner

    cli = CliRunner()
    res = cli.invoke(app, ['deprioritize', '--help'])
    assert res.exit_code == 0
    assert 'deprioritize' in res.stdout.lower()


def test_restore_help_lists_command():
    """`memex memory restore --help` returns clean."""
    from typer.testing import CliRunner

    cli = CliRunner()
    res = cli.invoke(app, ['restore', '--help'])
    assert res.exit_code == 0
    assert 'restore' in res.stdout.lower()
