"""Session briefing fetch.

The briefing is a token-budgeted markdown summary produced by Memex's
``/vaults/{vault_id}/session-briefing`` endpoint. We fetch it once per session,
in the background so ``initialize()`` returns quickly, and cache the result
for ``system_prompt_block()`` to consume.
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
        """Block up to ``timeout`` seconds for the briefing; return it or ''."""
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

The 5-step flow turns a vague "X is fixed" into precise paired writes:

1. **Disambiguate first.** If the scope is ambiguous — multiple candidate
   notes, multiple candidate topics, or a topic that may span notes — ASK
   before writing. Examples that warrant a clarification turn: "telegram
   issues from yesterday" with three Telegram-mentioning notes; "the auth
   bug we discussed" with no temporal anchor; "the issues are fixed" with
   multiple distinct issues conflated.

2. **Route by info quality.** Title-fragment known →
   `memex_find_note(query="...")` (indexed, cheap). Title unknown, content
   only → `memex_memory_search` (full pipeline, expensive but right). Then
   pick a coverage path:
   - **Option A — entity-anchored** (highest recall when topic ↔ entity):
     `memex_list_entities(query="...")` → `memex_get_entity_mentions(entity_id=...)`.
     Structural; no semantic-rank miss.
   - **Option B — cross-note semantic** (no entity anchor):
     `memex_memory_search(query="...", after="...", top_k=30)`. CRITICAL:
     `top_k` must be **≥30** — the default 5 is too narrow.
   - **Option C — single-note PageIndex traversal** (provably one note):
     `memex_get_page_indices(note_id)` → pick fix-relevant chunk_ids →
     `memex_get_memory_units(chunk_ids=[...])`. Captures every unit in the
     chunk; semantic top-k can miss paraphrased mentions.

3. **Mandatory LLM judgment over the candidate set.** READ the unit bodies
   and judge which ones actually correspond to the user's claim. Memory
   units are short (~1–3 sentences); reading does not blow up context. The
   judgment cannot be skipped — daily-reflection notes contain episodic
   observations ("worked on memex 3h today") that look superficially
   relevant but are not fix-targets. NEVER bulk-write against the raw
   candidate set.

4. **+5. Paired writes against the LLM-judged-relevant subset only.** For
   each unit the LLM judged relevant, issue BOTH writes:
   `memex_record_outcome(unit_ids=[...], success=false, reason="...")` AND
   `memex_memory_deprioritize(unit_id=..., reason="...")`. Same subset.

**Orthogonal axes (MW is the gradient; deprioritize is the binary):**

| Tool | Question it answers | Cardinality | Reversible? |
|------|---------------------|-------------|-------------|
| `memex_record_outcome(success=...)` | "Did this memory help when retrieved?" | Append-only counter | No (audit log) |
| `memex_memory_deprioritize(reason)` | "Should this surface by default at all?" | Binary state | Yes (memory_restore) |

User-confirmed-fix is BOTH signals at once. Don't collapse them into one
combined call — keep the primitives orthogonal.

**Imperfect recall is by design.** None of Options A/B/C give *provable*
100% recall. Exploration is the safety net — units that slip past
will re-surface, the user re-confirms, another `record_outcome(success=false)`
compounds the MW penalty. User-driven resolution is a GRADIENT across many
turns, not a one-shot delete.

**Historical / audit-query routing rule (separate path).** When the user
asks HOW THINGS CHANGED — "how has my view on X evolved", "what did I used
to think about Y", "show me everything I believed about Z including the
wrong stuff", or any of: "evolved", "used to", "history of", "what
changed", "audit", "show me everything", "show me the hidden ones" — DO
NOT use the resolution flow. Route differently:
- Ordered chain on a specific unit → `memex_get_unit_history(unit_id)`
  (graph walk through contradiction links, oldest → newest).
- Broader audit / "show me everything including hidden stuff" →
  `memex_memory_search(query="...", apply_pre_filter=False)` — bypasses
  MW + FSFM + confidence pre-filters so contradicted, behaviorally-failed,
  and decayed units appear. Post-reranker boosts still apply, so
  contradicted units rank below clean ones — correct for audit queries.

Disambiguation between resolution-flow and historical-routing is the
agent's responsibility."""


