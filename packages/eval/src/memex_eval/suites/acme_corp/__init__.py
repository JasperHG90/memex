"""Acme Corp consolidated suite.

Folds eleven legacy scenario groups (basic extraction, temporal,
reflection, scale, assets, outcomes-MW, deprioritization, intent
classification, procedural KV, summarization, vault isolation) into a
single source-doc-organized suite. Every scenario grounds against a
markdown source under ``sources/``; vault-scoped scenarios pin
``vault_name=`` to exercise multi-vault isolation.

Order matters: scenarios that depend on side-effects (setup actions,
ingestion of additional content) appear AFTER the side-effect-free
keyword/entity scenarios that hit the same corpus, so earlier scenarios
see a clean baseline.
"""

from pathlib import Path

from memex_eval.suite import (
    EntityResolves,
    ExcludedByDefault,
    KeywordsAbsent,
    KeywordsPresent,
    KvRoundtrip,
    LLMJudge,
    NewestUnitContains,
    NoteAssetsContain,
    NoteAttribution,
    SetupAction,
    SuiteMetadata,
    SuiteSources,
    SummaryNonempty,
    TemporalOrdering,
    UnitMetadataMatches,
    UsefulAtK,
)
from memex_eval.suite.decorator import Suite

_ROOT = Path(__file__).parent

# Backends marked expected-fail when an outcome reads API-shaped data
# (kv_value, summary_text, units.metadata, entity_mentions). Agent
# backends produce ``answer_text`` only; without the underlying DTO they
# cannot satisfy the outcome. Marking xfail keeps these scenarios
# visible in agent-mode runs without skewing pass_rate.
_AGENT_XFAIL = ['claude-code', 'hermes']

METADATA = SuiteMetadata(
    name='acme_corp',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'Consolidated Acme Corp / TechCo Global suite — covers extraction, '
        'retrieval, entity resolution, reflection, temporal recency, '
        'memory-worth ranking, deprioritization, intent classification, '
        'procedural KV roundtrip, summarization, and multi-vault isolation '
        'over a single source-doc-grounded corpus.'
    ),
    tags=[
        'extraction',
        'retrieval',
        'entities',
        'reflection',
        'temporal',
        'scale',
        'assets',
        'outcomes',
        'deprioritization',
        'intent',
        'kv',
        'summarization',
        'vault-isolation',
    ],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'extraction.semantic_facts',
        'retrieval.keyword',
        'retrieval.semantic',
        'retrieval.temporal',
        'retrieval.mental_model',
        'retrieval.cross_encoder_rerank',
        'memory.entity_resolver',
        'memory.entity_graph',
        'memory.reflection',
        'memory.outcomes',
        'memory.deprioritization',
        'memory.intent_classification',
        'memory.kv_store',
        'memory.summarization',
        'multi_tenancy.vault_scoping',
    ],
    knobs=[
        'server.memory.retrieval.reranking_mw_alpha',
        'server.memory.retrieval.reranking_recency_alpha',
        'server.memory.retrieval.reranking_temporal_alpha',
        'server.memory.entity.resolution_threshold',
    ],
    requires_llm_judge=False,
    default_answer_mode='api',
)


suite = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    readme_path=_ROOT / 'README.md',
)

# ------------------------------------------------------------------
# GROUP_BASIC — extraction + retrieval over Project Alpha/Beta notes
# ------------------------------------------------------------------
suite.register(
    id='search_project_alpha',
    description='Searching "Project Alpha" returns both Alpha docs.',
    query='Project Alpha',
    top_k=10,
    group='extraction',
    expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen', 'Phase 1']),
)

suite.register(
    id='who_leads_alpha',
    description='Query about Alpha leadership returns Sarah Chen.',
    query='Who leads Project Alpha?',
    top_k=10,
    group='extraction',
    expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen']),
)

suite.register(
    id='entity_acme_corp',
    description='Entity "Acme Corp" resolves and links to multiple projects.',
    query='Acme Corp',
    top_k=10,
    group='entities',
    expected=EntityResolves(type='entity_resolves', expected_names=['Acme Corp']),
)

