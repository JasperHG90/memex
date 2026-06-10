"""MCP procedural-plane end-to-end tests (mock API substrate).

The procedural plane ships 8 MCP tools (create / upsert / get /
get_by_identity / update / deprecate / search / briefing_cards).
Three regressions hid in the read-side tools (get_by_identity,
search, briefing_cards) and were caught by an adversarial review
of the agent-facing surface — not by the existing test suite,
which only covered tool description char caps. This file pins
each fix so a regression that re-introduces any of them surfaces
here, before the agent sees a broken tool.

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
    ProceduralBriefingCard,
    ProceduralBriefingCards,
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


def _make_briefing_cards_response(
    *, cards: list[ProceduralBriefingCard]
) -> ProceduralBriefingCards:
    """Build a briefing-cards response with the entry nested under
    .entry. The MCP tool must project ``c.entry.id`` etc."""
    return ProceduralBriefingCards(
        cards=cards,
        context_keys=['global'],
        total_pinned=len(cards),
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

    facade = AsyncMock()
    facade.get_by_identity = AsyncMock(return_value=expected_entry)
    mock_api.procedural = facade

    result = await mcp_client.call_tool(
        'memex_procedural_get_by_identity',
        {
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'rotate',
            'context': 'creds',
        },
    )

    facade.get_by_identity.assert_awaited_once()
    call = facade.get_by_identity.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs['kind'] == 'procedure'
    assert kwargs['scope'] == 'global'
    assert kwargs['verb'] == 'rotate'
    assert kwargs['context'] == 'creds'
    # search MUST NOT be called — that was the bug.
    facade.search.assert_not_called()

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

    facade = AsyncMock()
    facade.get_by_identity = AsyncMock(return_value=None)
    mock_api.procedural = facade

    await mcp_client.call_tool(
        'memex_procedural_get_by_identity',
        {
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'rotate',
            'context': 'creds',
        },
    )

    facade.get_by_identity.assert_awaited_once()
    facade.search.assert_not_called()
    # The tool's return is `McpProceduralEntry | None`. A null
    # result surfaces as empty content. Assert the tool didn't crash
    # and didn't fall through to a search call.
    assert not facade.search.await_count


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

    facade = AsyncMock()
    facade.search = AsyncMock(return_value=response)
    mock_api.procedural = facade

    result = await mcp_client.call_tool(
        'memex_procedural_search',
        {
            'request': ProceduralSearchRequest(query='rotate').model_dump(mode='json'),
        },
    )

    facade.search.assert_awaited_once()
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
# memex_procedural_briefing_cards — DTO projection + vault_id threading
# ---------------------------------------------------------------------------


async def test_mcp_briefing_cards_projects_entry_attributes(mock_api, mock_config, mcp_client):
    """The MCP briefing_cards tool projects ``c.entry.id``,
    ``c.entry.title``, etc. Same DTO-nesting bug as the search
    tool — the briefing surface was crashing on every call.
    """
    from uuid import uuid4

    entry = _make_entry_dto(
        entry_id=uuid4(),
        title='pinned procedure',
        kind='procedure',
        scope='global',
        verb='rotate',
        context='creds',
    )
    briefing_card = ProceduralBriefingCard(
        entry=entry,
        pin_position=0,
        context_key='global',
    )
    response = _make_briefing_cards_response(cards=[briefing_card])

    facade = AsyncMock()
    facade.briefing_cards = AsyncMock(return_value=response)
    mock_api.procedural = facade

    result = await mcp_client.call_tool(
        'memex_procedural_briefing_cards',
        {
            'context_keys': ['global'],
        },
    )

    facade.briefing_cards.assert_awaited_once()
    parsed = result.structured_content or {}
    cards = parsed.get('cards', [])
    assert len(cards) == 1
    c0 = cards[0]
    assert c0['title'] == 'pinned procedure'
    assert c0['kind'] == 'procedure'
    assert c0['entry_id'] == str(entry.id)
    assert c0['pin_position'] == 0
    assert c0['matched_via'] == 'pin'


async def test_mcp_briefing_cards_threads_vault_id(mock_api, mock_config, mcp_client):
    """The MCP briefing_cards tool accepts a ``vault_id`` parameter and
    threads it to the facade's ``briefing_cards`` call as the
    multi-tenancy guardrail.

    A previous regression accepted no vault_id at the MCP boundary
    and the service call had no filter — a vault-A caller would
    silently receive vault-B pinned entries in their briefing
    (a P0 IDOR on the briefing surface, which feeds the agent's
    working memory).
    """
    from uuid import uuid4

    facade = AsyncMock()
    facade.briefing_cards = AsyncMock(return_value=_make_briefing_cards_response(cards=[]))
    mock_api.procedural = facade
    # The vault_id string is resolved to a UUID via
    # ``api.resolve_vault_identifier`` (the standard MCP vault
    # resolution helper).
    expected_vault = uuid4()
    mock_api.resolve_vault_identifier = AsyncMock(return_value=expected_vault)

    await mcp_client.call_tool(
        'memex_procedural_briefing_cards',
        {
            'context_keys': ['global'],
            'vault_id': 'my-vault',
        },
    )

    facade.briefing_cards.assert_awaited_once()
    call = facade.briefing_cards.await_args
    assert call is not None
    kwargs = call.kwargs
    # The resolved vault_id (UUID, not the string the caller passed)
    # was threaded through to the facade.
    assert kwargs.get('vault_id') == expected_vault
    mock_api.resolve_vault_identifier.assert_awaited_once_with('my-vault')


async def test_mcp_briefing_cards_without_vault_id_passes_none(mock_api, mock_config, mcp_client):
    """Omitting the ``vault_id`` parameter threads ``None`` to the
    facade — the operator-only cross-vault briefing path. The
    briefing service applies the strict no-tenant filter only
    when a non-None vault_id is passed; passing None retains
    the global result set for operator/CLI contexts.
    """

    facade = AsyncMock()
    facade.briefing_cards = AsyncMock(return_value=_make_briefing_cards_response(cards=[]))
    mock_api.procedural = facade

    await mcp_client.call_tool(
        'memex_procedural_briefing_cards',
        {
            'context_keys': ['global'],
        },
    )

    facade.briefing_cards.assert_awaited_once()
    call = facade.briefing_cards.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs.get('vault_id') is None
    # Vault resolution must NOT be called when no vault_id is supplied.
    mock_api.resolve_vault_identifier.assert_not_awaited()
