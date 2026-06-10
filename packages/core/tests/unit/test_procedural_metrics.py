"""Procedural-plane metrics + tracing helpers.

The 5 new metrics and the search/briefing/upsert span emission are
tested here as black-box counter/histogram increments — we exercise
the helpers directly (they are module-level functions) so the tests
do not need the FastAPI app or a real Postgres.
"""

from __future__ import annotations

import pytest

from memex_core.metrics import (
    PROCEDURAL_BRIEFING_CARDS_TOTAL,
    PROCEDURAL_IDENTITY_CONFLICT_TOTAL,
    PROCEDURAL_OPERATIONS_TOTAL,
    PROCEDURAL_SEARCH_DURATION_SECONDS,
)
from memex_core.server.procedural import (
    _context_count_bucket,
    _record_briefing_cards,
    _record_identity_conflict,
    _record_search,
    _record_write_outcome,
)
from memex_core.services.procedural_repository import (
    ProceduralEntryNotFound,
    ProceduralIdentityConflict,
)


# --- _context_count_bucket -------------------------------------------------


@pytest.mark.parametrize(
    'n, bucket',
    [
        (0, '1'),  # Defensive — the route rejects min_length=0
        (1, '1'),
        (2, '2'),
        (5, '5'),
        (6, '6_to_10'),
        (10, '6_to_10'),
        (11, '10+'),
        (9999, '10+'),
    ],
)
def test_context_count_bucket_boundaries(n, bucket):
    assert _context_count_bucket(n) == bucket


# --- _record_write_outcome -------------------------------------------------


def test_record_write_outcome_success():
    _record_write_outcome('create', 'procedure', None)
    val = PROCEDURAL_OPERATIONS_TOTAL.labels(
        operation='create', kind='procedure', outcome='success'
    )._value.get()
    assert val >= 1


def test_record_write_outcome_identity_conflict():
    exc = ProceduralIdentityConflict('x')
    _record_write_outcome('create', 'strategy', exc)
    val = PROCEDURAL_OPERATIONS_TOTAL.labels(
        operation='create', kind='strategy', outcome='identity_conflict'
    )._value.get()
    assert val >= 1


def test_record_write_outcome_not_found():
    exc = ProceduralEntryNotFound('x')
    _record_write_outcome('update', 'procedure', exc)
    val = PROCEDURAL_OPERATIONS_TOTAL.labels(
        operation='update', kind='procedure', outcome='not_found'
    )._value.get()
    assert val >= 1


def test_record_write_outcome_generic_error():
    """Anything that is not the two domain errors is labelled `error`."""
    _record_write_outcome('deprecate', 'case', RuntimeError('boom'))
    val = PROCEDURAL_OPERATIONS_TOTAL.labels(
        operation='deprecate', kind='case', outcome='error'
    )._value.get()
    assert val >= 1


# --- _record_search -------------------------------------------------------


def _hit(matched_via: str) -> object:
    """Build a minimal stub that quacks like an ProceduralSearchHit."""
    return type('Hit', (), {'matched_via': matched_via})()


class _Resp:
    def __init__(self, hits: list[object]) -> None:
        self.hits = hits


def test_record_search_bm25_only():
    """A single-stream result keeps the stream-label granular — that's
    the whole point of the histogram label, not a coarse 'mixed' bucket."""
    before = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(
        kind='procedure', streams='bm25_only'
    )._sum.get()
    _record_search(_api_stub(), 'procedure', _Resp([_hit('bm25')]), 0.01)
    after = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(
        kind='procedure', streams='bm25_only'
    )._sum.get()
    assert after >= before + 0.01


def test_record_search_rrf_when_both_streams_hit():
    """When both bm25 and vector hit, the label is `rrf` (Reciprocal
    Rank Fusion) — not two separate observations."""
    before = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(kind='strategy', streams='rrf')._sum.get()
    _record_search(
        _api_stub(),
        'strategy',
        _Resp([_hit('bm25'), _hit('vector'), _hit('rrf')]),
        0.05,
    )
    after = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(kind='strategy', streams='rrf')._sum.get()
    assert after >= before + 0.05


def test_record_search_pin_only():
    before = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(kind='all', streams='pin_only')._sum.get()
    _record_search(_api_stub(), None, _Resp([_hit('pin')]), 0.001)
    after = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(kind='all', streams='pin_only')._sum.get()
    assert after >= before + 0.001


