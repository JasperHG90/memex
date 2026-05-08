"""Subprocess server lifecycle for sweep harnesses.

Spawns a fresh ``python -m memex_core.server`` per sweep point with
override env vars, polls /health, runs the suite, then SIGTERMs (with a
SIGKILL fallback on timeout). Local-only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import IO

import httpx

logger = logging.getLogger('memex_eval.suite.server_control')


def free_port() -> int:
    """Allocate an ephemeral port the OS hasn't yet bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


def is_localhost(server_url: str) -> bool:
    return any(host in server_url for host in ('localhost', '127.0.0.1', '0.0.0.0'))


class SpawnedServer:
    """A subprocess-managed memex server instance."""

    def __init__(
        self,
        env_overlay: dict[str, str] | None = None,
        port: int | None = None,
        startup_timeout_s: float = 60.0,
        graceful_shutdown_s: float = 30.0,
    ) -> None:
        self.env_overlay = dict(env_overlay or {})
        self.port = port or free_port()
        self.startup_timeout_s = startup_timeout_s
        self.graceful_shutdown_s = graceful_shutdown_s
        self.process: subprocess.Popen[bytes] | None = None
        self._log_path: Path | None = None
        self._log_fp: IO[bytes] | None = None

    @property
    def server_url(self) -> str:
        return f'http://127.0.0.1:{self.port}/api/v1/'

    @property
    def base_url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    def __enter__(self) -> 'SpawnedServer':
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def start(self) -> None:
        env = os.environ.copy()
        env.update(self.env_overlay)
        env.setdefault('MEMEX_SERVER__HOST', '127.0.0.1')
        env['MEMEX_SERVER__PORT'] = str(self.port)
        cmd = [
            'uv',
            'run',
            'memex',
            'server',
            'run',
            '--host',
            '127.0.0.1',
            '--port',
            str(self.port),
        ]
        # Redirect to a log file rather than PIPE — large startup logs would
        # otherwise fill the OS pipe buffer and stall the server.
        self._log_path = Path(tempfile.mkstemp(prefix='memex-eval-srv-', suffix='.log')[1])
        self._log_fp = self._log_path.open('wb')
        logger.info('Spawning memex server: %s (logs: %s)', ' '.join(cmd), self._log_path)
        self.process = subprocess.Popen(  # noqa: S603
            cmd, env=env, stdout=self._log_fp, stderr=subprocess.STDOUT
        )
        self._wait_for_ready()

    def _wait_for_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_s
        last_err: str = ''
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                tail = ''
                if self._log_path and self._log_path.is_file():
                    with contextlib.suppress(Exception):
                        tail = self._log_path.read_bytes()[-1000:].decode(errors='replace')
                raise RuntimeError(
                    f'Memex server exited prematurely (rc={self.process.returncode}): {tail}'
                )
            try:
                resp = httpx.get(f'{self.base_url}/api/v1/health', timeout=2.0)
                if resp.status_code == 200:
                    logger.info('Memex server ready at %s', self.server_url)
                    return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPError) as e:
                last_err = str(e)
            time.sleep(0.5)
        self._force_kill()
        raise RuntimeError(
            f'Memex server failed to become ready at {self.server_url} '
            f'within {self.startup_timeout_s}s: {last_err}'
        )

    def stop(self) -> str:
        """Stop the server. Returns 'graceful' or 'kill' for tagging."""
        if self.process is None:
            return 'no-process'
        try:
            if self.process.poll() is not None:
                return 'already-exited'
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=self.graceful_shutdown_s)
                return 'graceful'
            except subprocess.TimeoutExpired:
                self._force_kill()
                return 'kill'
        finally:
            with contextlib.suppress(Exception):
                if self._log_fp is not None:
                    self._log_fp.close()
                    self._log_fp = None

    def _force_kill(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()
            with contextlib.suppress(Exception):
                self.process.wait(timeout=5.0)


__all__ = ['SpawnedServer', 'free_port', 'is_localhost']
