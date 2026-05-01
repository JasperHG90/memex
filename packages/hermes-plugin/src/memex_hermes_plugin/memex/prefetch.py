"""Two-layer prefetch: memory units + whole notes.

Fires on ``queue_prefetch`` after each turn, joined on the next turn's
``prefetch`` with a short timeout. Cache is overwritten on each fire — we
deliberately don't accumulate, because user queries drift.

Returns a formatted markdown block with two sections for injection into the
next user message.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from uuid import UUID

from .async_bridge import run_sync
from .config import HermesMemexConfig

logger = logging.getLogger(__name__)


class PrefetchCache:
    """Holds the most recent prefetch result pair.

    Uses a generation counter so ``queue`` is non-blocking: stale threads
    from a prior generation silently drop their results on the way to
    writing. The previous approach joined prior threads (up to ~1s each),
    which blocked the main thread between turns — a problem for agent UX.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._facts: list[Any] = []
        self._notes: list[Any] = []
        self._generation: int = 0
        self._pending: set[int] = set()
        # Per-layer completion signals for the current generation. Both start
        # set so a fresh cache with no queue() yet treats consume() as idle.
        self._facts_done: threading.Event = threading.Event()
        self._notes_done: threading.Event = threading.Event()
        self._facts_done.set()
        self._notes_done.set()

    def queue(
        self,
        query: str,
        *,
        api: Any,
        config: HermesMemexConfig,
        vault_id: UUID | None,
    ) -> None:
        """Fire background fetches for ``query``. Non-blocking (<1ms)."""
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._facts = []
            self._notes = []
            self._pending = {gen}
            # Replace events so stale completions from prior generations
            # cannot satisfy this generation's wait. Bind new instances under
            # the lock and let fetch closures capture them by reference.
            self._facts_done = threading.Event()
            self._notes_done = threading.Event()
            facts_done = self._facts_done
            notes_done = self._notes_done

        vault_ids: list[Any] | None = [vault_id] if vault_id else None

        def _fetch_facts() -> None:
            try:
                result = run_sync(
                    api.search(
                        query=query,
                        limit=config.recall.facts_limit,
                        vault_ids=vault_ids,
                        token_budget=config.recall.token_budget,
                        strategies=config.recall.strategies,
                        include_stale=config.recall.include_stale,
                        include_superseded=config.recall.include_superseded,
                    ),
                    timeout=30.0,
                )
                with self._lock:
                    if gen == self._generation:
                        self._facts = list(result or [])
            except Exception as e:
                logger.debug('Prefetch facts failed: %s', e)
            finally:
                facts_done.set()

        def _fetch_notes() -> None:
            try:
                result = run_sync(
                    api.search_notes(
                        query=query,
                        limit=config.recall.notes_limit,
                        vault_ids=vault_ids,
                        expand_query=config.recall.expand_query,
                        strategies=config.recall.strategies,
                    ),
                    timeout=30.0,
                )
                with self._lock:
                    if gen == self._generation:
                        self._notes = list(result or [])
            except Exception as e:
                logger.debug('Prefetch notes failed: %s', e)
            finally:
                notes_done.set()

        threading.Thread(
            target=_fetch_facts, daemon=True, name=f'memex-prefetch-facts-{gen}'
        ).start()
        threading.Thread(
            target=_fetch_notes, daemon=True, name=f'memex-prefetch-notes-{gen}'
        ).start()

    def consume(self, timeout: float = 3.0) -> str:
        """Wait up to ``timeout`` for the current generation's results, then format.

        Waits for BOTH layers to report completion (success or failure) or for
        the deadline to pass. Returns whatever populated by then; '' if nothing
        arrived. After consumption the buffers are cleared so the next turn
        starts fresh.

        Note on the wait shape: an earlier implementation broke the wait as
        soon as EITHER layer reported data, then snapshot+cleared. Under load
        the slow layer's results landed in the cleared buffer for the same
        generation and were lost on the next ``queue()``. The both-or-deadline
        wait fixes this; partial results are only returned post-deadline.
        """
        with self._lock:
            facts_done = self._facts_done
            notes_done = self._notes_done

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if facts_done.is_set() and notes_done.is_set():
                break
            time.sleep(0.05)

        with self._lock:
            facts = list(self._facts)
            notes = list(self._notes)
            self._facts = []
            self._notes = []

        if not facts and not notes:
            return ''

        blocks: list[str] = []

        if facts:
            blocks.append('## Memex — Facts')
            for u in facts:
                text = getattr(u, 'text', None) or ''
                if not text:
                    continue
                note_id = getattr(u, 'note_id', None)
                suffix = f' _(note: {note_id})_' if note_id else ''
                blocks.append(f'- {text}{suffix}')

        if notes:
            if blocks:
                blocks.append('')
            blocks.append('## Memex — Related Notes')
            for r in notes:
                metadata = getattr(r, 'metadata', None) or {}
                title = metadata.get('name') or metadata.get('title') or '(untitled)'
                note_id = getattr(r, 'note_id', None)
                blocks.append(f'- **{title}** _(id: {note_id})_')
                summaries = getattr(r, 'summaries', None) or []
                first = next(
                    (getattr(s, 'summary', None) or getattr(s, 'text', None) for s in summaries),
                    None,
                )
                if first:
                    blocks.append(f'  - {first}')

        return '\n'.join(blocks).strip()


__all__ = ['PrefetchCache']
