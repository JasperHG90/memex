"""Tests for the dashboard CLI commands."""

import signal
from unittest.mock import MagicMock, patch, AsyncMock

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
            patch('memex_cli.dashboard._get_dashboard_dir') as mock_dir,
            patch('memex_cli.dashboard.log_file_path', return_value='/tmp/dashboard.log'),
            patch('builtins.open', create=True),
            patch('memex_cli.dashboard.subprocess.Popen') as mock_popen,
            patch('memex_cli.dashboard.write_pid'),
        ):
            mock_dir.return_value = MagicMock(exists=lambda: True)
            mock_dir.return_value.__truediv__ = lambda self, x: MagicMock(exists=lambda: True)
            mock_popen.return_value.pid = 9999
            result = runner.invoke(app, ['start', '--daemon'])
            assert result.exit_code == 0
            assert 'daemon mode' in result.stdout
            assert 'PID 9999' in result.stdout

    def test_start_dev_daemon_warns(self, runner, mock_dashboard_installed, mock_config):
        """--dev with --daemon should warn and ignore daemon."""
        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_proc.pid = 1234

        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=True),
            patch('memex_cli.dashboard._get_dashboard_dir') as mock_dir,
            patch('memex_cli.dashboard.subprocess.Popen', return_value=mock_proc),
        ):
            mock_dir.return_value = MagicMock(exists=lambda: True)
            result = runner.invoke(app, ['start', '--dev', '--daemon'])
            assert 'not supported with --dev' in result.stdout

    def test_start_foreground_uses_popen_with_new_session(
        self, runner, mock_dashboard_installed, mock_config
    ):
        """Foreground mode uses Popen with start_new_session=True for group cleanup."""
        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_proc.pid = 5555

        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=True),
            patch('memex_cli.dashboard._get_dashboard_dir') as mock_dir,
            patch('memex_cli.dashboard.subprocess.Popen', return_value=mock_proc) as mock_popen,
        ):
            mock_dir.return_value = MagicMock(exists=lambda: True)
            mock_dir.return_value.__truediv__ = lambda self, x: MagicMock(exists=lambda: True)
            result = runner.invoke(app, ['start', '--dev'])
            assert result.exit_code == 0
            popen_kwargs = mock_popen.call_args
            assert popen_kwargs[1]['start_new_session'] is True

    def test_start_foreground_keyboard_interrupt_kills_group(
        self, runner, mock_dashboard_installed, mock_config
    ):
        """Ctrl+C in foreground mode sends SIGTERM to the process group."""
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = [KeyboardInterrupt, None]
        mock_proc.returncode = -15
        mock_proc.pid = 7777

        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=True),
            patch('memex_cli.dashboard._get_dashboard_dir') as mock_dir,
            patch('memex_cli.dashboard.subprocess.Popen', return_value=mock_proc),
            patch('memex_cli.dashboard.os.killpg') as mock_killpg,
        ):
            mock_dir.return_value = MagicMock(exists=lambda: True)
            mock_dir.return_value.__truediv__ = lambda self, x: MagicMock(exists=lambda: True)
            result = runner.invoke(app, ['start', '--dev'])
            assert 'Dashboard stopped' in result.stdout
            mock_killpg.assert_any_call(7777, signal.SIGTERM)

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
