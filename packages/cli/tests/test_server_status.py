from memex_cli.server import app as server_app
from unittest.mock import AsyncMock, patch


def test_server_status_not_running(runner):
    with patch('memex_cli.server.read_pid', return_value=None):
        result = runner.invoke(server_app, ['status'])
        assert result.exit_code == 1
        assert 'Server is NOT running' in result.stdout


def test_server_status_running_healthy(runner, mock_config):
    with (
        patch('memex_cli.server.read_pid', return_value=123),
        patch('httpx.AsyncClient.get', new_callable=AsyncMock) as mock_get,
    ):
        mock_get.return_value.status_code = 200

        result = runner.invoke(server_app, ['status'], obj=mock_config)
        assert result.exit_code == 0
        assert 'Server is running' in result.stdout
        assert 'Health check passed' in result.stdout


def test_server_status_running_unhealthy(runner, mock_config):
    import httpx

    with (
        patch('memex_cli.server.read_pid', return_value=123),
        patch('httpx.AsyncClient.get', new_callable=AsyncMock) as mock_get,
    ):
        mock_get.side_effect = httpx.RequestError('Connection refused')

        result = runner.invoke(server_app, ['status'], obj=mock_config)
        assert result.exit_code == 0
        assert 'Server is running' in result.stdout
        assert 'Error connecting to server' in result.stdout
