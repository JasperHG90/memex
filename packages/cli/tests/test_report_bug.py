"""Tests for the report-bug CLI command."""

import importlib.metadata
from unittest.mock import patch

from typer.testing import CliRunner

from memex_cli.report_bug import (
    GITHUB_ISSUES_URL,
    _build_issue_url,
    _collect_system_info,
    _get_memex_version,
)
from memex_cli.utils import LAZY_SUBCOMMANDS

runner = CliRunner()


# ===========================================================================
# Unit tests — helper functions
# ===========================================================================


class TestGetMemexVersion:
    """Tests for _get_memex_version."""

    def test_returns_string(self):
        version = _get_memex_version()
        assert isinstance(version, str)
        assert len(version) > 0

    @patch(
        'memex_cli.report_bug.importlib.metadata.version',
        side_effect=importlib.metadata.PackageNotFoundError('memex-cli'),
    )
    def test_returns_unknown_on_error(self, _mock_version):
        assert _get_memex_version() == 'unknown'


class TestCollectSystemInfo:
    """Tests for _collect_system_info."""

    def test_contains_memex_version(self):
        info = _collect_system_info()
        assert 'Memex version:' in info

    def test_contains_python_version(self):
        info = _collect_system_info()
        assert 'Python version:' in info

    def test_contains_os(self):
        info = _collect_system_info()
        assert 'OS:' in info

    def test_format_is_multiline(self):
        info = _collect_system_info()
        lines = info.strip().split('\n')
        assert len(lines) == 3
        for line in lines:
            assert line.startswith('- ')


class TestBuildIssueUrl:
    """Tests for _build_issue_url."""

    def test_starts_with_github_url(self):
        url = _build_issue_url('test info')
        assert url.startswith(GITHUB_ISSUES_URL)

    def test_includes_template_param(self):
        url = _build_issue_url('test info')
        assert 'template=bug_report.yml' in url

    def test_includes_labels_param(self):
        url = _build_issue_url('test info')
        assert 'labels=bug' in url

    def test_includes_environment_param(self):
        url = _build_issue_url('some system info')
        assert 'environment=' in url

    def test_url_encodes_system_info(self):
        info = '- Memex version: 1.0\n- Python: 3.12'
        url = _build_issue_url(info)
        # Newlines and special chars should be URL-encoded
        assert '\n' not in url.split('?', 1)[1]


# ===========================================================================
# Registration test
# ===========================================================================


class TestRegistration:
    """Verify the command is registered in LAZY_SUBCOMMANDS."""

    def test_report_bug_in_lazy_subcommands(self):
        assert 'report-bug' in LAZY_SUBCOMMANDS

    def test_report_bug_points_to_correct_module(self):
        assert LAZY_SUBCOMMANDS['report-bug'] == 'memex_cli.report_bug:app'


# ===========================================================================
# CLI integration tests
# ===========================================================================


class TestReportBugCLI:
    """Integration tests for the report-bug command via CliRunner."""

    @patch('memex_cli.report_bug.webbrowser.open', return_value=True)
    def test_success_opens_browser(self, mock_open):
        from memex_cli.report_bug import app

        result = runner.invoke(app)
        assert result.exit_code == 0
        assert 'Opening GitHub issue page' in result.output
        mock_open.assert_called_once()
        url = mock_open.call_args[0][0]
        assert url.startswith(GITHUB_ISSUES_URL)

    @patch('memex_cli.report_bug.webbrowser.open', return_value=False)
    def test_browser_fail_shows_url(self, mock_open):
        from memex_cli.report_bug import app

        result = runner.invoke(app)
        assert result.exit_code == 0
        assert 'Could not open browser' in result.output
        assert GITHUB_ISSUES_URL in result.output

    @patch('memex_cli.report_bug.webbrowser.open', return_value=True)
    def test_output_contains_system_info(self, _mock_open):
        from memex_cli.report_bug import app

        result = runner.invoke(app)
        assert 'Memex version:' in result.output
        assert 'Python version:' in result.output
        assert 'OS:' in result.output
