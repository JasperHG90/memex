"""Agent-surface SSOT discipline tests.

Pins token budgets, content presence, and determinism for the Tier 1b
universal system-prompt content exported from
``memex_common.agent_surface``.

See ``CLAUDE.md`` §"Agent-surface architecture" for the three-tier model.
"""

from __future__ import annotations

import pytest

from memex_common import agent_surface as ags


# ---------------------------------------------------------------------------
# Budget — keep the universal block compact. Empirical target ~1,000 tokens.
# Hard cap set higher than current size to allow modest growth without
# slowing the test loop on every PR; a sustained breach should prompt a
# review rather than auto-pass.
# ---------------------------------------------------------------------------


_UNIVERSAL_CHAR_CAP = 10_000  # ~2,860 tokens at 3.5 chars/token (empirical cl100k)
# Bumped 5,500 → 6,000 when CRITICAL_HEADER / VIRTUAL_UNIT / CRITICAL_FOOTER
# adopted `<critical_constraint name="…">` XML tags (Anthropic best practice:
# XML disambiguates load-bearing constraints; the model attends more reliably
# to named-block content than to bullet-list prose).
# Bumped 6,000 → 6,500 when KV_NAMESPACE added a kv_routing constraint + app
# / global / project examples — 4 of 5 KV scenarios failed in the post-fix
# eval because sonnet was using `Write` to local files instead of
# `memex_kv_put` and (when it did use KV) picking `user:` for app-scoped
# settings.
# Bumped 6,500 → 6,800 to strengthen two further triggers: "<app>" cue
# overrides "I"/"my" in KV namespace picking; "record it as a success" →
# ``memex_record_outcome`` on existing units, NEVER a fresh ``memex_add_note``.
# Bumped 6,800 → 8,200 when retrieval-routing and resolution-flow added
# explicit examples + wake words (KV: get / KV: search / Store in KV:),
# vault-survey examples, and the outcome-routing constraint with example +
# wrong-path counterexample. The 4 sections drove the claude-code eval from
# 0.36 → 0.94 pass rate; the +1.4K chars (cache hit) is justified.
# Bumped 8,200 → 8,800 when the deprio-leak fix expanded VIRTUAL_UNIT to
# describe the new HTTP 400 + ``source_memory_units`` contract (observations
# are read-only projections of MUs; deprio on an observation UUID is
# redirected to the underlying MU IDs). The expanded prose lands the
# behavioural contract directly in the universal surface rather than
# relying on the per-tool description alone.
# Bumped 8,800 → 9,100 when V5 slim-mode guidance landed in
# RETRIEVAL_ROUTING — list-shape browse tools (`memex_recent_notes`,
# `memex_list_notes`, `memex_list_entities`) accept `slim=True` to drop
# heavy per-row fields, keeping responses under Claude Code's hook-output
# cap on realistic vault sizes.
# Bumped 9,100 → 9,400 when KV_NAMESPACE added the project-scoped procedure
# pattern (`project:<id>:procedure:<verb>:<context>`) + the
# `procedure_scope_default` critical constraint. The directive prevents
# silent miscategorization: without it the agent auto-scopes procedures by
# cwd / git remote / active vault, producing project keys the user didn't
# ask for. The 300-char additions also condensed existing KV table cells
# and examples — net delta is ~250 chars, hence the modest cap bump.
# Bumped 9,600 → 9,800 when two `Store procedure: ...` wake-words were
# added to the KV-routing line. These bypass scope-inference reasoning
# and force the agent to emit the exact `<scope>:procedure:<verb>:<context>`
# key the user typed — the deterministic escape hatch when the routing
# rule's classification is unreliable.
# Bumped 9,400 → 9,600 when the procedure_scope_default constraint gained
# an imperatives-vs-preferences clause. Eval traces on glm-5.1 showed the
# agent writing `project:<id>:git:pr-only` and `project:<id>:package-manager`
# for imperative statements ("commits via PR", "use uv") — classifying them
# as preferences instead of procedures. The new clause pins
# "always/never/must/via/use Y not Z" as procedure-shaped and forces the
# `:procedure:` infix. ~190 chars; lifts pass rate on the project-scope
# scenarios from ~0% to a measurable signal.
# Bumped 9,800 → 10,000 when KV_NAMESPACE collapsed the two scope-specific
# procedure rows ("global default" + "project EXPLICIT cue") into one
# general row (`<scope>:procedure:<verb>:<context-tag>`), and added two
# examples covering `user:procedure:*` and `app:<id>:procedure:*` — Hermes
# review flagged that those scopes were valid in code but never modeled
# in the agent-facing surface, so the agent would never emit them even
# when the scope cue ("I" / "when I use <app>") was unambiguous.


