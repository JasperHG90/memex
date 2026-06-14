"""`RemoteMemexAPI` procedural-plane client methods.

The remote client mirrors the HTTP ``/procedural/*`` surface 1:1 — 8
methods, one per route. These tests guard:

* The method names (``procedural_*``, NOT ``procedural_*`` — the
  legacy engine-internal name has leaked into the public HTTP/CLI
  surface in past refactors and must not).
* The return types are the DTOs the routes emit, not raw dicts.
* The body-routing for ``procedural_briefing_cards`` uses a JSON
  array body (the route is ``Body(min_length=1)`` on a list, not a
  single dict).
"""

from __future__ import annotations

import inspect
from uuid import UUID

from memex_common.client import RemoteMemexAPI
from memex_common.procedural_schemas import (
    ProceduralBriefingCards,
    ProceduralEntryCreate,
    ProceduralEntryDTO,
    ProceduralEntryUpdate,
    ProceduralSearchRequest,
    ProceduralSearchResponse,
    ShortLabel,
)


def _public_methods(cls):
    return {
        name
        for name, member in inspect.getmembers(cls, predicate=inspect.iscoroutinefunction)
        if not name.startswith('_')
    }


def test_client_exposes_the_procedural_surface():
    methods = {name for name in _public_methods(RemoteMemexAPI) if name.startswith('procedural_')}
    expected = {
        # briefing_cards remains a CLI/eval/operator read — the
        # AGENT-facing briefing tool is gone (cards arrive inside the
        # session briefing), but the HTTP composition surface stays.
        'procedural_briefing_cards',
        'procedural_create',
        'procedural_deprecate',
        'procedural_get',
        'procedural_get_by_identity',
        'procedural_search',
        'procedural_update',
        'procedural_upsert',
        # Curation + version-ledger surface (§18.8 / §19.8).
        'procedural_pin',
        'procedural_unpin',
        'procedural_list_pins',
        'procedural_list_versions',
        'procedural_list',
        'procedural_rollback',
        # §18.5 enactment-outcome report (bump counters, no version row).
        'procedural_report_outcome',
        # §9 derivation drain (drains cases → procedures/strategies).
        'procedural_derive',
    }
    assert methods == expected, (
        f'Procedural client surface drift.\n  Expected: {sorted(expected)}\n  '
        f'Got:      {sorted(methods)}'
    )


def test_client_exposes_case_submit():
    """Cases are notes — the submission path is its own method, not a
    procedural_* plane write (§18.3 / §18.9.0)."""
    assert 'case_submit' in _public_methods(RemoteMemexAPI)


def test_client_does_not_expose_legacy_experiential_methods():
    """The legacy engine-internal name must NOT leak into the public
    client surface — the plane is named *procedural* everywhere
    (JG directive 2026-06-10)."""
    methods = _public_methods(RemoteMemexAPI)
    assert not any(name.startswith('experiential_') for name in methods), (
        'RemoteMemexAPI must not expose legacy `experiential_*` methods '
        '— the public surface is the procedural plane.'
    )


def test_procedural_create_signature():
    sig = inspect.signature(RemoteMemexAPI.procedural_create)
    params = sig.parameters
    assert 'payload' in params
    assert params['payload'].annotation is ProceduralEntryCreate
    ret = sig.return_annotation
    assert ret is ProceduralEntryDTO


def test_procedural_get_signature():
    sig = inspect.signature(RemoteMemexAPI.procedural_get)
    assert sig.parameters['entry_id'].annotation is UUID
    assert sig.parameters['vault_id'].default is None
    assert sig.return_annotation is ProceduralEntryDTO


def test_procedural_get_by_identity_signature():
    sig = inspect.signature(RemoteMemexAPI.procedural_get_by_identity)
    params = sig.parameters
    assert params['kind'].annotation is str
    assert params['scope'].annotation is ShortLabel
    assert params['verb'].default is None
    assert params['context'].default is None
    assert params['vault_id'].default is None
    # Returns the DTO or None — the "did we already learn this?" probe.
    assert sig.return_annotation in (
        ProceduralEntryDTO | None,
        'ProceduralEntryDTO | None',
    )


def test_procedural_update_signature():
    sig = inspect.signature(RemoteMemexAPI.procedural_update)
    assert sig.parameters['entry_id'].annotation is UUID
    assert sig.parameters['payload'].annotation is ProceduralEntryUpdate
    assert sig.return_annotation is ProceduralEntryDTO


def test_procedural_deprecate_signature():
    sig = inspect.signature(RemoteMemexAPI.procedural_deprecate)
    assert sig.parameters['entry_id'].annotation is UUID
    assert sig.parameters['superseded_by_id'].default is None
    assert sig.parameters['vault_id'].default is None
    assert sig.return_annotation is ProceduralEntryDTO


def test_procedural_upsert_signature():
    sig = inspect.signature(RemoteMemexAPI.procedural_upsert)
    assert sig.parameters['payload'].annotation is ProceduralEntryCreate
    assert sig.return_annotation is ProceduralEntryDTO


def test_procedural_search_signature():
    sig = inspect.signature(RemoteMemexAPI.procedural_search)
    assert sig.parameters['request'].annotation is ProceduralSearchRequest
    assert sig.return_annotation is ProceduralSearchResponse


def test_procedural_briefing_cards_signature():
    # The route is POST /procedural/briefing-cards with the body
    # shaped as a JSON list of context keys. The client method must
    # take a list of ShortLabel, not a single ShortLabel. (Python
    # evaluates the annotation to the real type under `from __future__
    # import annotations` only when stringified — here it resolves.
    # ShortLabel is an Annotated[str, ...] alias, so the evaluated
    # annotation collapses to list[str].)
    import typing

    ann = typing.get_type_hints(RemoteMemexAPI.procedural_briefing_cards)
    origin = typing.get_origin(ann['context_keys'])
    assert origin is list
    assert ann['return'] is ProceduralBriefingCards


def test_procedural_list_signature():
    """The enumeration surface (e.g. drafts awaiting confirmation) that
    search cannot serve. All filters optional; returns a list of DTOs."""
    sig = inspect.signature(RemoteMemexAPI.procedural_list)
    params = sig.parameters
    assert params['status'].default is None
    assert params['scope'].default is None
    assert params['kind'].default is None
    assert params['vault_id'].default is None
    assert params['limit'].default == 50
    ret = sig.return_annotation
    assert ret in (list[ProceduralEntryDTO], 'list[ProceduralEntryDTO]')


def test_procedural_report_outcome_signature():
    """POST /procedural/{entry_id}/report — bump enactment counters.
    ``outcome`` is required; ``vault_id`` is an optional scoping guard."""
    sig = inspect.signature(RemoteMemexAPI.procedural_report_outcome)
    params = sig.parameters
    assert params['entry_id'].annotation is UUID
    assert params['outcome'].annotation is str
    assert params['vault_id'].default is None
    assert sig.return_annotation is ProceduralEntryDTO


def test_procedural_method_uuids_are_uuid_typed():
    """All methods that take an entry_id must declare it as UUID —
    not str — so the client cannot pass a malformed identifier
    silently. (The HTTP layer would 404; better to type-fence at
    the call site.)"""
    sig = inspect.signature(RemoteMemexAPI.procedural_get)
    assert sig.parameters['entry_id'].annotation is UUID
    sig = inspect.signature(RemoteMemexAPI.procedural_update)
    assert sig.parameters['entry_id'].annotation is UUID
    sig = inspect.signature(RemoteMemexAPI.procedural_deprecate)
    assert sig.parameters['entry_id'].annotation is UUID
