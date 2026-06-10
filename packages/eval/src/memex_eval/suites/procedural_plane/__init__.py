"""V7 Procedural Plane eval suite.

10 scenarios gate the V7 procedural-plane (case / procedure / strategy)
contract. The plane has 3 kinds, 4 scopes, an identity anchor
``(kind, scope, verb, context)`` UNIQUE NULLS NOT DISTINCT, and 8
HTTP routes (create/upsert/get/get_by_identity/update/deprecate/
search/briefing_cards). This suite covers the routing, identity,
lifecycle, and pin-chain contracts that an agent depends on for
write-routing ("this is how to do X" → procedural plane, NOT
note/KV).

Order matters — scenarios that depend on seeded state (search
hits, briefing cards, deprecate-drops-from-search) appear AFTER
their setup actions seed the relevant entries.

IMPORTANT (import order): the imports of ``_outcomes`` and
``_setup_actions`` MUST appear before any ``suite.register(...)``
call. The ``@register_outcome`` and ``@register_setup_action``
decorators fire at import time; scenarios reference
``ProceduralEntryRoundtrip(...)`` /
``ProceduralSearchResults(...)`` /
``SetupAction(kind='procedural_upsert', ...)`` by name and those
registrations must already exist.
"""

from __future__ import annotations

import logging
from pathlib import Path

from memex_eval.suite import SetupAction, SuiteMetadata, SuiteSources
from memex_eval.suite.decorator import Suite

logger = logging.getLogger('memex_eval.suites.procedural_plane')

# Side-effect imports — DO NOT MOVE. These decorator registrations
# populate the framework's outcome and setup-action registries before
# any scenario reference resolves.
from . import _outcomes  # noqa: F401 — decorator side effect
from . import _setup_actions  # noqa: F401 — decorator side effect
from ._outcomes import ProceduralEntryRoundtrip, ProceduralSearchResults

_ROOT = Path(__file__).parent


METADATA = SuiteMetadata(
    name='procedural_plane',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'V7 procedural-plane contract — 10 scenarios pinning the '
        '(kind, scope, verb, context) identity anchor, the 3 kinds '
        '(case / procedure / strategy), the write/read lifecycle '
        '(create/upsert/get_by_identity/deprecate), the hybrid '
        'BM25+vector+RRF search, and the pin-chain briefing-cards '
        'union. Every scenario exercises the public surface the '
        'agent sees — no internals, no shortcuts.'
    ),
    tags=[
        'procedural',
        'v7',
        'identity-anchor',
        'pin-chain',
        'lifecycle',
        'search',
    ],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'procedural.identity_anchor',
        'procedural.create',
        'procedural.upsert',
        'procedural.get_by_identity',
        'procedural.deprecate',
        'procedural.search',
        'procedural.briefing_cards',
    ],
    knobs=[
        'server.memory.procedural.enabled',
        'server.memory.procedural.search_default_bm25_weight',
        'server.memory.procedural.search_default_vector_weight',
        'server.memory.procedural.identity_conflict_mode',
        'server.memory.procedural.briefing_default_limit_per_context',
    ],
    requires_llm_judge=False,
    default_answer_mode='api',
)

suite = Suite(
    metadata=METADATA,
    sources=SuiteSources(notes=[]),  # no source corpus — V7 is its own write surface
    readme_path=_ROOT / 'README.md',
)


# ---------------------------------------------------------------------------
# 1. Identity-anchor uniqueness: create with the same (kind, scope, verb, context)
#    as an existing entry MUST 409. The procedural plane's UNIQUE NULLS NOT
#    DISTINCT constraint is the load-bearing piece — without it, two agents
#    could write contradictory procedures under the same anchor and search
#    would return ambiguous hits.
# ---------------------------------------------------------------------------

