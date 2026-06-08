"""Vault kind + per-vault synthesis policy.

A vault's ``kind`` governs the synthesis-and-discovery surfaces (reflection,
vault summary, wildcard search, listing, briefing), never the retention floor
(extraction always runs). ``content`` vaults default both synthesis behaviours
on; ``system`` vaults default them off. The per-vault ``policy`` blob overrides
those defaults field by field.
"""

from __future__ import annotations

import enum
import logging
import warnings

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# Forward ref: VaultKind is defined below. Populated after the class body
# runs so the frozenset can carry the enum values. Use the literal strings
# directly so this module has no top-level circular dep on the class object.
_KNOWN_KINDS: frozenset[str] = frozenset({'content', 'system'})


class VaultKind(enum.StrEnum):
    """Behavioural classifier for a vault — corpus vs infrastructure."""

    CONTENT = 'content'
    SYSTEM = 'system'


# Belt-and-braces: the frozenset above is a literal so the module loads even
# if VaultKind ever changes name. This assert forces a future rename to
# update both sites in one diff. The sync between VaultKind and the
# DB CHECK constraint ``vaults_kind_check`` is enforced at migration time.
assert {k.value for k in VaultKind} == set(_KNOWN_KINDS), (
    '_KNOWN_KINDS and VaultKind disagree; update both before renaming a kind'
)


class VaultPolicy(BaseModel):
    """Per-vault override of the kind-derived synthesis defaults.

    A field left ``None`` defers to the kind default. Unknown keys are rejected
    so typos surface instead of silently falling back.
    """

    model_config = ConfigDict(extra='forbid')

    reflect: bool | None = None
    summarize: bool | None = None


def coerce_policy(policy: VaultPolicy | dict | None) -> VaultPolicy:
    """Normalise a raw blob / model / None into a validated ``VaultPolicy``."""
    if policy is None:
        return VaultPolicy()
    if isinstance(policy, VaultPolicy):
        return policy
    if isinstance(policy, dict):
        return VaultPolicy.model_validate(policy)
    raise TypeError(f'policy must be VaultPolicy | dict | None, got {type(policy)!r}')


def _kind_value(kind: VaultKind | str) -> str:
    return kind.value if isinstance(kind, enum.Enum) else str(kind)


def is_system(kind: VaultKind | str) -> bool:
    """Whether ``kind`` classifies a vault as system (synthesis silent).

    Fail-open on unknown values: a bad/missing kind string is treated as
    *not system* (i.e. content-like) so a corrupt row stays visible on
    browse surfaces rather than being silently muted. An ``UnknownVaultKind``
    warning is emitted so the issue surfaces in logs. The DB CHECK and the
    ``CreateVaultRequest`` Pydantic validator are the load-bearing guards
    in practice; this branch only fires on a direct DB write or a
    post-migration corruption.
    """
    value = _kind_value(kind)
    if value not in _KNOWN_KINDS:
        warnings.warn(
            f'Unknown vault kind {value!r}; treating as content. '
            'The DB CHECK + CreateVaultRequest validator should have caught this.',
            UnknownVaultKind,
            stacklevel=2,
        )
        return False
    return value == VaultKind.SYSTEM.value


def is_content(kind: VaultKind | str) -> bool:
    """Whether ``kind`` classifies a vault as content (synthesis default-on)."""
    value = _kind_value(kind)
    if value not in _KNOWN_KINDS:
        warnings.warn(
            f'Unknown vault kind {value!r}; treating as content. '
            'The DB CHECK + CreateVaultRequest validator should have caught this.',
            UnknownVaultKind,
            stacklevel=2,
        )
        return True
    return value == VaultKind.CONTENT.value


def reflect_enabled(kind: VaultKind | str, policy: VaultPolicy | dict | None = None) -> bool:
    """Whether per-entity reflection should run for a vault of this kind+policy."""
    resolved = coerce_policy(policy)
    if resolved.reflect is not None:
        return resolved.reflect
    return is_content(kind)


def summarize_enabled(kind: VaultKind | str, policy: VaultPolicy | dict | None = None) -> bool:
    """Whether vault-summary generation should run for a vault of this kind+policy."""
    resolved = coerce_policy(policy)
    if resolved.summarize is not None:
        return resolved.summarize
    return is_content(kind)


class UnknownVaultKind(UserWarning):
    """Raised (via warnings.warn) when a kind string is not in VaultKind.

    The default behaviour is fail-open (treat as content); this warning
    exists so a corrupt row surfaces in logs without crashing the call.
    """
