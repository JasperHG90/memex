"""CLI tests for ``memex lint stats`` — the read + refresh surface of the
lint auto-learning loop's Layer 2 telemetry rollup.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from memex_cli.lint import app


def _row(rule: str, *, vault_id: str | None = None, **counts: int) -> dict[str, Any]:
    accept = int(counts.get('accept_count', 0))
    no_op = int(counts.get('no_op_count', 0))
    dismiss = int(counts.get('dismiss_count', 0))
    legacy = int(counts.get('legacy_count', 0))
    labelled = accept + no_op + dismiss
    accept_rate: float | None = accept / labelled if labelled else None
    return {
        'rule_name': rule,
        'vault_id': vault_id,
        'window_start': '2026-04-23T00:00:00+00:00',
        'window_end': '2026-05-23T00:00:00+00:00',
        'accept_count': accept,
        'no_op_count': no_op,
        'dismiss_count': dismiss,
        'legacy_count': legacy,
        'total_count': labelled + legacy,
        'labelled_count': labelled,
        'accept_rate': accept_rate,
        'median_surprise': None,
        'median_time_to_resolve_seconds': None,
        'refreshed_at': '2026-05-23T00:00:00+00:00',
    }


def test_lint_stats_renders_table(runner, mock_config, mock_api, strip_ansi):
    """``memex lint stats`` renders the telemetry table with accept_rate."""
    mock_api.lint_telemetry = AsyncMock(
        return_value={
            'rows': [
                _row('cold_low_mw_unit', accept_count=7, no_op_count=2, dismiss_count=1),
                _row('llm_schema_drift', accept_count=0, dismiss_count=4, legacy_count=10),
            ]
        }
    )
    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['stats'], obj=mock_config)

    assert result.exit_code == 0, strip_ansi(result.stdout)
    text = strip_ansi(result.stdout)
    # Rich may truncate cell text in narrow terminals — assert on a prefix
    # the table is guaranteed to show before the ellipsis.
    assert 'cold_lo' in text
    assert 'llm_sch' in text
    # 7 / (7+2+1) = 0.7 = 70.0%
    assert '70.0%' in text
    # llm_schema_drift has 0 accepts on 4 labelled = 0.0%
    assert '0.0%' in text


def test_lint_stats_empty_renders_hint(runner, mock_config, mock_api, strip_ansi):
    """No rollup rows → user-facing nudge to run refresh."""
    mock_api.lint_telemetry = AsyncMock(return_value={'rows': []})
    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['stats'], obj=mock_config)

    assert result.exit_code == 0
    assert 'memex lint stats refresh' in strip_ansi(result.stdout)


def test_lint_stats_refresh_calls_client(runner, mock_config, mock_api, strip_ansi):
    """``memex lint stats refresh`` calls the refresh client method."""
    mock_api.lint_telemetry_refresh = AsyncMock(
        return_value={
            'rows_written': 4,
            'rules_seen': 2,
            'proposals_aggregated': 7,
            'window_start': '2026-04-23T00:00:00+00:00',
            'window_end': '2026-05-23T00:00:00+00:00',
            'vault_id': None,
        }
    )
    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['stats', 'refresh'], obj=mock_config)

    assert result.exit_code == 0, strip_ansi(result.stdout)
    mock_api.lint_telemetry_refresh.assert_awaited_once_with(vault_id=None, window_days=30)
    text = strip_ansi(result.stdout)
    assert 'refreshed' in text
    assert '4 rows' in text
    assert '2 rules' in text


def test_lint_stats_passes_rule_filter(runner, mock_config, mock_api, strip_ansi):
    """--rule X is forwarded to lint_telemetry."""
    mock_api.lint_telemetry = AsyncMock(return_value={'rows': []})
    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['stats', '--rule', 'cold_low_mw_unit'], obj=mock_config)

    assert result.exit_code == 0
    call = mock_api.lint_telemetry.await_args
    assert call is not None
    assert call.kwargs.get('rule') == 'cold_low_mw_unit'
