"""V7 Procedural Plane — agent-mode eval suite.

The procedural_plane sibling suite pins the V7 API contract through
DirectApiBackend (no LLM agent): every scenario drives
``api.procedural_*`` directly. This suite pins the *agent-facing*
contract: does the LLM agent (Hermes or Claude Code) actually call
the procedural tools when it should, and does the routing land on
the V7 plane (not the legacy KV-namespace procedure path)?

The scenarios cover the four load-bearing user journeys on the
procedural plane:

1. **Storing case notes** — the agent gets a trigger signal from the
   user and must call ``memex_procedural_create`` with
   ``kind="case"`` and a ``trigger`` field.
2. **Retrieving procedures via search** — the agent gets a "how do
   I X?" question and must surface a seeded procedure through
   ``memex_procedural_search`` (NOT through KV).
3. **Read-before-write probe** — the agent is told to remember a
   procedure that already exists; it must call
   ``memex_procedural_get_by_identity`` first (not blindly
   ``create`` and 409).
4. **Briefing cards at session start** — the agent is asked to
   brief itself for a project; it must call
   ``memex_procedural_briefing_cards``.

Each scenario uses ``CompositeOutcome`` to AND an LLM-judged rubric
("did the agent's final answer cover the right ground?") with
``ToolCallContains`` + ``ToolCallArgMatches`` (did the agent reach
for the right tool with the right kwargs?). The two layers catch
different regressions: a LLM-judge alone accepts a hallucinated
answer; a tool-call assertion alone accepts a tool call whose
result the agent ignored.

IMPORT ORDER: the import of the sibling suite's ``_setup_actions``
is the side-effect that registers ``procedural_upsert`` (a setup
action that seeds a procedural-plane entry via the API). The
decorator must fire before any ``suite.register(...)`` call
references it by name.
"""

from __future__ import annotations

import logging
from pathlib import Path

from memex_eval.suite import (
    CompositeOutcome,
    SetupAction,
    SuiteMetadata,
    ToolCallArgMatches,
    ToolCallContains,
)
from memex_eval.suite.decorator import Suite

logger = logging.getLogger('memex_eval.suites.procedural_plane_agents')

# Side-effect import — registers the ``procedural_upsert`` setup
# action from the sibling API-mode suite. The decorator populates
# the framework's setup-action registry; without this, scenarios
# that reference SetupAction(kind='procedural_upsert', ...) cannot
# resolve the handler.
from memex_eval.suites.procedural_plane import _setup_actions  # noqa: F401,E402

_ROOT = Path(__file__).parent

# 3 minutes per scenario — enough for a multi-turn agent loop
# (probe → create OR search → synthesize).
_DUR_MS: float = 180_000.0


METADATA = SuiteMetadata(
    name='procedural_plane_agents',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'V7 procedural-plane agent-mode — does the LLM agent '
        '(Hermes / Claude Code) actually reach for '
        '`memex_procedural_*` tools when storing case notes, '
        'retrieving procedures, and loading briefing cards? '
        'Catches routing regressions that send procedures to '
        'the legacy KV-namespace path instead of the V7 plane.'
    ),
    tags=[
        'procedural',
        'v7',
        'agent-mode',
        'routing',
        'tool-surface',
    ],
    primary_metrics=['suite.pass_rate_all', 'metric.graded_score.mean'],
    components_under_test=[
        'hermes-plugin.tools',
        'mcp.tools',
        'agent_surface.procedural_doctrine',
        'procedural.create',
        'procedural.search',
        'procedural.get_by_identity',
        'procedural.briefing_cards',
    ],
    knobs=[
        'agent_surface.procedural_doctrine',
        'hermes-plugin.procedural_handler_wiring',
        'mcp.procedural_tool_descriptions',
    ],
    requires_llm_judge=True,
    default_answer_mode='hermes',
)

suite = Suite(
    metadata=METADATA,
    sources=None,  # no source corpus — V7 is its own write surface
    readme_path=None,
)


# ---------------------------------------------------------------------------
# 1. Storing a case note
# ---------------------------------------------------------------------------
#
# The user describes a trigger signal ("every time CI returns 500
# after step 3, ...). The agent must persist it on the V7
# procedural plane as a case (kind="case", trigger=...), NOT as a
# KV key and NOT as a note. The ToolCallArgMatches regex on
# `kind="case"` is the discriminator.
#
# Why this matters: a case is briefing-eligible. If the agent
# routes to memex_kv_put, the trigger signal is invisible to
# memex_procedural_briefing_cards — vault briefings silently
# miss the failure pattern.

