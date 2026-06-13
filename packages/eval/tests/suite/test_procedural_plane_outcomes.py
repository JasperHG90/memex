"""Unit tests for the procedural-plane eval suite contracts.

The suite is integration-tested against a real Memex server (see
``memex-eval suite run procedural_plane``). This file pins the parts
that don't need a live server:

* ``ProceduralEntryRoundtrip`` / ``ProceduralSearchResults`` classify
  AgentAnswer shapes into pass/fail correctly.
* The ``_classify_outcome`` helper maps the procedural API's
  404 / 409 / 200 / unknown-error responses to the right status
  string.
* The suite's ``METADATA`` carries the load-bearing knobs and
  components-under-test.
* Each of the 10 scenario IDs is registered (no typos / renames).
* The suite-private ``procedural_upsert`` setup action validates
  required params and surfaces Pydantic errors loudly (no
  silent-absorb of malformed seeds).

The tests deliberately avoid driving ``DirectApiBackend`` end-to-end
because that requires a live server (covered by the integration
suite). What we test here is the OUTCOME LOGIC and SUITE SHAPE —
the things a typo or refactor would break silently.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from memex_eval.suite.agents import AgentAnswer
from memex_eval.suite.loader import load_suite
from memex_eval.suites.procedural_plane._outcomes import (
    ProceduralEntryRoundtrip,
    ProceduralSearchResults,
    _classify_outcome,
)


# ---------------------------------------------------------------------------
# _classify_outcome — maps AgentAnswer → status string
# ---------------------------------------------------------------------------


def test_classify_success_with_units():
    """A successful call leaves ``answer.units`` populated and ``error`` None."""
    ans = AgentAnswer(units=[SimpleNamespace(kind='procedure')])
    assert _classify_outcome(ans) == 'success'


def test_classify_not_found_when_units_empty():
    """An unbound get_by_identity returns empty units + no error → 'not_found'."""
    ans = AgentAnswer(units=[])
    assert _classify_outcome(ans) == 'not_found'


@pytest.mark.parametrize(
    'err_str',
    [
        'HTTP 409 conflict on identity anchor',
        '409 Conflict: anchor exists',
        'IdentityAnchorConflict',
    ],
)
def test_classify_409_maps_to_conflict(err_str: str):
    """A 409 error string classifies as 'conflict' regardless of casing."""
    ans = AgentAnswer(units=[], error=err_str)
    assert _classify_outcome(ans) == 'conflict'


@pytest.mark.parametrize(
    'err_str',
    [
        'HTTP 404 not found',
        '404 Not Found',
        'Not Found',
    ],
)
def test_classify_404_maps_to_not_found(err_str: str):
    """A 404 error string classifies as 'not_found'.

    The helper matches on substring ``'404'`` or ``'not found'`` —
    the canonical error shapes raised by ``httpx.HTTPStatusError``
    on the procedural plane. A regression that broadens the
    match (e.g. to the bare word 'not') would falsely classify
    arbitrary errors as ``not_found``."""
    ans = AgentAnswer(units=[], error=err_str)
    assert _classify_outcome(ans) == 'not_found'


def test_classify_unknown_error_maps_to_any_error():
    """An unrecognized error (5xx, transport) classifies as 'any_error'."""
    ans = AgentAnswer(units=[], error='HTTP 500 internal server error')
    assert _classify_outcome(ans) == 'any_error'


# ---------------------------------------------------------------------------
# ProceduralEntryRoundtrip.score — outcome scoring paths
# ---------------------------------------------------------------------------


def _dto(**kwargs) -> SimpleNamespace:
    """Build a SimpleNamespace that mimics ``ProceduralEntryDTO``."""
    return SimpleNamespace(
        id=UUID('00000000-0000-0000-0000-000000000001'),
        kind=kwargs.get('kind'),
        scope=kwargs.get('scope'),
        verb=kwargs.get('verb'),
        **{k: v for k, v in kwargs.items() if k not in ('kind', 'scope', 'verb')},
    )


def test_entry_roundtrip_success_with_field_match():
    """Success + all field-level expectations met → pass=1.0."""
    outcome = ProceduralEntryRoundtrip(
        type='procedural_entry_roundtrip',
        operation='upsert',
        kind='procedure',
        scope='global',
        verb='deploy',
        context='staging',
        expect_status='success',
        expect_kind='procedure',
        expect_scope='global',
        expect_verb='deploy',
    )
    ans = AgentAnswer(units=[_dto(kind='procedure', scope='global', verb='deploy')])
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 1.0
    assert metrics['outcome_status_match'] == 1.0
    assert metrics['field_match'] == 1.0


def test_entry_roundtrip_conflict_classified_correctly():
    """A 409 (conflict) error string maps to expect_status='conflict' → pass."""
    outcome = ProceduralEntryRoundtrip(
        type='procedural_entry_roundtrip',
        operation='create',
        kind='procedure',
        scope='global',
        verb='rotate',
        context='api_key',
        expect_status='conflict',
    )
    ans = AgentAnswer(units=[], error='HTTP 409 conflict on identity anchor')
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 1.0
    assert metrics['outcome_status_match'] == 1.0
    # Field-match is only checked when status='success' AND units is
    # non-empty — on a 409 there are no fields to assert.
    assert metrics['field_match'] == 1.0


def test_entry_roundtrip_field_mismatch_fails():
    """A successful call with the wrong kind → pass=0.0 (field mismatch)."""
    outcome = ProceduralEntryRoundtrip(
        type='procedural_entry_roundtrip',
        operation='upsert',
        kind='procedure',
        scope='global',
        verb='deploy',
        context='staging',
        expect_status='success',
        expect_kind='strategy',  # …but the DTO is a procedure
    )
    ans = AgentAnswer(units=[_dto(kind='procedure', scope='global', verb='deploy')])
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 0.0
    assert metrics['outcome_status_match'] == 1.0  # status matched…
    assert metrics['field_match'] == 0.0  # …but the kind didn't.


def test_entry_roundtrip_kind_none_passes_field_check():
    """When expect_kind is None, the outcome doesn't assert kind — only
    the load-bearing fields. A regression that re-introduces a kind
    check on a None-expected outcome would surface here."""
    outcome = ProceduralEntryRoundtrip(
        type='procedural_entry_roundtrip',
        operation='get_by_identity',
        kind='procedure',
        scope='user',
        verb='commit',
        context='prefix',
        expect_status='success',
        # expect_kind=None — don't assert kind
    )
    ans = AgentAnswer(units=[_dto(kind='procedure', scope='user', verb='commit')])
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 1.0


# ---------------------------------------------------------------------------
# ProceduralSearchResults.score — read-call paths
# ---------------------------------------------------------------------------


def test_search_results_cardinality_floor_fails():
    """A search that returns zero hits when min_hits=1 → pass=0.0."""
    outcome = ProceduralSearchResults(
        type='procedural_search_results',
        operation='search',
        query='anything',
        min_hits=1,
    )
    ans = AgentAnswer(units=[])  # zero hits
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 0.0
    assert metrics['cardinality'] == 0.0
    # No error means the call succeeded — just no hits.
    assert metrics['error'] == 1.0


def test_search_results_error_short_circuits_to_zero():
    """A failed search (error set) → all scores 0.0, including pass."""
    outcome = ProceduralSearchResults(
        type='procedural_search_results',
        operation='search',
        query='anything',
        min_hits=1,
    )
    ans = AgentAnswer(units=[], error='HTTP 500 internal server error')
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 0.0
    assert metrics['error'] == 0.0
    assert metrics['cardinality'] == 0.0
    assert metrics['field_match'] == 0.0


def test_search_results_field_match_via_kind():
    """A hit with the expected kind passes the field-match check."""
    outcome = ProceduralSearchResults(
        type='procedural_search_results',
        operation='search',
        query='rollback',
        min_hits=1,
        expect_kind='procedure',
    )
    ans = AgentAnswer(units=[_dto(kind='procedure', scope='project:x', verb='rollback')])
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 1.0
    assert metrics['field_match'] == 1.0


def test_search_results_field_match_fails_when_no_hit_matches():
    """All hits are the wrong kind → field_match=0.0."""
    outcome = ProceduralSearchResults(
        type='procedural_search_results',
        operation='search',
        query='rollback',
        min_hits=1,
        expect_kind='strategy',  # …but the hit is a procedure
    )
    ans = AgentAnswer(units=[_dto(kind='procedure', scope='project:x', verb='rollback')])
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 0.0
    assert metrics['field_match'] == 0.0


def test_search_results_briefing_pin_position_passes():
    """A briefing-cards call whose first card is at pin_position=0 → pass=1.0."""
    outcome = ProceduralSearchResults(
        type='procedural_search_results',
        operation='briefing_cards',
        context_keys=['global', 'project:proc-eval', 'app:eval'],
        min_hits=1,
        expect_first_pin_pos=0,
    )
    first = SimpleNamespace(
        entry=_dto(kind='procedure', scope='global', verb='test'),
        pin_position=0,
        context_key='global',
    )
    ans = AgentAnswer(units=[first])
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 1.0
    assert metrics['pin_position'] == 1.0


def test_search_results_briefing_pin_position_fails_when_wrong():
    """A briefing-cards call whose first card is at pin_position=2 → pin_pos=0.0."""
    outcome = ProceduralSearchResults(
        type='procedural_search_results',
        operation='briefing_cards',
        context_keys=['global', 'project:proc-eval', 'app:eval'],
        min_hits=1,
        expect_first_pin_pos=0,  # expect global first
    )
    first = SimpleNamespace(
        entry=_dto(kind='procedure', scope='project:proc-eval', verb='test'),
        pin_position=2,  # …but the first card is at pin 2
        context_key='project:proc-eval',
    )
    ans = AgentAnswer(units=[first])
    metrics = outcome.score(ans, scenario=None)
    assert metrics['pass'] == 0.0
    assert metrics['pin_position'] == 0.0


# ---------------------------------------------------------------------------
# Suite shape — loadable, 10 scenarios, METADATA knobs/identity intact
# ---------------------------------------------------------------------------


def test_suite_loads_with_ten_scenarios():
    """The suite package is discoverable via ``load_suite`` and has 11 scenarios.

    The count is the procedural contract; a regression that drops a
    scenario (e.g. round-3 review cut) surfaces here. The 11th is the §9
    derivation gate (`derivation_distills_procedure_from_cases`), added
    with the cases→procedures pipeline. These scenarios drive the
    HTTP/API layer directly (DirectApiBackend), which retains full
    procedural CRUD — distinct from the agent (MCP/Hermes) surface, which
    is read-only + case_submit."""
    suite = load_suite('procedural_plane')
    assert len(suite.scenarios) == 11, (
        f'procedural_plane should have 11 scenarios, got {len(suite.scenarios)}: '
        f'{[s.id for s in suite.scenarios]}'
    )


def test_suite_scenario_ids_are_stable():
    """Pin the 11 scenario IDs — a typo or rename breaks the integration gate."""
    expected = {
        'identity_anchor_collision_returns_409',
        'upsert_on_existing_anchor_updates_in_place',
        'get_by_identity_returns_seeded_entry',
        'get_by_identity_returns_404_when_unbound',
        'search_returns_seeded_procedure',
        'briefing_cards_pin_chain_union',
        'briefing_cards_pin_position_order',
        'deprecate_drops_from_published_search',
        'status_published_hides_drafts',
        'case_submit_files_note_with_explicit_assignment',
        'derivation_distills_procedure_from_cases',
    }
    suite = load_suite('procedural_plane')
    actual = {s.id for s in suite.scenarios}
    assert actual == expected, (
        f'scenario IDs drifted: missing={expected - actual}, extra={actual - expected}'
    )


def test_suite_metadata_knobs_match_v7_contracts():
    """The METADATA.knobs list names the procedural server-side tunables. A
    regression that drops a knob surfaces here."""
    suite = load_suite('procedural_plane')
    knobs = set(suite.metadata.knobs)
    # The five load-bearing knobs.
    assert 'server.memory.procedural.enabled' in knobs
    assert 'server.memory.procedural.search_default_bm25_weight' in knobs
    assert 'server.memory.procedural.search_default_vector_weight' in knobs
    assert 'server.memory.procedural.identity_conflict_mode' in knobs
    assert 'server.memory.procedural.briefing_default_limit_per_context' in knobs


def test_suite_metadata_components_under_test_pinned():
    """The METADATA.components_under_test names the components."""
    suite = load_suite('procedural_plane')
    cuts = set(suite.metadata.components_under_test)
    expected = {
        'procedural.identity_anchor',
        'procedural.create',
        'procedural.upsert',
        'procedural.get_by_identity',
        'procedural.deprecate',
        'procedural.search',
        'procedural.briefing_cards',
    }
    assert expected.issubset(cuts), f'missing components: {expected - cuts}'


def test_suite_does_not_require_llm_judge():
    """The suite is read-side heavy and bypasses extraction — no LLM
    judge needed. A regression that flips this would make the suite
    CI-flake on ANTHROPIC_API_KEY."""
    suite = load_suite('procedural_plane')
    assert suite.metadata.requires_llm_judge is False


def test_suite_default_answer_mode_is_api():
    """The suite uses the direct-API backend (no agent in the loop).
    A regression that flips to claude-code would require a CLI
    subprocess + LLM access."""
    suite = load_suite('procedural_plane')
    assert suite.metadata.default_answer_mode == 'api'


# ---------------------------------------------------------------------------
# Suite-private setup action — required param validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procedural_upsert_rejects_missing_required_params():
    """The setup action validates required params at the top of ``run``
    and raises ``ValueError`` with the load-bearing context.

    Param names carry the ``kind_`` prefix to avoid colliding with the
    ``SetupAction.kind`` discriminator — see the handler docstring."""
    from memex_eval.suites.procedural_plane._setup_actions import _ProceduralUpsert

    handler = _ProceduralUpsert()
    with pytest.raises(ValueError, match='requires kind_kind, kind_scope, and kind_title'):
        await handler.run(
            api=None,  # type: ignore[arg-type]  # validation fires before the call
            vault_id=UUID('00000000-0000-0000-0000-000000000000'),
            params={'kind_kind': 'procedure'},  # kind_scope and kind_title missing
        )


@pytest.mark.asyncio
async def test_procedural_upsert_rejects_payload_validation_error():
    """When the params build a malformed ``ProceduralEntryCreate``
    (e.g. an unknown kind), the action raises a ValueError that
    surfaces the Pydantic error — not a silent-absorb."""
    from memex_eval.suites.procedural_plane._setup_actions import _ProceduralUpsert

    handler = _ProceduralUpsert()
    with pytest.raises(ValueError, match='payload validation failed'):
        await handler.run(
            api=None,  # type: ignore[arg-type]
            vault_id=UUID('00000000-0000-0000-0000-000000000000'),
            params={
                'kind_kind': 'not-a-real-kind',  # kind is a Literal — must fail
                'kind_scope': 'global',
                'kind_title': 'procedural-suite-malformed',
            },
        )


# ---------------------------------------------------------------------------
# Suite-private setup action — lifecycle re-assert (tombstone regression)
#
# The procedural identity anchor (kind, scope, verb, context) is a single
# vault-agnostic DB-global row, and ``_ProceduralUpsert.teardown`` deprecates
# it after every scenario/replicate. ``upsert_by_identity`` deliberately
# PRESERVES a deprecated state on rewrite (anti-resurrect guard), so a later
# scenario re-seeding the SAME anchor lands ``status='deprecated'`` and is
# invisible to the default ``status='published'`` search — the seed silently
# no-ops. ``run`` must re-assert the requested state via explicit PATCH.
# ---------------------------------------------------------------------------


_SEED_ENTRY_ID = UUID('00000000-0000-0000-0000-000000000001')


class _FakeProceduralApi:
    """Async stand-in for ``RemoteMemexAPI`` covering the calls
    ``_ProceduralUpsert.run`` makes in the no-pin / no-deprecate path.

    ``upsert_status`` models the lifecycle state the upsert lands in:
    ``'deprecated'`` reproduces the tombstone a prior teardown left on the
    shared anchor (the exact condition that made procedural_search return 0
    hits in the agent_integration suite)."""

    def __init__(self, upsert_status: str) -> None:
        self._upsert_status = upsert_status
        self.update_calls: list[tuple[UUID, str]] = []

    async def procedural_upsert(self, create):  # noqa: ANN001
        return _dto(status=self._upsert_status)

    async def procedural_update(self, entry_id, payload, *, vault_id=None):  # noqa: ANN001
        self.update_calls.append((entry_id, payload.status))
        return _dto(status=payload.status)


def _seed_params(**overrides: str) -> dict[str, str]:
    params = {
        'kind_kind': 'procedure',
        'kind_scope': 'global',
        'kind_verb': 'deploy',
        'kind_context': 'payments',
        'kind_title': 'Deploy the payments service',
        'kind_trigger': 'deploying the payments service to any environment',
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_procedural_upsert_reasserts_published_after_tombstone():
    """Regression: an upsert that lands on a deprecated tombstone is PATCHed
    back to the requested published state via exactly one re-assert call."""
    from memex_eval.suites.procedural_plane._setup_actions import _ProceduralUpsert

    handler = _ProceduralUpsert()
    api = _FakeProceduralApi(upsert_status='deprecated')  # prior teardown's tombstone
    result = await handler.run(
        api=api,  # type: ignore[arg-type]
        vault_id=UUID('00000000-0000-0000-0000-000000000000'),
        params=_seed_params(),
    )
    assert api.update_calls == [(_SEED_ENTRY_ID, 'published')]
    assert result['identity_anchor'] == 'procedure/global/deploy/payments'


@pytest.mark.asyncio
async def test_procedural_upsert_no_patch_when_already_published():
    """A fresh anchor that upserts straight to published needs no PATCH —
    the re-assert is conditional, not an unconditional extra write."""
    from memex_eval.suites.procedural_plane._setup_actions import _ProceduralUpsert

    handler = _ProceduralUpsert()
    api = _FakeProceduralApi(upsert_status='published')
    await handler.run(
        api=api,  # type: ignore[arg-type]
        vault_id=UUID('00000000-0000-0000-0000-000000000000'),
        params=_seed_params(),
    )
    assert api.update_calls == []


@pytest.mark.asyncio
async def test_procedural_upsert_reasserts_requested_draft_status():
    """The re-assert honors the REQUESTED state, not a hardcoded 'published':
    a draft seed landing on a tombstone is PATCHed back to draft."""
    from memex_eval.suites.procedural_plane._setup_actions import _ProceduralUpsert

    handler = _ProceduralUpsert()
    api = _FakeProceduralApi(upsert_status='deprecated')
    await handler.run(
        api=api,  # type: ignore[arg-type]
        vault_id=UUID('00000000-0000-0000-0000-000000000000'),
        params=_seed_params(kind_status='draft'),
    )
    assert api.update_calls == [(_SEED_ENTRY_ID, 'draft')]


# ---------------------------------------------------------------------------
# expects_backend_error opt-in — the runner must NOT escalate an expected
# 4xx (409/404) to status='error' before ProceduralEntryRoundtrip scores it.
# ---------------------------------------------------------------------------


def _roundtrip(expect_status: str):
    from memex_eval.suites.procedural_plane._outcomes import ProceduralEntryRoundtrip

    return ProceduralEntryRoundtrip(
        type='procedural_entry_roundtrip',
        operation='create',
        kind='procedure',
        scope='global',
        verb='rotate',
        context='api_key',
        expect_status=expect_status,  # type: ignore[arg-type]
    )


def test_roundtrip_expects_backend_error_only_for_4xx_contracts():
    """conflict / not_found interpret answer.error; success / any do not."""
    assert _roundtrip('conflict').expects_backend_error(None) is True  # type: ignore[arg-type]
    assert _roundtrip('not_found').expects_backend_error(None) is True  # type: ignore[arg-type]
    assert _roundtrip('success').expects_backend_error(None) is False  # type: ignore[arg-type]
    assert _roundtrip('any').expects_backend_error(None) is False  # type: ignore[arg-type]


def test_base_outcome_does_not_expect_backend_error_by_default():
    """Every other outcome keeps the fail-loud escalation (default False)."""
    from memex_eval.suite.base import KeywordsPresent

    kw = KeywordsPresent(type='keywords_present', keywords=['x'])
    assert kw.expects_backend_error(None) is False  # type: ignore[arg-type]
