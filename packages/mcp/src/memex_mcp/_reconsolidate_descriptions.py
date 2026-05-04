"""Verbatim agent prompt text for reconsolidate/consolidate tools.

Sourced from cognitive-memory-research-report.md §4 reconsolidate step 6 (lines
758-775 as of 2026-05-01 against memory_augmentation @ 1c0a464). When the
spec changes, the verbatim test (TC9) fails — that is the contract.
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
    'is concrete evidence of conflicting information.'
)

MEMEX_MEMORY_CONSOLIDATE_DESCRIPTION = (
    'memory_consolidate — Vault-wide batch curation. Identifies low-MW + stale units\n'
    'and deprioritizes them. Writes findings to the maintenance ledger.\n'
    '\n'
    '- vault_id: target vault\n'
    '- dry_run (default false): if true, preview without making changes\n'
    '\n'
    'Use sparingly (e.g., monthly per vault). For per-entity hygiene, prefer\n'
    'memory_reconsolidate.'
)
