"""Vault kind + per-vault synthesis policy.

A vault's ``kind`` governs the synthesis-and-discovery surfaces (reflection,
vault summary, wildcard search, listing, briefing), never the retention floor
(extraction always runs). ``content`` vaults default both synthesis behaviours
on; ``system`` vaults default them off. The per-vault ``policy`` blob overrides
those defaults field by field.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict


class VaultKind(enum.StrEnum):
    """Behavioural classifier for a vault — corpus vs infrastructure."""

    CONTENT = 'content'
    SYSTEM = 'system'


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


def _is_content(kind: VaultKind | str) -> bool:
    value = kind.value if isinstance(kind, enum.Enum) else str(kind)
    return value == VaultKind.CONTENT.value


def is_system(kind: VaultKind | str) -> bool:
    return not _is_content(kind)


def reflect_enabled(kind: VaultKind | str, policy: VaultPolicy | dict | None = None) -> bool:
    """Whether per-entity reflection should run for a vault of this kind+policy."""
    resolved = coerce_policy(policy)
    if resolved.reflect is not None:
        return resolved.reflect
    return _is_content(kind)


def summarize_enabled(kind: VaultKind | str, policy: VaultPolicy | dict | None = None) -> bool:
    """Whether vault-summary generation should run for a vault of this kind+policy."""
    resolved = coerce_policy(policy)
    if resolved.summarize is not None:
        return resolved.summarize
    return _is_content(kind)
