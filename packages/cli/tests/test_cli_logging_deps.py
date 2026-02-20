from memex_cli import app
from unittest.mock import patch


def test_mcp_run_no_stdout_logs(tmp_path, runner, monkeypatch):
    """
    Test that running 'mcp run' does not output logs to stdout.
    We mock the actual mcp.run_async to avoid starting a real server.
    """
    # Create a dummy config file to avoid errors
    config_file = tmp_path / 'config.yaml'
    config_file.write_text('server:\n  file_store:\n    type: local\n    root: /tmp/memex')

    # FastMCP might try to import something, so we patch at the right level
    with patch('memex_mcp.server.mcp.run_async'):
        # Run the command
        result = runner.invoke(app, ['--config', str(config_file), 'mcp', 'run'])

        # Verify
        assert result.exit_code == 0
        # Result stdout should be empty because logs should be redirected or suppressed
        # and mcp.run_async is mocked.
        assert result.stdout.strip() == ''
