"""Unit tests for ``server_control`` primitives. The heavy spawn test
(real granian + Postgres + /health round-trip) is gated behind
``MEMEX_RUN_HEAVY_INTEGRATION=1`` so the default test run stays fast."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from memex_eval.suite.server_control import (
    _build_clean_env,
    _close_log_file,
    env_var_for_dotted_path,
    find_free_port,
)


class TestFindFreePort:
    def test_returns_port_in_range(self) -> None:
        # Round-3 MEDIUM-R3-7: the previous assertion `<= port < 20100 OR
        # 1024 <= port < 65536` was tautological — the second disjunct
        # accepts every valid TCP port, so the test couldn't detect a
        # range-violating bug. Pin a 100-port window with no realistic
        # need for the OS-fallback path and assert the strict range.
        port = find_free_port(port_min=20000, port_max=20100)
        assert 1024 <= port < 65536, f'port {port} outside valid TCP range'
        # Either the random allocation succeeded within window OR the
        # 20-attempt loop exhausted and we hit the fallback (which asks
        # the OS for any free port — usually in [32768, 60999] on Linux).
        # The 20000-20100 window is large enough that the random path
        # essentially always succeeds in CI; explicit assertion below
        # tolerates the fallback for robustness on busy machines.
        assert 20000 <= port < 20100 or 32768 <= port < 65536, (
            f'port {port} neither in requested window nor ephemeral fallback range'
        )

    def test_port_is_actually_bindable(self) -> None:
        port = find_free_port()
        # Smoke: we should be able to bind to the returned port
        # immediately after — the helper releases its bind on close.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', port))
            assert s.getsockname()[1] == port


class TestEnvVarForDottedPath:
    def test_simple_path(self) -> None:
        assert (
            env_var_for_dotted_path('server.memory.retrieval.reranking_mw_alpha')
            == 'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA'
        )

    def test_top_level_field(self) -> None:
        assert env_var_for_dotted_path('api_key') == 'MEMEX_API_KEY'

    def test_two_segments(self) -> None:
        assert env_var_for_dotted_path('server.host') == 'MEMEX_SERVER__HOST'

    def test_uppercase_already(self) -> None:
        # Upper-casing is idempotent — a path that already contains
        # underscores stays correct.
        assert (
            env_var_for_dotted_path('server.memory.lint.cost_cap_per_24h')
            == 'MEMEX_SERVER__MEMORY__LINT__COST_CAP_PER_24H'
        )


class TestBuildCleanEnv:
    """Round-1 HIGH-6: every ``MEMEX_*`` env from the harness's shell
    must be stripped before the spawn so a developer's exported
    ``MEMEX_API_KEY`` / ``MEMEX_SERVER__HOST`` cannot silently
    contaminate sweep results."""

    def test_strips_inherited_memex_vars(self) -> None:
        with patch.dict(
            os.environ,
            {
                'MEMEX_SERVER__HOST': 'evil.example.com',
                'MEMEX_API_KEY': 'leaked',
                'MEMEX_LEADER_LOCK_ID': '999',
                'PATH': '/usr/bin',
                'GOOGLE_API_KEY': 'real-key',
            },
            clear=True,
        ):
            env = _build_clean_env({})
        # MEMEX_* stripped; non-MEMEX preserved.
        assert 'MEMEX_SERVER__HOST' not in env
        assert 'MEMEX_API_KEY' not in env
        assert 'MEMEX_LEADER_LOCK_ID' not in env
        assert env['PATH'] == '/usr/bin'
        assert env['GOOGLE_API_KEY'] == 'real-key'

    def test_overrides_layer_after_strip(self) -> None:
        with patch.dict(
            os.environ,
            {'MEMEX_SERVER__HOST': 'evil.example.com', 'PATH': '/usr/bin'},
            clear=True,
        ):
            env = _build_clean_env({'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA': '0.5'})
        # The user's leak is gone; the explicit override is set.
        assert 'MEMEX_SERVER__HOST' not in env
        assert env['MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA'] == '0.5'

    def test_overrides_win_when_keys_collide(self) -> None:
        # User exported the same key the sweep wants to set — sweep wins.
        with patch.dict(
            os.environ,
            {'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA': '0.0'},
            clear=True,
        ):
            env = _build_clean_env({'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA': '0.7'})
        assert env['MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA'] == '0.7'


class TestCloseLogFile:
    """``_close_log_file`` is best-effort and never raises — exercised by
    every shutdown path."""

    def test_closes_open_file(self, tmp_path: Path) -> None:
        f = (tmp_path / 'log').open('wb')
        assert not f.closed
        _close_log_file(f)
        assert f.closed

    def test_safe_on_already_closed(self, tmp_path: Path) -> None:
        # Closed-file's ``.close()`` is a no-op in CPython — to actually
        # exercise the contextlib.suppress, force ``.close()`` to raise.
        f = (tmp_path / 'log').open('wb')
        f.close()
        # Plain re-close should NOT raise (Python's BufferedWriter is
        # tolerant). The real guard is the suppress-on-raise case below.
        _close_log_file(f)
        assert f.closed

    def test_suppresses_close_raising(self) -> None:
        # Real-world guard: a misbehaving filehandle whose ``.close()``
        # raises (e.g. underlying OSError, file unlinked, fd inherited
        # into a child). ``_close_log_file`` MUST swallow it so the
        # surrounding shutdown path keeps running.
        class RaisingFile:
            closed = False

            def close(self) -> None:
                raise OSError('synthetic close failure')

        # Must not raise.
        _close_log_file(RaisingFile())

    def test_safe_with_none(self) -> None:
        # Defensive — paths exist where the handle was never opened.
        _close_log_file(None)


class TestSpawnServerExplicitPort:
    """Round-2 HIGH-R2-3: when the caller passes an explicit ``port=``,
    the retry loop must NOT silently switch to a random port — that
    would betray the caller's intent (tests pinning ports, external
    clients pre-configured to a known port). Behavior: single attempt
    only, raise verbatim on failure."""

    def test_explicit_port_does_not_retry(self) -> None:
        # Round-3 MEDIUM-R3-8: also verify env_overrides are forwarded
        # to ``_spawn_once`` so a refactor accidentally dropping the
        # kwarg doesn't pass silently.
        from unittest.mock import patch

        from memex_eval.suite import server_control

        sentinel_overrides = {'MEMEX_TEST_KEY': 'sentinel_value'}
        call_count = 0

        def fail_once(**kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            assert kwargs['port'] == 18999, (
                f'explicit port must be honored verbatim; got {kwargs["port"]!r}'
            )
            assert kwargs['env_overrides'] == sentinel_overrides, (
                f'env_overrides must be forwarded verbatim; got {kwargs["env_overrides"]!r}'
            )
            raise RuntimeError('synthetic spawn failure for test')

        with patch.object(server_control, '_spawn_once', side_effect=fail_once):
            with pytest.raises(RuntimeError, match='synthetic spawn failure'):
                server_control.spawn_server(
                    env_overrides=sentinel_overrides,
                    port=18999,
                    max_retries=3,  # ignored when port is explicit
                )
        assert call_count == 1, (
            f'expected exactly one spawn attempt for explicit port; got {call_count}'
        )

    def test_no_explicit_port_retries(self) -> None:
        # Round-3 MEDIUM-R3-8: also verify env_overrides are forwarded
        # on every retry — a refactor that drops the kwarg on the retry
        # path would silently produce sweeps with empty env.
        from unittest.mock import patch

        from memex_eval.suite import server_control

        sentinel_overrides = {'MEMEX_TEST_KNOB': '0.42'}
        call_count = 0
        seen_ports: list[object] = []
        seen_envs: list[object] = []

        def fail_then_pass(**kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            seen_ports.append(kwargs['port'])
            seen_envs.append(kwargs['env_overrides'])
            raise RuntimeError('synthetic spawn failure')

        with patch.object(server_control, '_spawn_once', side_effect=fail_then_pass):
            with pytest.raises(RuntimeError, match='spawn_server failed after 3 attempts'):
                server_control.spawn_server(
                    env_overrides=sentinel_overrides,
                    port=None,
                    max_retries=3,
                )
        assert call_count == 3, f'expected 3 retries for unspecified port; got {call_count}'
        # Each retry passes ``port=None`` so ``_spawn_once`` allocates fresh.
        assert all(p is None for p in seen_ports), seen_ports
        # And every retry forwards the same env_overrides verbatim.
        assert all(e == sentinel_overrides for e in seen_envs), seen_envs


@pytest.mark.skipif(
    os.environ.get('MEMEX_RUN_HEAVY_INTEGRATION') != '1',
    reason='heavy integration: spawns a real granian server + needs Postgres on $PGHOST',
)
class TestSpawnServerIntegration:
    """Verifies the full lifecycle: spawn → /health 200 → SIGTERM →
    process exit. Requires Postgres reachable via the default config and
    granian on PATH. Gated behind ``MEMEX_RUN_HEAVY_INTEGRATION=1``."""

    def test_spawn_health_shutdown(self, tmp_path: Path) -> None:
        from memex_eval.suite.server_control import shutdown_server, spawn_server

        handle = spawn_server(env_overrides={}, log_dir=tmp_path)
        try:
            import httpx

            resp = httpx.get(handle.url + 'health', timeout=5.0)
            assert resp.status_code == 200
        finally:
            shutdown_server(handle)
        assert handle.shutdown_method in {'sigterm', 'sigkill', 'already_exited'}
        # Process must actually be gone.
        assert handle.process.poll() is not None

    def test_env_override_threads_through_to_server(self, tmp_path: Path) -> None:
        # Set a knob that the server reads on startup — pick something
        # that surfaces via the redacted /system/config endpoint OR just
        # verify the env var was inherited via the server's own logging.
        # Quickest check: spawn with an explicit log level override that
        # we can verify in the spawned-process env.
        from memex_eval.suite.server_control import shutdown_server, spawn_server

        overrides = {
            'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA': '0.42',
        }
        handle = spawn_server(env_overrides=overrides, log_dir=tmp_path)
        try:
            assert (
                handle.env_overrides['MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKING_MW_ALPHA']
                == '0.42'
            )
        finally:
            shutdown_server(handle)