def test_record_search_empty_result_uses_bm25_only_label():
    """An empty result set is labelled `bm25_only` — the cheapest bucket.
    We never emit an unlabelled observation; absence of a kind label
    would leave the histogram with a wildcard, which Prometheus
    aggregations cannot reason about.
    """
    before = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(
        kind='procedure', streams='bm25_only'
    )._sum.get()
    _record_search(_api_stub(), 'procedure', _Resp([]), 0.001)
    after = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(
        kind='procedure', streams='bm25_only'
    )._sum.get()
    assert after >= before + 0.001


def test_record_search_kind_falls_back_to_all():
    """A `None` kind is labelled 'all' so the histogram keeps a bounded
    label set even when the request did not narrow the search."""
    before = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(kind='all', streams='bm25_only')._sum.get()
    _record_search(_api_stub(), None, _Resp([_hit('bm25')]), 0.001)
    after = PROCEDURAL_SEARCH_DURATION_SECONDS.labels(kind='all', streams='bm25_only')._sum.get()
    assert after >= before + 0.001


# --- _record_briefing_cards ----------------------------------------------


def test_record_briefing_cards_single_context():
    before = PROCEDURAL_BRIEFING_CARDS_TOTAL.labels(context_count_bucket='1')._value.get()
    _record_briefing_cards(['project:42'])
    after = PROCEDURAL_BRIEFING_CARDS_TOTAL.labels(context_count_bucket='1')._value.get()
    assert after == before + 1


def test_record_briefing_cards_bucketed():
    """A request with 7 context keys lands in the 6_to_10 bucket."""
    before = PROCEDURAL_BRIEFING_CARDS_TOTAL.labels(context_count_bucket='6_to_10')._value.get()
    _record_briefing_cards([f'ctx:{i}' for i in range(7)])
    after = PROCEDURAL_BRIEFING_CARDS_TOTAL.labels(context_count_bucket='6_to_10')._value.get()
    assert after == before + 1


def test_record_briefing_cards_10plus_bucket():
    """20 context keys land in 10+ — bounded cardinality is the goal."""
    before = PROCEDURAL_BRIEFING_CARDS_TOTAL.labels(context_count_bucket='10+')._value.get()
    _record_briefing_cards([f'ctx:{i}' for i in range(20)])
    after = PROCEDURAL_BRIEFING_CARDS_TOTAL.labels(context_count_bucket='10+')._value.get()
    assert after == before + 1


# --- _record_identity_conflict -------------------------------------------


def test_record_identity_conflict_uses_config_mode():
    """The mode label is the operator's setting — pin that a config flip
    is reflected in the metric label (this is what tells the operator
    'the agent is now using upsert, not 409-ing')."""
    api = _api_stub_with_mode('upsert')
    before = PROCEDURAL_IDENTITY_CONFLICT_TOTAL.labels(kind='procedure', mode='upsert')._value.get()
    _record_identity_conflict(api, 'procedure', ProceduralIdentityConflict('x'))
    after = PROCEDURAL_IDENTITY_CONFLICT_TOTAL.labels(kind='procedure', mode='upsert')._value.get()
    assert after == before + 1


def test_record_identity_conflict_reject_mode():
    api = _api_stub_with_mode('reject')
    before = PROCEDURAL_IDENTITY_CONFLICT_TOTAL.labels(kind='case', mode='reject')._value.get()
    _record_identity_conflict(api, 'case', ProceduralIdentityConflict('x'))
    after = PROCEDURAL_IDENTITY_CONFLICT_TOTAL.labels(kind='case', mode='reject')._value.get()
    assert after == before + 1


# --- helpers -------------------------------------------------------------


def _api_stub():
    """The search helper accepts a MemexAPI; it never reads anything off it.
    The simpler stub means tests stay decoupled from MemexAPI construction.
    """
    return type('API', (), {})()


def _api_stub_with_mode(mode: str):
    """Build a stub matching the real `api.config.server.memory.procedural`
    attribute path. The depth of nesting equals the access path length,
    not the conceptual class count."""

    class Procedural:
        identity_conflict_mode = mode

    class Memory:
        procedural = Procedural()

    class Server:
        memory = Memory()

    class Config:
        server = Server()

    class API:
        config = Config()

    return API()
