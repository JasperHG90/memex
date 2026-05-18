"""Pure-data scenario specs for the agent_integration suite.

See ``memex_eval.suite.read_scenario_specs`` for the contract:
this module must be importable WITHOUT triggering the parent
package's ``__init__.py`` (no relative imports, no side effects
beyond appending to ``SCENARIO_SPECS``).

One scenario (``feedback_deprioritize_observation_400_recovery``)
asserts against the suite-private ``deprio_recovers_from_400``
outcome. Its expected value is expressed as a dict
(``{'type': 'deprio_recovers_from_400'}``) so this module can
be loaded standalone — class resolution happens later when the
parent ``__init__.py`` calls ``suite.register(**spec)``, by which
time the suite-private ``_outcomes`` module has registered the
type via its decorator.
"""

from __future__ import annotations

from memex_eval.suite import (
    AnyOfOutcomes,
    CompositeOutcome,
    KeywordsPresent,
    LLMJudge,
    SetupAction,
    ToolCallArgMatches,
    ToolCallContains,
    ToolCallCountAcross,
)

SCENARIO_SPECS: list[dict] = []

_DUR_MS = 180_000.0


def _register(**kwargs) -> None:
    SCENARIO_SPECS.append(kwargs)


# --- Smoke layer (kept from 1.0.0; passes under both hermes and claude-code) ---

_register(
    id='agent_finds_alpha_lead',
    group='smoke',
    description=(
        'The agent retrieves information about Project Alpha and identifies '
        'Sarah Chen as the lead in its final answer.'
    ),
    query='Who leads Project Alpha and what tech stack do they use?',
    top_k=10,
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            'The answer identifies Sarah Chen as the Project Alpha lead and '
            'mentions at least one tech-stack item (Python, PostgreSQL, '
            'Kafka, or React).'
        ),
        threshold=0.5,
    ),
)

_register(
    id='agent_keywords_in_answer',
    group='smoke',
    description="Sarah Chen must be named in the agent's answer.",
    query='Who is the Project Alpha lead?',
    top_k=10,
    max_duration_ms=_DUR_MS,
    expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen']),
)

_register(
    id='agent_calls_memex_search',
    group='smoke',
    description=(
        'The agent calls at least one memex search tool before answering. '
        'Either listed tool satisfies the integration check.'
    ),
    query='Tell me about Project Alpha at Acme Corp.',
    top_k=10,
    max_duration_ms=_DUR_MS,
    expected=ToolCallContains(
        type='tool_call_contains',
        expected_tools=['memex_memory_search', 'memex_note_search'],
        min_count=1,
        match_mode='any',
    ),
)


# --- Triage & comprehension ---

_register(
    id='triage_picks_relevant_from_many',
    group='triage',
    query='What programming language is Project Alpha built in?',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            KeywordsPresent(type='keywords_present', keywords=['Python']),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer states Project Alpha is built in Python and '
                    'cites project-alpha-kickoff or project-alpha-q3-update '
                    'as the source.'
                ),
                threshold=0.5,
            ),
        ],
    ),
)

_register(
    id='multi_hop_cross_doc',
    group='triage',
    query="What did Sarah Chen's team learn from the Redis incident?",
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            "The answer identifies Sarah Chen's team, names Redis as the "
            'failing component, and states the lesson learned (switching '
            'to in-process cache or equivalent). The answer cites the '
            'incident note and the team retro note.'
        ),
        threshold=0.5,
    ),
)

_register(
    id='multi_hop_entity_to_facts',
    group='triage',
    query='What coding conventions has Sarah Chen contributed to in this codebase?',
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            'The answer names at least one specific coding convention '
            'attributed to Sarah Chen (ruff line-length 100, type-stub-first '
            'policy, or similar) and cites team-coding-standards.md as the source.'
        ),
        threshold=0.5,
    ),
)


# --- Temporal ---

_register(
    id='temporal_latest_revenue',
    group='temporal',
    query=(
        'Looking at the quarterly revenue notes in this vault, what was the headline '
        'total revenue number for Q3 2025? Single value only.'
    ),
    max_duration_ms=_DUR_MS,
    expected=KeywordsPresent(type='keywords_present', keywords=['18.1']),
)

_register(
    id='temporal_evolution',
    group='temporal',
    query='How has quarterly revenue evolved through 2025?',
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            'The answer lists Q1, Q2, and Q3 revenue figures in chronological '
            'order: $12.5M (Q1), $15.3M (Q2), $18.1M (Q3). The answer '
            'describes the trajectory (growth).'
        ),
        threshold=0.5,
    ),
)

