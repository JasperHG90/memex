"""F6 — ``memex lint`` CLI tests (TC-20-5).

Five cases covering the four subcommands plus mutual-exclusion of the
scope flags. The CLI delegates to ``RemoteMemexAPI``, so these tests mock
the api context and assert (a) the right client method is called with
the right kwargs, (b) help-text fences hold.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

from memex_cli.lint import app


def _normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


# ---------------------------------------------------------------------------
# Help-text fence
# ---------------------------------------------------------------------------


def test_lint_top_help_documents_subcommands(runner, strip_ansi):
    """``memex lint --help`` lists every subcommand and the ledger purpose."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'status' in text
    assert 'findings' in text
    assert 'dismiss' in text
    assert 'resolve' in text
    assert 'maintenance ledger' in text.lower()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_lint_status_default_is_all_scope(runner, mock_config, mock_api, strip_ansi):
    """``memex lint status`` (no flags) calls scope=all and prints the count."""
    mock_api.lint_status = AsyncMock(return_value={'scope': 'all', 'pending': 7})

    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['status'], obj=mock_config)

    assert result.exit_code == 0, strip_ansi(result.stdout) + strip_ansi(result.stderr or '')
    mock_api.lint_status.assert_awaited_once_with(scope='all', vault_id=None)
    text = strip_ansi(result.stdout)
    assert '7 pending findings' in text


def test_lint_status_rejects_multiple_scope_flags(runner, mock_config, strip_ansi):
    """Passing both --vault and --global exits with code 2."""
    result = runner.invoke(
        app,
        ['status', '--vault', '00000000-0000-0000-0000-000000000001', '--global'],
        obj=mock_config,
    )
    assert result.exit_code == 2
    text = strip_ansi(result.stdout) + strip_ansi(result.stderr or '')
    assert 'at most one' in text.lower()


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def test_lint_findings_passes_type_filter(runner, mock_config, mock_api, strip_ansi):
    """``memex lint findings --type governance`` forwards the filter."""
    mock_api.lint_findings = AsyncMock(
        return_value={
            'count': 1,
            'findings': [
                {
                    'id': '11111111-1111-1111-1111-111111111111',
                    'lint_type': 'governance',
                    'rule_name': 'sensitive_unreviewed_unit',
                    'target_type': 'memory_unit',
                    'target_id': 'unit-id',
                    'vault_id': '22222222-2222-2222-2222-222222222222',
                }
            ],
        }
    )

    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['findings', '--type', 'governance'], obj=mock_config)

    assert result.exit_code == 0, strip_ansi(result.stdout)
    mock_api.lint_findings.assert_awaited_once_with(
        vault_id=None,
        lint_type='governance',
        status='pending',
        limit=50,
    )
    text = strip_ansi(result.stdout)
    # Rich may truncate the rule_name column; assert lint_type instead
    # (shorter, always rendered in full).
    assert 'governance' in text
    assert 'memory_unit' in text


def test_lint_review_accepts_routing_type(runner, mock_config, mock_api, strip_ansi):
    """``memex lint review --type routing`` is valid (regression: the review
    command's type gate omitted 'routing' though findings/help accept it)."""
    mock_api.lint_findings = AsyncMock(return_value={'count': 0, 'findings': []})

    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['review', '--no-tui', '--type', 'routing'], obj=mock_config)

    text = strip_ansi(result.stdout)
    assert 'Unknown --type' not in text
    assert result.exit_code == 0, text
    mock_api.lint_findings.assert_awaited_once()
    call = mock_api.lint_findings.await_args
    assert call is not None
    assert call.kwargs['lint_type'] == 'routing'


# ---------------------------------------------------------------------------
# dismiss + resolve
# ---------------------------------------------------------------------------


def test_lint_dismiss_calls_client_and_prints_id(runner, mock_config, mock_api, strip_ansi):
    """``memex lint dismiss <id>`` calls ``lint_dismiss`` and prints the id."""
    fid = '33333333-3333-3333-3333-333333333333'
    mock_api.lint_dismiss = AsyncMock(return_value={'finding_id': fid, 'status': 'dismissed'})

    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['dismiss', fid], obj=mock_config)

    assert result.exit_code == 0, strip_ansi(result.stdout)
    mock_api.lint_dismiss.assert_awaited_once_with(fid)
    assert 'dismissed' in strip_ansi(result.stdout).lower()
    assert fid in strip_ansi(result.stdout)
