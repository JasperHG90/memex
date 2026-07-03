"""Tests for RemoteMemexAPI.head_note — A.5 cheap existence check.

Covers the three response branches: 200 → True, 404 → False, and any
other non-2xx (5xx/4xx) → ``httpx.HTTPStatusError`` raised from the real
response (NOT a fabricated one — that was the High finding from
Hermes' round-2 review on PR #191).
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from memex_common.client import RemoteMemexAPI


@pytest.fixture
def mock_client():
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.fixture
def api(mock_client: AsyncMock) -> RemoteMemexAPI:
    """RemoteMemexAPI with the HTTP client swapped for a mock."""
    api = RemoteMemexAPI.__new__(RemoteMemexAPI)
    api.client = mock_client
    return api


def _make_response(status_code: int, url: str = 'http://test/notes/x') -> httpx.Response:
    """A real httpx.Response with a real request attached."""
    return httpx.Response(
        status_code=status_code,
        headers={'content-type': 'application/json'},
        request=httpx.Request('HEAD', url),
    )


@pytest.mark.asyncio
async def test_head_note_returns_true_on_200(api: RemoteMemexAPI, mock_client: AsyncMock) -> None:
    note_id = uuid4()
    mock_client.head.return_value = _make_response(200)

    assert await api.head_note(note_id) is True
    mock_client.head.assert_awaited_once_with(f'notes/{note_id}')


@pytest.mark.asyncio
async def test_head_note_returns_false_on_404(api: RemoteMemexAPI, mock_client: AsyncMock) -> None:
    note_id = uuid4()
    mock_client.head.return_value = _make_response(404)

    assert await api.head_note(note_id) is False


@pytest.mark.asyncio
async def test_head_note_raises_httpstatus_error_on_5xx(
    api: RemoteMemexAPI, mock_client: AsyncMock
) -> None:
    """5xx surfaces as a real ``httpx.HTTPStatusError`` carrying the
    actual response (with headers, request, status_code). This is the
    regression for Hermes' High on PR #191 — the previous implementation
    constructed a fabricated response missing headers/request/stream
    internals.
    """
    note_id = uuid4()
    response = _make_response(503)
    mock_client.head.return_value = response

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await api.head_note(note_id)

    # The exception carries the REAL response — accessing .headers and
    # .request would have crashed with the previous fabricated shape.
    assert excinfo.value.response is response
    assert excinfo.value.response.status_code == 503
    assert excinfo.value.response.headers['content-type'] == 'application/json'
    assert excinfo.value.response.request is not None
    assert excinfo.value.response.request.method == 'HEAD'


@pytest.mark.asyncio
async def test_head_note_raises_httpstatus_error_on_4xx_other_than_404(
    api: RemoteMemexAPI, mock_client: AsyncMock
) -> None:
    """4xx other than 404 (e.g. 401, 403, 422) still surfaces as
    ``httpx.HTTPStatusError`` rather than getting confused for "not
    found"."""
    note_id = uuid4()
    response = _make_response(401)
    mock_client.head.return_value = response

    with pytest.raises(httpx.HTTPStatusError):
        await api.head_note(note_id)


@pytest.mark.asyncio
async def test_head_note_uses_module_level_httpx(
    api: RemoteMemexAPI, mock_client: AsyncMock
) -> None:
    """Followup to PR #191 Medium: ``httpx`` must be imported at module
    level (already was on the import block at the top of client.py).
    Locked in by checking that head_note doesn't redundantly re-import.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(RemoteMemexAPI.head_note))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ', '.join(alias.name for alias in node.names)
            pytest.fail(f'head_note has an inline import: {names}')
