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


_ROUTING_GUIDE = """### How to use Memex tools

Match the tool to the query type:

- **Title known** → `memex_find_note(query="title fragment")` for title lookups.
  Returns note IDs and match scores.
- **Vault scoping** — pass `vault_ids=["my-vault", "rituals"]` or `vault_ids=["*"]`
  for all vaults. Omit to use the session-bound vault. Do NOT use `tags` for
  vault filtering — `tags` filters note metadata (e.g. "meeting", "bug").
- **Vault discovery** → `memex_list_vaults()` to enumerate available vaults;
  `memex_get_vault_summary(vault_id="...")` for a precomputed narrative view
  of a vault's contents.
- **Content / document lookup** → call `memex_recall` AND `memex_retrieve_notes`
  in the same assistant message. Recall returns distilled facts; retrieve_notes
  returns source documents. Use both only when the query genuinely benefits —
  a simple title lookup doesn't.
- **Broad / panoramic** ("what do you know about X?", "overview of X") →
  `memex_survey(query)` as a single call. The server decomposes into
  sub-questions and fans out in parallel.
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
- **KV store** → persist facts and preferences across sessions with
  `memex_kv_write(value, key)` / `memex_kv_get(key)` / `memex_kv_search(query)`
  / `memex_kv_list()`. Keys MUST start with a namespace: `global:`, `user:`,
  `project:<id>:`, or `app:<id>:`. Deletion is CLI-only (use `memex kv delete`).
- **Capturing work**:
    - `memex_retain` for a NEW note (or to fully overwrite an existing one).
      Pass a fresh note_key for a one-off capture.
    - `memex_append(note_key=..., delta=...)` to ADD progress to an existing
      note (the running session note, an ongoing reflection, a meeting log).
      Send only the new content — the server reads the existing body and
      concatenates atomically. Prefer this over re-`memex_retain`-ing the
      whole body each turn.
- **Templates for structured captures** → `memex_list_templates` to see slugs,
  `memex_get_template(slug)` for the markdown scaffold, then `memex_retain(...,
  template=slug)` so the note is tagged for filtering. Prefer a template for
  ADRs, retros, technical briefs, RFCs, or any note with clear sections."""


def format_briefing_block(
    briefing: str,
    *,
    vault_id: str | None,
    project_id: str,
    session_note_key: str,
    kv_instructions_if_no_vault: bool,
) -> str:
    """Compose the Memex system-prompt block.

    Includes vault/project metadata, the session note key, routing guidance
    for tool selection, and the fetched briefing markdown. If no vault is
    resolved, appends guidance on how to bind one via the KV store.
    """
    lines = ['## Memex Memory']
    if vault_id:
        lines.append(f'Active vault: `{vault_id}` · Project: `{project_id}`')
    else:
        lines.append(f'Project: `{project_id}` · **No vault bound to this project.**')

    lines.append(
        f'\nSession note key: `{session_note_key}`. Use '
        '`memex_append(note_key="...", delta="...")` with this key to add '
        'meaningful progress to the running session note — only the delta '
        'goes over the wire and the server concatenates atomically. '
        'Use `memex_retain(note_key="...")` only for the FIRST capture or to '
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

    if briefing:
        lines.append('\n---\n')
        lines.append(briefing)

    return '\n'.join(lines)


__all__ = ['BriefingCache', 'format_briefing_block']