def _approx_tokens(text: str) -> int:
    """~3.5 chars/token — empirical against tiktoken cl100k_base on this repo's markdown."""
    return (len(text) * 2 + 6) // 7


def test_compose_universal_within_budget() -> None:
    out = ags.compose_universal()
    assert len(out) <= _UNIVERSAL_CHAR_CAP, (
        f'compose_universal() is {len(out)} chars (~{_approx_tokens(out)} tokens), '
        f'exceeding cap {_UNIVERSAL_CHAR_CAP}. Either trim a section or '
        'lift the cap with rationale.'
    )


# ---------------------------------------------------------------------------
# Determinism — load-bearing for prompt-prefix cache hits.
# ---------------------------------------------------------------------------


def test_compose_universal_is_deterministic() -> None:
    """Calling compose_universal() twice must return byte-identical output.

    Per dbreunig's Claude Code cache-boundary analysis, the cacheable
    prompt prefix must produce identical bytes across turns/sessions —
    a single non-deterministic byte (timestamp, uuid, env probe) breaks
    every downstream cache hit.
    """
    a = ags.compose_universal()
    b = ags.compose_universal()
    assert a == b


# ---------------------------------------------------------------------------
# Content presence — load-bearing keywords that the agent must internalise.
# These are the constraints whose absence has historically caused eval
# regressions; pinning them here means an accidental trim fails CI.
# ---------------------------------------------------------------------------


_REQUIRED_KEYWORDS: tuple[str, ...] = (
    # V11 record_outcome contract — must be visible to every agent.
    'units=[{unit_id, verb, reason}]',
    'success=True',  # the bare-success rejection
    '400',
    # Read-only observations invariant (V21).
    'unit_metadata.virtual',
    'source_memory_units',
    # KV namespace prefixes.
    'user:',
    'project:<id>:',
    'global:',
    'app:<app-id>:',
    # KV holds preferences/settings/conventions; how-to WORKFLOWS go to
    # the procedural plane (memex_procedural_create), NOT KV `procedure:`
    # keys (the deprecated path). The kv_vs_procedural constraint pins
    # that boundary.
    'kv_vs_procedural',
    # KV scope-qualifier rule.
    'scope qualifier',
    # 5-step flow anchors.
    'Disambiguate',
    'top_k',
    # Citation discipline.
    'Cite',
    # Retrieval routing tool names.
    'memex_find_note',
    'memex_memory_search',
    'memex_note_search',
    'memex_get_vault_summary',
    'memex_survey',
    # Resolution-flow verbs.
    'memex_record_outcome',
    'memex_memory_deprioritize',
    # Search-query hygiene (promoted from CLAUDE_CODE_HARNESS to Tier 1b so
    # every Memex consumer — Hermes, MCP-hosted agents, Claude Code —
    # inherits the same query-formulation discipline).
    '<critical_constraint name="search-queries">',
    'NEVER as keyword lists',
    # V5 slim-mode guidance — pin so agents see the slim=True opt-in for
    # the three list-shape browse tools.
    'slim=True',
    'memex_recent_notes',
    'memex_list_notes',
)


@pytest.mark.parametrize('kw', _REQUIRED_KEYWORDS)
def test_compose_universal_carries_required_keyword(kw: str) -> None:
    out = ags.compose_universal()
    assert kw in out, (
        f'compose_universal() is missing required keyword {kw!r}. '
        'Either restore the section that carried it, or update '
        'this test if the constraint was deliberately dropped.'
    )


