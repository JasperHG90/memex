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

Hardening: ``MEMEX_*`` env vars are stripped from the spawned server's
environment before knob overrides are layered, so a developer's exported
``MEMEX_SERVER__HOST`` / ``MEMEX_API_KEY`` / etc. cannot silently
contaminate sweep results. The caller must be explicit about every knob.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# Default ranges tuned for local docker/orbstack deployments. The free-port
# search starts above the well-known register so a `8000`-collision (active
# memex server) doesn't shadow our spawn.
_DEFAULT_PORT_MIN = 18000
_DEFAULT_PORT_MAX = 19000
_DEFAULT_HEALTH_TIMEOUT_SECONDS = 60.0
_DEFAULT_SHUTDOWN_GRACE_SECONDS = 30.0
_DEFAULT_SPAWN_MAX_RETRIES = 3

# Env vars that must NEVER be inherited from the harness's own
# environment into the spawned server. The orchestrator explicitly opts
# every knob in via ``env_overrides``; anything else is a leak from the
# developer's shell.
_MEMEX_ENV_PREFIX = 'MEMEX_'


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
    # Process-group id captured at spawn time so SIGTERM/SIGKILL escalation
    # cannot accidentally signal a recycled-PID's group later (the classic
    # POSIX kill-after-exit hazard, real on PID-constrained containers).
    pgid: int = -1
    # Log filehandle held by the parent so granian can keep writing to it.
    # Closed by ``shutdown_server`` after the child exits; do not access
    # from outside this module.
    _log_file: Any = field(default=None, repr=False)
    shutdown_method: str = 'unset'  # 'sigterm' | 'sigkill' | 'already_exited' | 'unset'

    @property
    def pid(self) -> int:
        return self.process.pid


def find_free_port(*, port_min: int = _DEFAULT_PORT_MIN, port_max: int = _DEFAULT_PORT_MAX) -> int:
    """Return an OS-assigned free TCP port within ``[port_min, port_max)``.

    Random allocation with retry. A port can race-bind between the bind()
    here and the granian spawn — caller is expected to retry the entire
    spawn on bind failure rather than re-using an aged-out port from this
    call (``spawn_server`` does this automatically).
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


def _build_clean_env(env_overrides: dict[str, str]) -> dict[str, str]:
    """Build the spawned server's environment.

    Strips every ``MEMEX_*`` env var the harness inherited from the user's
    shell so a stray ``MEMEX_SERVER__HOST`` or ``MEMEX_API_KEY`` cannot
    silently contaminate the spawned server. Every knob the sweep wants
    set must come through ``env_overrides`` explicitly. Non-MEMEX env
    (PATH, HOME, PG*, AWS_*, GOOGLE_API_KEY, etc.) is preserved so the
    server can still reach Postgres / LLM APIs.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(_MEMEX_ENV_PREFIX)}
    env.update(env_overrides)
    return env


def spawn_server(
    *,
    env_overrides: dict[str, str],
    port: int | None = None,
    workers: int = 1,
    health_timeout: float = _DEFAULT_HEALTH_TIMEOUT_SECONDS,
    log_dir: Path | None = None,
    max_retries: int = _DEFAULT_SPAWN_MAX_RETRIES,
) -> ServerHandle:
    """Spawn a memex-core server subprocess with the given env overrides.

    Uses ``granian --interface asgi memex_core.server:app`` (matches the
    ``memex server run`` command exactly so behavior under sweep is the
    same as in production). Builds a clean env (every ``MEMEX_*`` from the
    parent is stripped; see ``_build_clean_env``), then layers
    ``env_overrides`` on top.

    Returns a ``ServerHandle`` once ``/api/v1/health`` is 200.

    Retry semantics depend on whether ``port`` was specified explicitly:

    - ``port`` is None (default): retries up to ``max_retries`` times on
      bind / startup failure, allocating a fresh free port each retry —
      port collisions on busy machines are real and should not lose data
      points to a single failure.
    - ``port`` is explicit: a single attempt only. Retrying on a different
      port would violate the caller's intent (e.g. tests pinning to a
      known port, or a user wiring an external client). The original
      ``RuntimeError`` is re-raised verbatim.

    Raises ``RuntimeError`` if every retry fails.
    """
    if log_dir is None:
        log_dir = Path('/tmp/memex-eval-sweep')
    log_dir.mkdir(parents=True, exist_ok=True)

    if port is not None:
        # Explicit port: do NOT retry on a different port — that would
        # silently betray the caller's intent. One shot, surfaces failure.
        return _spawn_once(
            env_overrides=env_overrides,
            port=port,
            workers=workers,
            health_timeout=health_timeout,
            log_dir=log_dir,
        )

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return _spawn_once(
                env_overrides=env_overrides,
                port=None,  # re-pick a free port each attempt
                workers=workers,
                health_timeout=health_timeout,
                log_dir=log_dir,
            )
        except RuntimeError as exc:
            last_error = exc
            logger.warning(
                'sweep-spawn: attempt %d/%d failed: %s',
                attempt + 1,
                max_retries,
                exc,
            )
    assert last_error is not None  # we returned on success above
    raise RuntimeError(
        f'spawn_server failed after {max_retries} attempts; last error: {last_error}'
    ) from last_error


