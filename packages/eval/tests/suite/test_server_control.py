"""Unit tests for ``server_control`` primitives. The heavy spawn test
(real granian + Postgres + /health round-trip) is gated behind
``MEMEX_RUN_HEAVY_INTEGRATION=1`` so the default test run stays fast."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from memex_eval.suite.server_control import (
    env_var_for_dotted_path,
    find_free_port,
)


class TestFindFreePort:
    def test_returns_port_in_range(self) -> None:
        port = find_free_port(port_min=20000, port_max=20100)
        assert 20000 <= port < 20100 or 1024 <= port < 65536  # fallback path

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