# ---------------------------------------------------------------------------
# Section constants — exist + non-empty.
# ---------------------------------------------------------------------------


_SECTION_CONSTANTS = (
    'CRITICAL_HEADER',
    'STORAGE_MODEL',
    'RETRIEVAL_ROUTING',
    'SEARCH_QUERIES',
    'RESOLUTION_FLOW',
    'AXES',
    'HISTORICAL_ROUTING',
    'VIRTUAL_UNIT',
    'KV_NAMESPACE',
    'CITATIONS',
    'CRITICAL_FOOTER',
)


@pytest.mark.parametrize('name', _SECTION_CONSTANTS)
def test_section_constant_exists_and_non_empty(name: str) -> None:
    val = getattr(ags, name)
    assert isinstance(val, str)
    assert val.strip(), f'{name} is empty'


def test_layer_routing_primer_still_exported() -> None:
    """The 4-layer routing primer is kept as a standalone export even
    though ``compose_universal()`` does not include it by default —
    agents that want the 4-layer table can append it explicitly."""
    assert ags.LAYER_ROUTING_PRIMER_TABLE
    assert ags.LAYER_ROUTING_PRIMER_FRAGMENT


# ---------------------------------------------------------------------------
# U-shaped composition — header AND footer carry the same 4 load-bearing
# constraints (primacy + recency). This makes the model see them at both
# ends, where attention is strongest.
# ---------------------------------------------------------------------------


def test_header_and_footer_both_mention_record_outcome_shape() -> None:
    assert 'units=' in ags.CRITICAL_HEADER
    assert 'units=' in ags.CRITICAL_FOOTER


def test_header_and_footer_both_mention_virtual_units() -> None:
    assert 'virtual' in ags.CRITICAL_HEADER
    assert 'virtual' in ags.CRITICAL_FOOTER


def test_header_and_footer_both_mention_kv_scope_rule() -> None:
    assert 'scope qualifier' in ags.CRITICAL_HEADER
    assert 'scope qualifier' in ags.CRITICAL_FOOTER


def test_header_and_footer_both_mention_citations() -> None:
    assert 'Cite' in ags.CRITICAL_HEADER or 'cite' in ags.CRITICAL_HEADER.lower()
    assert 'Cite' in ags.CRITICAL_FOOTER or 'cite' in ags.CRITICAL_FOOTER.lower()


def test_no_bare_metadata_virtual_attribute_path_anywhere() -> None:
    """Round-2 trip-wire: the virtual-unit attribute path is
    ``unit_metadata.virtual``, NOT ``metadata.virtual``. A bare
    ``metadata.virtual`` reappearing in any section would silently mislead
    agents into filtering by a non-existent attribute and let virtual units
    leak through to ``memex_memory_deprioritize`` (which then returns 404).

    The required-keyword test in this file pins ``unit_metadata.virtual``
    EXISTS, but ``'metadata.virtual' in s`` is True for both forms (the
    bug substring is a tail of the correct one). This trip-wire asserts
    every occurrence of ``metadata.virtual`` is prefixed by ``unit_``."""
    out = ags.compose_universal()
    # Remove every legitimate occurrence; what's left should be empty.
    residual = out.replace('unit_metadata.virtual', '')
    assert 'metadata.virtual' not in residual, (
        'Bare `metadata.virtual` (wrong attribute path) leaked into '
        '`compose_universal()` output. Use `unit_metadata.virtual`.'
    )


# ---------------------------------------------------------------------------
# V7 procedural-plane composition — opt-in addition to the universal block.
# Pin determinism, content presence, and the "additive not subtractive"
# contract: ``compose_with_procedural`` MUST contain everything
# ``compose_universal`` does (the universal block is the SSOT), plus the
# procedural-plane doctrine on top.
# ---------------------------------------------------------------------------


def test_compose_with_procedural_is_deterministic() -> None:
    """The V7 composition must be byte-equal across calls — the
    cacheable-prompt-prefix invariant still holds for the procedural
    variant. A regression that introduces a UUID/timestamp/env probe
    into PROCEDURAL_PLANE would surface here.
    """
    a = ags.compose_with_procedural()
    b = ags.compose_with_procedural()
    assert a == b


