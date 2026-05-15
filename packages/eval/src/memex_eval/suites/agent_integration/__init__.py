"""Agent integration suite — Hermes or Claude-Code agent against memex tools.

Default agent: ``hermes`` (memex-hermes-plugin). Override with
``--answer-mode claude-code`` to run the same scenarios against the
Claude Code MCP integration. Under ``claude-code`` the suite backend
mounts the memex Claude Code plugin via ``--plugin-dir`` so the agent
gets briefing parity with Hermes (KV-namespace routing, citation
discipline, ``/remember`` / ``/recall`` skills). The plugin is
auto-resolved from the monorepo's ``packages/claude-code-plugin/``;
override with ``MEMEX_CLAUDE_PLUGIN_DIR``.

The suite covers two layers:

1. **Smoke** (3 scenarios) — verifies the agent loads the plugin, finds
   a basic fact, and calls a search tool.
2. **Tool-surface synthesis** (20 active scenarios + 3 xfail tripwires) —
   measures synthesis quality across triage, multi-hop, temporal,
   entity-graph, survey, faithfulness, navigation, feedback discipline,
   KV-namespace conventions, and append-vs-add lifecycle. Plus xfail
   tripwires for the three real plugin gaps under ``hermes`` mode
   (``delete_assets``, ``get_due_for_review``, ``memory_review``);
   these tools exist in MCP so the same scenarios should xpass under
   ``--answer-mode claude-code``.

Required env vars:
- ``GOOGLE_API_KEY`` — for the LLM judge (``gemini/gemini-3-flash-preview``).
- ``OLLAMA_API_KEY`` — for the Hermes agent (``glm-5.1:cloud``) if running
  in hermes mode. Not needed under ``--answer-mode claude-code``.
- ``ANTHROPIC_API_KEY`` — for Claude Code if running in that mode.

Optional:
- ``EVAL_JUDGE_MODEL`` — override the judge model.
- ``HERMES_MODEL`` — override the agent model under hermes mode.
- ``MEMEX_EVAL_CLAUDE_MODEL`` — override the agent model under
  ``claude-code`` mode (default ``claude-sonnet-4-6``).
- ``MEMEX_CLAUDE_PLUGIN_DIR`` — override the Claude Code plugin
  location (default: monorepo's ``packages/claude-code-plugin/``).

Compatibility with run modes:
- ``--from-snapshot auto``: supported. Mutations don't poison the cache;
  cache is read-only-imported into a fresh vault every run.
- ``--keep-vault`` / ``--reuse-vault``: mutating scenarios skip with
  ``skip_reason='mutating_under_reuse_vault'``. The plugin-gap xfail
  tripwires also skip under reuse (they're marked mutating too); to
  verify gap-closure run WITHOUT ``--reuse-vault``.

End-of-run cleanup:
- Default mode wipes every SQLModel-managed table (drop_all) and
  recreates the schema. API-level vault deletion alone leaked rows in
  ``reflection_queue``, ``audit_logs``, ``kv_entries``, and
  ``outcome_audit_log`` between runs; the schema wipe fixes that. The
  server's connection pool is terminated; restart it after a run if you
  see asyncpg ``InterfaceError`` on the next request (rare — usually
  reconnects lazily).
- ``--keep-vault`` and ``--reuse-vault`` suppress the wipe so the
  preserved vault survives.
"""

from pathlib import Path

from memex_eval.suite import (
    AnyOfOutcomes,
    CompositeOutcome,
    KeywordsPresent,
    LLMJudge,
    SetupAction,
    SuiteMetadata,
    SuiteSources,
    ToolCallArgMatches,
    ToolCallContains,
    ToolCallCountAcross,
)
from memex_eval.suite.decorator import Suite

# Suite-private extension modules — import for decorator side effects
# (must run BEFORE any ``suite.register(...)`` call that references the
# registered names). See ``.claude/rules/eval-suites.md``
# (baseline-anchor-stability rule, import-order subsection).
from memex_eval.suites.agent_integration._outcomes import DeprioRecoversFrom400  # noqa: F401,E402
from memex_eval.suites.agent_integration import _setup_actions as _seed_actions  # noqa: F401,E402

_ROOT = Path(__file__).parent
_DUR_MS = 180_000.0

METADATA = SuiteMetadata(
    name='agent_integration',
    schema_version='1',
    suite_version='2.0.0',
    description=(
        'Agent integration + tool-surface synthesis. Verifies a (model x '
        'agent) pair (Hermes or Claude Code) uses memex tools correctly: '
        'triage, multi-hop synthesis, temporal recency, entity resolution, '
        'survey routing, faithfulness (citation + refusal), large-doc '
        'navigation, feedback discipline (record_outcome, deprioritize), '
        'KV-namespace conventions, and append-vs-add lifecycle choice. '
        'Plus xfail tripwires for plugin gaps under hermes mode '
        '(delete_assets, get_due_for_review, memory_review).'
    ),
    tags=['agent', 'integration', 'mcp', 'hermes', 'tool-surface'],
    primary_metrics=[
        'suite.pass_rate_non_mutating',
        'suite.pass_rate_all',
        'metric.graded_score.mean',
        'cost.total_usd',
    ],
    components_under_test=[
        'hermes-plugin.tools',
        'hermes-plugin.provider',
        'mcp.tools',
        'memex.api',
    ],
    knobs=[],
    requires_llm_judge=True,
    default_answer_mode='hermes',
)

suite = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    readme_path=_ROOT / 'README.md',
)


# --- Smoke layer (kept from 1.0.0; passes under both hermes and claude-code) ---

suite.register(
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

suite.register(
    id='agent_keywords_in_answer',
    group='smoke',
    description="Sarah Chen must be named in the agent's answer.",
    query='Who is the Project Alpha lead?',
    top_k=10,
    max_duration_ms=_DUR_MS,
    expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen']),
)

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
    id='temporal_latest_revenue',
    group='temporal',
    query=(
        'Looking at the quarterly revenue notes in this vault, what was the headline '
        'total revenue number for Q3 2025? Single value only.'
    ),
    max_duration_ms=_DUR_MS,
    expected=KeywordsPresent(type='keywords_present', keywords=['18.1']),
)

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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
        'successful resolution so we lock the lesson in.'
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

suite.register(
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

suite.register(
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

suite.register(
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
    expected=DeprioRecoversFrom400(type='deprio_recovers_from_400'),
    replicates_override=1,
    mutating_scenario=True,
)


# --- KV / state ---

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
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

suite.register(
    id='kv_retrieves_convention',
    group='kv',
    query="What's our indentation convention in this repo?",
    max_duration_ms=_DUR_MS,
    expected=KeywordsPresent(type='keywords_present', keywords=['7']),
    replicates_override=1,
    depends_on_prior_scenarios=['kv_writes_project_preference'],
)


# --- Lifecycle ---

suite.register(
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


suite.register(
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


suite.register(
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

suite.register(
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


SUITE = suite.build()