def _spawn_once(
    *,
    env_overrides: dict[str, str],
    port: int | None,
    workers: int,
    health_timeout: float,
    log_dir: Path,
) -> ServerHandle:
    """Single-shot spawn; called by ``spawn_server`` inside its retry loop."""
    if port is None:
        port = find_free_port()
    log_path = log_dir / f'server-{port}-{int(time.time())}.log'
    env = _build_clean_env(env_overrides)
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

    # Open the log file. We close it on EVERY error path before raising so
    # the parent process does not leak fds across many sweep points. On the
    # success path the fd is held open in the ServerHandle (granian writes
    # to it for the rest of the process's life) and closed by
    # ``shutdown_server`` after ``process.wait()`` returns.
    log_file = log_path.open('wb')
    proc: subprocess.Popen[bytes] | None = None
    cleanup_owned_by_branch = False  # set True iff an inner branch already
    # called shutdown_server on the handle (and thus closed log_file). The
    # outer ``except BaseException`` reads this flag to skip the redundant
    # cleanup pass — single source of truth.
    try:
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                'granian binary not found on PATH; install with '
                '``uv add granian --package memex-core``.'
            ) from exc
        except OSError as exc:
            raise RuntimeError(f'subprocess.Popen failed for granian: {exc}') from exc

        # Capture pgid once at spawn time. Cached value avoids the
        # ``os.getpgid(pid)``-after-exit race on PID-recycle-prone systems.
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            # Child exited between Popen and getpgid — treat as failed spawn.
            raise RuntimeError(
                f'memex-core server (pid={proc.pid}) exited immediately after '
                f'spawn before pgid could be captured.'
            ) from None

        url = f'http://127.0.0.1:{port}/api/v1/'
        handle = ServerHandle(
            process=proc,
            port=port,
            url=url,
            log_path=log_path,
            env_overrides=dict(env_overrides),
            pgid=pgid,
            _log_file=log_file,
        )

        # Race window: the subprocess may exit before health turns on.
        # Poll both signals — process status AND health endpoint — so an
        # immediate crash surfaces as RuntimeError rather than a 60s wait.
        deadline = time.monotonic() + health_timeout
        spawn_started = time.monotonic()
        while time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                log_tail = _tail_log(log_path, lines=40)
                # Process exited during startup: filehandle gets closed
                # by the outer except handler, after which we re-raise.
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
                        time.monotonic() - spawn_started,
                    )
                    # Hand off the log_file to the handle; do NOT close it
                    # here — granian still writes to it.
                    return handle
            except httpx.HTTPError:
                pass
            time.sleep(0.25)

        # Health-timeout: kill the child cleanly via ``shutdown_server``
        # (which closes the log fd as part of its contract), then raise.
        # Set ``cleanup_owned_by_branch`` so the outer ``except`` skips
        # its redundant cleanup pass.
        log_tail = _tail_log(log_path, lines=40)
        with contextlib.suppress(Exception):
            shutdown_server(handle)
        cleanup_owned_by_branch = True
        raise RuntimeError(
            f'memex-core server on port={port} did not become healthy within '
            f'{health_timeout:.0f}s. Last 40 log lines:\n{log_tail}'
        )
    except BaseException:
        # Any exception (RuntimeError on early-exit / KeyboardInterrupt /
        # MemoryError, ...) before the health-poll branch left us
        # responsible for the log filehandle and any spawned subprocess.
        # The health-timeout branch sets ``cleanup_owned_by_branch=True``
        # and we skip; otherwise we own the fd + child cleanup here.
        if not cleanup_owned_by_branch:
            try:
                if proc is not None and proc.poll() is None:
                    with contextlib.suppress(Exception):
                        proc.terminate()
                        proc.wait(timeout=5.0)
            finally:
                with contextlib.suppress(Exception):
                    log_file.close()
        raise