_register(
    id='temporal_superseded_handling',
    group='temporal',
    query='Tell me about our data warehouse setup.',
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            'The answer leads with the current data warehouse setup '
            '(PostgreSQL 16 + pgvector) as primary, and acknowledges the '
            'legacy Redshift setup as historical/deprecated. The answer '
            'does NOT present the legacy setup as current.'
        ),
        threshold=0.5,
    ),
)


# --- Entity ---

_register(
    id='entity_resolution_canonical',
    group='entity',
    query='Tell me about Sarah Chen.',
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            "The answer treats 'Sarah Chen' as one canonical entity (no "
            "fragmentation across name variants like 'Sarah' and 'Sarah Chen' "
            'as separate people). The answer cites sarah-chen-profile or '
            'other notes mentioning her.'
        ),
        threshold=0.5,
    ),
)

_register(
    id='entity_cooccurrence_strongest',
    group='entity',
    query='Who works most closely with Sarah Chen, based on their joint appearances in notes?',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_get_entity_cooccurrences'],
                min_count=1,
                match_mode='any',
            ),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer names the top co-occurring entity for Sarah '
                    'Chen and gives at least one example of joint context.'
                ),
                threshold=0.5,
            ),
        ],
    ),
)

_register(
    id='entity_mentions_enumeration',
    group='entity',
    query='Where is Apache Kafka mentioned in this vault?',
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=('The answer lists at least two notes that mention Apache Kafka.'),
        threshold=0.5,
    ),
)


# --- Survey ---

_register(
    id='survey_vault_overview',
    group='survey',
    query="What's in this vault — give me a high-level overview.",
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_get_vault_summary'],
                min_count=1,
                match_mode='any',
            ),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer mentions the major topics: Project Alpha, '
                    'engineering practices, revenue figures, and at least one '
                    'operational topic (incident or retro).'
                ),
                threshold=0.5,
            ),
        ],
    ),
)

_register(
    id='survey_broad_topic',
    group='survey',
    query=(
        'Give me a comprehensive picture of Project Alpha — leadership, '
        'tech stack, status, incidents, lessons.'
    ),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            AnyOfOutcomes(
                type='any_of',
                children=[
                    ToolCallContains(
                        type='tool_call_contains',
                        expected_tools=['memex_survey'],
                        min_count=1,
                        match_mode='any',
                    ),
                    ToolCallCountAcross(
                        type='tool_call_count_across',
                        expected_tools=['memex_memory_search', 'memex_note_search'],
                        min_total=3,
                    ),
                ],
            ),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer covers leadership (Sarah Chen), tech stack '
                    '(Python, PostgreSQL, Kafka, React, Redis), at least one '
                    'incident or retro, and the Q3 status.'
                ),
                threshold=0.5,
            ),
        ],
    ),
)


# --- Faithfulness ---

_register(
    id='faithfulness_refuses_out_of_corpus',
    group='faithfulness',
    query="What's our employee benefits policy?",
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            'The answer states explicitly that information about an employee '
            'benefits policy is not available in this vault. The answer does '
            'NOT fabricate, infer, or invent a benefits policy. Phrases like '
            "'typically' or 'usually companies have' that introduce "
            'hallucinated content count as fabrication.'
        ),
        threshold=0.75,
    ),
)

_register(
    id='faithfulness_citations',
    group='faithfulness',
    query='Why was PostgreSQL chosen as the database for Project Alpha?',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=[
                    'memex_memory_search',
                    'memex_note_search',
                    'memex_find_note',
                ],
                min_count=1,
                match_mode='any',
            ),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer gives at least one concrete reason for '
                    'choosing PostgreSQL (ACID, JSONB+pgvector, team '
                    'familiarity, or similar). Citing tech-stack-decision-record '
                    '(by title or note id) is a bonus. The answer does NOT fabricate reasons not '
                    'present in the corpus.'
                ),
                threshold=0.5,
            ),
        ],
    ),
)


# --- Navigation ---

_register(
    id='navigation_via_page_index',
    group='navigation',
    query="What's the testing convention in the engineering handbook?",
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            KeywordsPresent(
                type='keywords_present',
                keywords=['pytest'],
            ),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer identifies the testing convention from the '
                    'engineering handbook (pytest, with a brief explanation of '
                    'how it is used). The answer does NOT fabricate a '
                    'convention not in the corpus.'
                ),
                threshold=0.5,
            ),
        ],
    ),
)

