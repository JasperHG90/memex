"""Verbatim agent prompt text for summarize_node tool.

When the descriptions change, the verbatim test fails — that is the contract.
"""

from __future__ import annotations

MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION = (
    'memory_summarize_node — Trigger reflection SYNCHRONOUSLY on a specific entity or\n'
    'note set. This is the synchronous counterpart to the background reflect loop.\n'
    'Use when you notice mid-conversation that retrieved facts about a topic\n'
    'are conflicting, incomplete, or scattered, and you want Memex to consolidate them\n'
    'into a coherent mental model before continuing.\n'
    '\n'
    '- entity_id: focus reflection on a single entity (preferred for per-topic work)\n'
    '- note_ids: alternatively, focus on a specific set of notes\n'
    '- scope: "incremental" (default — only new evidence) or "full" (re-evaluate all,\n'
    '  capped at 1000 units)\n'
    '\n'
    'Returns a ReflectionResult with the updated/new MentalModel(s). Use sparingly;\n'
    'reflection is LLM-intensive. Default to background reflection unless you have a\n'
    'specific in-session reason to trigger now.\n'
    '\n'
    'Rate-limit per (entity, vault). On rejection the response carries a structured\n'
    '`retry_after_seconds` envelope — honor it rather than retry-looping.'
)
