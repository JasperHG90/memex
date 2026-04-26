"""Route-level integration tests for ``GET /api/v1/resources``.

These cover AC-001: empty/whitespace/dot-equivalent paths must return a clean
404 (not a 500 from the underlying filestore). Includes the literal
``?paths=foo`` reproducer from issue #59 — FastAPI ``redirect_slashes``
turns it into a 307 to ``/api/v1/resources/`` which then must 404.

The happy-path round-trip lives in ``tests/test_e2e_resources.py`` (full
ingestion E2E with a real LLM); these tests stay in ``[core]`` and use
``app.dependency_overrides`` so they run without the LLM marker.
"""

from unittest.mock import AsyncMock
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from memex_core.server import app
from memex_core.server.common import get_api


pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """TestClient with the ``MemexAPI`` dependency mocked.

    The route guards run before the API is touched, so the mock only needs
    to exist — these tests never expect ``api.get_resource`` to be called.
    """
    mock_api = AsyncMock()
    app.dependency_overrides[get_api] = lambda: mock_api
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides = {}


@pytest.mark.parametrize('path', ['', '/', '///'])
def test_resource_empty_path_returns_404(client: TestClient, path: str) -> None:
    """An empty / whitespace / all-slash path must 404 cleanly, not 500."""
    resp = client.get(f'/api/v1/resources/{path}')
    assert resp.status_code == 404
    assert resp.json()['detail'] == 'Resource path is empty'


@pytest.mark.parametrize(
    'path',
    [
        # Literal '.' / './' / '/.' / '/./' are normalised away by httpx but
        # still hit the route as the empty path — covered for completeness.
        '.',
        './',
        '/.',
        '/./',
        # Percent-encoded forms survive httpx URL normalisation and are
        # decoded by FastAPI inside the route — these are the actual
        # adversarial reproducer for AC-001's "no 500" promise.
        '%2E',
        '%2E/',
        '%2E%2E',
        '%2E%2E/',
        '/%2E',
        '/%2E%2E',
        '%2E%2F%2E',
    ],
)
def test_resource_dot_path_returns_404(client: TestClient, path: str) -> None:
    """``.``/``..`` and their URL-encoded variants must 404, not 500.

    Without the extended ``is_root_key`` guard, FastAPI URL-decodes ``%2E``
    to ``.``; ``validate_path_safe`` then collapses that to the storage
    root and ``_cat_file(root)`` raises ``IsADirectoryError`` which the
    ``OSError`` branch of ``_handle_error`` previously mapped to 500.
    """
    resp = client.get(f'/api/v1/resources/{path}')
    assert resp.status_code == 404
    assert resp.json()['detail'] == 'Resource path is empty'


def test_resource_query_param_paths_returns_404(client: TestClient) -> None:
    """The literal ``?paths=foo`` reproducer from issue #59 must 404, not 500.

    FastAPI's ``redirect_slashes`` redirects ``GET /api/v1/resources?paths=foo``
    to ``GET /api/v1/resources/?paths=foo``; the route's ``{path:path}``
    parameter then receives the empty string. Followed end-to-end, the
    response must be a clean 404.
    """
    resp = client.get('/api/v1/resources?paths=foo', follow_redirects=True)
    assert resp.status_code == 404
    assert resp.json()['detail'] == 'Resource path is empty'