_register(
    id='navigation_summarize_section',
    group='navigation',
    query='Summarize the deployment section of the engineering handbook.',
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            'The answer summarizes the deployment-section content of '
            'the engineering handbook (environments, release cadence, '
            'rollback, migrations) and does NOT include content from '
            'unrelated sections (testing, style, security).'
        ),
        threshold=0.5,
    ),
)


# --- Feedback / MW ---

_register(
    id='feedback_surfaces_candidate_notes',
    group='feedback',
    description=(
        'User describes a vague topic ("a bug a while back around caching") '
        'without naming a specific note. The agent should call a note-level '
        'search (note_search / find_note / list_notes / recent_notes) and '
        'present candidate notes by title + date so the user can recognize '
        'and pick. It should NOT dive straight to memory units or pick a '
        'single note silently.'
    ),
    query=(
        'We had some bug a while back related to our caching layer — can '
        'you remind me what notes we have on that?'
    ),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=[
                    'memex_note_search',
                    'memex_find_note',
                    'memex_list_notes',
                    'memex_recent_notes',
                ],
                min_count=1,
                match_mode='any',
            ),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer surfaces two or more candidate notes (e.g. '
                    'the Redis incident note, the Q3 retro, the Project '
                    'Alpha Q3 update). Each candidate is identified by its '
                    'title or a clear descriptor AND a date or time '
                    'reference so the user can recognize which one they '
                    'mean. The answer presents the candidates for the user '
                    'to pick from rather than silently selecting one and '
                    'narrating its contents.'
                ),
                threshold=0.5,
            ),
        ],
    ),
)

_register(
    id='feedback_records_success',
    group='feedback',
    description=(
        'Discoverability check: given a clear user signal of success on a '
        'specific recent fix, the agent should call memex_record_outcome '
        "with a per-unit 'helpful' verb attached to memory units found via "
        'search. The server contract requires units=[{unit_id, verb, reason}]; '
        'the loose check asserts the verb is helpful — verifying the agent '
        'attached the outcome to the *right* memory units is a multi-turn '
        'property the single-turn framework cannot fairly judge.'
    ),
    query=(
        'Hey, that Redis cache fix we landed last week — switching to '
        "in-process caching — it's been holding. Record that as a "
        "successful resolution so we don't go round on this again."
    ),
    max_duration_ms=_DUR_MS,
    expected=ToolCallArgMatches(
        type='tool_call_arg_matches',
        tool='memex_record_outcome',
        arg_name='units',
        regex=r'helpful',
        min_count=1,
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='feedback_clarifies_under_ambiguity',
    group='feedback',
    description=(
        'Ambiguous success signal with no specific referent. Agent should '
        'ask which fix the user means before calling record_outcome blindly. '
        'The vault contains multiple bug-fix-shaped memories (Redis cache, '
        'incident, retro); "that worked" is genuinely under-specified.'
    ),
    query=("Hey, that worked! Record it as a success so we don't repeat the issue."),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_record_outcome',
                arg_name='success',
                regex=r'.*',
                expect_absent=True,
            ),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer asks the user to clarify which specific fix '
                    'or topic they are referring to (Redis cache, incident, '
                    'or another) before recording any outcome. The answer '
                    'does NOT silently pick one of the possibilities and act '
                    'on it.'
                ),
                threshold=0.5,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='feedback_deprioritize_obsolete',
    group='feedback',
    query=(
        'Stop suggesting Redis as a solution — we removed it after the '
        'incident. Mark our Redis-related memory as deprioritized.'
    ),
    max_duration_ms=_DUR_MS,
    expected=ToolCallContains(
        type='tool_call_contains',
        expected_tools=['memex_memory_deprioritize'],
        min_count=1,
        match_mode='any',
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='feedback_deprioritize_observation_400_recovery',
    group='feedback',
    description=(
        'V21 contract: when the agent passes an observation UUID to '
        'memex_memory_deprioritize, the server returns HTTP 400 with '
        'source_memory_units; the agent must retry against one of those '
        'MU IDs. The setup action seeds a MentalModel + Observation citing '
        'two MUs ingested from the kafka-batching-strategy source note. '
        'Pass: ≥1 deprio call targets one of the cited MU IDs.'
    ),
    query=(
        'We changed direction on the Kafka batching strategy — the 250ms '
        'time-based windows are no longer what we recommend. Deprioritize '
        "our Kafka batching memory so it doesn't keep surfacing."
    ),
    max_duration_ms=_DUR_MS,
    setup_actions=[
        SetupAction(
            kind='seed_mental_model_observation',
            # The four explicit fields below are documented defaults of the
            # handler; we name them here so the scenario reads as a contract.
            note_key='kafka-batching-strategy',
            entity_name='Kafka Batching Strategy',
            observation_title='Kafka producers use 250ms time-based batching windows',
            max_evidence_mus=2,
        ),
    ],
    expected={'type': 'deprio_recovers_from_400'},
    replicates_override=1,
    mutating_scenario=True,
)


