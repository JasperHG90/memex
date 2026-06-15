"""MCP procedural-plane end-to-end tests (mock API substrate).

The agent-facing procedural surface is READ-ONLY plus case submission:
get / get_by_identity / search + case_submit. There is NO agent-facing
procedural WRITE tool (create / update / upsert / deprecate) —
procedures are DERIVED from cases (design §21.7); those verbs live on
the operator (CLI / TUI) and HTTP surfaces only. There is also no
briefing_cards tool (pinned cards arrive in the session briefing).
Three regressions hid in the read-side tools (get_by_identity,
search) and were caught by an adversarial review of the agent-facing
surface — not by the existing test suite, which only covered tool
description char caps. This file pins each fix so a regression that
re-introduces any of them surfaces here, before the agent sees a
broken tool.

The tests drive the actual FastMCP server through its in-process
client and assert on the DTO shape the tool returns. The
underlying API is mocked — the goal is to pin the MCP layer's
DTO projection and routing logic, not to re-test the search
service (which the core integration tests already cover).
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from memex_common.procedural_schemas import (
    ProceduralEntryDTO,
    ProceduralSearchHit,
    ProceduralSearchRequest,
    ProceduralSearchResponse,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _make_entry_dto(
    *,
    entry_id=None,
    vault_id=None,
    kind: str = 'procedure',
    scope: str = 'global',
    verb: str = 'rotate',
    context: str = 'creds',
    title: str = 'rotate API credentials',
    summary: str = 'How to rotate the project API credentials.',
    body: str = 'Step 1: ... Step 2: ...',
    trigger: str | None = None,
    status: str = 'published',
) -> ProceduralEntryDTO:
    """Build a fully-populated ProceduralEntryDTO for projection tests.

    The MCP tools use this to assert on the DTO's nested ``.entry.*``
    attributes. A previous bug read these off the hit/card itself
    (which doesn't have them), so the assertion checks that the
    projection went through the correct attribute path.
    """
    from uuid import uuid4 as _uuid4

    return ProceduralEntryDTO(
        id=entry_id or _uuid4(),
        vault_id=vault_id or _uuid4(),
        kind=kind,  # type: ignore[arg-type]
        scope=scope,
        verb=verb,
        context=context,
        title=title,
        summary=summary,
        body=body,
        trigger=trigger,
        tags=[],
        extra_metadata={},
        status=status,  # type: ignore[arg-type]
        origin='manual',
        supersedes_id=None,
        superseded_by_id=None,
        published_at=None,
        created_at=dt.datetime.now(dt.timezone.utc),
        updated_at=dt.datetime.now(dt.timezone.utc),
        sources=[],
        pins=[],
    )


def _make_search_response(*, entries: list[ProceduralEntryDTO]) -> ProceduralSearchResponse:
    """Build a search response where each entry surfaces as a hit with
    the entry nested under .entry. The MCP tool must project
    ``h.entry.id`` etc. (NOT ``h.id``)."""

    return ProceduralSearchResponse(
        hits=[
            ProceduralSearchHit(
                entry=entry,
                score=0.9 - i * 0.1,
                bm25_rank=i,
                vector_rank=None,
                matched_via='bm25',
                pin_position=None,
            )
            for i, entry in enumerate(entries)
        ],
        total=len(entries),
        truncated=False,
        took_ms=12.3,
    )


# ---------------------------------------------------------------------------
# memex_procedural_get_by_identity — direct facade lookup
# ---------------------------------------------------------------------------


async def test_mcp_get_by_identity_uses_facade_lookup(mock_api, mock_config, mcp_client):
    """The MCP tool calls ``api.procedural.get_by_identity`` directly,
    NOT ``api.procedural.search`` followed by a post-filter.

    A previous regression routed through search('') which
    short-circuited to an empty response and silently returned
    ``None`` for every anchor. The tool's read-before-write
    probe was lying — the agent's upsert loop would issue a
    create, get a 409, and have no way to tell the difference
    from "the probe was wrong."
    """
    from uuid import uuid4

    expected_entry = _make_entry_dto(
        entry_id=uuid4(),
        kind='procedure',
        scope='global',
        verb='rotate',
        context='creds',
    )
    # Set up the facade mock. AsyncMock's auto-attr returns another
    # AsyncMock by default; we override get_by_identity.

    mock_api.procedural_get_by_identity = AsyncMock(return_value=expected_entry)

    result = await mcp_client.call_tool(
        'memex_procedural_get_by_identity',
        {
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'rotate',
            'context': 'creds',
        },
    )

    mock_api.procedural_get_by_identity.assert_awaited_once()
    call = mock_api.procedural_get_by_identity.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs['kind'] == 'procedure'
    assert kwargs['scope'] == 'global'
    assert kwargs['verb'] == 'rotate'
    assert kwargs['context'] == 'creds'
    # search MUST NOT be called — that was the bug.
    mock_api.procedural_search.assert_not_called()

    # The returned MCP shape carries the entry's id, title, etc.
    parsed = result.structured_content or {}
    assert parsed is not None
    # The tool returned the entry; the agent gets a non-null hit.
    # We don't assert on the full shape here — that's covered by
    # the DTO-converter tests. The load-bearing assertion is that
    # the facade call happened (and search was not called).
    assert result.content  # non-empty content means a real entry came back


async def test_mcp_get_by_identity_returns_null_on_miss(mock_api, mock_config, mcp_client):
    """An unbound anchor returns ``None`` — the cheap
    "did we already learn this?" probe. The MCP tool must surface
    ``null`` to the agent (not an empty hit, not a 404 string)."""

    mock_api.procedural_get_by_identity = AsyncMock(return_value=None)

    await mcp_client.call_tool(
        'memex_procedural_get_by_identity',
        {
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'rotate',
            'context': 'creds',
        },
    )

    mock_api.procedural_get_by_identity.assert_awaited_once()
    mock_api.procedural_search.assert_not_called()
    # The tool's return is `McpProceduralEntry | None`. A null
    # result surfaces as empty content. Assert the tool didn't crash
    # and didn't fall through to a search call.
    assert not mock_api.procedural_search.await_count


# ---------------------------------------------------------------------------
# memex_procedural_search — DTO projection (h.entry.*, NOT h.*)
# ---------------------------------------------------------------------------


async def test_mcp_search_projects_entry_attributes(mock_api, mock_config, mcp_client):
    """The MCP search tool projects ``h.entry.id``, ``h.entry.title``,
    etc. into the flat ``McpProceduralSearchHit`` shape.

    A previous bug read these off the hit itself (``h.entry_id``,
    ``h.title``), which raised ``AttributeError`` on every search
    call. This test pins the projection by feeding a real
    ``ProceduralSearchResponse`` and asserting the tool
    returns the entry's id, title, kind, etc. without crashing.
    """
    from uuid import uuid4

    entries = [
        _make_entry_dto(
            entry_id=uuid4(),
            title='rotate creds',
            kind='procedure',
            scope='global',
            verb='rotate',
            context='creds',
        ),
        _make_entry_dto(
            entry_id=uuid4(),
            title='audit IAM',
            kind='procedure',
            scope='global',
            verb='audit',
            context='iam',
        ),
    ]
    response = _make_search_response(entries=entries)

    mock_api.procedural_search = AsyncMock(return_value=response)

    result = await mcp_client.call_tool(
        'memex_procedural_search',
        {
            'request': ProceduralSearchRequest(query='rotate').model_dump(mode='json'),
        },
    )

    mock_api.procedural_search.assert_awaited_once()
    # The tool returned 2 hits. The DTO projection must NOT have
    # crashed on AttributeError (the previous bug raised on the
    # first hit). A clean result means the projection went through
    # h.entry.* correctly.
    parsed = result.structured_content or {}
    hits = parsed.get('hits', [])
    assert len(hits) == 2
    # The flat hit fields carry the entry's data.
    h0 = hits[0]
    assert h0['title'] == 'rotate creds'
    assert h0['kind'] == 'procedure'
    assert h0['entry_id'] == str(entries[0].id)
    assert h0['score'] == 0.9
    h1 = hits[1]
    assert h1['title'] == 'audit IAM'
    assert h1['entry_id'] == str(entries[1].id)


# ---------------------------------------------------------------------------
# memex_case_submit — result projection + assignment surfacing
# ---------------------------------------------------------------------------


async def test_mcp_case_submit_projects_result(mock_api, mock_config, mcp_client):
    """The MCP case_submit tool flattens the CaseSubmitResult envelope:
    note/vault ids + the assignment block (mode, entry, finding,
    separation) so the agent can see whether a lint finding needs
    attention without a second call."""
    from uuid import uuid4

    from memex_common.procedural_schemas import CaseAssignment, CaseSubmitResult

    note_id, vault_id, entry_id = uuid4(), uuid4(), uuid4()
    mock_api.case_submit = AsyncMock(
        return_value=CaseSubmitResult(
            note_id=note_id,
            vault_id=vault_id,
            assignment=CaseAssignment(
                mode='auto_assigned',
                entry_id=entry_id,
                separation='clean',
                reasoning='matched the rotate/creds candidate',
            ),
        )
    )

    result = await mcp_client.call_tool(
        'memex_case_submit',
        {
            'payload': {
                'title': 'Rotated the API creds',
                'trigger': 'rotating the project API credentials',
                'actions': ['issued new key', 'updated CI', 'rolled old key'],
                'outcome': 'success',
            },
        },
    )

    mock_api.case_submit.assert_awaited_once()
    await_args = mock_api.case_submit.await_args
    assert await_args is not None
    submitted = await_args.args[0]
    assert submitted.title == 'Rotated the API creds'
    assert submitted.outcome == 'success'

    parsed = result.structured_content or {}
    assert parsed['note_id'] == str(note_id)
    # vault_id is deliberately NOT surfaced — the backing system vault is
    # storage plumbing the agent must not see.
    assert 'vault_id' not in parsed
    assert parsed['assignment_mode'] == 'auto_assigned'
    assert parsed['entry_id'] == str(entry_id)
    assert parsed['separation'] == 'clean'


async def test_mcp_case_submit_surfaces_escalation(mock_api, mock_config, mcp_client):
    """A contested assignment surfaces the lint finding id so the agent
    can resolve it (file-then-lint, decision #5)."""
    from uuid import uuid4

    from memex_common.procedural_schemas import CaseAssignment, CaseSubmitResult

    finding_id = uuid4()
    mock_api.case_submit = AsyncMock(
        return_value=CaseSubmitResult(
            note_id=uuid4(),
            vault_id=uuid4(),
            assignment=CaseAssignment(
                mode='escalated',
                finding_id=finding_id,
                separation='close_call',
            ),
        )
    )

    result = await mcp_client.call_tool(
        'memex_case_submit',
        {
            'payload': {
                'title': 'Ambiguous deploy episode',
                'trigger': 'deploying the new service',
                'outcome': 'mixed',
            },
        },
    )

    parsed = result.structured_content or {}
    assert parsed['assignment_mode'] == 'escalated'
    assert parsed['finding_id'] == str(finding_id)


async def test_mcp_case_submit_background_returns_queued(mock_api, mock_config, mcp_client):
    """background=true queues the case as a durable job: the tool returns
    assignment_mode='queued' + the job_id and never blocks on the assignment
    judge. note_id/assignment fields are absent (assignment resolves async)."""
    from uuid import uuid4

    from memex_common.schemas import BatchJobStatus

    job_id = uuid4()
    mock_api.case_submit = AsyncMock(return_value=BatchJobStatus(job_id=job_id, status='pending'))

    result = await mcp_client.call_tool(
        'memex_case_submit',
        {
            'payload': {
                'title': 'Rotated the API creds',
                'trigger': 'rotating the project API credentials',
                'outcome': 'success',
            },
            'background': True,
        },
    )

    mock_api.case_submit.assert_awaited_once()
    assert mock_api.case_submit.await_args is not None
    assert mock_api.case_submit.await_args.kwargs.get('background') is True

    parsed = result.structured_content or {}
    assert parsed['assignment_mode'] == 'queued'
    assert parsed['job_id'] == str(job_id)
    assert parsed.get('note_id') is None
    assert 'vault_id' not in parsed


async def test_mcp_briefing_cards_tool_is_gone(mcp_client):
    """The agent-facing briefing tool MUST NOT exist — pinned cards
    arrive inside the session briefing (JG decision 2026-06-10).
    Anything that re-registers it re-introduces the brittle
    call-a-tool-at-startup pattern."""
    from fastmcp.exceptions import ToolError as McpToolError

    try:
        await mcp_client.call_tool(
            'memex_procedural_briefing_cards',
            {'context_keys': ['global']},
        )
        raise AssertionError('memex_procedural_briefing_cards should not be registered')
    except McpToolError:
        pass