_LAYER_ROUTING_PRIMER = LAYER_ROUTING_PRIMER_TABLE


_STORAGE_MODEL_PRIMER = """### How Memex stores knowledge

Three layers:

- **Notes** — source markdown documents. `note_key` upsert creates new
  versions; old versions stay queryable. Use `memex_append_note` to extend an
  existing note instead of re-sending the whole body.
- **Memory units** — atomic facts/events extracted from notes at ingestion.
  **Append-only.** Contradiction detection runs at extraction time: it
  records typed links and lowers an older unit's confidence when a new
  note conflicts with it. Note supersession cascades to stale on its
  memory units. Don't try to edit, replace, or delete memory units — to
  record a change, add a new note via `memex_add_note`.
- **KV store** — namespaced operational state (preferences, project
  bindings, conventions). Mutable upsert by exact key; entries support
  TTL.

Reflection is a separate background loop that reads memory units and
synthesises **observations** about entities, bundled into versioned
per-entity **mental models** with trend tracking
(new/strengthening/stable/weakening/stale). Trends live on observations,
not on memory units. Reflection output is read-only — surface it via
search."""


_ROUTING_GUIDE = """### How to use Memex tools

Match the tool to the query type:

- **Vault scoping** — pass `vault_ids=["my-vault", "rituals"]` or `vault_ids=["*"]`
  for all vaults. Omit to use the session-bound vault. Do NOT use `tags` for
  vault filtering — `tags` filters note metadata (e.g. "meeting", "bug").
- **Vault discovery** → `memex_list_vaults()` to enumerate available vaults;
  `memex_get_vault_summary(vault_id="...")` for a precomputed narrative view
  of a vault's contents.
- **Title known** → `memex_find_note(query="title fragment")` for title lookups.
  Returns note IDs and match scores.
- **Content / document lookup** → call `memex_memory_search` AND `memex_note_search`
  in the same assistant message. memory_search returns distilled memory
  units; note_search returns source documents. Use both only when the query
  genuinely benefits — a simple title lookup doesn't.
- **Broad / panoramic** ("what do you know about X?", "overview of X") →
  start with `memex_get_vault_summary(vault_id="...")` — it's cheap and
  precomputed, and often answers the question on its own. Escalate to
  `memex_survey(query)` only if the summary is too coarse: survey
  decomposes into sub-questions and fans out in parallel, which is more
  thorough but much more expensive.
- **Relationships / entities** → `memex_list_entities` first, then
  `memex_get_entity_mentions` and/or `memex_get_entity_cooccurrences` with the
  returned entity_id. The latter two are safe to call in parallel if both are
  needed; otherwise pick the one that fits the question.
- **Batch fetch** — hydrate IDs from prior calls: `memex_get_entities(entity_ids=[...])`
  and `memex_get_memory_units(unit_ids=[...])` accept lists of UUIDs and return
  the batch. Faster than serial single-ID fetches.
- **Lineage / relationships** → `memex_get_memory_links(unit_ids=[...])` for typed
  links (temporal / semantic / causal / contradiction) between memory units;
  `memex_get_lineage(entity_type=..., entity_id=...)` for the provenance chain
  (note ↔ memory_unit ↔ observation ↔ mental_model).
- **KV store** → namespaced operational state — preferences, project
  bindings, conventions — via `memex_kv_write(value, key)` /
  `memex_kv_get(key)` / `memex_kv_search(query)` / `memex_kv_list()`. Keys
  MUST start with `global:`, `user:`, `project:<id>:`, `app:<id>:`, or
  `procedure:<verb>:<context-tag>` (RFC-007). The `procedure:` namespace is
  for compact, learned how-tos owned by the agent — write a procedure that
  worked using `memex_kv_write`, then read the active value with
  `memex_kv_get(key)` or pass `include_history=true` to inspect the
  envelope (active value + version + capped 5-version history). Memex
  tracks per-procedure success/failure counters server-side
  (procedure_outcomes table); on the MCP surface those counters are
  updated via `memex_record_outcome(target_type="kv_key", kv_key=..., success=...)`.
  Deletion is CLI-only (`memex kv delete`).
- **Capturing work**:
    - `memex_add_note` for a NEW note (or to fully overwrite an existing one).
      Pass a fresh note_key for a one-off capture.
    - `memex_append_note(note_key=..., delta=...)` to ADD progress to an existing
      note (the running session note, an ongoing reflection, a meeting log).
      Send only the new content — the server reads the existing body and
      concatenates atomically. Prefer this over re-`memex_add_note`-ing the
      whole body each turn.
- **Templates for structured captures** → `memex_list_templates` to see slugs,
  `memex_get_template(slug)` for the markdown scaffold, then `memex_add_note(...,
  template=slug)` so the note is tagged for filtering. Prefer a template for
  ADRs, retros, technical briefs, RFCs, or any note with clear sections.
- **Curating memory** — when a memory unit turns out to be misleading, outdated,
  or noise that contaminates retrieval:
    - `memex_memory_deprioritize(unit_id, reason=...)` is the NON-DESTRUCTIVE
      verb. The unit stays on the entity graph and remains recallable via
      `include_deprioritized=true`; only its retrieval rank drops. Pair with
      `memex_record_outcome(success=false, ...)` when the agent itself
      discovered the unit was wrong. Reversible via `memex_memory_restore`.
    - Archive (CLI-only) is the DESTRUCTIVE counterpart — it removes the unit
      from the entity graph and is irreversible. Prefer deprioritize unless
      the unit MUST leave the graph entirely (e.g. PII the user wants gone).
- **Synchronously consolidating mid-conversation** — when retrieved facts about
  a topic are conflicting, incomplete, or scattered, you can ask Memex to
  consolidate them BEFORE continuing:
    - `memex_memory_summarize_node(entity_id, scope='incremental'|'full')` is
      the SYNCHRONOUS counterpart to background `reflect`. `'incremental'`
      (default) consolidates only new evidence; `'full'` re-evaluates all
      evidence (capped at the most-recent 1000 units). Returns the updated
      mental model in the same turn so the agent can act on it.
    - Background `reflect` (scheduler-driven) is the cheaper default. Reach
      for `summarize_node` only with an in-session reason.
    - Rate-limited per (entity, vault). On rejection the response includes
      `retry_after_seconds`; do NOT retry-loop.
- **Reconsolidating versus consolidating** — two related but distinct
  curation verbs:
    - `memex_memory_reconsolidate(entity_id, vault_id)` is **ENTITY-SCOPED**.
      Use when you notice retrieved facts about a specific entity disagree.
      Runs contradiction detection across that entity's linked units, then
      reflection. Acquires a per-entity Postgres advisory lock — concurrent
      reconsolidations on the same entity serialise.
    - `memex_memory_consolidate(vault_id, dry_run)` is **VAULT-SCOPED**.
      Identifies low-MW + stale units across the entire vault and
      deprioritizes them, writing findings to the maintenance ledger. Use
      sparingly (e.g., monthly per vault). `dry_run=true` returns the
      candidate list as a preview without writes.
    - Reach for `reconsolidate` on concrete contradiction signals;
      `consolidate` is the periodic batch."""


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
    revisit_due_count: int | None = None,
) -> str:
    """Compose the Memex system-prompt block.

    Includes vault/project metadata, the session note key, routing guidance
    for tool selection, the fetched briefing markdown, and (optionally) the
    diagnostics summary block and procedural-observations block. If
    no vault is resolved, appends guidance on how to bind one via the KV
    store.
    """
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
        '`memex_append_note(note_key="...", delta="...")` with this key to add '
        'meaningful progress to the running session note — only the delta '
        'goes over the wire and the server concatenates atomically. '
        'Use `memex_add_note(note_key="...")` only for the FIRST capture or to '
        'fully replace the body; otherwise prefer append.'
    )

    if kv_instructions_if_no_vault:
        from .project import project_vault_kv_key

        lines.append(
            f'\nTo bind this project to a vault, set the KV key '
            f'`{project_vault_kv_key(project_id)}` to the vault name. Ask the '
            'user which vault to use.'
        )

    lines.append('\n' + _ROUTING_GUIDE)

    if procedural_observations:
        lines.append('\n' + _render_procedural_block(procedural_observations))

    if lint_pending_count is not None and lint_pending_count > 0:
        lines.append('\n' + _render_lint_block(lint_pending_count))

    if revisit_due_count is not None and revisit_due_count > 0:
        lines.append('\n' + _render_revisit_block(revisit_due_count))

    if diagnostics_summary:
        lines.append('\n' + _render_diagnostics_block(diagnostics_summary))

    if briefing:
        lines.append('\n---\n')
        lines.append(briefing)

    return '\n'.join(lines)