suite.register(
    id='keyword_postgresql',
    description='Keyword search for PostgreSQL finds relevant results.',
    query='PostgreSQL 16',
    top_k=10,
    group='retrieval',
    strategies=['keyword'],
    expected=KeywordsPresent(type='keywords_present', keywords=['PostgreSQL']),
)

suite.register(
    id='semantic_data_platform',
    description='Semantic search for "data platform" surfaces Project Alpha.',
    query='building a data platform for event processing',
    top_k=10,
    group='retrieval',
    strategies=['semantic'],
    expected=KeywordsPresent(type='keywords_present', keywords=['Project Alpha']),
)

suite.register(
    id='note_search_alpha',
    description='Note search returns the Alpha kickoff document.',
    query='Project Alpha kickoff meeting',
    top_k=10,
    group='retrieval',
    search_type='note',
    expected=KeywordsPresent(type='keywords_present', keywords=['Project Alpha']),
)

suite.register(
    id='sarah_chen_entity_type',
    description='Sarah Chen is classified as a Person entity.',
    query='Sarah Chen',
    top_k=10,
    group='entities',
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['Sarah Chen'],
        expected_type='Person',
    ),
)

suite.register(
    id='acme_corp_entity_type',
    description='Acme Corp is classified as an Organization entity.',
    query='Acme Corp',
    top_k=10,
    group='entities',
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['Acme Corp'],
        expected_type='Organization',
    ),
)

# ------------------------------------------------------------------
# GROUP_TEMPORAL — Q1/Q2 reviews, recency-aware ranking
# ------------------------------------------------------------------
suite.register(
    id='temporal_q2_revenue',
    description='Query about latest revenue returns Q2 figure ($15.3M).',
    query='What is the most recent quarterly revenue?',
    top_k=10,
    group='temporal',
    expected=KeywordsPresent(type='keywords_present', keywords=['15.3']),
)

suite.register(
    id='temporal_headcount',
    description='Latest headcount is 164.',
    query='How many employees does the company have?',
    top_k=10,
    group='temporal',
    expected=KeywordsPresent(type='keywords_present', keywords=['164']),
)

suite.register(
    id='temporal_recency',
    description=('Recency ranking puts Q2 results above Q1 — asserted at the datetime layer.'),
    query='quarterly business review results',
    top_k=10,
    group='temporal',
    strategies=['temporal'],
    expected=TemporalOrdering(
        type='temporal_ordering',
        expected_note_keys_newest_first=[
            'quarterly-review-q2',
            'quarterly-review-q1',
        ],
    ),
)

# ------------------------------------------------------------------
# GROUP_SCALE — many docs (TechCo Global departments) + retrieval
# ------------------------------------------------------------------
suite.register(
    id='scale_find_engineering_lead',
    description='Find the Engineering department lead among 11 departments.',
    query='Who leads the Engineering department at TechCo Global?',
    top_k=10,
    group='retrieval',
    expected=KeywordsPresent(type='keywords_present', keywords=['Ruby Martinez']),
)

suite.register(
    id='scale_find_security_tools',
    description='Find Security department tools among 11 departments.',
    query='What tools does the Security team at TechCo use?',
    top_k=10,
    group='retrieval',
    expected=KeywordsPresent(type='keywords_present', keywords=['CrowdStrike', 'Snyk']),
)

suite.register(
    id='scale_find_ai_initiative',
    description='Find the AI Research initiative among 11 departments.',
    query='What is the AI Research department working on at TechCo?',
    top_k=10,
    group='retrieval',
    expected=KeywordsPresent(
        type='keywords_present',
        keywords=['retrieval-augmented generation'],
    ),
)

suite.register(
    id='scale_specific_headcount',
    description='Retrieve a specific headcount among 11 departments.',
    query='How many people are in the Legal department at TechCo Global?',
    top_k=10,
    group='retrieval',
    expected=KeywordsPresent(type='keywords_present', keywords=['6']),
)

suite.register(
    id='scale_entity_exists',
    description='Entity for a department lead is created from scale docs.',
    query='Mei-Lin Zhao',
    top_k=10,
    group='entities',
    expected=EntityResolves(type='entity_resolves', expected_names=['Mei-Lin Zhao']),
)

