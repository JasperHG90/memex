"""Verbatim agent prompt text for summarize_node tool.

When the descriptions change, the verbatim test fails — that is the contract.
"""

from __future__ import annotations

MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION = (
    'memory_summarize_node — Trigger reflection synchronously on a specific entity or\n'
    'note set. Use when you notice mid-conversation that retrieved facts about a topic\n'
    'are conflicting, incomplete, or scattered, and you want Memex to consolidate them\n'
    'into a coherent mental model before continuing.\n'
    '\n'
    '- entity_id: focus reflection on a single entity (preferred for per-topic work)\n'
    '- note_ids: alternatively, focus on a specific set of notes\n'
    '- scope: "incremental" (default — only new evidence) or "full" (re-evaluate all)\n'
    '\n'
    'Returns a ReflectionResult with the updated/new MentalModel(s). Use sparingly;\n'
    'reflection is LLM-intensive. Default to background reflection unless you have a\n'
    'specific in-session reason to trigger now.\n'
    '\n'
    'Error envelopes:\n'
    '- {"error":"rate_limit_exceeded","retry_after_seconds":N,...}: per-entity bucket\n'
    '  empty; wait N seconds.\n'
    '- {"error":"reflection_abandoned","retry_after_seconds":N,"hint":...}: a concurrent\n'
    '  worker just refreshed the model. The fresh state is already persisted — prefer\n'
    '  re-reading via memex_get_entity / memex_memory_search rather than retrying.'
)
