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


def test_kv_does_not_carry_procedure_write_routing() -> None:
    """How-to procedures live on the procedural plane (derived from cases
    via ``memex_case_submit``), NOT in KV ``<scope>:procedure:*`` keys (the
    deprecated path). The KV namespace block must NOT route procedure
    WRITES, and must pin the kv_vs_procedural boundary.
    """
    # The deprecated KV-procedure write pattern must be gone from the
    # namespace routing table.
    assert 'project:<id>:procedure:<verb>:<context-tag>' not in KV_NAMESPACE, (
        'KV_NAMESPACE still routes procedure WRITES to a `procedure:` key — '
        'that path is deprecated; how-tos go to the procedural plane.'
    )
    # And the boundary constraint must be present.
    assert 'kv_vs_procedural' in KV_NAMESPACE, (
        'KV_NAMESPACE missing the kv_vs_procedural constraint that draws the '
        'KV-convention vs procedural-plane line.'
    )


def test_how_to_routing_goes_to_the_plane_not_kv() -> None:
    """The old ``procedure_scope_default`` KV directive is gone —
    procedures are no longer a KV write path. compose_universal() must
    instead carry the kv_vs_procedural boundary so agents send how-to
    workflows to the procedural plane, not a KV ``procedure:`` key.
    """
    text = compose_universal()
    assert 'procedure_scope_default' not in text, (
        'the deprecated `procedure_scope_default` KV directive is still present; '
        'procedures route to the plane now, not KV.'
    )
    assert 'kv_vs_procedural' in text, (
        '`kv_vs_procedural` constraint missing from compose_universal(); without '
        'it agents may still write how-tos to KV procedure keys.'
    )
