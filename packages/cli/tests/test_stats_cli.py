from memex_cli.stats import app as stats_app
from memex_common.schemas import SystemStatsCountsDTO, TokenUsageResponse, TokenUsageStatDTO
from datetime import datetime, timezone


def test_stats_system(runner, mock_api, monkeypatch):
    mock_api.get_stats_counts.return_value = SystemStatsCountsDTO(
        memories=100, entities=50, reflection_queue=5
    )
    monkeypatch.setattr('memex_cli.stats.get_api_context', lambda config: mock_api)

    result = runner.invoke(stats_app, ['system'])
    assert result.exit_code == 0
    assert '100' in result.stdout
    assert '50' in result.stdout


def test_stats_tokens(runner, mock_api, monkeypatch):
    mock_api.get_token_usage.return_value = TokenUsageResponse(
        usage=[TokenUsageStatDTO(date=datetime.now(timezone.utc).date(), total_tokens=1500)]
    )
    monkeypatch.setattr('memex_cli.stats.get_api_context', lambda config: mock_api)

    result = runner.invoke(stats_app, ['tokens'])
    assert result.exit_code == 0
    assert '1,500' in result.stdout