# --- KV / state ---

_register(
    id='kv_writes_project_preference',
    group='kv',
    description=(
        'Project-scoped convention: the query references "this repo" so the '
        'namespace must be project:. global: would over-broaden; user: '
        'would mis-attribute as a personal preference.'
    ),
    query='Remember this for future sessions: we use 7-character indentation in this repo (unusual but deliberate).',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_write'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='key',
                regex=r'^project:.+',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='kv_writes_user_preference',
    group='kv',
    description=(
        'Personal preference: the query is first-person and identity-shaped '
        'so the namespace must be user:. project: or global: would '
        'over-attribute the preference to the team or the ecosystem.'
    ),
    query=("Remember about me: I prefer Neovim as my editor and I'm a senior backend engineer."),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_write'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='key',
                regex=r'^user:.+',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='kv_writes_global_convention',
    group='kv',
    description=(
        'Cross-project ecosystem fact: the query frames the convention as '
        'company-wide / language-wide, not tied to one repo. The namespace '
        'must be global:.'
    ),
    query=(
        'Remember this company-wide standard: across all our projects, '
        'we standardise on Python 3.12 as the minimum runtime.'
    ),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_write'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='key',
                regex=r'^global:.+',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='kv_writes_app_setting',
    group='kv',
    description=(
        'App-scoped setting: the convention only applies inside one agent / '
        'app surface (Claude Code, Hermes, etc.). Namespace must be app:.'
    ),
    query=(
        'Remember this for whenever I use Claude Code: default to dark theme '
        'and show line numbers in code blocks.'
    ),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_write'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='key',
                regex=r'^app:.+',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='kv_retrieves_convention',
    group='kv',
    query="What's our indentation convention in this repo?",
    max_duration_ms=_DUR_MS,
    expected=KeywordsPresent(type='keywords_present', keywords=['7']),
    replicates_override=1,
    depends_on_prior_scenarios=['kv_writes_project_preference'],
)


# --- KV / hard wake-word triggers (must pass) ---
# These scenarios test the explicit imperative triggers documented in
# agent_surface.RETRIEVAL_ROUTING: "Store in KV: <key>=<value>", "KV: get
# <key>", "KV: search <query>". The agent should execute the matching
# memex_kv_* call verbatim — bypassing any routing reasoning.

_register(
    id='kv_wakeword_store_user',
    group='kv',
    description=(
        'Hard wake-word write — user: namespace. The agent must call '
        'memex_kv_write with the exact key the user typed (no namespace '
        'rewriting, no scope-cue reinterpretation).'
    ),
    query='Store in KV: user:editor=Neovim',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_write'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='key',
                regex=r'^user:editor$',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='kv_wakeword_store_project',
    group='kv',
    description=(
        'Hard wake-word write — project:<id>: namespace. Verbatim key '
        'including the project segment.'
    ),
    query='Store in KV: project:eval-suite:lang=Python 3.12',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_write'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='key',
                regex=r'^project:eval-suite:lang$',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='kv_wakeword_store_global',
    group='kv',
    description=(
        'Hard wake-word write — global: namespace. Verbatim key, no demotion to user: or project:.'
    ),
    query='Store in KV: global:lang_min=Python 3.12',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_write'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='key',
                regex=r'^global:lang_min$',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='kv_wakeword_store_app',
    group='kv',
    description=(
        'Hard wake-word write — app:<app-id>: namespace. Verbatim key including the app segment.'
    ),
    query='Store in KV: app:claude-code:theme=dark',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_write'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='key',
                regex=r'^app:claude-code:theme$',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)

