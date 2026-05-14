"""Verbatim agent prompt text for reconsolidate/consolidate tools.

When the descriptions change, the verbatim test fails — that is the contract.
"""

from __future__ import annotations

MEMEX_MEMORY_RECONSOLIDATE_DESCRIPTION = (
    'memory_reconsolidate — Re-evaluate memories for a specific entity, detecting\n'
    'contradictions and updating mental models. Use when you notice retrieved facts\n'
    'about an entity disagree.\n'
    '\n'
    '- entity_id: target entity\n'
    '\n'
    'Runs contradiction detection across all units linked to the entity, then triggers\n'
    'reflection to produce updated mental models. LLM-intensive — use only when there\n'
    'is concrete evidence of conflicting information.\n'
    '\n'
    'Returns ``abandoned: true`` when a concurrent worker refreshed the mental model\n'
    'first; the fresh state is already persisted — prefer re-reading via\n'
    'memex_get_entity / memex_memory_search rather than retrying reconsolidate.'
)

MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION = (
    'memory_consolidate — Vault-wide batch curation. Identifies low-Memory-Worth + stale units\n'
    'and deprioritizes them. Writes findings to the maintenance ledger.\n'
    '\n'
    '- vault_id: target vault\n'
    '- dry_run (default false): if true, preview without making changes\n'
    '\n'
    'Use sparingly (e.g., monthly per vault). For per-entity hygiene, prefer\n'
    'memory_reconsolidate.'
)