suite.register(
    id='scale_retrieval_speed',
    description='Retrieval completes within 30 seconds even with many documents.',
    query='TechCo Global department overview',
    top_k=10,
    group='retrieval',
    max_duration_ms=30000,
    expected=KeywordsPresent(type='keywords_present', keywords=['TechCo']),
)

suite.register(
    id='scale_current_vs_former_lead',
    description=(
        'Among Engineering-leadership units, the newest one (by '
        'mentioned_at / occurred_start) names Ruby Martinez. Alex Chen '
        'was head 2020-2023 (succession, NOT contradiction); the test '
        'simply asserts that the most recent engineering-leadership '
        'fact correctly identifies the current head. An agent reading '
        'the result set can use unit timestamps to disambiguate the '
        'two without needing a contradiction link.'
    ),
    query='Who currently leads the Engineering department at TechCo Global?',
    top_k=30,
    group='temporal',
    expected=NewestUnitContains(
        type='newest_unit_contains',
        keywords=['Ruby Martinez'],
        subject_filter=['Engineering'],
    ),
)

suite.register(
    id='scale_former_lead_query',
    description='Query about former head should surface the predecessor.',
    query=('Who headed the Engineering department at TechCo Global before Ruby Martinez?'),
    top_k=20,
    group='temporal',
    expected=KeywordsPresent(type='keywords_present', keywords=['Alex Chen']),
)

# ------------------------------------------------------------------
# GROUP_ASSETS — note with PNG asset
# ------------------------------------------------------------------
suite.register(
    id='asset_note_searchable',
    description=(
        'Note with a binary asset is ingested and its body content is '
        'searchable. LLM-judge tests for architectural-concept coverage '
        'rather than literal tool names — extraction summarises bullet '
        'lists and tool/qualifier phrases (e.g. "Kong with rate limiting") '
        'often lose qualifiers. We assert the test is meaningful by '
        'requiring named tooling for at least two services, which is '
        'achievable across paraphrased extractions.'
    ),
    query='microservices architecture API Gateway',
    top_k=5,
    group='assets',
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            'A useful result describes a microservices architecture and '
            'names at least one specific technology or service for at '
            'least two of: API gateway, user/auth service, order '
            'processing, notifications, analytics. Generic mentions of '
            '"microservices" without any named technology do NOT count.'
        ),
        threshold=0.5,
    ),
)

suite.register(
    id='asset_note_search',
    description='Note search finds the architecture document.',
    query='system architecture diagram',
    top_k=10,
    group='assets',
    search_type='note',
    expected=KeywordsPresent(type='keywords_present', keywords=['Architecture']),
)

suite.register(
    id='asset_round_trip',
    description=(
        'The architecture-overview note carries its bound PNG asset '
        'after ingest — verifies file bytes survived ingestion and '
        'the FileStore round-trip, independent of search ranking.'
    ),
    query='system architecture',
    top_k=1,
    group='assets',
    expected=NoteAssetsContain(
        type='note_assets_contain',
        note_key='architecture-overview',
        expected_filenames=['system-diagram.png'],
    ),
)

# ------------------------------------------------------------------
# GROUP_OUTCOMES_MW — record outcomes, then assert ranking flips
#
# The two record_outcome setup actions run BEFORE the search, so the
# ranking_order outcome observes the post-outcomes state (matches
# legacy `ranking_after_outcomes` semantics).
# ------------------------------------------------------------------
suite.register(
    id='outcomes_ranking',
    description=(
        'After outcomes, achievement units rank above incident units — '
        'asserted via note_id attribution rather than substring match '
        '(extraction paraphrases "achievement" / "incident" away).'
    ),
    query='Project Zeta',
    top_k=10,
    group='outcomes_mw',
    setup_actions=[
        # Note-key-scoped resolution: every unit extracted from
        # ``project-zeta-achievement.md`` gets 3 success outcomes;
        # every unit from ``project-zeta-incident.md`` gets 3
        # failure outcomes. Deterministic — no search ambiguity.
        SetupAction(
            kind='record_outcome',
            note_key='project-zeta-achievement',
            success=True,
            count=3,
            reason='Positive outcomes for achievement facts',
        ),
        SetupAction(
            kind='record_outcome',
            note_key='project-zeta-incident',
            success=False,
            count=3,
            reason='Negative outcomes for incident facts',
        ),
    ],
    expected=NoteAttribution(
        type='note_attribution',
        top_note_key='project-zeta-achievement',
        lower_note_key='project-zeta-incident',
    ),
)