__all__ = ['BriefingCache', 'format_briefing_block']


# ============================================================
# Tier A — Briefing blocks
# F6:  pending lint count                 (WS-linter)
# F14: procedural observations            (WS-quick-wins)
# F20: N memories due for review          (WS-revisit)
# F32: diagnostic summary                 (WS-diagnostics)
# ============================================================


# --- F6 ---  (filled by WS-linter)
def _render_lint_block(pending_count: int) -> str:
    """Render the maintenance-ledger pending-count block.

    Surfaces the pending count from ``maintenance_proposals`` and points the
    operator at the CLI for triage. Read-only on the agent surface — the
    agent should NOT auto-resolve findings; deferring to a human is the
    intended default in v1.
    """
    return (
        f'### Maintenance findings\n'
        f'- {pending_count} pending lint findings. To inspect them, '
        f'run `memex lint findings`.'
    )


# --- F14 --- (filled by WS-quick-wins)
def _render_procedural_block(observations: list[dict[str, Any]]) -> str:
    """Render the procedural-observations block.

    ``observations`` is a list of ``{kv_key, success_co_count,
    failure_co_count, last_outcome_at}`` dicts (typically the top-N rows
    from ``procedure_outcomes`` for the active vault, sorted by
    ``last_outcome_at`` desc). The rendered block exposes each procedure's
    MW counters and an actionability cue per RFC-007 §155-185 — the agent
    is told to pair every procedure call with ``memex_record_outcome`` so
    the counters stay calibrated.
    """
    lines = ['### Learned procedures (recent)']
    if not observations:
        lines.append(
            '- No procedure keys recorded yet. When you discover a how-to that '
            'works, write it to a `procedure:<verb>:<context-tag>` key (e.g. '
            '`procedure:write_pr:commit-style`) and record the outcome.'
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
        'Read the active value with `memex_kv_get(key)`; pair every use with '
        '`memex_record_outcome(target_type="kv_key", kv_key=..., success=...)` '
        'so the counters reflect what actually worked. Use '
        '`memex_kv_get(key, include_history=true)` to see prior versions.'
    )
    return '\n'.join(lines)


