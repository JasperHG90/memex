"""Agent integration suite — Hermes or Claude-Code agent against memex tools.

Default agent: ``hermes`` (memex-hermes-plugin). Override with
``--answer-mode claude-code`` to run the same scenarios against the
Claude Code MCP integration.

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

Compatibility with run modes:
- ``--from-snapshot auto``: supported. Mutations don't poison the cache;
  cache is read-only-imported into a fresh vault every run.
- ``--keep-vault`` / ``--reuse-vault``: mutating scenarios skip with
  ``skip_reason='mutating_under_reuse_vault'``. The plugin-gap xfail
  tripwires also skip under reuse (they're marked mutating too); to
  verify gap-closure run WITHOUT ``--reuse-vault``.
"""

from pathlib import Path

from memex_eval.suite import (
    AnyOfOutcomes,
    CompositeOutcome,
    InlineNote,
    KeywordsPresent,
    LLMJudge,
    SuiteMetadata,
    SuiteSources,
    ToolCallArgMatches,
    ToolCallContains,
    ToolCallCountAcross,
)
from memex_eval.suite.decorator import Suite

_ROOT = Path(__file__).parent
_DUR_MS = 90_000.0

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
        'Just the most recent quarterly revenue number for 2025 — single value, no other context.'
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
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_get_entity_mentions'],
                min_count=1,
                match_mode='any',
            ),
            LLMJudge(
                type='llm_judge',
                rubric=('The answer lists at least two notes that mention Apache Kafka.'),
                threshold=0.5,
            ),
        ],
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
                    'familiarity, or similar) AND cites '
                    'tech-stack-decision-record (by title or note id) as the '
                    'source. The answer does NOT fabricate reasons not '
                    'present in the corpus.'
                ),
                threshold=0.75,
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
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_get_page_indices', 'memex_get_nodes'],
                min_count=1,
                match_mode='any',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_read_note',
                arg_name='note_id',
                regex=r'{engineering_handbook_id}',
                expect_absent=True,
            ),
            KeywordsPresent(
                type='keywords_present',
                keywords=['pytest'],
            ),
        ],
    ),
)

suite.register(
    id='navigation_summarize_section',
    group='navigation',
    query='Summarize the deployment section of the engineering handbook.',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            AnyOfOutcomes(
                type='any_of',
                children=[
                    ToolCallContains(
                        type='tool_call_contains',
                        expected_tools=['memex_memory_summarize_node'],
                        min_count=1,
                        match_mode='any',
                    ),
                    ToolCallContains(
                        type='tool_call_contains',
                        expected_tools=['memex_get_page_indices', 'memex_get_nodes'],
                        min_count=1,
                        match_mode='any',
                    ),
                ],
            ),
            LLMJudge(
                type='llm_judge',
                rubric=(
                    'The answer summarizes the deployment-section content of '
                    'the engineering handbook (environments, release cadence, '
                    'rollback, migrations) and does NOT include content from '
                    'unrelated sections (testing, style, security).'
                ),
                threshold=0.5,
            ),
        ],
    ),
)


# --- Feedback / MW ---

suite.register(
    id='feedback_records_success',
    group='feedback',
    query=(
        'Looking at the prior debug-session note '
        '(inline-feedback_records_success-prior-debug-session-redis-cache): '
        'the in-process cache fix worked. Please record this as a success '
        "against the relevant memory units so we don't suggest Redis again."
    ),
    max_duration_ms=_DUR_MS,
    inline_notes=[
        InlineNote(
            note_key='prior-debug-session-redis-cache',
            title='Prior debug session — Redis cache fix',
            content=(
                'In session abc123 the agent retrieved Redis-incident memories '
                'and suggested switching to in-process cache. The user reports '
                'the fix worked and the cascading-outage symptoms have stopped '
                'recurring.'
            ),
        ),
    ],
    expected=ToolCallArgMatches(
        type='tool_call_arg_matches',
        tool='memex_record_outcome',
        arg_name='success',
        regex=r'^True$',
        min_count=1,
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


# --- KV / state ---

suite.register(
    id='kv_writes_preference',
    group='kv',
    query='Remember this for future sessions: we use 4-space indentation in this repo.',
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
                regex=r'^(global|project|user|app):.+',
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
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_kv_get', 'memex_kv_search'],
                min_count=1,
                match_mode='any',
            ),
            KeywordsPresent(type='keywords_present', keywords=['4']),
        ],
    ),
    replicates_override=1,
    depends_on_prior_scenarios=['kv_writes_preference'],
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


# --- Plugin-gap xfail tripwires (xfail under hermes; should xpass under claude-code) ---

suite.register(
    id='review_loop_drives_due',
    group='review',
    query='What memory units are due for review right now?',
    max_duration_ms=_DUR_MS,
    expected=ToolCallContains(
        type='tool_call_contains',
        expected_tools=['memex_get_due_for_review'],
        min_count=1,
        match_mode='any',
    ),
    expected_failure_modes=['hermes'],
    replicates_override=1,
    mutating_scenario=True,
)

suite.register(
    id='review_loop_records_rating',
    group='review',
    query=(
        "I just reviewed the fact about Project Alpha's tech stack — it was "
        'easy. Record that rating.'
    ),
    max_duration_ms=_DUR_MS,
    expected=ToolCallContains(
        type='tool_call_contains',
        expected_tools=['memex_memory_review'],
        min_count=1,
        match_mode='any',
    ),
    expected_failure_modes=['hermes'],
    replicates_override=1,
    mutating_scenario=True,
)

suite.register(
    id='asset_lifecycle_detach',
    group='lifecycle',
    query=(
        'I want to remove an attached image from a note. Which memex tool '
        'would I use, and what arguments would it take?'
    ),
    max_duration_ms=_DUR_MS,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            'The answer names memex_delete_assets as the tool that removes '
            'an attached image from a note, and lists at least one argument '
            '(note_id, note_key, or asset_id/filename).'
        ),
        threshold=0.5,
    ),
    expected_failure_modes=['hermes'],
    replicates_override=1,
    mutating_scenario=True,
)


SUITE = suite.build()