suite.register(
    id='identity_anchor_collision_returns_409',
    description=(
        'A second procedural_create on the same (kind, scope, verb, context) '
        'anchor returns 409 — the identity-anchor UNIQUE NULLS NOT DISTINCT '
        'constraint is the contract that prevents two agents from writing '
        'contradictory procedures under the same name.'
    ),
    query='',
    top_k=10,
    expected=ProceduralEntryRoundtrip(
        type='procedural_entry_roundtrip',
        operation='create',
        kind='procedure',
        scope='global',
        verb='rotate',
        context='api_key',
        expect_status='conflict',
        title='procedural-suite-rotate-key',
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='global',
            kind_verb='rotate',
            kind_context='api_key',
            kind_title='procedural-suite-rotate-key',
        ),
    ],
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 2. Upsert idempotency: re-submitting the same anchor updates the existing
#    entry rather than 409ing. The V7 contract is the GET-then-INSERT-or-UPDATE
#    pattern; if the GET path is broken (e.g. wrong identity-anchor parsing),
#    upsert silently creates a new row and breaks search.
# ---------------------------------------------------------------------------

suite.register(
    id='upsert_on_existing_anchor_updates_in_place',
    description=(
        'A second procedural_upsert on the same (kind, scope, verb, context) '
        'anchor is a no-op conflict — the entry is the SAME row, not a new one. '
        'V7 identity-anchor resolution correctly maps the second call to the '
        'existing row.'
    ),
    query='',
    top_k=10,
    expected=ProceduralEntryRoundtrip(
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
        title='procedural-suite-deploy-staging',
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='global',
            kind_verb='deploy',
            kind_context='staging',
            kind_title='procedural-suite-deploy-staging',
        ),
    ],
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 3. get_by_identity returns the entry when bound. The route is the cheap
#    "did we already learn this?" probe every agent runs before creating.
# ---------------------------------------------------------------------------

suite.register(
    id='get_by_identity_returns_seeded_entry',
    description=(
        'procedural_get_by_identity with the seeded anchor returns the entry '
        'with the correct (kind, scope, verb) shape. The probe is the cheap '
        '"did we already learn this?" check that agents use before '
        'procedural_create.'
    ),
    query='',
    top_k=10,
    expected=ProceduralEntryRoundtrip(
        type='procedural_entry_roundtrip',
        operation='get_by_identity',
        kind='procedure',
        scope='user',
        verb='commit',
        context='prefix',
        expect_status='success',
        expect_kind='procedure',
        expect_scope='user',
        expect_verb='commit',
        title='procedural-suite-commit-prefix',
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='user',
            kind_verb='commit',
            kind_context='prefix',
            kind_title='procedural-suite-commit-prefix',
        ),
    ],
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 4. get_by_identity returns 404 when unbound. The cheap
#    "have we learned this?" probe is the gate to the create path.
# ---------------------------------------------------------------------------

suite.register(
    id='get_by_identity_returns_404_when_unbound',
    description=(
        'procedural_get_by_identity with an unbound anchor returns 404 / '
        'not-found. The cheap probe is the gate to the create path — '
        'misclassifying "unbound" as "bound" would skip the create step '
        'and silently drop the new procedure.'
    ),
    query='',
    top_k=10,
    expected=ProceduralEntryRoundtrip(
        type='procedural_entry_roundtrip',
        operation='get_by_identity',
        kind='strategy',
        scope='global',
        verb='unbound-anchor-probe',
        context='procedural-suite-unbound',
        expect_status='not_found',
        title='procedural-suite-never-seeded',
    ),
    # No setup action — the anchor is intentionally unbound.
)


# ---------------------------------------------------------------------------
# 5. Hybrid search returns the seeded entry. The BM25 + vector + RRF
#    composition is the V7 retrieval surface; a regression that drops
#    either stream would surface here.
# ---------------------------------------------------------------------------

