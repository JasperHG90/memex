"""Tests for shared process management utilities."""

import os
import signal
from unittest.mock import patch

import pytest

from memex_cli.process import (
    check_port_available,
    graceful_stop,
    pid_file_path,
    read_pid,
    remove_pid,
    write_pid,
)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect PID files to a temp directory."""
    monkeypatch.setattr('memex_cli.process.CACHE_DIR', tmp_path)
    return tmp_path


class TestPidFileLifecycle:
    def test_write_and_read_pid(self, cache_dir):
        write_pid('test', os.getpid())
        assert read_pid('test') == os.getpid()

    def test_read_pid_missing(self, cache_dir):
        assert read_pid('nonexistent') is None

    def test_read_pid_stale_process(self, cache_dir):
        """Stale PID file (dead process) is cleaned up automatically."""
        write_pid('test', 999999)
        assert read_pid('test') is None
        assert not pid_file_path('test').exists()

    def test_read_pid_corrupted_file(self, cache_dir):
        """Corrupted PID file is cleaned up."""
        path = pid_file_path('test')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not-a-number')
        assert read_pid('test') is None
        assert not path.exists()

    def test_remove_pid(self, cache_dir):
        write_pid('test', os.getpid())
        remove_pid('test')
        assert not pid_file_path('test').exists()

    def test_remove_pid_missing(self, cache_dir):
        """Removing a non-existent PID file does not raise."""
        remove_pid('nonexistent')


class TestCheckPortAvailable:
    def test_available_port(self):
        assert check_port_available('127.0.0.1', 59999) is True

    def test_occupied_port(self):
        """Binding and listening on a socket reports occupied."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', 0))
            s.listen(1)
            _, port = s.getsockname()
            assert check_port_available('127.0.0.1', port) is False


class TestGracefulStop:
    def test_no_process(self, cache_dir):
        assert graceful_stop('nonexistent') is False

    def test_process_exits_on_sigterm(self, cache_dir):
        """Process exits immediately after SIGTERM."""
        write_pid('test', 12345)

        def mock_kill(pid, sig):
            if sig == signal.SIGTERM:
                return  # Accept SIGTERM
            if sig == 0:
                # Process is gone after SIGTERM
                raise ProcessLookupError

        with (
            patch('memex_cli.process.read_pid', return_value=12345),
            patch('memex_cli.process.os.kill', side_effect=mock_kill),
        ):
            result = graceful_stop('test')

        assert result is True

    def test_already_dead_on_sigterm(self, cache_dir):
        """Process already dead when we send SIGTERM."""
        write_pid('test', 12345)

        # Make read_pid return the value (skip os.kill(pid, 0) in read_pid)
        with (
            patch('memex_cli.process.read_pid', return_value=12345),
            patch('memex_cli.process.os.kill', side_effect=ProcessLookupError),
        ):
            result = graceful_stop('test')

        assert result is False