_register(
    id='kv_wakeword_kv_get',
    group='kv',
    description=(
        'Hard wake-word read: "KV: get <key>" must route to memex_kv_get '
        'with the exact key the user typed — no fuzzy search, no '
        'note-search fallback.'
    ),
    query='KV: get user:editor',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_get'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_get',
                arg_name='key',
                regex=r'^user:editor$',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    depends_on_prior_scenarios=['kv_wakeword_store_user'],
)

_register(
    id='kv_wakeword_kv_search',
    group='kv',
    description=(
        'Hard wake-word fuzzy lookup: "KV: search <query>" must route to '
        'memex_kv_search — not memex_kv_get, not memex_memory_search, not '
        'memex_note_search.'
    ),
    query='KV: search editor preference',
    max_duration_ms=_DUR_MS,
    expected=ToolCallContains(
        type='tool_call_contains',
        expected_tools=['memex_kv_search'],
        min_count=1,
        match_mode='any',
    ),
    replicates_override=1,
    depends_on_prior_scenarios=['kv_wakeword_store_user'],
)

_register(
    id='kv_wakeword_store_with_ttl',
    group='kv',
    description=(
        'Hard wake-word write with TTL: when the user specifies an '
        'expiration, the agent must pass ttl_seconds (positive integer) '
        'to memex_kv_write — not store unexpiring.'
    ),
    query='Store in KV: user:current_focus=ticket-456 (expires in 1 hour, ttl_seconds=3600)',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_write'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='key',
                regex=r'^user:current_focus$',
                min_count=1,
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_kv_write',
                arg_name='ttl_seconds',
                regex=r'^[1-9]\d*$',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)


# --- Lifecycle ---

_register(
    id='lifecycle_append_meeting',
    group='lifecycle',
    query='Add to the March meeting notes: we decided to use BigQuery for analytics.',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_append_note'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_append_note',
                arg_name='delta',
                regex=r'BigQuery',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)


_register(
    id='lifecycle_archive_legacy_warehouse_note',
    group='lifecycle',
    query=(
        'The legacy warehouse note is deprecated and should be hidden from '
        'normal retrieval but kept available for audit. Archive it.'
    ),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_find_note', 'memex_note_search'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_set_note_status'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_set_note_status',
                arg_name='status',
                regex=r'^archived$',
                min_count=1,
            ),
        ],
    ),
    replicates_override=1,
    mutating_scenario=True,
)


_register(
    id='lifecycle_append_parent_remains_retrievable',
    group='lifecycle',
    query=('What did the team decide for analytics in the March meeting notes?'),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_note_search', 'memex_memory_search'],
                min_count=1,
                match_mode='any',
            ),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer names BigQuery (the decision the prior '
                    'scenario appended to the March meeting notes) and '
                    'attributes it to the March meeting notes. A response '
                    'that says the note is gone, archived, stale, or '
                    'cannot be found fails. A response that mentions a '
                    'different analytics tool fails. The append flow '
                    'extends content in-place on the same note_id, so the '
                    'note must still be retrievable as an active note '
                    'containing both the original content and the '
                    'BigQuery addition.'
                ),
                threshold=0.5,
            ),
        ],
    ),
    replicates_override=1,
    depends_on_prior_scenarios=['lifecycle_append_meeting'],
    mutating_scenario=True,
)


# --- Plugin-gap xfail tripwires (xfail under hermes; should xpass under claude-code) ---
#
# Two earlier `review_loop_*` tripwires were dropped: the MCP tools they
# pointed at were retired alongside the FSFM-inspired deprioritization
# redesign on the release branch. `asset_lifecycle_detach` is the only
# remaining tripwire — it tests that the agent picks the right tool for
# detaching an attached image, which Hermes still misses in its plugin
# briefing but Claude Code resolves via the MCP tool surface.

_register(
    id='asset_lifecycle_detach',
    group='lifecycle',
    query=(
        'Please detach the attached image from the architecture-overview note. '
        'Invoke the appropriate memex tool to perform the detach (do not just '
        'name it). If the call surfaces an error because no asset is present, '
        'that is fine — the tool invocation itself is what matters.'
    ),
    max_duration_ms=_DUR_MS,
    expected=ToolCallContains(
        type='tool_call_contains',
        expected_tools=['memex_delete_assets'],
        min_count=1,
        match_mode='any',
    ),
    expected_failure_modes=['hermes'],
    replicates_override=1,
    mutating_scenario=True,
)
