"""McpProceduralEntry.origin must accept the full DTO origin value set.

Regression guard: the DTO emits ``OriginLiteral = Literal['seed',
'derived', 'authored', 'manual', 'import']`` (procedural_schemas.py) and
``_dto_to_mcp_entry`` copies ``origin=dto.origin`` straight through. When
the MCP model's ``origin`` was the narrower ``Literal['manual', 'derived',
'imported']`` a derived/seed/authored entry raised ``ValidationError`` at
the MCP tool boundary — i.e. every procedure produced by the derivation
pipeline (origin='derived'/'seed') broke ``memex_procedural_get`` /
``_search``. This pins the MCP origin literal to the DTO's exactly.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

from memex_common.procedural_schemas import ProceduralEntryDTO
from memex_mcp.models import McpProceduralEntry
from memex_mcp.server import _dto_to_mcp_entry


def _make_dto(origin: str) -> ProceduralEntryDTO:
    return ProceduralEntryDTO(
        id=uuid4(),
        vault_id=uuid4(),
        kind='procedure',  # type: ignore[arg-type]
        scope='global',
        verb='rotate',
        context='creds',
        title='rotate API credentials',
        summary='How to rotate the project API credentials.',
        body='Step 1: ... Step 2: ...',
        trigger=None,
        tags=[],
        extra_metadata={},
        status='published',  # type: ignore[arg-type]
        origin=origin,  # type: ignore[arg-type]
        supersedes_id=None,
        superseded_by_id=None,
        published_at=None,
        created_at=dt.datetime.now(dt.timezone.utc),
        updated_at=dt.datetime.now(dt.timezone.utc),
        sources=[],
        pins=[],
    )


@pytest.mark.parametrize('origin', ['seed', 'derived', 'authored', 'manual', 'import'])
def test_mcp_entry_round_trips_every_dto_origin(origin: str) -> None:
    """Every DTO-valid origin must survive _dto_to_mcp_entry without a
    ValidationError — including the previously-missing 'seed', 'authored',
    and 'import' spellings the derivation/seed paths emit."""
    dto = _make_dto(origin)
    entry = _dto_to_mcp_entry(dto)
    assert entry.origin == origin


def test_mcp_entry_omits_backing_vault_id() -> None:
    """The agent-facing entry must NOT expose its backing vault id. Procedures
    are vault-agnostic knowledge stored in a hidden ``procedural`` system vault;
    echoing that id leaks storage plumbing the agent must not reason about.
    Guards both the model shape and the DTO->MCP mapper output."""
    assert 'vault_id' not in McpProceduralEntry.model_fields
    entry = _dto_to_mcp_entry(_make_dto('derived'))
    assert 'vault_id' not in entry.model_dump()


def test_mcp_entry_origin_matches_dto_literal() -> None:
    """The MCP origin value set must equal the DTO's OriginLiteral set
    exactly — drift in either direction is the contract bug."""
    from typing import get_args

    from memex_common.procedural_schemas import OriginLiteral

    field = McpProceduralEntry.model_fields['origin']
    mcp_values = set(get_args(field.annotation))
    assert mcp_values == set(get_args(OriginLiteral))


def test_mcp_entry_rejects_unknown_origin() -> None:
    """extra='forbid' + the closed literal still reject a junk origin."""
    with pytest.raises(ValidationError):
        _make_dto('seed').model_copy(update={})  # sanity: DTO builds
        McpProceduralEntry(
            id=uuid4(),
            kind='procedure',
            scope='global',
            title='x',
            summary='y',
            origin='bogus',  # type: ignore[arg-type]
            created_at=dt.datetime.now(dt.timezone.utc),
            updated_at=dt.datetime.now(dt.timezone.utc),
        )
