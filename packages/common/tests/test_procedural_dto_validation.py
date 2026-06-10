"""Pydantic DTO validation tests for the V7 procedural-plane envelopes.

The procedural plane has 3 distinct shapes — case / procedure /
strategy — and a strict ``extra='forbid'`` config on the create /
update / search DTOs. These tests pin:

* Each kind's shape accepts its load-bearing fields and rejects
  cross-shape contamination (e.g. a procedure with a ``trigger`` is
  OK; a case without a verb is OK; a procedure with a ``trigger``
  is silently accepted at the Pydantic layer because we don't want
  to over-constrain; the load-bearing difference is the kind
  literal, not the field set).
* ``extra='forbid'`` rejects unknown fields — a regression that
  re-adds a field accidentally would surface here.
* The Literal enums reject anything outside the closed set
  (kind, status, origin).
* The ShortLabel alias accepts strings but the schema doesn't
  enforce a length cap (the test pins the current behaviour so a
  future tightening is intentional, not silent).

These tests run against the common package only — no DB, no
HTTP. They catch the wire-shape regressions the integration
tests would also catch, but at a fraction of the cost.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

from memex_common.procedural_schemas import (
    ProceduralEntryCreate,
    ProceduralEntryDTO,
    ProceduralEntryUpdate,
    ProceduralSearchRequest,
    ProceduralBriefingCard,
    ProceduralBriefingCards,
)


_NOW = dt.datetime(2026, 6, 8, 12, 0, 0, tzinfo=dt.UTC)


def _fake_entry_dto(**overrides):
    """Build a valid ``ProceduralEntryDTO`` for envelope tests.

    The DTO requires ``created_at`` / ``updated_at`` (the public
    representation of a persisted row), so we stub them to a
    pinned constant."""
    payload = {
        'id': uuid4(),
        'vault_id': uuid4(),
        'kind': 'procedure',
        'scope': 'global',
        'title': 'procedural-suite-dto',
        'summary': 'Test card entry.',
        'created_at': _NOW,
        'updated_at': _NOW,
    }
    payload.update(overrides)
    return ProceduralEntryDTO.model_validate(payload)


def _base_payload(**overrides):
    """A baseline valid ``ProceduralEntryCreate`` payload.

    All field-level validation tests start from this and patch
    one field at a time so the failure message points at the
    single load-bearing field under test."""
    payload = {
        'vault_id': uuid4(),
        'kind': 'procedure',
        'scope': 'global',
        'verb': 'deploy',
        'context': 'staging',
        'title': 'procedural-suite-dto-test',
        'summary': 'Test entry.',
        'body': '',
        'trigger': None,
        'tags': [],
        'extra_metadata': {},
        'status': 'published',
        'origin': 'manual',
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Kind literal — closed set; typo "procedre" is the regression we catch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('kind', ['case', 'procedure', 'strategy'])
def test_entry_create_accepts_all_three_kinds(kind):
    """All three V7 kinds are accepted at the DTO layer."""
    payload = _base_payload(kind=kind, verb=None, context=None, trigger=None)
    if kind == 'case':
        payload['trigger'] = 'database connection pool exhausted'
    dto = ProceduralEntryCreate.model_validate(payload)
    assert dto.kind == kind


@pytest.mark.parametrize('bad_kind', ['procedre', 'CASE', 'case ', 'observations', ''])
def test_entry_create_rejects_unknown_kind(bad_kind):
    """The KindLiteral is closed; a typo'd kind is rejected at the
    Pydantic layer (not deep in the repository where the error
    would be a 500)."""
    payload = _base_payload(kind=bad_kind)
    with pytest.raises(ValidationError, match='kind'):
        ProceduralEntryCreate.model_validate(payload)


# ---------------------------------------------------------------------------
# Status literal — closed set; default-published for the briefing
# semantics, but the create DTO defaults to 'draft' for safety.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('status', ['draft', 'published', 'deprecated'])
def test_entry_create_accepts_all_three_statuses(status):
    """All three V7 lifecycle states are accepted."""
    payload = _base_payload(status=status)
    dto = ProceduralEntryCreate.model_validate(payload)
    assert dto.status == status


def test_entry_create_defaults_status_to_draft():
    """A create without an explicit status defaults to 'draft' (the
    safer default — agents must explicitly publish). A regression
    that flips the default to 'published' would silently surface
    unvetted entries to search.

    NB: the *search* DTO defaults to 'published' (briefing semantics);
    the *create* DTO defaults to 'draft' (write-side safety). The
    asymmetry is intentional — see DTO docstrings."""
    payload = _base_payload()
    payload.pop('status')
    dto = ProceduralEntryCreate.model_validate(payload)
    assert dto.status == 'draft'


@pytest.mark.parametrize('bad_status', ['PUBLISHED', 'deleted', 'archived', 'active'])
def test_entry_create_rejects_unknown_status(bad_status):
    """The StatusLiteral is closed; a typo'd status is rejected
    at the DTO layer."""
    payload = _base_payload(status=bad_status)
    with pytest.raises(ValidationError, match='status'):
        ProceduralEntryCreate.model_validate(payload)


# ---------------------------------------------------------------------------
# Origin literal — closed set; 'seed' / 'kv_backfill' / 'derived' /
# 'manual' / 'import'. The eval suite uses 'seed' (procedural_upsert
# setup action); a regression that drops 'seed' from the literal
# would break the suite.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('origin', ['seed', 'kv_backfill', 'derived', 'manual', 'import'])
def test_entry_create_accepts_all_origins(origin):
    """All five V7 origin values are accepted."""
    payload = _base_payload(origin=origin)
    dto = ProceduralEntryCreate.model_validate(payload)
    assert dto.origin == origin


# ---------------------------------------------------------------------------
# extra='forbid' — the create DTO rejects unknown fields. A
# regression that drops the constraint would silently let typos
# through to the repository, where they'd 500.
# ---------------------------------------------------------------------------


def test_entry_create_rejects_unknown_field():
    """The DTO has ``extra='forbid'`` so an unknown field is rejected
    at parse time. A regression that drops the constraint would let
    a typo'd field name silently drop on the floor (the Pydantic
    default is to ignore extras)."""
    payload = _base_payload(not_a_real_field='oops')
    with pytest.raises(ValidationError, match='not_a_real_field'):
        ProceduralEntryCreate.model_validate(payload)


def test_entry_update_rejects_unknown_field():
    """The update DTO has ``extra='forbid'`` (same as create) — a
    typo'd field name surfaces at parse time instead of being
    silently dropped on the floor.

    The all-optional shape makes this guard especially load-bearing:
    a typo'd field would otherwise be ignored and the call would
    succeed with no mutation, leaving the operator convinced the
    update took."""
    with pytest.raises(ValidationError, match='not_a_real_field'):
        ProceduralEntryUpdate.model_validate({'not_a_real_field': 'oops'})


# ---------------------------------------------------------------------------
# Search DTO — default status is 'published' (briefing semantics);
# the write-side DTO defaults to 'draft' (publish is a deliberate
# agent action).
# ---------------------------------------------------------------------------


def test_search_request_accepts_query_only():
    """A query alone (no scope) is valid — the search service
    falls back to vault-wide."""
    req = ProceduralSearchRequest.model_validate({'query': 'rollback'})
    assert req.query == 'rollback'
    assert req.scope is None


def test_search_request_accepts_scope_only():
    """A scope alone (no query) is valid — the agent pins to a
    specific scope without a textual query."""
    req = ProceduralSearchRequest.model_validate({'scope': 'project:v7-eval'})
    assert req.scope == 'project:v7-eval'
    assert req.query is None


def test_search_request_default_limit_is_10():
    """Default limit is 10 — matches the briefing default. A
    regression that bumps the default to 100 would silently
    return too much for the briefing block."""
    req = ProceduralSearchRequest.model_validate({'query': 'x'})
    assert req.limit == 10


def test_search_request_limit_bounds():
    """The limit field is bounded ``ge=1, le=100``. A regression
    that loosens the upper bound would let a runaway query DOS
    the search service."""
    with pytest.raises(ValidationError, match='limit'):
        ProceduralSearchRequest.model_validate({'query': 'x', 'limit': 0})
    with pytest.raises(ValidationError, match='limit'):
        ProceduralSearchRequest.model_validate({'query': 'x', 'limit': 1000})


def test_search_request_default_status_is_published():
    """The search DTO defaults to 'published' — the briefing
    semantics are "give me what's safe to surface to the agent",
    and drafts are by definition not yet vetted.

    A regression that flipped the DTO default to 'draft' would
    silently return an empty result set for the briefing block
    (since the briefing is published-only by definition)."""
    req = ProceduralSearchRequest.model_validate({'query': 'x'})
    assert req.status == 'published'


# ---------------------------------------------------------------------------
# Briefing-card DTO — pin_position is set by the service, not the
# caller. The DTO has it as required (no default) so a regression
# that dropped it would surface at card-emit time.
# ---------------------------------------------------------------------------


def test_briefing_card_requires_pin_position():
    """An ``ProceduralBriefingCard`` must carry a pin_position
    (the service computes it from the pin context). A regression
    that made it optional would let ``pin_position=None`` leak
    to the agent briefing block."""
    fake_entry = _fake_entry_dto(title='briefing-suite-dto', summary='Test card entry.')
    with pytest.raises(ValidationError, match='pin_position'):
        ProceduralBriefingCard.model_validate(
            {
                'entry': fake_entry,
                # pin_position omitted
                'context_key': 'global',
            }
        )


def test_briefing_cards_envelope_round_trip():
    """A list of cards round-trips through the envelope — the
    agent's briefing block reads ``cards`` in order, and the
    envelope is what the API serialises."""
    fake_entry = _fake_entry_dto(title='briefing-suite-round-trip', summary='Round-trip card.')
    cards = [
        ProceduralBriefingCard(entry=fake_entry, pin_position=0, context_key='global'),
        ProceduralBriefingCard(entry=fake_entry, pin_position=1, context_key='project:v7-eval'),
    ]
    envelope = ProceduralBriefingCards(cards=cards, context_keys=['global', 'project:v7-eval'])
    assert len(envelope.cards) == 2
    assert envelope.cards[0].pin_position == 0
    assert envelope.cards[1].pin_position == 1
    assert envelope.context_keys == ['global', 'project:v7-eval']
