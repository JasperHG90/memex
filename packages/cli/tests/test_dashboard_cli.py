"""Tests for the dashboard CLI commands."""

import signal
import tarfile
import io
from unittest.mock import MagicMock, patch, AsyncMock

import httpx
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
def mock_api_server_live():
    """Mock the API server health check to return True."""
    with patch('memex_cli.dashboard._check_api_server', return_value=True):
        yield


@pytest.fixture
def mock_config():
    """Mock parse_memex_config to return a config with dashboard settings."""
    with patch('memex_cli.dashboard.parse_memex_config') as mock:
        mock.return_value.dashboard.host = '0.0.0.0'
        mock.return_value.dashboard.port = 3001
        mock.return_value.server_url = 'http://localhost:8000'
        yield mock


class TestDashboardStart:
    def test_start_api_server_not_running(self, runner, mock_dashboard_installed, mock_config):
        """Should exit with error when API server is not reachable."""
        with patch('memex_cli.dashboard._check_api_server', return_value=False):
            result = runner.invoke(app, ['start'])
            assert result.exit_code == 1
            assert 'not reachable' in result.stdout
            assert 'memex server start' in result.stdout

    def test_start_already_running(
        self, runner, mock_dashboard_installed, mock_api_server_live, mock_config
    ):
        with patch('memex_cli.dashboard.read_pid', return_value=1234):
            result = runner.invoke(app, ['start'])
            assert result.exit_code == 0
            assert 'already running' in result.stdout
            assert 'PID 1234' in result.stdout

    def test_start_port_in_use(
        self, runner, mock_dashboard_installed, mock_api_server_live, mock_config
    ):
        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=False),
        ):
            result = runner.invoke(app, ['start'])
            assert result.exit_code == 1
            assert 'already in use' in result.stdout

    def test_start_daemon_mode(
        self, runner, mock_dashboard_installed, mock_api_server_live, mock_config
    ):
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

    def test_start_dev_daemon_warns(
        self, runner, mock_dashboard_installed, mock_api_server_live, mock_config
    ):
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
        self, runner, mock_dashboard_installed, mock_api_server_live, mock_config
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
        self, runner, mock_dashboard_installed, mock_api_server_live, mock_config
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

    def test_start_not_installed(self, runner):
        """Should exit with helpful message pointing to memex dashboard install."""
        with (
            patch('memex_cli.dashboard.shutil.which', return_value='/usr/bin/node'),
            patch('memex_cli.dashboard._get_dashboard_dir') as mock_dir,
        ):
            mock_dir.return_value = MagicMock()
            mock_dir.return_value.__truediv__ = lambda self, x: MagicMock(exists=lambda: False)
            result = runner.invoke(app, ['start'])
            assert result.exit_code == 1
            assert 'not installed' in result.stdout
            assert 'memex dashboard install' in result.stdout

    def test_start_production_uses_serve_cjs(
        self, runner, mock_dashboard_installed, mock_api_server_live, mock_config
    ):
        """Production mode should use node serve.cjs with --api flag."""
        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_proc.pid = 8888

        with (
            patch('memex_cli.dashboard.read_pid', return_value=None),
            patch('memex_cli.dashboard.check_port_available', return_value=True),
            patch('memex_cli.dashboard._get_dashboard_dir') as mock_dir,
            patch('memex_cli.dashboard.subprocess.Popen', return_value=mock_proc) as mock_popen,
        ):
            mock_dir.return_value = MagicMock(exists=lambda: True)
            mock_dir.return_value.__truediv__ = lambda self, x: MagicMock(exists=lambda: True)
            result = runner.invoke(app, ['start'])
            assert result.exit_code == 0
            cmd = mock_popen.call_args[0][0]
            assert cmd[0] == 'node'
            assert '--api' in cmd
            assert 'http://localhost:8000' in cmd


