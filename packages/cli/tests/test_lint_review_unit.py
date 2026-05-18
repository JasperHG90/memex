"""F7 — unit tests for ``memex lint review`` prompt-loop verdict collection.

These tests drive the CLI via Typer's ``CliRunner`` with simulated stdin
and a mocked ``RemoteMemexAPI``. They cover:

  - all-skip session collects 0 verdicts and writes nothing.
  - one accept + skip(s) → exactly one resolve call when ``--apply`` is set.
  - quit on first finding exits cleanly without traversing the rest.
  - invalid keypress re-prompts (still yields a valid verdict).
  - dry-run default never invokes ``lint_resolve`` / ``lint_dismiss``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from memex_cli.lint import app


_FINDINGS = [
    {
        'id': '11111111-1111-1111-1111-111111111111',
        'lint_type': 'quality',
        'rule_name': 'cold_low_mw_unit',
        'target_type': 'memory_unit',
        'target_id': 'unit-aaa',
        'target_text': 'Release captain approves staging.',
        'vault_id': '22222222-2222-2222-2222-222222222222',
        'evidence': {'mw_score': 0.18, 'success_co_count': 1, 'failure_co_count': 6},
        'suggested_action': 'Deprioritize the unit; 5+ outcomes with low MW.',
        'created_at': '2026-04-30T12:00:00+00:00',
        'status': 'pending',
        'source': 'rule',
    },
    {
        'id': '33333333-3333-3333-3333-333333333333',
        'lint_type': 'structural',
        'rule_name': 'orphan_mental_model',
        'target_type': 'mental_model',
        'target_id': 'mm-bbb',
        'vault_id': '22222222-2222-2222-2222-222222222222',
        'evidence': {'observation_count': 3, 'linked_active_units': 0},
        'suggested_action': 'Archive the orphan mental model.',
        'created_at': '2026-04-29T08:00:00+00:00',
        'status': 'pending',
        'source': 'rule',
    },
    {
        'id': '44444444-4444-4444-4444-444444444444',
        'lint_type': 'governance',
        'rule_name': 'sensitive_unreviewed_unit',
        'target_type': 'memory_unit',
        'target_id': 'unit-ccc',
        'vault_id': '22222222-2222-2222-2222-222222222222',
        'evidence': {'risk_class': 'sensitive'},
        'suggested_action': 'Review the sensitive unit; no review in 30+ days.',
        'created_at': '2026-04-28T08:00:00+00:00',
        'status': 'pending',
        'source': 'rule',
    },
]


def _patched_api(mock_api):
    """Wire ``lint_findings`` to return our fixture set; stub the resolve/dismiss verbs."""
    mock_api.lint_findings = AsyncMock(
        return_value={'count': len(_FINDINGS), 'findings': _FINDINGS}
    )
    mock_api.lint_resolve = AsyncMock(
        side_effect=lambda fid: {'finding_id': fid, 'status': 'resolved'}
    )
    mock_api.lint_dismiss = AsyncMock(
        side_effect=lambda fid: {'finding_id': fid, 'status': 'dismissed'}
    )
    return mock_api


def _invoke(runner, mock_api, mock_config, args, stdin):
    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = _patched_api(mock_api)
        gac.return_value.__aexit__.return_value = None
        return runner.invoke(app, args, obj=mock_config, input=stdin)


# ---------------------------------------------------------------------------
# verdict collection
# ---------------------------------------------------------------------------


def test_all_skip_session_collects_no_verdicts(runner, mock_config, mock_api, strip_ansi):
    """Three skips → 0 accepts, 0 dismisses, 3 skipped, no API mutation calls."""
    result = _invoke(runner, mock_api, mock_config, ['review', '--all'], stdin='s\ns\ns\n')

    assert result.exit_code == 0, strip_ansi(result.stdout)
    text = strip_ansi(result.stdout)
    assert 'skipped' in text.lower()
    assert mock_api.lint_resolve.await_count == 0
    assert mock_api.lint_dismiss.await_count == 0


def test_render_finding_shows_target_text_when_present(runner, mock_config, mock_api, strip_ansi):
    """Memory-unit findings render the unit's text so the reviewer isn't flying blind.

    Pinned by: F<target_text>-rendering. Without this, a reviewer sees a UUID and
    rule_name but cannot judge accept/dismiss without a separate fetch.
    """
    result = _invoke(runner, mock_api, mock_config, ['review', '--all'], stdin='s\ns\ns\n')

    assert result.exit_code == 0, strip_ansi(result.stdout)
    text = strip_ansi(result.stdout)
    assert _FINDINGS[0]['target_text'] in text, (
        f'target_text was not rendered in the review card; output was:\n{text}'
    )


def test_one_accept_two_skips_resolves_only_first_when_apply(
    runner, mock_config, mock_api, strip_ansi
):
    """``a, s, s`` with ``--apply`` → exactly one resolve call, no dismiss calls."""
    result = _invoke(
        runner, mock_api, mock_config, ['review', '--all', '--apply'], stdin='a\ns\ns\n'
    )

    assert result.exit_code == 0, strip_ansi(result.stdout)
    assert mock_api.lint_resolve.await_count == 1
    mock_api.lint_resolve.assert_awaited_with(_FINDINGS[0]['id'])
    assert mock_api.lint_dismiss.await_count == 0


def test_quit_early_exits_without_traversing_rest(runner, mock_config, mock_api, strip_ansi):
    """``q`` on the first finding → loop exits, summary prints, no API mutation."""
    result = _invoke(runner, mock_api, mock_config, ['review', '--all', '--apply'], stdin='q\n')

    assert result.exit_code == 0, strip_ansi(result.stdout)
    text = strip_ansi(result.stdout)
    assert 'quit' in text.lower() or 'ended early' in text.lower()
    assert mock_api.lint_resolve.await_count == 0
    assert mock_api.lint_dismiss.await_count == 0


def test_invalid_keypress_reprompts(runner, mock_config, mock_api, strip_ansi):
    """``z`` (invalid) → re-prompt; subsequent ``s`` accepted; loop progresses."""
    stdin = 'z\ns\ns\ns\n'
    result = _invoke(runner, mock_api, mock_config, ['review', '--all'], stdin=stdin)

    assert result.exit_code == 0, strip_ansi(result.stdout)
    text = strip_ansi(result.stdout)
    assert 'invalid' in text.lower()
    assert mock_api.lint_resolve.await_count == 0
    assert mock_api.lint_dismiss.await_count == 0


def test_dry_run_collects_verdicts_without_calling_apply(runner, mock_config, mock_api, strip_ansi):
    """Without ``--apply``, accept + dismiss collect verdicts but never call the API."""
    result = _invoke(runner, mock_api, mock_config, ['review', '--all'], stdin='a\nd\ns\n')

    assert result.exit_code == 0, strip_ansi(result.stdout)
    text = strip_ansi(result.stdout)
    assert 'dry-run' in text.lower() or 'would have applied' in text.lower()
    assert mock_api.lint_resolve.await_count == 0
    assert mock_api.lint_dismiss.await_count == 0


def test_review_rejects_multiple_scope_flags(runner, mock_config, strip_ansi):
    """Passing both ``--vault`` and ``--global`` exits with code 2."""
    result = runner.invoke(
        app,
        ['review', '--vault', '00000000-0000-0000-0000-000000000001', '--global'],
        obj=mock_config,
    )
    assert result.exit_code == 2
    assert 'at most one' in (strip_ansi(result.stdout) + strip_ansi(result.stderr or '')).lower()


def test_review_rejects_unknown_lint_type(runner, mock_config, strip_ansi):
    """Unknown ``--type`` exits with code 2 (mirrors ``lint findings``)."""
    result = runner.invoke(app, ['review', '--type', 'bogus'], obj=mock_config)
    assert result.exit_code == 2
    assert 'unknown' in strip_ansi(result.stdout).lower()


def test_apply_with_dismiss_calls_dismiss_path(runner, mock_config, mock_api, strip_ansi):
    """``d`` verdict with ``--apply`` calls ``lint_dismiss`` (not ``lint_resolve``)."""
    result = _invoke(
        runner, mock_api, mock_config, ['review', '--all', '--apply'], stdin='d\ns\ns\n'
    )

    assert result.exit_code == 0, strip_ansi(result.stdout)
    assert mock_api.lint_dismiss.await_count == 1
    mock_api.lint_dismiss.assert_awaited_with(_FINDINGS[0]['id'])
    assert mock_api.lint_resolve.await_count == 0


def test_missing_id_skips_finding_without_prompting(runner, mock_config, mock_api, strip_ansi):
    """Finding without ``id`` is skipped + flagged; loop never prompts on the bad row."""
    bad_findings = [
        # No 'id' key at all — defensive guard must catch this.
        {
            'lint_type': 'quality',
            'rule_name': 'malformed_no_id',
            'target_type': 'memory_unit',
            'target_id': 'unit-zzz',
            'evidence': {},
            'suggested_action': 'inspect',
            'status': 'pending',
        },
        _FINDINGS[0],
    ]
    mock_api.lint_findings = AsyncMock(
        return_value={'count': len(bad_findings), 'findings': bad_findings}
    )
    mock_api.lint_resolve = AsyncMock()
    mock_api.lint_dismiss = AsyncMock()

    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['review', '--all', '--apply'], obj=mock_config, input='s\n')

    assert result.exit_code == 0, strip_ansi(result.stdout)
    text = strip_ansi(result.stdout).lower()
    assert 'missing' in text and 'id' in text
    assert mock_api.lint_resolve.await_count == 0
    assert mock_api.lint_dismiss.await_count == 0


def test_empty_findings_short_circuits(runner, mock_config, mock_api, strip_ansi):
    """Empty findings list → loop exits immediately, no API mutations, summary still prints."""
    mock_api.lint_findings = AsyncMock(return_value={'count': 0, 'findings': []})
    mock_api.lint_resolve = AsyncMock()
    mock_api.lint_dismiss = AsyncMock()

    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(app, ['review', '--all'], obj=mock_config, input='')

    assert result.exit_code == 0, strip_ansi(result.stdout)
    text = strip_ansi(result.stdout).lower()
    assert 'no pending findings' in text
    assert mock_api.lint_resolve.await_count == 0
    assert mock_api.lint_dismiss.await_count == 0


def test_apply_error_isolated_per_finding(runner, mock_config, mock_api, strip_ansi):
    """One ``lint_resolve`` raising must NOT abort the loop.

    Verdicts: ``a, a, s`` with ``--apply``. The first resolve call raises;
    the second succeeds. The session must traverse all three findings,
    surface the failure (``apply failed``), and still print a final summary
    that records the error count.
    """
    mock_api.lint_findings = AsyncMock(
        return_value={'count': len(_FINDINGS), 'findings': _FINDINGS}
    )
    mock_api.lint_dismiss = AsyncMock(
        side_effect=lambda fid: {'finding_id': fid, 'status': 'dismissed'}
    )

    call_count = {'n': 0}

    async def _flaky_resolve(fid):
        call_count['n'] += 1
        if call_count['n'] == 1:
            raise RuntimeError('boom')
        return {'finding_id': fid, 'status': 'resolved'}

    mock_api.lint_resolve = AsyncMock(side_effect=_flaky_resolve)

    with patch('memex_cli.lint.get_api_context') as gac:
        gac.return_value.__aenter__.return_value = mock_api
        gac.return_value.__aexit__.return_value = None
        result = runner.invoke(
            app, ['review', '--all', '--apply'], obj=mock_config, input='a\na\ns\n'
        )

    assert result.exit_code == 0, strip_ansi(result.stdout)
    text = strip_ansi(result.stdout).lower()
    assert 'apply failed' in text
    assert 'apply errors' in text
    assert mock_api.lint_resolve.await_count == 2


# ---------------------------------------------------------------------------
# help-text fence
# ---------------------------------------------------------------------------


def test_review_help_documents_apply_and_scope(runner, strip_ansi):
    """``memex lint review --help`` advertises ``--apply`` and the scope flags."""
    result = runner.invoke(app, ['review', '--help'])
    assert result.exit_code == 0
    text = strip_ansi(result.stdout)
    assert '--apply' in text
    assert '--vault' in text


@pytest.mark.parametrize('flag', ['--global', '--all'])
def test_review_help_lists_each_scope_flag(runner, strip_ansi, flag):
    """Each scope flag is documented in --help."""
    result = runner.invoke(app, ['review', '--help'])
    assert result.exit_code == 0
    assert flag in strip_ansi(result.stdout)