suite.register(
    id='search_returns_seeded_procedure',
    description=(
        'procedural_search with a query that matches the seeded entry '
        'returns at least one hit. The hybrid BM25 + vector + RRF '
        'composition is the V7 retrieval surface; a regression that '
        'drops either stream would silently surface zero hits.'
    ),
    query='rollback database migration',
    top_k=10,
    expected=ProceduralSearchResults(
        type='procedural_search_results',
        operation='search',
        query='rollback database migration',
        min_hits=1,
        expect_kind='procedure',
        expect_scope='project:v7-eval',
        expect_verb='rollback',
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='project:v7-eval',
            kind_verb='rollback',
            kind_context='db_migration',
            kind_title='procedural-suite-rollback-migration',
            kind_summary=(
                'Roll back the database migration by running alembic downgrade '
                '-1 from the v7-eval project root, then verify the schema with '
                'memex database verify.'
            ),
        ),
    ],
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 6. Briefing-cards pin-chain union. The pin chain ``global → project → app``
#    is the precedence rule for which entry surfaces for a given context.
#    A regression that drops the union (e.g. only the most-specific pin
#    wins) would lose the global rule that applies to every project.
# ---------------------------------------------------------------------------

suite.register(
    id='briefing_cards_pin_chain_union',
    description=(
        'procedural_briefing_cards with [global, project:v7-eval, app:eval] '
        'returns the pin-chain union — both the global-scope rule AND the '
        'project:v7-eval-specific rule. The chain is the precedence rule '
        'that drives which entry surfaces for a given context; a regression '
        'that drops the union (e.g. only most-specific wins) would lose the '
        'global rule that applies to every project.'
    ),
    query='',
    top_k=10,
    expected=ProceduralSearchResults(
        type='procedural_search_results',
        operation='briefing_cards',
        context_keys=['global', 'project:v7-eval', 'app:eval'],
        min_hits=2,
        expect_scope='global',
    ),
    setup_actions=[
        # The global-scope rule that every project inherits.
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='global',
            kind_verb='test',
            kind_context='before_commit',
            kind_title='procedural-suite-test-before-commit-global',
            kind_summary='Run the test suite before every commit.',
        ),
        # The project-specific rule that overrides the global one.
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='project:v7-eval',
            kind_verb='test',
            kind_context='before_commit',
            kind_title='procedural-suite-test-before-commit-project',
            kind_summary=(
                'Run the test suite AND the procedural-plane eval gate '
                'before every commit in v7-eval.'
            ),
        ),
    ],
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 7. Briefing-cards pin-position contract. The card list is sorted by
#    pin order — most-general pin (global) first. A regression that
#    sorts by something else (e.g. creation time) would scramble
#    the briefing's precedence narrative.
# ---------------------------------------------------------------------------

suite.register(
    id='briefing_cards_pin_position_order',
    description=(
        'The briefing-cards list is sorted by pin position: global first, '
        'then project, then app. A regression that sorts by creation '
        'time or entry ID would scramble the briefing precedence narrative '
        'and surface the project rule before the global rule.'
    ),
    query='',
    top_k=10,
    expected=ProceduralSearchResults(
        type='procedural_search_results',
        operation='briefing_cards',
        context_keys=['global', 'project:v7-eval', 'app:eval'],
        min_hits=2,
        expect_scope='global',
        expect_first_pin_pos=0,  # global is the first pin in the chain
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='global',
            kind_verb='lint',
            kind_context='pre_push',
            kind_title='procedural-suite-lint-pre-push-global',
            kind_summary='Run lint before every push.',
        ),
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='project:v7-eval',
            kind_verb='lint',
            kind_context='pre_push',
            kind_title='procedural-suite-lint-pre-push-project',
            kind_summary=('Run lint AND the V7 spec-fence test before every push in v7-eval.'),
        ),
    ],
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 8. Deprecate drops the entry from default search (status='published' filter)
#    but keeps the entry reachable via get. The deprecate-drops-from-search
#    contract is what makes the "superseded by" pattern work — old
#    procedures fade out of search hits but remain inspectable.
#
#    Note: scenario 8 uses ``deprecate_after=True`` on the upsert setup
#    action so the entry lands in ``status='deprecated'`` BEFORE the
#    query fires. The runner does NOT substitute ``$context.key``
#    references between sequential setup actions, so combining
#    upsert+deprecate into a single action is the simplest way to
#    sequence them. The ``deprecate_after`` flag is suite-private.
# ---------------------------------------------------------------------------

