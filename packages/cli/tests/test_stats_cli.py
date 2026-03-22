from memex_cli.stats import app as stats_app
from memex_common.schemas import SystemStatsCountsDTO


def test_stats_system(runner, mock_api, monkeypatch):
    mock_api.get_stats_counts.return_value = SystemStatsCountsDTO(
        memories=100, entities=50, reflection_queue=5
    )
    monkeypatch.setattr('memex_cli.stats.get_api_context', lambda config: mock_api)

    # With only one command, Typer auto-promotes it (no subcommand needed)
    result = runner.invoke(stats_app, [])
    assert result.exit_code == 0
    assert '100' in result.stdout
    assert '50' in result.stdout
