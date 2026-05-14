"""Session briefing fetch.

Token-budgeted markdown summary from Memex's session-briefing endpoint.
Fetched once per session in the background; cached for system_prompt_block().
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from uuid import UUID

from memex_common.agent_harnesses import HERMES_HARNESS as _HERMES_HARNESS
from memex_common.agent_surface import LAYER_ROUTING_PRIMER_TABLE, compose_universal

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


_LAYER_ROUTING_PRIMER = LAYER_ROUTING_PRIMER_TABLE  # back-compat re-export


# Tier 2 Hermes-specific framing. The string itself lives in
# `memex_common.agent_harnesses` as the SSOT; this name is a re-export so
# the Hermes plugin code can refer to `_HERMES_HARNESS` without an import
# rewrite, and so identity tests can pin both surfaces (in-process and CLI
# bridge) to the same object.


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
    lint_pending_winner_proposals: int | None = None,
) -> str:
    """Compose the Memex system-prompt block."""
    lines = ['## Memex Memory']
    if vault_id:
        lines.append(f'Active vault: `{vault_id}` · Project: `{project_id}`')
    else:
        lines.append(f'Project: `{project_id}` · **No vault bound to this project.**')

    # Tier 1b (universal SSOT) — same bytes every call; cacheable prefix.
    lines.append('\n## Memex — system instructions\n\n' + compose_universal())
    # Tier 2 — Hermes-specific framing layered on top.
    lines.append('\n' + _HERMES_HARNESS)

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

    if procedural_observations:
        lines.append('\n' + _render_procedural_block(procedural_observations))

    if lint_pending_count is not None and lint_pending_count > 0:
        lines.append(
            '\n'
            + _render_lint_block(
                lint_pending_count,
                pending_winner_proposals=lint_pending_winner_proposals,
            )
        )

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


def _render_lint_block(
    pending_count: int,
    *,
    pending_winner_proposals: int | None = None,
) -> str:
    lines = [
        '### Maintenance findings',
        f'- {pending_count} pending lint findings. Inspect with `memex lint findings`.',
    ]
    if pending_winner_proposals is not None and pending_winner_proposals > 0:
        lines.append(
            f'- {pending_winner_proposals} have proposed winners. '
            'Apply with `memex_lint_apply_winner` after surfacing to the user.'
        )
    return '\n'.join(lines)


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
