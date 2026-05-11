"""Session briefing fetch.

Token-budgeted markdown summary from Memex's session-briefing endpoint.
Fetched once per session in the background; cached for system_prompt_block().
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from uuid import UUID

from memex_common.agent_surface import LAYER_ROUTING_PRIMER_TABLE

from .async_bridge import run_sync

logger = logging.getLogger(__name__)


class BriefingCache:
    """Thread-safe cache with a single in-flight fetch."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._result: str = ''
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    def start_fetch(
        self,
        api: Any,
        vault_id: UUID,
        budget: int,
        project_id: str | None,
    ) -> None:
        """Fire the background briefing fetch. Safe to call multiple times."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._result = ''
            self._error = None

            def _run() -> None:
                try:
                    text = run_sync(
                        api.get_session_briefing(
                            vault_id=vault_id,
                            budget=budget,
                            project_id=project_id,
                        ),
                        timeout=30.0,
                    )
                    with self._lock:
                        self._result = text or ''
                except Exception as e:
                    logger.debug('Briefing fetch failed: %s', e)
                    with self._lock:
                        self._error = str(e)
                finally:
                    self._ready.set()

            self._thread = threading.Thread(
                target=_run,
                daemon=True,
                name='memex-briefing',
            )
            self._thread.start()

    def get(self, timeout: float = 5.0) -> str:
        """Block up to timeout seconds for the briefing; return it or ''."""
        if not self._ready.wait(timeout=timeout):
            return ''
        with self._lock:
            return self._result

    def get_error(self) -> str | None:
        with self._lock:
            return self._error

    def reset(self) -> None:
        """Clear the cached result. For session refresh or tests."""
        with self._lock:
            self._result = ''
            self._error = None
            self._ready.clear()


_RESOLUTION_FLOW_PRIMER = """### When the user reports an issue resolved (§3.5 5-step flow)

1. **Disambiguate first.** If scope is ambiguous — multiple candidate notes, no temporal anchor, or conflated issues — ASK before writing.

2. **Route by info quality.** Title-fragment → `memex_find_note(query="...")`. Title unknown → `memex_memory_search`. Then pick a coverage path:
   - **A: entity-anchored**: `memex_list_entities` → `memex_get_entity_mentions`. Structural; no rank miss.
   - **B: cross-note semantic**: `memex_memory_search(top_k=30)`. CRITICAL: `top_k` ≥ 30.
   - **C: single-note PageIndex**: `memex_get_page_indices` → `memex_get_memory_units(chunk_ids=[...])`.

3. **LLM-judge the candidates.** READ unit bodies; pick the fix-relevant subset. NEVER bulk-write.

4.+5. **Paired writes** against the judged-relevant subset:
   `memex_record_outcome(units=[{"unit_id":"...","verb":"not_helpful","reason":"..."}])` AND
   `memex_memory_deprioritize(unit_id=..., reason="...")`. Same subset.

   Orthogonal axes: `record_outcome` = MW gradient (append-only); `deprioritize` = binary surface state (reversible via `memory_restore`).

**Imperfect recall is by design** — exploration is the safety net. Resolution is a GRADIENT across turns, not one-shot delete.

**Historical / audit-query routing (separate path).** Triggers: "evolved", "used to", "history of", "what changed", "audit", "show me everything/hidden".
- Specific unit → `memex_get_unit_history(unit_id)`.
- Broad audit → `memex_memory_search(apply_pre_filter=False)`. Bypasses MW/FSFM/confidence filters; reranker boosts still apply."""


_LAYER_ROUTING_PRIMER = LAYER_ROUTING_PRIMER_TABLE


_STORAGE_MODEL_PRIMER = """### How Memex stores knowledge

Three layers:
- **Notes** — source markdown. `note_key` upserts new versions; old stay queryable. Use `memex_append_note` to extend; `memex_add_note` for first capture or full replace.
- **Memory units** — atomic facts/events from ingestion. **Append-only.** Contradiction detection runs at extraction time; note supersession cascades to stale. Don't edit/replace/delete units — add a new note to record a change.
- **KV store** — namespaced operational state. Mutable upsert by exact key; entries support TTL.