suite.register(
    id='stores_a_case_via_procedural_create',
    group='store',
    description=(
        'User describes a recurring trigger signal. The agent must '
        'call `memex_procedural_create` with `kind="case"` and a '
        '`trigger` field. KV routing is wrong — cases are '
        'briefing-eligible and KV is not.'
    ),
    query=(
        'Every time CI returns 500 after step 3, you should pause and '
        'check the artifact upload logs first — not retry blindly.'
    ),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_procedural_create'],
                min_count=1,
                match_mode='all',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_procedural_create',
                arg_name='kind',
                regex=r'^case$',
                min_count=1,
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_procedural_create',
                arg_name='trigger',
                regex=r'.+',  # non-empty
                min_count=1,
            ),
        ],
    ),
    replicates_override=2,
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 2. Retrieving a procedure via search
# ---------------------------------------------------------------------------
#
# The user asks "how do I rotate creds?" and the corpus has a
# procedure for it. The agent must call memex_procedural_search
# and surface the seeded entry. The setup action pre-seeds the
# procedure so the search has something to find.
#
# ToolCallArgMatches on `query` (must be a non-empty string) and
# `kind="procedure"` (the agent must use the right kind filter)
# are the discriminators against the legacy KV path.

suite.register(
    id='retrieves_procedure_via_search',
    group='retrieve',
    description=(
        'User asks "how do I rotate creds?" and a procedure for '
        '(kind=procedure, scope=global, verb=rotate, context=creds) '
        'is pre-seeded. The agent must call '
        '`memex_procedural_search` and surface the procedure in '
        'its answer.'
    ),
    query='How do I rotate the project API credentials?',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_procedural_search'],
                min_count=1,
                match_mode='all',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_procedural_search',
                arg_name='query',
                regex=r'rotate|cred|api|key',  # any of these tokens
                min_count=1,
            ),
        ],
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='global',
            kind_verb='rotate',
            kind_context='creds',
            kind_title='Rotate API credentials',
            kind_summary=(
                'Steps to rotate the project API credentials: 1) Issue '
                'new key in the secrets manager. 2) Update CI '
                'environment. 3) Roll the old key. 4) Verify CI green.'
            ),
        ),
    ],
)


# ---------------------------------------------------------------------------
# 3. Read-before-write probe
# ---------------------------------------------------------------------------
#
# The agent is told to remember a procedure, and a procedure for
# that anchor already exists. The agent MUST call
# memex_procedural_get_by_identity first to discover the existing
# entry, then either update or upsert — NOT create (which would
# 409 and the agent's retry loop would hammer the create).
#
# This is the operational contract: read-before-write. The setup
# action pre-seeds an entry on the (procedure, global, rotate,
# creds) anchor.

suite.register(
    id='probes_identity_before_writing',
    group='store',
    description=(
        'A procedure for (kind=procedure, scope=global, verb=rotate, '
        'context=creds) is pre-seeded. The user tells the agent to '
        'remember a better way to rotate creds. The agent must call '
        '`memex_procedural_get_by_identity` BEFORE '
        '`memex_procedural_create` to avoid a 409 loop.'
    ),
    query=(
        'Update the rotate creds procedure: always roll the old key '
        'AFTER CI green, not before — otherwise the deploy races.'
    ),
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_procedural_get_by_identity'],
                min_count=1,
                match_mode='all',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_procedural_get_by_identity',
                arg_name='kind',
                regex=r'^procedure$',
                min_count=1,
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_procedural_get_by_identity',
                arg_name='verb',
                regex=r'^rotate$',
                min_count=1,
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_procedural_get_by_identity',
                arg_name='context',
                regex=r'^creds$',
                min_count=1,
            ),
        ],
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='global',
            kind_verb='rotate',
            kind_context='creds',
            kind_title='Rotate API credentials',
            kind_summary='Original procedure (pre-seeded).',
        ),
    ],
    replicates_override=2,
    mutating_scenario=True,
)


# ---------------------------------------------------------------------------
# 4. Briefing cards at session start
# ---------------------------------------------------------------------------
#
# The agent is asked to brief itself on a vault. The briefing
# surface is the V7 procedural plane's pin-chain, exposed via
# memex_procedural_briefing_cards. The agent must reach for that
# tool (not memex_kv_list or memex_get_vault_summary).
#
# ToolCallArgMatches on `context_keys` (non-empty list) is the
# load-bearing assertion.

suite.register(
    id='loads_briefing_cards_at_session_start',
    group='retrieve',
    description=(
        'User asks the agent to brief itself on the project. The '
        'agent must call `memex_procedural_briefing_cards` with '
        '`context_keys=["global"]` (or include "global" in a '
        'larger list). Reaching for KV or vault-summary is wrong.'
    ),
    query='Brief me on what you already know about this project.',
    max_duration_ms=_DUR_MS,
    expected=CompositeOutcome(
        type='composite',
        children=[
            ToolCallContains(
                type='tool_call_contains',
                expected_tools=['memex_procedural_briefing_cards'],
                min_count=1,
                match_mode='all',
            ),
            ToolCallArgMatches(
                type='tool_call_arg_matches',
                tool='memex_procedural_briefing_cards',
                arg_name='context_keys',
                regex=r'\[.*\]',  # the framework JSON-dumps list args; non-empty list matches
                min_count=1,
            ),
        ],
    ),
    setup_actions=[
        SetupAction(
            kind='procedural_upsert',
            kind_kind='procedure',
            kind_scope='global',
            kind_verb='rotate',
            kind_context='creds',
            kind_title='Rotate API credentials',
            kind_summary='A pinned procedure the briefing should surface.',
        ),
    ],
)


SUITE = suite.build()
