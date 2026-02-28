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


def _kill_tree(pid: int, sig: signal.Signals) -> None:
    """Send *sig* to the process group led by *pid*, falling back to the single PID.

    When a daemon is launched with ``start_new_session=True`` its PID equals
    the PGID, so ``os.killpg`` terminates the entire tree (npm/npx + children).
    If the process isn't a group leader ``killpg`` raises ``PermissionError`` or
    ``ProcessLookupError``; we fall back to a plain ``os.kill`` in that case.
    """
    try:
        os.killpg(pid, sig)
    except (PermissionError, ProcessLookupError, OSError):
        os.kill(pid, sig)


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
        _kill_tree(pid, signal.SIGTERM)
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
        _kill_tree(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    remove_pid(service)
    return True