class TestDashboardInstall:
    def test_install_no_node(self, runner):
        """Should exit if Node.js is not available."""
        with patch('memex_cli.dashboard.shutil.which', return_value=None):
            result = runner.invoke(app, ['install'])
            assert result.exit_code == 1
            assert 'Node.js' in result.stdout

    def test_install_already_installed(self, runner, tmp_path):
        """Should exit if dashboard is already installed (without --force)."""
        (tmp_path / 'dist').mkdir()
        with (
            patch('memex_cli.dashboard.shutil.which', return_value='/usr/bin/node'),
            patch('memex_cli.dashboard._get_install_dir', return_value=tmp_path),
        ):
            result = runner.invoke(app, ['install'])
            assert result.exit_code == 0
            assert 'already installed' in result.stdout

    def test_install_downloads_and_extracts(self, runner, tmp_path):
        """Should download tarball and extract to install dir."""
        install_dir = tmp_path / 'dashboard'

        # Create a fake tarball in memory
        tarball_path = tmp_path / 'fake.tar.gz'
        with tarfile.open(tarball_path, 'w:gz') as tf:
            # Add a dist/index.html
            content = b'<html></html>'
            info = tarfile.TarInfo(name='dist/index.html')
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
            # Add serve.cjs
            serve_content = b'// serve'
            info2 = tarfile.TarInfo(name='serve.cjs')
            info2.size = len(serve_content)
            tf.addfile(info2, io.BytesIO(serve_content))

        with (
            patch('memex_cli.dashboard.shutil.which', return_value='/usr/bin/node'),
            patch('memex_cli.dashboard._get_install_dir', return_value=install_dir),
            patch('memex_cli.dashboard._download_dashboard_asset', return_value=tarball_path),
        ):
            result = runner.invoke(app, ['install'])
            assert result.exit_code == 0
            assert 'installed to' in result.stdout
            assert (install_dir / 'dist' / 'index.html').exists()
            assert (install_dir / 'serve.cjs').exists()

    def test_install_force_overwrites(self, runner, tmp_path):
        """--force should overwrite existing installation."""
        install_dir = tmp_path / 'dashboard'
        install_dir.mkdir(parents=True)
        (install_dir / 'dist').mkdir()
        (install_dir / 'old_file.txt').write_text('old')

        tarball_path = tmp_path / 'fake.tar.gz'
        with tarfile.open(tarball_path, 'w:gz') as tf:
            content = b'<html>new</html>'
            info = tarfile.TarInfo(name='dist/index.html')
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))

        with (
            patch('memex_cli.dashboard.shutil.which', return_value='/usr/bin/node'),
            patch('memex_cli.dashboard._get_install_dir', return_value=install_dir),
            patch('memex_cli.dashboard._download_dashboard_asset', return_value=tarball_path),
        ):
            result = runner.invoke(app, ['install', '--force'])
            assert result.exit_code == 0
            assert 'installed to' in result.stdout
            assert (install_dir / 'dist' / 'index.html').exists()
            assert not (install_dir / 'old_file.txt').exists()

    def test_install_with_version(self, runner, tmp_path):
        """--version should be passed through to the download function."""
        install_dir = tmp_path / 'dashboard'

        tarball_path = tmp_path / 'fake.tar.gz'
        with tarfile.open(tarball_path, 'w:gz') as tf:
            content = b'<html></html>'
            info = tarfile.TarInfo(name='dist/index.html')
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))

        with (
            patch('memex_cli.dashboard.shutil.which', return_value='/usr/bin/node'),
            patch('memex_cli.dashboard._get_install_dir', return_value=install_dir),
            patch(
                'memex_cli.dashboard._download_dashboard_asset', return_value=tarball_path
            ) as mock_dl,
        ):
            result = runner.invoke(app, ['install', '--version', 'v0.0.3a'])
            assert result.exit_code == 0
            mock_dl.assert_called_once_with('v0.0.3a')


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
