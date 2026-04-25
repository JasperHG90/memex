"""POC-001 codified as a permanent regression test (AC-006 part a).

PR #43 already wired ``timeout=model_config.timeout`` at all six
``dspy.LM(...)`` construction sites on ``origin/main``. This test is the
permanent guard that ``dspy.LM(timeout=N)`` actually plumbs through DSPy →
LiteLLM → openai-python → httpx as a socket-level deadline. A future bump of
DSPy/LiteLLM/httpx that breaks the plumbing must turn this test red.

Source POC: ``.dev-team-artifacts/extraction-wedge-fix/pocs/001-dspy-timeout-plumbing/poc.py``.
RESULTS.md: ``.dev-team-artifacts/extraction-wedge-fix/pocs/001-dspy-timeout-plumbing/RESULTS.md``
(verdict PASS at 2.319 s wall clock).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import dspy
import litellm
import pytest

# Match the POC. ``num_retries=0`` is critical — otherwise the test wall clock
# becomes ``timeout × (1 + num_retries)`` which is litellm's retry budget, not
# the underlying socket deadline this test is verifying.
TIMEOUT_S = 2.0
THRESHOLD_S = 3.0


def _start_hang_server() -> tuple[socket.socket, int, threading.Event]:
    """Localhost stub that accepts a TCP connection then never responds.

    Forces a *response-side* hang: ``connect()``/``send()`` succeed, the
    client blocks on ``read()`` until its timeout fires.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def _serve() -> None:
        sock.settimeout(0.5)
        held: list[socket.socket] = []
        try:
            while not stop.is_set():
                try:
                    client, _addr = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    # Socket closed by fixture teardown — exit cleanly.
                    return
                client.settimeout(0.5)
                try:
                    client.recv(4096)
                except (socket.timeout, OSError):
                    pass
                held.append(client)
        finally:
            for c in held:
                try:
                    c.close()
                except OSError:
                    pass

    thread = threading.Thread(target=_serve, name='hang-server', daemon=True)
    thread.start()
    return sock, port, stop


@pytest.fixture
def hang_server() -> Iterator[tuple[str, threading.Event, socket.socket]]:
    sock, port, stop = _start_hang_server()
    api_base = f'http://127.0.0.1:{port}/v1'
    try:
        yield api_base, stop, sock
    finally:
        stop.set()
        try:
            sock.close()
        except OSError:
            pass


def test_dspy_lm_timeout_plumbs_to_socket(
    hang_server: tuple[str, threading.Event, socket.socket],
) -> None:
    """``dspy.LM(timeout=N)`` must surface a Timeout within ``< THRESHOLD_S``.

    Asserts plumbing DSPy → LiteLLM → openai-python → httpx for the
    construction shape used at ``packages/core/src/memex_core/memory/engine.py:61``
    (and the five other ``dspy.LM(...)`` sites). If this test fails, the wedge
    fix's network-side cancellability is a placebo and PR1 must add an explicit
    ``httpx.Timeout`` below DSPy.
    """
    api_base, _stop, _sock = hang_server

    lm = dspy.LM(
        model='openai/gpt-4o-mini',
        api_base=api_base,
        api_key='sk-test-not-real',
        timeout=TIMEOUT_S,
        num_retries=0,
    )

    start = time.monotonic()
    with pytest.raises(BaseException) as exc_info:
        lm('hello world')
    duration = time.monotonic() - start

    assert duration < THRESHOLD_S, (
        f'dspy.LM(timeout={TIMEOUT_S}) did not raise within {THRESHOLD_S:.1f}s '
        f'(actual: {duration:.3f}s) — timeout is not plumbing through to httpx '
        'as a socket deadline. The wedge fix needs an httpx.Timeout fallback '
        'below DSPy. See AC-006 / RFC-001 §1.5(c).'
    )

    # POC-001 RESULTS.md confirmed exception type is litellm.exceptions.Timeout
    # (subclass of openai.APITimeoutError). Assert the lineage so a regression
    # that swaps the exception type to a generic RuntimeError still surfaces.
    assert isinstance(exc_info.value, litellm.exceptions.Timeout), (
        f'expected litellm.exceptions.Timeout, got {type(exc_info.value).__module__}.'
        f'{type(exc_info.value).__qualname__}: {exc_info.value!s:.200}'
    )
