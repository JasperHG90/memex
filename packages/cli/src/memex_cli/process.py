"""Shared daemon/process management for CLI services."""

import os
import signal
import socket
import time
from pathlib import Path

from platformdirs import user_cache_dir, user_log_dir
from rich.console import Console

console = Console()

CACHE_DIR = Path(user_cache_dir('memex', appauthor=False))
LOG_DIR = Path(user_log_dir('memex', appauthor=False))
GRACEFUL_TIMEOUT = 10  # seconds before SIGKILL


def pid_file_path(service: str) -> Path:
    """Return path to PID file for a service (e.g. 'server', 'dashboard')."""
    return CACHE_DIR / f'{service}.pid'


def log_file_path(service: str) -> Path:
    """Return path to log file for a service."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f'{service}.log'


def write_pid(service: str, pid: int) -> None:
    """Write a PID file for a service."""
    path = pid_file_path(service)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid))


def read_pid(service: str) -> int | None:
    """Read PID from file. Returns None if missing or stale (auto-cleans stale files)."""
    path = pid_file_path(service)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)  # Check if process is alive
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        path.unlink(missing_ok=True)
        return None


def remove_pid(service: str) -> None:
    """Remove PID file for a service."""
    pid_file_path(service).unlink(missing_ok=True)


def check_port_available(host: str, port: int) -> bool:
    """Return True if the port is available for binding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) != 0


def graceful_stop(service: str) -> bool:
    """Stop a service: SIGTERM, wait up to GRACEFUL_TIMEOUT, then SIGKILL.

    Returns True if a process was stopped.
    """
    pid = read_pid(service)
    if pid is None:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f'Sent SIGTERM to {service} (PID {pid}), waiting for exit...')
    except ProcessLookupError:
        remove_pid(service)
        return False

    # Poll for process exit
    for _ in range(GRACEFUL_TIMEOUT * 10):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            remove_pid(service)
            return True

    # Still alive — escalate
    try:
        console.print(f'[yellow]Process {pid} did not exit, sending SIGKILL...[/yellow]')
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    remove_pid(service)
    return True
