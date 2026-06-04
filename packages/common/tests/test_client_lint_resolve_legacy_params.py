"""The HTTP client must serialize ``legacy_params`` as TOP-LEVEL body keys.

The cockpit's entity-collapse apply sends ``winner_id`` / ``member_ids`` via
``legacy_params`` so the server carveout (which reads them from the top-level
payload) honours the chosen subset. If they were nested under ``params`` — or
dropped — the server would silently merge the entire cluster. This pins the
wire format.
"""

from __future__ import annotations

from typing import Any

import pytest

from memex_common.client import RemoteMemexAPI


@pytest.mark.asyncio
async def test_lint_resolve_puts_legacy_params_at_body_top_level():
    api = RemoteMemexAPI.__new__(RemoteMemexAPI)  # bypass HTTP/__init__
    captured: dict[str, Any] = {}

    async def _fake_post(path: str, data: Any, params: Any = None) -> Any:
        captured['path'] = path
        captured['body'] = data
        return {'status': 'resolved'}

    api._post = _fake_post  # type: ignore[method-assign]

    await api.lint_resolve(
        'finding-1',
        action=None,
        legacy_params={'winner_id': 'w', 'member_ids': ['w', 'l1']},
    )

    body = captured['body']
    assert captured['path'] == 'lint/findings/finding-1/resolve'
    # No canned action → server carveout fires; winner/members are top-level.
    assert 'action' not in body
    assert body['winner_id'] == 'w'
    assert body['member_ids'] == ['w', 'l1']
    # And NOT nested under a 'params' slot.
    assert 'params' not in body or 'winner_id' not in (body.get('params') or {})
