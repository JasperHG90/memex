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
    NoteAttribution,
    Scenario,
    SetupAction,
    Suite,
    SuiteMetadata,
    SuiteSources,
    SummaryNonempty,
    TemporalOrdering,
    UnitMetadataMatches,
    UsefulAtK,
)

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


SCENARIOS = [
    # ------------------------------------------------------------------
    # GROUP_BASIC — extraction + retrieval over Project Alpha/Beta notes
    # ------------------------------------------------------------------
    Scenario(
        id='search_project_alpha',
        description='Searching "Project Alpha" returns both Alpha docs.',
        query='Project Alpha',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen', 'Phase 1']),
    ),
    Scenario(
        id='who_leads_alpha',
        description='Query about Alpha leadership returns Sarah Chen.',
        query='Who leads Project Alpha?',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen']),
    ),
    Scenario(
        id='entity_acme_corp',
        description='Entity "Acme Corp" resolves and links to multiple projects.',
        query='Acme Corp',
        top_k=10,
        expected=EntityResolves(type='entity_resolves', expected_names=['Acme Corp']),
    ),
    Scenario(
        id='keyword_postgresql',
        description='Keyword search for PostgreSQL finds relevant results.',
        query='PostgreSQL 16',
        top_k=10,
        strategies=['keyword'],
        expected=KeywordsPresent(type='keywords_present', keywords=['PostgreSQL']),
    ),
    Scenario(
        id='semantic_data_platform',
        description='Semantic search for "data platform" surfaces Project Alpha.',
        query='building a data platform for event processing',
        top_k=10,
        strategies=['semantic'],
        expected=KeywordsPresent(type='keywords_present', keywords=['Project Alpha']),
    ),
    Scenario(
        id='note_search_alpha',
        description='Note search returns the Alpha kickoff document.',
        query='Project Alpha kickoff meeting',
        top_k=10,
        search_type='note',
        expected=KeywordsPresent(type='keywords_present', keywords=['Project Alpha']),
    ),
    Scenario(
        id='sarah_chen_entity_type',
        description='Sarah Chen is classified as a Person entity.',
        query='Sarah Chen',
        top_k=10,
        expected=EntityResolves(
            type='entity_resolves',
            expected_names=['Sarah Chen'],
            expected_type='Person',
        ),
    ),
    Scenario(
        id='acme_corp_entity_type',
        description='Acme Corp is classified as an Organization entity.',
        query='Acme Corp',
        top_k=10,
        expected=EntityResolves(
            type='entity_resolves',
            expected_names=['Acme Corp'],
            expected_type='Organization',
        ),
    ),
    # ------------------------------------------------------------------
    # GROUP_TEMPORAL — Q1/Q2 reviews, recency-aware ranking
    # ------------------------------------------------------------------
    Scenario(
        id='temporal_q2_revenue',
        description='Query about latest revenue returns Q2 figure ($15.3M).',
        query='What is the most recent quarterly revenue?',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['15.3']),
    ),
    Scenario(
        id='temporal_headcount',
        description='Latest headcount is 164.',
        query='How many employees does the company have?',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['164']),
    ),
    Scenario(
        id='temporal_recency',
        description=(
            'Recency ranking puts Q2 results above Q1 — asserted at the '
            'datetime layer. Avoids the brittle quarter-label substring '
            'check (extraction paraphrases "Q2 2025" → "April-June 2025").'
        ),
        query='quarterly business review results',
        top_k=10,
        strategies=['temporal'],
        expected=TemporalOrdering(
            type='temporal_ordering',
            expected_note_keys_newest_first=[
                'quarterly-review-q2',
                'quarterly-review-q1',
            ],
        ),
    ),
    # ------------------------------------------------------------------
    # GROUP_SCALE — many docs (TechCo Global departments) + retrieval
    # ------------------------------------------------------------------
    Scenario(
        id='scale_find_engineering_lead',
        description='Find the Engineering department lead among 11 departments.',
        query='Who leads the Engineering department at TechCo Global?',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['Ruby Martinez']),
    ),
    Scenario(
        id='scale_find_security_tools',
        description='Find Security department tools among 11 departments.',
        query='What tools does the Security team at TechCo use?',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['CrowdStrike', 'Snyk']),
    ),
    Scenario(
        id='scale_find_ai_initiative',
        description='Find the AI Research initiative among 11 departments.',
        query='What is the AI Research department working on at TechCo?',
        top_k=10,
        expected=KeywordsPresent(
            type='keywords_present',
            keywords=['retrieval-augmented generation'],
        ),
    ),
    Scenario(
        id='scale_specific_headcount',
        description='Retrieve a specific headcount among 11 departments.',
        query='How many people are in the Legal department at TechCo Global?',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['6']),
    ),
    Scenario(
        id='scale_entity_exists',
        description='Entity for a department lead is created from scale docs.',
        query='Mei-Lin Zhao',
        top_k=10,
        expected=EntityResolves(type='entity_resolves', expected_names=['Mei-Lin Zhao']),
    ),
    Scenario(
        id='scale_retrieval_speed',
        description='Retrieval completes within 30 seconds even with many documents.',
        query='TechCo Global department overview',
        top_k=10,
        max_duration_ms=30000,
        expected=KeywordsPresent(type='keywords_present', keywords=['TechCo']),
    ),
    Scenario(
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
        expected=NewestUnitContains(
            type='newest_unit_contains',
            keywords=['Ruby Martinez'],
            subject_filter=['Engineering'],
        ),
    ),
    Scenario(
        id='scale_former_lead_query',
        description='Query about former head should surface the predecessor.',
        query=('Who headed the Engineering department at TechCo Global before Ruby Martinez?'),
        top_k=20,
        expected=KeywordsPresent(type='keywords_present', keywords=['Alex Chen']),
    ),
    # ------------------------------------------------------------------
    # GROUP_ASSETS — note with PNG asset
    # ------------------------------------------------------------------
    Scenario(
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
    ),
    Scenario(
        id='asset_note_search',
        description='Note search finds the architecture document.',
        query='system architecture diagram',
        top_k=10,
        search_type='note',
        expected=KeywordsPresent(type='keywords_present', keywords=['Architecture']),
    ),
    # ------------------------------------------------------------------
    # GROUP_OUTCOMES_MW — record outcomes, then assert ranking flips
    #
    # The two record_outcome setup actions run BEFORE the search, so the
    # ranking_order outcome observes the post-outcomes state (matches
    # legacy `ranking_after_outcomes` semantics).
    # ------------------------------------------------------------------
    Scenario(
        id='outcomes_ranking',
        description=(
            'After outcomes, achievement units rank above incident units — '
            'asserted via note_id attribution rather than substring match '
            '(extraction paraphrases "achievement" / "incident" away).'
        ),
        query='Project Zeta',
        top_k=10,
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
    ),
    Scenario(
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
        # No setup_actions: relies on the outcomes already recorded by the
        # prior ``outcomes_ranking`` scenario in the same run. Order
        # matters — see suite-level docstring.
        expected=NoteAttribution(
            type='note_attribution',
            top_note_key='project-zeta-incident',
            lower_note_key='project-zeta-achievement',
        ),
    ),
    # ------------------------------------------------------------------
    # GROUP_DEPRIORITIZATION — Widget Lite is deprioritized; default
    # search hides it; the override flag re-surfaces it.
    # ------------------------------------------------------------------
    Scenario(
        id='deprioritized_excluded_by_default',
        description=(
            'After deprioritization, discontinued product content is excluded from default search.'
        ),
        query='Widget product',
        top_k=10,
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
    ),
    Scenario(
        id='deprioritized_visible_with_flag',
        description='Deprioritized content reappears when include_deprioritized=True.',
        query='Widget product',
        top_k=10,
        include_deprioritized=True,
        expected=KeywordsPresent(type='keywords_present', keywords=['discontinued', 'migration']),
    ),
    Scenario(
        id='note_search_still_works',
        description='Note search finds Widget Lite even after deprioritization.',
        query='Widget Lite discontinued',
        top_k=10,
        search_type='note',
        expected=KeywordsPresent(type='keywords_present', keywords=['Widget Lite']),
    ),
    # ------------------------------------------------------------------
    # GROUP_INTENT_CLASSIFICATION — units carry intent_class metadata.
    # The outcome reads `unit.metadata['intent_class']`; agent backends
    # have no DTO to inspect, so xfail there.
    # ------------------------------------------------------------------
    Scenario(
        id='permanent_intent',
        description='Units from permanent docs carry permanent intent class.',
        query='company core values permanent principles',
        top_k=10,
        expected=UnitMetadataMatches(
            type='unit_metadata_matches',
            expected_metadata={'intent_class': 'permanent'},
        ),
        expected_failure_modes=_AGENT_XFAIL,
    ),
    Scenario(
        id='durable_intent',
        description='Units from durable docs carry durable intent class.',
        query='annual product roadmap 2025 deliverables',
        top_k=10,
        expected=UnitMetadataMatches(
            type='unit_metadata_matches',
            expected_metadata={'intent_class': 'durable'},
        ),
        expected_failure_modes=_AGENT_XFAIL,
    ),
    Scenario(
        id='ephemeral_intent',
        description='Units from ephemeral docs carry ephemeral intent class.',
        query='daily standup sprint blocker action items',
        top_k=10,
        expected=UnitMetadataMatches(
            type='unit_metadata_matches',
            expected_metadata={'intent_class': 'ephemeral'},
        ),
        expected_failure_modes=_AGENT_XFAIL,
    ),
    # ------------------------------------------------------------------
    # GROUP_PROCEDURAL_KV — KV write (setup) + read (outcome)
    # ------------------------------------------------------------------
    Scenario(
        id='kv_roundtrip_procedure',
        description='KV write followed by read returns the same value.',
        query='procedure:deploy:staging',
        top_k=10,
        setup_actions=[
            SetupAction(
                kind='kv_write',
                kv_key='procedure:deploy:staging',
                kv_value='For staging deploys, use --no-migrate flag after 6pm',
            ),
        ],
        expected=KvRoundtrip(
            type='kv_roundtrip',
            kv_key='procedure:deploy:staging',
            expected_value='For staging deploys, use --no-migrate flag after 6pm',
        ),
        expected_failure_modes=_AGENT_XFAIL,
    ),
    # ------------------------------------------------------------------
    # GROUP_SUMMARIZATION — summarize_node returns non-empty text
    # ------------------------------------------------------------------
    Scenario(
        id='summarize_dataforge_entity',
        description='Summarize node for DataForge entity returns a non-empty summary.',
        query='DataForge',
        top_k=10,
        expected=SummaryNonempty(type='summary_nonempty', entity_query='DataForge'),
        expected_failure_modes=_AGENT_XFAIL,
    ),
    # ------------------------------------------------------------------
    # GROUP_REFLECTION — reflection produces mental models. The
    # `trigger_reflections` setup action seeds reflections on the top-N
    # entities; subsequent mental_model search probes the result.
    #
    # Placed AFTER the other Acme/TechCo content so by the time
    # `get_top_entities` runs, the entity counts have stabilized
    # (Sarah Chen, Acme Corp, Project Alpha all appear in many notes).
    # ------------------------------------------------------------------
    Scenario(
        id='reflection_mental_model',
        description=(
            'Reflection on top entities produces a mental model; LLM-judge '
            'verifies it references Project Alpha, Sarah Chen as project '
            'lead, and Phase 1 completion.'
        ),
        query='Sarah Chen',
        top_k=10,
        setup_actions=[
            # ``target_entity_names`` blocks until both entities have a
            # materialized mental_model. ``min_mental_model_hits=5`` matches
            # the downstream ``mental_model_strategy`` scenario's top_k=5 so
            # it doesn't race the reflection writer (legacy behavior of "≥1
            # hit" left ~3 of ~24 expected observations queryable when the
            # consumer fired). ``probe_query`` matches the consumer query so
            # we observe what it will observe.
            SetupAction(
                kind='trigger_reflections',
                count=5,
                target_entity_names=['Sarah Chen', 'Project Alpha'],
                min_mental_model_hits=5,
                probe_query='Sarah Chen leadership',
                timeout_s=240,
            ),
        ],
        expected=LLMJudge(
            type='llm_judge',
            rubric=(
                'The result must reference Project Alpha, Sarah Chen as '
                'project lead, and Phase 1 completion.'
            ),
            threshold=0.5,
        ),
    ),
    Scenario(
        id='mental_model_strategy',
        description=(
            'Mental model retrieval strategy returns useful results for the '
            'reflected entity. LLM-judges each top-k result for relevance to '
            'the query rather than relying on surface-level keyword presence.'
        ),
        query='Sarah Chen leadership',
        top_k=5,
        strategies=['mental_model'],
        expected=UsefulAtK(
            type='useful_at_k',
            rubric=(
                'A useful result references Sarah Chen as a leader or '
                'lead role-holder, OR describes her leadership style, '
                'decisions, or scope of authority.'
            ),
            k=5,
            threshold=0.5,
        ),
    ),
    # ------------------------------------------------------------------
    # GROUP_VAULT_ISOLATION — last group; per-vault scoping. Each
    # scenario pins ``vault_name=`` so results are scoped to a single
    # vault. The two source notes carry ``vault_name:`` frontmatter so
    # they are ingested into distinct vaults at suite startup.
    # ------------------------------------------------------------------
    Scenario(
        id='vault_a_contains_gamma',
        description='Vault A search finds Project Gamma.',
        query='real-time platform',
        top_k=10,
        vault_name='bench-vault-a',
        expected=KeywordsPresent(type='keywords_present', keywords=['Elixir']),
    ),
    Scenario(
        id='vault_a_excludes_delta',
        description='Vault A search does NOT contain Project Delta content.',
        query='distributed processing engine',
        top_k=10,
        vault_name='bench-vault-a',
        expected=KeywordsAbsent(type='keywords_absent', keywords=['Scala', 'Akka']),
    ),
    Scenario(
        id='vault_b_contains_delta',
        description='Vault B search finds Project Delta.',
        query='distributed processing engine',
        top_k=10,
        vault_name='bench-vault-b',
        expected=KeywordsPresent(type='keywords_present', keywords=['Scala']),
    ),
    Scenario(
        id='vault_b_excludes_gamma',
        description='Vault B search does NOT contain Project Gamma content.',
        query='real-time collaboration platform',
        top_k=10,
        vault_name='bench-vault-b',
        expected=KeywordsAbsent(type='keywords_absent', keywords=['Elixir', 'Phoenix']),
    ),
    Scenario(
        id='vault_a_entity_isolation',
        description='Entity "Polaris Labs" exists only in vault A.',
        query='Polaris Labs',
        top_k=10,
        vault_name='bench-vault-a',
        expected=EntityResolves(type='entity_resolves', expected_names=['Polaris Labs']),
    ),
    Scenario(
        id='vault_b_entity_isolation',
        description='Entity "Nordic Data Systems" exists only in vault B.',
        query='Nordic Data Systems',
        top_k=10,
        vault_name='bench-vault-b',
        expected=EntityResolves(
            type='entity_resolves',
            expected_names=['Nordic Data Systems'],
        ),
    ),
]


SUITE = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    scenarios=SCENARIOS,
    readme_path=_ROOT / 'README.md',
)
