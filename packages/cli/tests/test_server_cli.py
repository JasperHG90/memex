from unittest.mock import patch

import pytest
from memex_cli.server import app


@pytest.fixture
def mock_dependencies():
    with (
        patch('memex_cli.server.check_core_installed'),
        patch('memex_cli.server._readiness_check', return_value=True),
        patch('memex_cli.server._initialize_database'),
        patch('memex_cli.server._initialize_models'),
        patch('memex_cli.server.parse_memex_config') as mock_conf,
        patch('memex_cli.server.read_pid', return_value=None),
        patch('memex_cli.server.check_port_available', return_value=True),
    ):
        # Setup mock config
        mock_conf.return_value.meta_store.instance.host = 'localhost'
        mock_conf.return_value.meta_store.instance.port = 5432

        yield


def test_start_reload(mock_dependencies, runner):
    with patch('uvicorn.run') as mock_run:
        result = runner.invoke(app, ['start', '--reload'])
        assert result.exit_code == 0
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert kwargs['reload'] is True


def test_start_prod(mock_dependencies, runner):
    with patch('os.execvp') as mock_execvp, patch('sys.stdout'), patch('sys.stderr'):
        result = runner.invoke(app, ['start', '--port', '9000'])
        assert result.exit_code == 0

        mock_execvp.assert_called_once()
        cmd = mock_execvp.call_args[0][1]
        assert 'granian' in cmd[0]
        assert '--port' in cmd
        assert '9000' in cmd
        assert '--workers' in cmd


def test_start_already_running(runner):
    """Test that start command exits if server is already running."""
    with (
        patch('memex_cli.server.check_core_installed'),
        patch('memex_cli.server.read_pid', return_value=1234),
    ):
        result = runner.invoke(app, ['start'])
        assert result.exit_code == 0
        assert 'already running' in result.stdout
        assert 'PID 1234' in result.stdout


def test_start_port_in_use(runner):
    """Test that start command exits if port is occupied."""
    with (
        patch('memex_cli.server.check_core_installed'),
        patch('memex_cli.server.read_pid', return_value=None),
        patch('memex_cli.server.check_port_available', return_value=False),
    ):
        result = runner.invoke(app, ['start'])
        assert result.exit_code == 1
        assert 'already in use' in result.stdout


def test_stop_with_running_server(runner):
    """Test stop command when server is running."""
    with patch('memex_cli.server.graceful_stop', return_value=True):
        result = runner.invoke(app, ['stop'])
        assert result.exit_code == 0
        assert 'Server stopped' in result.stdout


def test_stop_no_running_server(runner):
    """Test stop command when no server is running."""
    with patch('memex_cli.server.graceful_stop', return_value=False):
        result = runner.invoke(app, ['stop'])
        assert result.exit_code == 0
        assert 'No running server found' in result.stdout