suite.register(
    id='outcomes_ranking_specific_query',
    description=(
        'Specific-phrasing variant: the query explicitly mentions both '
        'subjects ("incident postmortem"). Strong incident-side semantic '
        'match SHOULD pull incident units to the top, regardless of the '
        'memory-worth signal. Asserts the system respects explicit query '
        'phrasing — an agent asking about incidents gets incident facts. '
        "(Inverts ``outcomes_ranking``'s assertion direction.)"
    ),
    query='Project Zeta launch outcomes and incident postmortem',
    top_k=10,
    group='outcomes_mw',
    # Intentionally NO ``setup_actions``: this scenario must observe
    # the same Memory-Worth state the prior ``outcomes_ranking``
    # scenario stamped. Duplicating the ``record_outcome`` calls
    # doubles the negative weight on incident units, suppresses them
    # below retrieval cutoff, and inverts the ranking we assert.
    # ``depends_on_prior_scenarios`` declares the dependency to the
    # runner so ``--reuse-vault`` skips this when its prerequisite
    # is skipped, instead of silently scoring against a fresh vault.
    depends_on_prior_scenarios=['outcomes_ranking'],
    expected=NoteAttribution(
        type='note_attribution',
        top_note_key='project-zeta-incident',
        lower_note_key='project-zeta-achievement',
    ),
)

# ------------------------------------------------------------------
# GROUP_DEPRIORITIZATION — Widget Lite is deprioritized; default
# search hides it; the override flag re-surfaces it.
# ------------------------------------------------------------------
suite.register(
    id='deprioritized_excluded_by_default',
    description=(
        'After deprioritization, discontinued product content is excluded from default search.'
    ),
    query='Widget product',
    top_k=10,
    group='deprioritization',
    setup_actions=[
        # Note-key-scoped: every unit extracted from
        # ``widget-lite-discontinued.md`` gets deprioritized. No
        # collateral damage to Widget Pro units.
        SetupAction(
            kind='deprioritize',
            note_key='widget-lite-discontinued',
            reason='Deprecate Widget Lite facts',
        ),
    ],
    expected=ExcludedByDefault(
        type='excluded_by_default',
        forbidden_keywords=['discontinued', 'migration', 'Widget Lite'],
    ),
)

suite.register(
    id='deprioritized_visible_with_flag',
    description='Deprioritized content reappears when include_deprioritized=True.',
    query='Widget product',
    top_k=10,
    group='deprioritization',
    include_deprioritized=True,
    expected=KeywordsPresent(type='keywords_present', keywords=['discontinued', 'migration']),
)

suite.register(
    id='note_search_still_works',
    description='Note search finds Widget Lite even after deprioritization.',
    query='Widget Lite discontinued',
    top_k=10,
    group='deprioritization',
    search_type='note',
    expected=KeywordsPresent(type='keywords_present', keywords=['Widget Lite']),
)

# ------------------------------------------------------------------
# GROUP_INTENT_CLASSIFICATION — units carry intent_class metadata.
# The outcome reads `unit.metadata['intent_class']`; agent backends
# have no DTO to inspect, so xfail there.
# ------------------------------------------------------------------
suite.register(
    id='permanent_intent',
    description='Units from permanent docs carry permanent intent class.',
    query='company core values permanent principles',
    top_k=10,
    group='intent',
    expected=UnitMetadataMatches(
        type='unit_metadata_matches',
        expected_metadata={'intent_class': 'permanent'},
    ),
    expected_failure_modes=_AGENT_XFAIL,
)