def shutdown_server(
    handle: ServerHandle, *, grace: float = _DEFAULT_SHUTDOWN_GRACE_SECONDS
) -> None:
    """Send SIGTERM to the server's process group; wait up to ``grace``
    seconds; SIGKILL on timeout. Idempotent — safe to call twice on the
    same handle. Annotates ``handle.shutdown_method`` so the caller can
    tag the MLflow run.

    Robust against KeyboardInterrupt during the grace wait: a second
    Ctrl-C immediately escalates to SIGKILL on the cached pgid before
    re-raising, so the spawned process group is never orphaned.
    """
    log_file = handle._log_file  # noqa: SLF001 — same module
    if handle.process.poll() is not None:
        if handle.shutdown_method == 'unset':
            handle.shutdown_method = 'already_exited'
        _close_log_file(log_file)
        return

    # SIGTERM the entire group so granian's worker (started via
    # ``start_new_session=True``) gets the signal too. The cached pgid
    # avoids the ``os.getpgid(pid)``-after-exit hazard.
    try:
        if handle.pgid > 0:
            os.killpg(handle.pgid, signal.SIGTERM)
        else:
            handle.process.terminate()
    except ProcessLookupError:
        handle.shutdown_method = 'already_exited'
        _close_log_file(log_file)
        return

    try:
        handle.process.wait(timeout=grace)
        handle.shutdown_method = 'sigterm'
        _close_log_file(log_file)
        return
    except subprocess.TimeoutExpired:
        pass
    except BaseException:
        # KeyboardInterrupt / any other BaseException during the wait:
        # immediately escalate to SIGKILL on the cached pgid before
        # re-raising. The caller may see an orphaned shutdown sequence,
        # but never an orphaned process group.
        with contextlib.suppress(Exception):
            if handle.pgid > 0:
                os.killpg(handle.pgid, signal.SIGKILL)
            else:
                handle.process.kill()
        with contextlib.suppress(Exception):
            handle.process.wait(timeout=5.0)
        handle.shutdown_method = 'sigkill'
        _close_log_file(log_file)
        raise

    # Grace expired — escalate to SIGKILL. The user gets a tag on the
    # MLflow run so they can spot points where the server didn't shut
    # down cleanly (likely scheduler / DB-connection-pool drain bugs).
    logger.warning(
        'sweep-shutdown: server pid=%d port=%d did not exit on SIGTERM '
        'after %.0fs; escalating to SIGKILL',
        handle.pid,
        handle.port,
        grace,
    )
    # Suppress broadly here: a permission error / OS quirk killing the pgid
    # must NOT skip ``_close_log_file`` below — leaking a fd per sweep
    # point chews through the process's fd budget on long sweeps.
    with contextlib.suppress(Exception):
        if handle.pgid > 0:
            os.killpg(handle.pgid, signal.SIGKILL)
        else:
            handle.process.kill()
    with contextlib.suppress(Exception):
        handle.process.wait(timeout=5.0)
    handle.shutdown_method = 'sigkill'
    _close_log_file(log_file)


def _close_log_file(log_file: Any) -> None:
    """Close a log filehandle if it's still open. Best-effort; never raises."""
    if log_file is None:
        return
    with contextlib.suppress(Exception):
        if not log_file.closed:
            log_file.close()


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
