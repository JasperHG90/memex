"""KV namespace parity test — post-2026-05-14 three-tier architecture.

Before 2026-05-14: every agent surface (MCP `instructions=`, hermes briefing,
Claude Code plugin rule, AGENTS.md) duplicated the KV namespace prefix list.
The test pinned all five prefixes across surfaces because drift had already
happened (the `procedure:` prefix was missing from `memex.md` once and no
test caught it).

After 2026-05-14 (three-tier agent-surface architecture):
- The KV namespace SSOT lives in ``memex_common.agent_surface.KV_NAMESPACE``
  (Tier 1b) and is surfaced via ``compose_universal()``.
- ``LAYER_ROUTING_PRIMER_TABLE`` (also Tier 1b) covers the
  ``procedure:`` prefix as part of its Procedural-observations row.
- The Claude Code plugin rule is intentionally trimmed (no local KV section);
  universal content arrives via the SessionStart hook in Phase 5.

This file pins the SSOT directly. Drift across surfaces is now impossible
because there is only one copy.
"""

from memex_common.agent_surface import (
    KV_NAMESPACE,
    LAYER_ROUTING_PRIMER_TABLE,
    compose_universal,
)


_CANONICAL_KV_PREFIXES = ('global:', 'user:', 'project:', 'app:', 'procedure:')


def test_procedure_prefix_in_layer_routing_table() -> None:
    """The ``procedure:`` namespace must appear in the canonical
    ``LAYER_ROUTING_PRIMER_TABLE`` (it's in the Procedural-observations row)."""
    assert 'procedure:' in LAYER_ROUTING_PRIMER_TABLE, (
        '`procedure:` prefix missing from LAYER_ROUTING_PRIMER_TABLE — '
        'the Procedural-observations row must include this namespace.'
    )


def test_kv_namespace_section_lists_all_prefixes() -> None:
    """The Tier 1b ``KV_NAMESPACE`` section must name every canonical prefix.

    This is the SSOT for the KV scope-qualifier rule; if a prefix is missing
    here it is missing everywhere downstream."""
    missing = [p for p in _CANONICAL_KV_PREFIXES if p not in KV_NAMESPACE]
    assert not missing, (
        f'`agent_surface.KV_NAMESPACE` missing canonical prefixes: {missing}. '
        'Every KV namespace prefix must appear in the SSOT.'
    )


def test_universal_block_surfaces_kv_namespace() -> None:
    """``compose_universal()`` must surface every KV prefix (this is what
    downstream agents see in the system prompt)."""
    text = compose_universal()
    missing = [p for p in _CANONICAL_KV_PREFIXES if p not in text]
    assert not missing, (
        f'`compose_universal()` does not surface KV prefixes: {missing}. '
        'Check that KV_NAMESPACE is included in the composition order.'
    )


def test_project_procedure_pattern_documented() -> None:
    """The project-scoped procedure pattern must appear in the SSOT and
    the universal block — otherwise agents won't know it exists.

    Without explicit documentation, agents fall back to either:
    - inventing keys under `project:<id>:<field>` (loses procedure envelope
      versioning), or
    - writing global procedures when the user clearly scoped to a project.
    """
    pattern = 'project:<id>:procedure:<verb>:<context-tag>'
    assert pattern in KV_NAMESPACE, (
        f'`agent_surface.KV_NAMESPACE` missing the project-scoped procedure '
        f'pattern: {pattern!r}. Add a row to the namespace table.'
    )
    text = compose_universal()
    assert pattern in text, (
        '`compose_universal()` does not surface the project-procedure pattern. '
        'Check that KV_NAMESPACE is included in the composition order.'
    )


def test_procedure_scope_default_directive_present() -> None:
    """The default-to-global directive must be present as a critical
    constraint, not buried in prose. Agents must default to global
    procedures and ASK before project-scoping on ambiguous input."""
    text = compose_universal()
    assert 'procedure_scope_default' in text, (
        '`procedure_scope_default` critical_constraint missing from compose_universal(). '
        'Without it, agents silently project-scope procedures by cwd / git remote / '
        'active vault.'
    )