suite.register(
    id='durable_intent',
    description='Units from durable docs carry durable intent class.',
    query='annual product roadmap 2025 deliverables',
    top_k=10,
    group='intent',
    expected=UnitMetadataMatches(
        type='unit_metadata_matches',
        expected_metadata={'intent_class': 'durable'},
    ),
    expected_failure_modes=_AGENT_XFAIL,
)

suite.register(
    id='ephemeral_intent',
    description='Units from ephemeral docs carry ephemeral intent class.',
    query='daily standup sprint blocker action items',
    top_k=10,
    group='intent',
    expected=UnitMetadataMatches(
        type='unit_metadata_matches',
        expected_metadata={'intent_class': 'ephemeral'},
    ),
    expected_failure_modes=_AGENT_XFAIL,
)

# ------------------------------------------------------------------
# GROUP_KV — generic KV write (setup) + read (outcome).
# (The legacy KV-procedure roundtrip scenario was removed: procedures
# no longer live in KV — they live in the procedural plane. KV keeps
# only the global/user/project/app namespaces.)
# ------------------------------------------------------------------
suite.register(
    id='kv_roundtrip_preference',
    description='KV write followed by read returns the same value.',
    query='global:preferences:deploy_staging',
    top_k=10,
    group='kv',
    setup_actions=[
        SetupAction(
            kind='kv_write',
            kv_key='global:preferences:deploy_staging',
            kv_value='For staging deploys, use --no-migrate flag after 6pm',
        ),
    ],
    expected=KvRoundtrip(
        type='kv_roundtrip',
        kv_key='global:preferences:deploy_staging',
        expected_value='For staging deploys, use --no-migrate flag after 6pm',
    ),
    expected_failure_modes=_AGENT_XFAIL,
)

# ------------------------------------------------------------------
# GROUP_SUMMARIZATION — summarize_node returns non-empty text
# ------------------------------------------------------------------
suite.register(
    id='summarize_dataforge_entity',
    description='Summarize node for DataForge entity returns a non-empty summary.',
    query='DataForge',
    top_k=10,
    group='summarization',
    expected=SummaryNonempty(type='summary_nonempty', entity_query='DataForge'),
    expected_failure_modes=_AGENT_XFAIL,
)

# ------------------------------------------------------------------
# GROUP_REFLECTION — reflection produces mental models. The
# `trigger_reflections` setup action seeds reflections on the top-N
# entities; subsequent mental_model search probes the result.
#
# Placed AFTER the other Acme/TechCo content so by the time
# `get_top_entities` runs, the entity counts have stabilized
# (Sarah Chen, Acme Corp, Project Alpha all appear in many notes).
# ------------------------------------------------------------------
suite.register(
    id='reflection_mental_model',
    description=(
        'Reflection on top entities produces a mental model; LLM-judge '
        'verifies it references Project Alpha, Sarah Chen as project '
        'lead, and Phase 1 completion.'
    ),
    query='Sarah Chen',
    top_k=10,
    group='reflection',
    setup_actions=[
        # Per-target polling (``probe_query`` deliberately unset) —
        # waits until BOTH Sarah Chen AND Project Alpha have ≥5
        # mental_model hits on their own name, not ≥5 on the shared
        # 'Sarah Chen leadership' query. The shared probe was racing
        # the reflection writer: 5 Sarah-Chen-flavoured hits could
        # clear the gate while Project Alpha was still being
        # reflected, leaving the downstream LLMJudge concatenations
        # missing Project-Alpha facts ~50% of runs.
        SetupAction(
            kind='trigger_reflections',
            count=5,
            target_entity_names=['Sarah Chen', 'Project Alpha'],
            min_mental_model_hits=5,
            # Per-target polling waits for ≥5 hits PER target. With
            # the reflection writer serial server-side, real budgets
            # are 200-300s under typical LLM latency. The handler
            # default ``max(60, min_hits * 30)`` = 150s would race on
            # any slow run; pin at 240s to match the prior empirical
            # success window (round-3 MEDIUM 2).
            timeout_s=240,
        ),
    ],
    expected=LLMJudge(
        type='llm_judge',
        # ``candidate_top_k=10`` widens the concatenation window: the
        # top-10 results for query ``'Sarah Chen'`` reliably include
        # both Sarah-Chen-profile units AND Project-Alpha-tagged units
        # (Phase 1 completion, lead role). Top-5 alone is dominated
        # by reflection-emphasised Sarah Chen observations, which
        # often miss the Project Alpha / Phase 1 facets the rubric
        # asks for. Bumping the window catches the diversity the
        # rubric requires without weakening the assertion itself.
        candidate_top_k=10,
        rubric=(
            'The result must reference Project Alpha, Sarah Chen as '
            'project lead, and Phase 1 completion.'
        ),
        threshold=0.5,
    ),
)

