"""Tests for the dashboard CLI commands."""

from unittest.mock import patch, AsyncMock

import pytest
from typer.testing import CliRunner

from memex_cli.dashboard import app


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_dashboard_installed():
    """Mock dashboard and reflex as importable."""
    with patch('memex_cli.dashboard.check_dashboard_installed'):
        yield


@pytest.fixture
def mock_config():
    """Mock parse_memex_config to return a config with dashboard settings."""
    with patch('memex_cli.dashboard.parse_memex_config') as mock:
        mock.return_value.dashboard.host = '0.0.0.0'
        mock.return_value.dashboard.port = 3001
        yield mock


class TestDashboardStart:
    def test_start_already_running(self, runner, mock_dashboard_installed, mock_config):
        with patch('memex_cli.dashboard.read_pid', return_value=1234):
            result = runner.invoke(app, ['start'])
            assert result.exit_code == 0
            assert 'already running' in result.stdout
            assert 'PID 1234' in result.stdout

    def test_start_port_in_use(self, runner, mock_dashboard_installed, mock_config):
        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=False),
        ):
            result = runner.invoke(app, ['start'])
            assert result.exit_code == 1
            assert 'already in use' in result.stdout

    def test_start_daemon_mode(self, runner, mock_dashboard_installed, mock_config):
        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=True),
            patch('memex_cli.dashboard._setup_runtime_cwd', return_value='/tmp'),
            patch('memex_cli.dashboard.log_file_path', return_value='/tmp/dashboard.log'),
            patch('builtins.open', create=True),
            patch('subprocess.Popen') as mock_popen,
            patch('memex_cli.dashboard.write_pid'),
        ):
            mock_popen.return_value.pid = 9999
            result = runner.invoke(app, ['start', '--daemon'])
            assert result.exit_code == 0
            assert 'daemon mode' in result.stdout
            assert 'PID 9999' in result.stdout

    def test_start_dev_daemon_warns(self, runner, mock_dashboard_installed, mock_config):
        """--dev with --daemon should warn and ignore daemon."""
        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=True),
            patch('memex_cli.dashboard._setup_runtime_cwd', return_value='/tmp'),
            patch('subprocess.run') as mock_run,
        ):
            result = runner.invoke(app, ['start', '--dev', '--daemon'])
            assert 'not supported with --dev' in result.stdout
            # Should still run (foreground dev mode)
            mock_run.assert_called_once()

    def test_start_prod_uses_single_port(self, runner, mock_dashboard_installed, mock_config):
        """Production mode should use --single-port to avoid port conflicts."""
        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=True),
            patch('memex_cli.dashboard._setup_runtime_cwd', return_value='/tmp'),
            patch('subprocess.run') as mock_run,
        ):
            result = runner.invoke(app, ['start'])
            assert result.exit_code == 0
            cmd = mock_run.call_args[0][0]
            assert '--single-port' in cmd
            assert '--env' in cmd
            assert 'prod' in cmd
            assert '--backend-port' in cmd
            assert '3001' in cmd

    def test_start_dev_uses_separate_ports(self, runner, mock_dashboard_installed, mock_config):
        """Dev mode should use separate frontend/backend ports."""
        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=True),
            patch('memex_cli.dashboard._setup_runtime_cwd', return_value='/tmp'),
            patch('subprocess.run') as mock_run,
        ):
            result = runner.invoke(app, ['start', '--dev'])
            assert result.exit_code == 0
            cmd = mock_run.call_args[0][0]
            assert '--single-port' not in cmd
            assert '--frontend-port' in cmd
            assert '3001' in cmd
            assert '--backend-port' in cmd
            assert '3002' in cmd

    def test_start_missing_deps(self, runner):
        """Should exit with helpful message when dashboard not installed."""
        with patch(
            'memex_cli.dashboard.check_dashboard_installed',
            side_effect=SystemExit(1),
        ):
            result = runner.invoke(app, ['start'])
            assert result.exit_code != 0


class TestDashboardStop:
    def test_stop_running(self, runner):
        with patch('memex_cli.dashboard.graceful_stop', return_value=True):
            result = runner.invoke(app, ['stop'])
            assert result.exit_code == 0
            assert 'Dashboard stopped' in result.stdout

    def test_stop_not_running(self, runner):
        with patch('memex_cli.dashboard.graceful_stop', return_value=False):
            result = runner.invoke(app, ['stop'])
            assert result.exit_code == 0
            assert 'No running dashboard found' in result.stdout


class TestDashboardStatus:
    def test_status_not_running(self, runner):
        with patch('memex_cli.dashboard.read_pid', return_value=None):
            result = runner.invoke(app, ['status'])
            assert result.exit_code == 1
            assert 'NOT running' in result.stdout

    def test_status_running_healthy(self, runner, mock_config):
        with (
            patch('memex_cli.dashboard.read_pid', return_value=5678),
            patch('httpx.AsyncClient.get', new_callable=AsyncMock) as mock_get,
        ):
            mock_get.return_value.status_code = 200
            result = runner.invoke(app, ['status'])
            assert result.exit_code == 0
            assert 'Dashboard is running' in result.stdout
            assert 'Health check passed' in result.stdout

    def test_status_running_unhealthy(self, runner, mock_config):
        import httpx

        with (
            patch('memex_cli.dashboard.read_pid', return_value=5678),
            patch(
                'httpx.AsyncClient.get',
                new_callable=AsyncMock,
                side_effect=httpx.RequestError('Connection refused'),
            ),
        ):
            result = runner.invoke(app, ['status'])
            assert result.exit_code == 0
            assert 'Dashboard is running' in result.stdout
            assert 'not responding' in result.stdout
