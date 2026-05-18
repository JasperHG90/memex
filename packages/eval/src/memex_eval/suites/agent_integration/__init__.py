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
- ``GOOGLE_API_KEY`` — for the LLM judge (``gemini/gemini-3.1-flash-lite-preview``).
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

from memex_eval.suite import SuiteMetadata, SuiteSources
from memex_eval.suite.decorator import Suite

# Suite-private extension modules — import for decorator side effects
# (the ``deprio_recovers_from_400`` outcome and the
# ``seed_mental_model_observation`` setup-action handler must be in
# the registries BEFORE the SCENARIO_SPECS loop below resolves their
# dict / kind discriminators).
from memex_eval.suites.agent_integration import _outcomes as _outcomes  # noqa: F401
from memex_eval.suites.agent_integration import _setup_actions as _setup_actions  # noqa: F401

_ROOT = Path(__file__).parent

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

from .scenarios import SCENARIO_SPECS  # noqa: E402

for _spec in SCENARIO_SPECS:
    suite.register(**_spec)

SUITE = suite.build()
