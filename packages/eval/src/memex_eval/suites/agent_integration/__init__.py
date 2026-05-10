"""Agent integration suite — verifies the Nous Hermes Agent, running with
the ``memex-hermes-plugin`` from ``packages/hermes-plugin``, answers
questions about a vault using the plugin's memory tools.

Default backend is ``hermes`` because that is the integration this suite
exists to test. Override with ``--answer-mode claude-code`` if you want
to run the same scenarios against the Claude Code MCP integration.
"""

from pathlib import Path

from memex_eval.suite import (
    KeywordsPresent,
    LLMJudge,
    SuiteMetadata,
    SuiteSources,
    ToolCallContains,
)
from memex_eval.suite.decorator import Suite

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='agent_integration',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'Agent integration suite — verifies LLM agents (Claude Code or '
        'Hermes) can answer questions about a Memex vault via MCP/plugin tools.'
    ),
    tags=['agent', 'integration', 'mcp', 'hermes'],
    primary_metrics=[
        'suite.pass_rate',
        'metric.graded_score.mean',
        'cost.total_usd',
        'tokens.total_in',
    ],
    components_under_test=[
        'mcp.tools',
        'hermes-plugin.provider',
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

suite.register(
    id='agent_finds_alpha_lead',
    description=(
        'The agent should retrieve information about Project Alpha and '
        'identify Sarah Chen as the lead in its final answer.'
    ),
    query='Who leads Project Alpha and what tech stack do they use?',
    top_k=10,
    expected=LLMJudge(
        type='llm_judge',
        rubric=(
            'The answer must identify Sarah Chen as the Project Alpha '
            'lead and mention at least one tech-stack item (Python, '
            'PostgreSQL, Kafka, or React).'
        ),
        threshold=0.5,
    ),
)

suite.register(
    id='agent_keywords_in_answer',
    description="Sarah Chen must be named in the agent's answer.",
    query='Who is the Project Alpha lead?',
    top_k=10,
    expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen']),
)

suite.register(
    id='agent_calls_memex_search',
    description=(
        'The hermes agent must call at least one Memex search tool '
        '(via the memex-hermes-plugin) before answering. Either of '
        'the listed tools satisfies the integration check; agents '
        'legitimately route a single question through only one.'
    ),
    query='Tell me about Project Alpha at Acme Corp.',
    top_k=10,
    expected=ToolCallContains(
        type='tool_call_contains',
        expected_tools=['memex_memory_search', 'memex_note_search'],
        min_count=1,
        match_mode='any',
    ),
)

SUITE = suite.build()
