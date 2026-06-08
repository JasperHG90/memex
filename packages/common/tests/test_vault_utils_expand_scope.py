"""Unit tests for ``memex_common.vault_utils.expand_vault_scope``.

Single source of truth for read-scope expansion. Both the service-layer
``VaultService.resolve_vault_scope`` and the MCP ``_resolve_vault_ids``
delegate to this helper, so the cases here are the cases both
implementations must satisfy.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from memex_common.vault_utils import expand_vault_scope

C1, C2 = uuid4(), uuid4()
S1, S2 = uuid4(), uuid4()
N1 = uuid4()


@pytest.mark.parametrize(
    'named,content,system,has_wildcard,include_sys,expected',
    [
        # Wildcard → all content
        ([], [C1, C2], [S1, S2], True, False, [C1, C2]),
        # Empty named + wildcard flag → all content
        ([], [C1, C2], [S1, S2], True, False, [C1, C2]),
        # Default scope (no named, no wildcard) → all content
        ([], [C1, C2], [S1, S2], False, False, [C1, C2]),
        # Named only → just named (no expansion)
        ([N1], [C1, C2], [S1, S2], False, False, [N1]),
        # Named + system opt-in → unconditional union of system
        ([N1], [C1, C2], [S1, S2], False, True, [N1, S1, S2]),
        # Wildcard + system opt-in → all content + all system
        ([], [C1, C2], [S1, S2], True, True, [C1, C2, S1, S2]),
        # Named + wildcard + system → all three sets, dedup
        ([N1], [C1, C2], [S1, S2], True, True, [N1, C1, C2, S1, S2]),
        # Dedup across named and content (rare but legal)
        # Note: when named_ids is non-empty AND has_wildcard=False, the
        # content branch is skipped, so [C1] (just the named) is the
        # result, not [C1, C2]. The named+wildcard case is what
        # triggers content expansion.
        ([C1], [C1, C2], [], False, False, [C1]),
    ],
)
def test_expand_vault_scope(named, content, system, has_wildcard, include_sys, expected):
    result = expand_vault_scope(
        named,
        content,
        system,
        has_wildcard=has_wildcard,
        include_system_vaults=include_sys,
    )
    assert result == expected


def test_expand_vault_scope_order_preserved():
    """Named ids come first, then content (in input order), then system.

    Note: content is only added when has_wildcard OR named is empty.
    With one named id and no wildcard, content is skipped.
    """
    # No named, wildcard → content + (system if opt-in)
    result = expand_vault_scope(
        [],
        [C1, C2],
        [S1, S2],
        has_wildcard=True,
        include_system_vaults=True,
    )
    assert result == [C1, C2, S1, S2]

    # Named only + system opt-in: named first, then system
    result = expand_vault_scope(
        [N1],
        [C1, C2],
        [S1, S2],
        has_wildcard=False,
        include_system_vaults=True,
    )
    assert result == [N1, S1, S2]

    # Wildcard + named: dedup keeps first occurrence
    result = expand_vault_scope(
        [N1],
        [C1, C2],
        [S1, S2],
        has_wildcard=True,
        include_system_vaults=True,
    )
    assert result == [N1, C1, C2, S1, S2]


def test_expand_vault_scope_empty_everything():
    """No named, no content, no system → empty list (not an error)."""
    result = expand_vault_scope([], [], [], has_wildcard=False, include_system_vaults=False)
    assert result == []