suite.register(
    id='deprecate_drops_from_published_search',
    description=(
        'After procedural_deprecate, the default search (status="published") '
        'does NOT return the entry — the lifecycle exit is the soft-delete '
        'that drives the "superseded by" pattern. The entry remains '
        'reachable via procedural_get so audit can still surface it.'
    ),
    query='deprecate-test-handle',
    top_k=10,
    expected=ProceduralSearchResults(
        type='procedural_search_results',
        operation='search',
        query='deprecate-test-handle',
        min_hits=0,  # expect ZERO published hits — the entry is deprecated
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='project:v7-eval',
            kind_verb='deprecate-test-handle',
            kind_context='deprecate-test-context',
            kind_title='procedural-suite-deprecate-handle',
            kind_summary=(
                'Test handle for the deprecate-drops-from-search scenario — '
                'this entry MUST disappear from default search after '
                'deprecate_after=True fires immediately after the upsert.'
            ),
            # Flip status='published' → 'deprecated' immediately after
            # upsert, BEFORE the search call. The runner doesn't chain
            # action returns, so this single action handles both.
            deprecate_after=True,
        ),
    ],
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 9. Status filter contract: status='published' (default) hides drafts. The
#    default-published filter is what makes the deprecate-and-fade pattern
#    work, and it MUST also hide drafts so the draft-review flow doesn't
#    surface unvetted entries. The full status='all' override would
#    require extending the StatusLiteral in ``procedural_schemas``;
#    that's deferred to a follow-up — for now, we test the
#    "drafts-are-hidden-by-default" half of the contract, which is the
#    load-bearing piece for agentic surfaces.
# ---------------------------------------------------------------------------

suite.register(
    id='status_published_hides_drafts',
    description=(
        'A default search (status="published") does NOT return a draft '
        'entry — the default filter hides drafts so the draft-review '
        'flow does not surface unvetted entries to the agent.'
    ),
    query='draft-review-handle',
    top_k=10,
    expected=ProceduralSearchResults(
        type='procedural_search_results',
        operation='search',
        query='draft-review-handle',
        # Default status='published' — drafts are hidden.
        min_hits=0,  # expect ZERO published hits — the entry is a draft
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='project:v7-eval',
            kind_verb='draft-review-handle',
            kind_context='draft-review-context',
            kind_title='procedural-suite-draft-handle',
            kind_summary=(
                'Draft entry for the status-published-hides-drafts scenario. '
                'This entry has status="draft" and MUST NOT surface in '
                'the default status="published" search results.'
            ),
            kind_status='draft',
        ),
    ],
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 10. Case-kind identity: case entries have NO verb/context — they have a
#     trigger signal instead. A regression that conflates the case shape
#     with the procedure shape would 500 on case-create.
# ---------------------------------------------------------------------------

suite.register(
    id='case_kind_roundtrip',
    description=(
        'A case-kind entry (recurring failure shape) round-trips with '
        'kind="case", scope set, and no verb/context. The case shape '
        'is a trigger signal, NOT a verb — a regression that conflates '
        'the two shapes would 500 on case-create or break the routing '
        'rules in agent_surface.'
    ),
    query='',
    top_k=10,
    expected=ProceduralEntryRoundtrip(
        type='procedural_entry_roundtrip',
        operation='upsert',
        kind='case',
        scope='global',
        verb=None,  # case entries have no verb
        context=None,  # …no context
        # Case entries carry a trigger signal — the discriminator
        # between case shape and procedure/strategy shape.
        trigger='database connection pool exhausted',
        expect_status='success',
        expect_kind='case',
        expect_scope='global',
        title='procedural-suite-case-handle',
    ),
    # The setup action passes the trigger via params — case-kind entries
    # carry a `trigger` field instead of verb/context.
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='case',
            kind_scope='global',
            kind_title='procedural-suite-case-handle',
            kind_trigger='database connection pool exhausted',
            kind_summary=(
                'Recurring failure shape: when the database connection '
                'pool is exhausted, the agent should restart the service '
                'and re-run the migration with --pool-size=20.'
            ),
        ),
    ],
    mutating_scenario=True,
)


SUITE = suite.build()