# --- F20 --- (filled by WS-revisit)
def _render_revisit_block(due_count: int) -> str:
    """Render the revisitation pending-count block.

    Surfaces the count of memory units whose `revisit_due_at <= now()` AND
    that pass the 5-gate eligibility predicate. The agent learns about both
    verbs (READ + WRITE) so it can disambiguate list-vs-record intent.
    """
    if due_count <= 0:
        return (
            '### Memories due for review\n'
            '- No memories currently due for review. The FSRS-5 scheduler will '
            'surface units here as their stability-based due dates pass.'
        )
    return (
        f'### Memories due for review\n'
        f'- {due_count} memories due for review. Use '
        f'`memex_get_due_for_review()` to list them, then call '
        f'`memex_memory_review(unit_id, quality)` for each one you reviewed '
        f"(quality is one of 'again', 'hard', 'good', 'easy'). Five "
        f"consecutive 'again' ratings auto-flips a unit to deprioritized; "
        f'`memex memory restore` is the only way back.'
    )


# --- F32 --- (filled by WS-diagnostics)
def _render_diagnostics_block(summary: dict[str, Any]) -> str:
    """Render the diagnostics summary as a compact markdown block.

    Includes manifold_status, unit_counts, avg_mw_score, and top entity names —
    at least three documented fields.
    """
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
        f'- Manifold status: `{manifold_status}` · cluster_count: `{cluster_count}`',
        f'- Units: `{active}` active · `{stale}` stale · `{deprioritized}` deprioritized',
        f'- Avg MW score: `{avg_mw:.2f}`',
    ]
    if names:
        lines.append('- Top entities: ' + ', '.join(f'`{n}`' for n in names))
    lines.append(
        'Surface via `memex_get_diagnostics_summary(vault_id=...)` for details, '
        'or run `memex diagnostics manifold|retrieval|summary --vault X` for full JSON.'
    )
    return '\n'.join(lines)