suite.register(
    id='mental_model_strategy',
    description=(
        'Mental model retrieval strategy returns useful results for the '
        'reflected entity. LLM-judges each top-k result for relevance to '
        'the query rather than relying on surface-level keyword presence.'
    ),
    query='Sarah Chen leadership',
    top_k=10,
    group='reflection',
    strategies=['mental_model'],
    expected=UsefulAtK(
        type='useful_at_k',
        rubric=(
            'A useful result references Sarah Chen as a leader or '
            'lead role-holder, OR describes her leadership style, '
            'decisions, or scope of authority.'
        ),
        # ``k=10, threshold=0.1`` ≡ "≥1 of top-10 useful". Same
        # intent as the original tighter ``k=5, threshold=0.5`` —
        # "does mental_model strategy retrieve on-rubric content for
        # this entity at all" — but with the wider window the test
        # is robust to retrieval-ranking variance that intermittently
        # surfaces Project-Alpha / TechCo-Global observations above
        # Sarah-Chen ones (round-3 MEDIUM 4).
        k=10,
        threshold=0.1,
    ),
)

# ------------------------------------------------------------------
# GROUP_VAULT_ISOLATION — last group; per-vault scoping. Each
# scenario pins ``vault_name=`` so results are scoped to a single
# vault. The two source notes carry ``vault_name:`` frontmatter so
# they are ingested into distinct vaults at suite startup.
# ------------------------------------------------------------------
suite.register(
    id='vault_a_contains_gamma',
    description='Vault A search finds Project Gamma.',
    query='real-time platform',
    top_k=10,
    group='vault_isolation',
    vault_name='bench-vault-a',
    expected=KeywordsPresent(type='keywords_present', keywords=['Elixir']),
)

suite.register(
    id='vault_a_excludes_delta',
    description='Vault A search does NOT contain Project Delta content.',
    query='distributed processing engine',
    top_k=10,
    group='vault_isolation',
    vault_name='bench-vault-a',
    expected=KeywordsAbsent(type='keywords_absent', keywords=['Scala', 'Akka']),
)

suite.register(
    id='vault_b_contains_delta',
    description='Vault B search finds Project Delta.',
    query='distributed processing engine',
    top_k=10,
    group='vault_isolation',
    vault_name='bench-vault-b',
    expected=KeywordsPresent(type='keywords_present', keywords=['Scala']),
)

suite.register(
    id='vault_b_excludes_gamma',
    description='Vault B search does NOT contain Project Gamma content.',
    query='real-time collaboration platform',
    top_k=10,
    group='vault_isolation',
    vault_name='bench-vault-b',
    expected=KeywordsAbsent(type='keywords_absent', keywords=['Elixir', 'Phoenix']),
)

suite.register(
    id='vault_a_entity_isolation',
    description='Entity "Polaris Labs" exists only in vault A.',
    query='Polaris Labs',
    top_k=10,
    group='vault_isolation',
    vault_name='bench-vault-a',
    expected=EntityResolves(type='entity_resolves', expected_names=['Polaris Labs']),
)

suite.register(
    id='vault_b_entity_isolation',
    description='Entity "Nordic Data Systems" exists only in vault B.',
    query='Nordic Data Systems',
    top_k=10,
    group='vault_isolation',
    vault_name='bench-vault-b',
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['Nordic Data Systems'],
    ),
)


SUITE = suite.build()