Reflection is a background loop synthesising observations into per-entity **mental models** with trend tracking (new/strengthening/stable/weakening/stale). Read-only — surface via search."""


_ROUTING_GUIDE = """### How to use Memex tools

- **Vault scoping** — `vault_ids=["my-vault"]` or `vault_ids=["*"]` for all. Omit for session-bound vault. `tags` filters note metadata, NOT vaults.
- **Vault discovery** → `memex_list_vaults()` / `memex_get_vault_summary(vault_id="...")`.
- **Title known** → `memex_find_note(query="fragment")`.
- **Content lookup** → `memex_memory_search` AND `memex_note_search` in parallel. Use both only when genuinely needed.
- **Broad/panoramic** → `memex_get_vault_summary` first (cheap, precomputed). Escalate to `memex_survey(query)` only if too coarse.
- **Entities** → `memex_list_entities` → `memex_get_entity_mentions` / `memex_get_entity_cooccurrences`.
- **Batch fetch** → `memex_get_entities(entity_ids=[...])` / `memex_get_memory_units(unit_ids=[...])`.
- **Lineage** → `memex_get_memory_links(unit_ids=[...])` for typed links; `memex_get_lineage(entity_type=..., entity_id=...)` for provenance chains.
- **KV store** — `memex_kv_write(value, key)` / `memex_kv_get(key)` / `memex_kv_search(query)` / `memex_kv_list()`. Keys MUST start with `global:`, `user:`, `project:<id>:`, `app:<id>:`, or `procedure:<verb>:<context-tag>`. The `procedure:` namespace stores learned how-tos: write via `memex_kv_write`, read active value via `memex_kv_get(key)`, inspect envelope via `memex_kv_get(key, include_history=true)`. Track outcomes via `memex_record_outcome(target_type="kv_key", kv_key=..., success=...)`. Deletion is CLI-only (`memex kv delete`).
- **Capturing work** — `memex_add_note` for NEW/replace; `memex_append_note(note_key=..., delta=...)` to extend existing. Prefer append over re-ingesting the whole body.
- **Templates** → `memex_list_templates` for slugs; `memex_get_template(slug)` for the scaffold; `memex_add_note(..., template=slug)` for structured captures.
- **Curating memory** — `memex_memory_deprioritize(unit_id, reason=...)` is NON-DESTRUCTIVE (unit stays on graph, recallable via `include_deprioritized=true`; rank drops). Pair with `memex_record_outcome(units=[{verb:"not_helpful", reason:"..."}])` when the agent found it wrong. Reversible via `memex_memory_restore`. Archive (CLI-only) is DESTRUCTIVE — prefer deprioritize unless PII removal is required.
- **Synchronous consolidation** — `memex_memory_summarize_node(entity_id, scope='incremental'|'full')` when in-session facts conflict or scatter. `'incremental'` (default) consolidates new evidence only; `'full'` re-evaluates all (capped 1000 units). Rate-limited per (entity, vault); on rejection, response includes `retry_after_seconds`.
- **Reconsolidate vs consolidate** — `memex_memory_reconsolidate(entity_id, vault_id)` is entity-scoped (contradiction detection + reflection); `memex_memory_consolidate(vault_id, dry_run)` is vault-scoped (batch deprioritizes low-MW + stale units, writes to maintenance ledger). Use `reconsolidate` on concrete contradiction signals; `consolidate` for periodic maintenance."""


def format_briefing_block(
    briefing: str,
    *,
    vault_id: str | None,
    project_id: str,
    session_note_key: str,
    kv_instructions_if_no_vault: bool,
    diagnostics_summary: dict[str, Any] | None = None,
    procedural_observations: list[dict[str, Any]] | None = None,
    lint_pending_count: int | None = None,
) -> str:
    """Compose the Memex system-prompt block."""
    lines = ['## Memex Memory']
    if vault_id:
        lines.append(f'Active vault: `{vault_id}` · Project: `{project_id}`')
    else:
        lines.append(f'Project: `{project_id}` · **No vault bound to this project.**')

    lines.append('\n' + _STORAGE_MODEL_PRIMER)
    lines.append('\n' + _LAYER_ROUTING_PRIMER)
    lines.append('\n' + _RESOLUTION_FLOW_PRIMER)

    lines.append(
        f'\nSession note key: `{session_note_key}`. Use '
        '`memex_append_note(note_key="...", delta="...")` to extend; '
        '`memex_add_note(note_key="...")` only for first capture or full replace.'
    )

    if kv_instructions_if_no_vault:
        from .project import project_vault_kv_key

        lines.append(
            f'\nTo bind this project to a vault, set `{project_vault_kv_key(project_id)}` to the vault name. Ask the user which vault to use.'
        )

    lines.append('\n' + _ROUTING_GUIDE)

    if procedural_observations:
        lines.append('\n' + _render_procedural_block(procedural_observations))

    if lint_pending_count is not None and lint_pending_count > 0:
        lines.append('\n' + _render_lint_block(lint_pending_count))

    if diagnostics_summary:
        lines.append('\n' + _render_diagnostics_block(diagnostics_summary))

    if briefing:
        lines.append('\n---\n')
        lines.append(briefing)

    return '\n'.join(lines)


__all__ = ['BriefingCache', 'format_briefing_block']


# ============================================================
# Tier A — Briefing blocks
# ============================================================


def _render_lint_block(pending_count: int) -> str:
    return (
        f'### Maintenance findings\n'
        f'- {pending_count} pending lint findings. Inspect with `memex lint findings`.'
    )


def _render_procedural_block(observations: list[dict[str, Any]]) -> str:
    lines = ['### Learned procedures (recent)']
    if not observations:
        lines.append(
            '- No procedure keys recorded yet. Write to `procedure:<verb>:<context-tag>` '
            'keys and record outcomes.'
        )
        return '\n'.join(lines)

    for obs in observations[:5]:
        key = obs.get('kv_key', '?')
        succ = int(obs.get('success_co_count', 0))
        fail = int(obs.get('failure_co_count', 0))
        last = obs.get('last_outcome_at')
        last_str = f' · last: {last}' if last else ''
        lines.append(f'- `{key}` — {succ} success / {fail} failure{last_str}')

    lines.append(
        'Read active value with `memex_kv_get(key)`; pair every use with '
        '`memex_record_outcome(target_type="kv_key", kv_key=..., success=...)`. '
        'Inspect envelope with `memex_kv_get(key, include_history=true)`.'
    )
    return '\n'.join(lines)


def _render_diagnostics_block(summary: dict[str, Any]) -> str:
    counts = summary.get('unit_counts') or {}
    active = counts.get('active', 0)
    stale = counts.get('stale', 0)
    deprioritized = counts.get('deprioritized', 0)
    manifold_status = summary.get('manifold_status', 'absent')
    avg_mw = summary.get('avg_mw_score', 0.0)
    cluster_count = summary.get('cluster_count')
    top_entities = summary.get('top_5_retrieved_entities') or []
    names = [e.get('name', '?') for e in top_entities[:5]]

    lines = [
        '### Memex diagnostics',
        f'- Manifold: `{manifold_status}` · clusters: `{cluster_count}`',
        f'- Units: `{active}` active · `{stale}` stale · `{deprioritized}` deprioritized',
        f'- Avg MW: `{avg_mw:.2f}`',
    ]
    if names:
        lines.append('- Top entities: ' + ', '.join(f'`{n}`' for n in names))
    lines.append(
        'Details: `memex_get_diagnostics_summary(vault_id=...)` or `memex diagnostics manifold|retrieval|summary --vault X`.'
    )
    return '\n'.join(lines)