def test_compose_with_procedural_is_superset_of_universal() -> None:
    """Every byte in ``compose_universal()`` MUST appear in
    ``compose_with_procedural()`` (additive only — the universal block
    is the SSOT and the procedural variant appends on top, never
    replaces).

    A regression that "compacts" the V7 variant by reordering or
    shortening the universal block would break MCP tool routing for
    agents that consume the V7 surface.
    """
    universal = ags.compose_universal()
    with_procedural = ags.compose_with_procedural()
    assert universal in with_procedural, (
        '`compose_with_procedural()` is missing bytes from '
        '`compose_universal()`. The V7 variant must be purely additive.'
    )


def test_compose_with_procedural_includes_v7_doctrine() -> None:
    """Pin the load-bearing V7 routing rules. These are the strings
    that drive an agent to route ``"this is how to do X"`` write intents
    to ``memex_procedural_create`` instead of ``memex_add_note`` or
    ``memex_kv_put``. Their absence is a silent routing failure.
    """
    out = ags.compose_with_procedural()
    # The doctrine block's identity marker.
    assert '## Procedural plane' in out
    # The two procedural kinds — cases are NOTES, not plane entries.
    assert '`procedure`' in out
    assert '`strategy`' in out
    assert 'Cases are NOTES' in out
    # Strategy anchor: (scope, verb) only — context forbidden (§18.1).
    assert 'FORBIDDEN' in out
    # No user scope on the plane.
    assert 'no user scope' in out
    # The load-bearing retrieve-first behavior: face a known task →
    # search the plane BEFORE re-deriving the workflow.
    assert 'procedural_retrieve_first' in out
    assert 'memex_procedural_search' in out
    # The identity-anchor rule (UNIQUE on (kind, scope, verb, context)).
    assert '(kind, scope, verb, context)' in out
    # The scope/pin grammar.
    assert 'global' in out and 'project' in out and 'app' in out
    # Cases enter via case_submit, with explicit case_of preferred.
    assert 'memex_case_submit' in out
    assert 'case_of' in out
    # There is NO agent-facing briefing tool — cards arrive in the
    # session briefing (JG decision 2026-06-10).
    assert 'memex_procedural_briefing_cards' not in out
    assert 'session briefing' in out
    # Idempotent re-writes via upsert.
    assert 'memex_procedural_upsert' in out
    # Pre-flight probe to avoid 409.
    assert 'memex_procedural_get_by_identity' in out
    # Lifecycle — soft-deprecate.
    assert 'memex_procedural_deprecate' in out


def test_compose_universal_does_not_include_v7_doctrine() -> None:
    """The procedural block is opt-in — it MUST NOT bleed into
    ``compose_universal()``. Pre-V7 agents (no procedural tools)
    consuming the universal block would burn ~1,750 chars on routing
    rules they cannot act on. This is the "do not leak" trip-wire."""
    out = ags.compose_universal()
    assert '## Procedural plane' not in out
    assert 'memex_procedural_create' not in out
    assert 'memex_procedural_briefing_cards' not in out


def test_procedural_plane_constant_exists_and_non_empty() -> None:
    """``PROCEDURAL_PLANE`` is the SSOT block — directly importable for
    callers that want to compose it themselves (e.g., a custom surface
    builder that doesn't go through ``compose_with_procedural``)."""
    assert isinstance(ags.PROCEDURAL_PLANE, str)
    assert ags.PROCEDURAL_PLANE.strip()
    # The block must contain the doctrine markers; if any of these
    # disappear, the procedural plane is no longer routable.
    assert 'memex_procedural_create' in ags.PROCEDURAL_PLANE
    assert 'memex_procedural_upsert' in ags.PROCEDURAL_PLANE


def test_compose_with_procedural_exports_in_dunder_all() -> None:
    """``compose_with_procedural`` and ``PROCEDURAL_PLANE`` are public
    surface — they MUST appear in ``__all__`` so static importers see
    them. A regression that renames either function without updating
    the export list would silently break callers."""
    assert 'compose_with_procedural' in ags.__all__
    assert 'PROCEDURAL_PLANE' in ags.__all__
