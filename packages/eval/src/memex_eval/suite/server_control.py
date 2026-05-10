"""Subprocess lifecycle for the sweep harness.

Spawns a memex-core server (`granian memex_core.server:app`) with knob
overrides injected as ``MEMEX_*`` env vars, polls ``/api/v1/health`` until
ready, returns a handle the caller uses to bind ``run_suite`` against the
right port. On exit, sends SIGTERM with a 30s grace window before SIGKILL
so the server's lifespan task can release `pg_try_advisory_lock` cleanly.

This module is the primitive; ``suite/sweep.py`` is the orchestrator.
Both are local-only — sweeping against a remote/staging server is a
nonsense operation because env-var overrides only take effect at process
startup, not on a long-running deployment.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


# Default ranges tuned for local docker/orbstack deployments. The free-port
# search starts above the well-known register so a `8000`-collision (active
# memex server) doesn't shadow our spawn.
_DEFAULT_PORT_MIN = 18000
_DEFAULT_PORT_MAX = 19000
_DEFAULT_HEALTH_TIMEOUT_SECONDS = 60.0
_DEFAULT_SHUTDOWN_GRACE_SECONDS = 30.0


@dataclass
class ServerHandle:
    """Live handle to a spawned memex-core server.

    Carries enough state for the caller to bind a client (``url``) and to
    diagnose a misbehaving sweep point (``pid``, ``log_path``,
    ``shutdown_method``). Created by ``spawn_server``; closed by
    ``shutdown_server`` (idempotent)."""

    process: subprocess.Popen[bytes]
    port: int
    url: str  # http://localhost:<port>/api/v1/
    log_path: Path
    env_overrides: dict[str, str]
    shutdown_method: str = 'unset'  # 'sigterm' | 'sigkill' | 'unset'

    @property
    def pid(self) -> int:
        return self.process.pid


def find_free_port(*, port_min: int = _DEFAULT_PORT_MIN, port_max: int = _DEFAULT_PORT_MAX) -> int:
    """Return an OS-assigned free TCP port within ``[port_min, port_max)``.

    Random allocation with retry. A port can race-bind between the bind()
    here and the granian spawn — caller is expected to retry the entire
    spawn on bind failure rather than re-using an aged-out port from this
    call.
    """
    import random

    for _ in range(20):
        port = random.randint(port_min, port_max - 1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    # Fallback: ask the OS for any free port in the ephemeral range.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def env_var_for_dotted_path(path: str) -> str:
    """Translate a dotted ``MemexConfig`` path to its env-var name.

    ``server.memory.retrieval.reranking_mw_alpha`` →
    ``MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA``

    Matches ``MemexConfig.model_config['env_prefix']='MEMEX_'`` +
    ``env_nested_delimiter='__'`` (defined at config.py:2070-2072).
    """
    parts = path.split('.')
    return 'MEMEX_' + '__'.join(p.upper() for p in parts)


def _wait_for_health(url: str, *, timeout: float = _DEFAULT_HEALTH_TIMEOUT_SECONDS) -> bool:
    """Poll ``<url>health`` until 200 or timeout. Returns True on ready,
    False on timeout. Uses a tight 250ms poll interval — server startup
    typically completes in 2-5s on a warm machine."""

    deadline = time.monotonic() + timeout
    health_url = url.rstrip('/') + '/health'
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(health_url, timeout=2.0)
            if resp.status_code == 200:
                return True
            last_error = f'HTTP {resp.status_code}'
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.25)
    logger.warning(
        'health-check timeout for %s after %.1fs (last_error=%s)',
        health_url,
        timeout,
        last_error,
    )
    return False


def spawn_server(
    *,
    env_overrides: dict[str, str],
    port: int | None = None,
    workers: int = 1,
    health_timeout: float = _DEFAULT_HEALTH_TIMEOUT_SECONDS,
    log_dir: Path | None = None,
) -> ServerHandle:
    """Spawn a memex-core server subprocess with the given env overrides.

    Uses ``granian --interface asgi memex_core.server:app`` (matches the
    ``memex server run`` command exactly so behavior under sweep is the
    same as in production). Inherits the parent's environment, then
    layers ``env_overrides`` on top.

    Returns a ``ServerHandle`` once ``/api/v1/health`` is 200. Raises
    ``RuntimeError`` on health-timeout or process exit before ready —
    caller retries with a fresh port (the previous one may have raced).
    The subprocess's stdout+stderr are tee'd to a log file under
    ``log_dir`` so a failed sweep point's diagnostics survive the sweep
    drain.
    """
    if port is None:
        port = find_free_port()
    if log_dir is None:
        log_dir = Path('/tmp/memex-eval-sweep')
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f'server-{port}-{int(time.time())}.log'

    env = os.environ.copy()
    env.update(env_overrides)
    # Single-worker is the right default for a sweep point — multi-worker
    # only matters under load and adds startup time we don't recoup.
    cmd = [
        'granian',
        '--interface',
        'asgi',
        '--host',
        '127.0.0.1',
        '--port',
        str(port),
        '--workers',
        str(workers),
        '--log-level',
        'warn',
        'memex_core.server:app',
    ]
    log_file = log_path.open('wb')
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        log_file.close()
        raise RuntimeError(
            'granian binary not found on PATH; install with '
            '``uv add granian --package memex-core``.'
        ) from exc
    url = f'http://127.0.0.1:{port}/api/v1/'
    handle = ServerHandle(
        process=proc,
        port=port,
        url=url,
        log_path=log_path,
        env_overrides=dict(env_overrides),
    )

    # Race window: the subprocess may exit before health turns on.
    # Poll both signals — process status AND health endpoint — so an
    # immediate crash surfaces as RuntimeError rather than a 60s wait.
    deadline = time.monotonic() + health_timeout
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            log_file.flush()
            log_file.close()
            log_tail = _tail_log(log_path, lines=40)
            raise RuntimeError(
                f'memex-core server (pid={proc.pid}, port={port}) exited '
                f'rc={rc} during startup. Last 40 log lines:\n{log_tail}'
            )
        try:
            resp = httpx.get(url + 'health', timeout=2.0)
            if resp.status_code == 200:
                logger.info(
                    'sweep-spawn: server ready on port=%d pid=%d after %.1fs',
                    port,
                    proc.pid,
                    health_timeout - (deadline - time.monotonic()),
                )
                return handle
        except httpx.HTTPError:
            pass
        time.sleep(0.25)

    # Timeout — kill and raise.
    log_file.flush()
    log_file.close()
    shutdown_server(handle)
    log_tail = _tail_log(log_path, lines=40)
    raise RuntimeError(
        f'memex-core server on port={port} did not become healthy within '
        f'{health_timeout:.0f}s. Last 40 log lines:\n{log_tail}'
    )


def shutdown_server(
    handle: ServerHandle, *, grace: float = _DEFAULT_SHUTDOWN_GRACE_SECONDS
) -> None:
    """Send SIGTERM to the server's process group; wait up to ``grace``
    seconds; SIGKILL on timeout. Idempotent — safe to call twice on the
    same handle. Annotates ``handle.shutdown_method`` so the caller can
    tag the MLflow run.
    """
    if handle.process.poll() is not None:
        # Already dead — nothing to do.
        if handle.shutdown_method == 'unset':
            handle.shutdown_method = 'already_exited'
        return
    pid = handle.process.pid
    try:
        # Signal the whole process group so granian's worker (started via
        # ``start_new_session=True``) gets the signal too.
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        handle.shutdown_method = 'already_exited'
        return
    try:
        handle.process.wait(timeout=grace)
        handle.shutdown_method = 'sigterm'
        return
    except subprocess.TimeoutExpired:
        pass
    # Grace expired — escalate to SIGKILL. The user gets a tag on the
    # MLflow run so they can spot points where the server didn't shut
    # down cleanly (likely scheduler / DB-connection-pool drain bugs).
    logger.warning(
        'sweep-shutdown: server pid=%d port=%d did not exit on SIGTERM '
        'after %.0fs; escalating to SIGKILL',
        pid,
        handle.port,
        grace,
    )
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        handle.process.wait(timeout=5.0)
    handle.shutdown_method = 'sigkill'


def _tail_log(path: Path, *, lines: int = 40) -> str:
    """Return the last ``lines`` lines of ``path`` for diagnostic
    breadcrumbs. Best-effort — returns a placeholder if the file isn't
    readable yet."""
    try:
        with path.open('rb') as f:
            content = f.read()
        text = content.decode('utf-8', errors='replace')
        return '\n'.join(text.splitlines()[-lines:])
    except OSError as exc:
        return f'<could not read {path}: {exc}>'


__all__ = [
    'ServerHandle',
    'env_var_for_dotted_path',
    'find_free_port',
    'shutdown_server',
    'spawn_server',
]
