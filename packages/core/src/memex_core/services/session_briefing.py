"""Session briefing service — generates a token-budgeted briefing for LLM agents.

Composes data from VaultSummaryService, MentalModel, and KVService into a
structured markdown briefing that fits within a specified token budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Final
from uuid import UUID

from sqlmodel import col, select

from memex_core.memory.sql_models import MentalModel
from memex_core.services.kv import KVService, format_procedure_display_name, is_procedure_key
from memex_core.services.vault_summary import VaultSummaryService
from memex_core.services.vaults import VaultService
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.session_briefing')

_TREND_PRIORITY: dict[str, int] = {
    'new': 0,
    'strengthening': 1,
    'weakening': 2,
    'stable': 3,
    'stale': 4,
}

_TREND_WEIGHTS: dict[str, float] = {
    'new': 3.0,
    'strengthening': 2.0,
    'weakening': 1.5,
    'stable': 0.5,
    'stale': 0.0,
}

_TREND_ARROWS: dict[str, str] = {
    'new': '\u2605',  # ★
    'strengthening': '\u2191',  # ↑
    'weakening': '\u2193',  # ↓
    'stable': '\u2192',  # →
    'stale': '\u26a0',  # ⚠
}

_THEME_TREND_ARROWS: dict[str, str] = {
    'growing': '\u2191',  # ↑
    'stable': '\u2192',  # →
    'dormant': '\u26a0',  # ⚠
}


# Sized to fit the motivating procedure:answer:session-briefing row (~900 chars).
# At 5 rows-of-cap ~ 1250 tokens, <= 63% of the 2000-token briefing budget.
_PROCEDURE_VALUE_MAX_CHARS: Final = 1000


def _estimate_tokens(text: str) -> int:
    """Rough chars-to-tokens estimate (÷4)."""
    return len(text) // 4


def _compute_importance(mm: MentalModel) -> float:
    """Compute importance score from trend-weighted observations."""
    return sum(_TREND_WEIGHTS.get(o.get('trend', 'stable'), 0.5) for o in (mm.observations or []))


def _build_kv_namespaces(project_id: str | None) -> list[str]:
    """Build the list of KV namespaces to include in the briefing.

    Procedures are NOT a top-level namespace — they live UNDER ``global:``,
    ``user:``, ``project:<id>:``, or ``app:<id>:`` as
    ``<scope>:procedure:<verb>:<context>`` keys. Rows whose key matches
    that shape are split out of ``kv_entries`` in ``_build_sections`` and
    render in their own ``## Procedures`` section. Step 4 of
    ``_apply_overflow`` drops only non-procedure KV under
    ``app:/user:/project:`` — procedure rows degrade separately in Step 5.

    ``'procedure'`` is kept in the fetch list for back-compat during the
    migration 046 deployment window: any unmigrated bare ``procedure:*``
    rows would otherwise become invisible until the DB sweep completes.
    After 046 runs they disappear from the bare namespace (they live under
    ``global:procedure:*``) and this entry no-ops.
    """
    ns = ['global', 'user', 'app:claude-code', 'procedure']
    if project_id:
        ns.append(f'project:{project_id}')
    return ns


def _sort_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort observations by trend priority: new > strengthening > weakening > stable > stale."""
    return sorted(observations, key=lambda o: _TREND_PRIORITY.get(o.get('trend', 'stable'), 3))


class SessionBriefingService:
    """Generates a token-budgeted session briefing by composing existing services."""

    def __init__(
        self,
        vault_summary_service: VaultSummaryService,
        metastore: AsyncBaseMetaStoreEngine,
        kv_service: KVService,
        vault_service: VaultService | None = None,
    ) -> None:
        self._vault_summary = vault_summary_service
        self._metastore = metastore
        self._kv = kv_service
        self._vaults = vault_service

    async def generate(
        self,
        vault_id: UUID,
        budget: int = 2000,
        project_id: str | None = None,
    ) -> str:
        """Generate a session briefing within the given token budget.

        Args:
            vault_id: The vault to generate a briefing for.
            budget: Token budget (1000 for compact, 2000 for standard).
            project_id: Optional project ID for scoping KV entries.

        Returns:
            A markdown-formatted briefing string.
        """
        summary, mental_models, kv_entries, vaults = await self._fetch_all(vault_id, project_id)

        sections = self._build_sections(
            summary, mental_models, kv_entries, vaults, vault_id, project_id, budget
        )

        assembled = self._assemble(sections)
        tokens = _estimate_tokens(assembled)

        if tokens > budget:
            assembled = self._apply_overflow(
                sections,
                budget,
                summary,
                mental_models,
                kv_entries,
            )

        return assembled

    async def _fetch_all(
        self,
        vault_id: UUID,
        project_id: str | None,
    ) -> tuple[Any, list[MentalModel], list[Any], list[Any]]:
        """Fetch all data sources in parallel."""
        summary_coro = self._vault_summary.get_summary(vault_id)
        models_coro = self._fetch_mental_models(vault_id)
        kv_coro = self._kv.list_entries(namespaces=_build_kv_namespaces(project_id))

        if self._vaults:
            vaults_coro = self._vaults.list_vaults_with_counts()
        else:

            async def _empty() -> list[Any]:
                return []

            vaults_coro = _empty()

        summary, mental_models, kv_entries, vaults = await asyncio.gather(
            summary_coro, models_coro, kv_coro, vaults_coro
        )
        return summary, mental_models, kv_entries, vaults

    async def _fetch_mental_models(self, vault_id: UUID) -> list[MentalModel]:
        """Fetch mental models for the vault, sorted by importance score."""
        async with self._metastore.session() as session:
            stmt = select(MentalModel).where(col(MentalModel.vault_id) == vault_id)
            result = await session.exec(stmt)
            models = list(result.all())
        models.sort(key=_compute_importance, reverse=True)
        return models

    def _build_sections(
        self,
        summary: Any,
        mental_models: list[MentalModel],
        kv_entries: list[Any],
        vaults: list[Any],
        vault_id: UUID,
        project_id: str | None,
        budget: int,
    ) -> list[tuple[str, str]]:
        """Build all sections in priority order."""
        sections: list[tuple[str, str]] = []

        # 1. Header (always included)
        sections.append(('header', self._build_header(summary, len(mental_models))))

        # 2. KV facts (priority 1) — procedure rows (global + project-scoped)
        # render in their own section below.
        non_proc = [e for e in kv_entries if not is_procedure_key(e.key)]
        proc = [e for e in kv_entries if is_procedure_key(e.key)]
        sections.append(('kv', self._build_kv_section(non_proc)))

        # 2b. Procedures (priority 1b — behavioural rules; high signal)
        sections.append(('procedures', self._build_procedures_section(proc)))

        # 3. Vault overview (priority 2 — narrative + compact themes in one section)
        sections.append(
            ('vault_overview', self._build_vault_overview(summary, compact=(budget < 2000)))
        )

        model_limit = 10 if budget >= 2000 else 5
        include_trends = budget >= 2000
        sections.append(
            (
                'mental_models',
                self._build_mental_models(mental_models[:model_limit], include_trends),
            )
        )

        # 5. Available vaults (priority 4)
        sections.append(('vaults', self._build_vaults_section(vaults, vault_id)))

        # 6. Vault binding (always included)
        sections.append(('binding', self._build_vault_binding(vault_id, project_id)))

        return sections

    def _build_header(self, summary: Any, model_count: int) -> str:
        """Build the header section with inline stats from inventory."""
        lines = ['# Session Briefing']
        stat_parts: list[str] = []
        if summary and summary.inventory:
            inv = summary.inventory
            total_notes = inv.get('total_notes', 0)
            stat_parts.append(f'{total_notes} notes')
            total_entities = inv.get('total_entities', 0)
            if total_entities:
                stat_parts.append(f'{total_entities} entities')
            recent = inv.get('recent_activity', {})
            if recent.get('7d', 0):
                stat_parts.append(f'{recent["7d"]} added this week')
        if model_count:
            stat_parts.append(f'{model_count} mental models')
        if summary and getattr(summary, 'updated_at', None):
            updated = summary.updated_at
            if hasattr(updated, 'strftime'):
                stat_parts.append(f'Updated {updated.strftime("%Y-%m-%d")}')
        if summary and getattr(summary, 'version', None):
            stat_parts.append(f'v{summary.version}')
        if stat_parts:
            lines.append(f'\n{" | ".join(stat_parts)}')
        return ''.join(lines) + '\n'

    def _build_kv_section(self, kv_entries: list[Any]) -> str:
        """Build the KV facts section.

        Caller (``_build_sections`` / ``_apply_overflow``) is responsible for
        excluding ``procedure:*`` rows — those render in their own section.
        """
        if not kv_entries:
            return ''
        lines = ['\n## Key-Value Facts\n']
        for entry in kv_entries:
            lines.append(f'- `{entry.key}`: {entry.value}')
        return '\n'.join(lines) + '\n'

    def _sanitize_procedure_value(self, raw: str | None) -> str:
        """Extract the human-readable value from a procedure KV row, defanged for markdown.

        ``raw`` is the value column of a ``KVEntry`` row — always ``str | None``
        per the schema; ``Any`` would mask accidental misuse.
        """
        if raw is None:
            return ''
        parsed_ok = True
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed_ok = False
            payload = None

        if isinstance(payload, dict) and 'value' in payload:
            inner = payload['value']
            # Envelope present but `value` is non-string — render just the inner
            # value (don't leak `v`/`tags`/`history` from the full envelope).
            text = inner if isinstance(inner, str) else str(inner)
        elif parsed_ok and payload is None:
            # `json.loads("null")` returns None — treat the same as `raw is None`
            # rather than rendering the literal string `"null"`.
            text = ''
        else:
            # Parse failed OR JSON was not a dict — render the raw stored string.
            text = str(raw)
        # Indent embedded newlines so a stored "\n## X" can't create a fake heading.
        # Normalise CRLF and bare CR first so all three line terminators are defanged
        # uniformly (markdown processors and screen readers each handle CR differently).
        text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\n  ')
        if len(text) > _PROCEDURE_VALUE_MAX_CHARS:
            # Reserve one char for the ellipsis so total length is exactly the cap.
            # rstrip() drops trailing whitespace at the truncation boundary so the
            # ellipsis abuts the last visible glyph; acceptable for behavioural
            # procedure rules (where trailing whitespace carries no semantics).
            text = text[: _PROCEDURE_VALUE_MAX_CHARS - 1].rstrip() + '…'
        return text

    @staticmethod
    def _defang_procedure_name(name: str) -> str:
        """Escape markdown metacharacters in a procedure key.

        Keys are validated at write time (`validate_procedure_key`) — the scoped
        threat model here is rendering-correctness against any key shape that
        passed validation (mostly `[A-Za-z0-9:_-]`, but defending against the
        broader set keeps this robust if the validator is ever loosened). The
        renderer wraps the name in `**…**` so unescaped `*`/backtick would
        corrupt the bold span or open a code span; `[` would open a link.
        """
        return (
            name.replace('\\', '\\\\')
            .replace('*', '\\*')
            .replace('`', '\\`')
            .replace('[', '\\[')
            .replace(']', '\\]')
        )

    def _build_procedures_section(self, entries: list[Any]) -> str:
        """Build the procedures section from KV rows under `procedure:*`."""
        if not entries:
            return ''
        lines = ['\n## Procedures\n']
        for entry in entries:
            # Defensive: callers normally filter via is_procedure_key, but
            # this method also runs against degenerate fixtures in tests —
            # skip anything that isn't a valid procedure key.
            if not is_procedure_key(entry.key):
                continue
            text = self._sanitize_procedure_value(entry.value)
            name = format_procedure_display_name(entry.key)
            if not name or not text:
                continue
            lines.append(f'- **{self._defang_procedure_name(name)}** — {text}')
        if len(lines) == 1:
            return ''
        return '\n'.join(lines) + '\n'

    def _build_vault_overview(self, summary: Any, compact: bool = False) -> str:
        """Build a single vault overview section: narrative + themes."""
        if not summary:
            return ''
        parts: list[str] = ['\n## Vault Overview\n']

        # Narrative (short synthesis)
        if summary.narrative:
            parts.append(summary.narrative)
            parts.append('')

        # Themes with trend indicators
        if summary.themes:
            for theme in summary.themes:
                name = theme.get('name', '')
                count = theme.get('note_count', 0)
                desc = theme.get('description', '')
                trend = theme.get('trend', 'stable')
                arrow = _THEME_TREND_ARROWS.get(trend, '')

                if compact or not desc:
                    parts.append(f'- {arrow} {name} ({count})')
                else:
                    parts.append(f'- {arrow} **{name}** ({count}): {desc}')

        return '\n'.join(parts) + '\n'

    def _build_mental_models(
        self,
        models: list[MentalModel],
        include_trends: bool,
    ) -> str:
        """Build the mental models section with optional trend indicators."""
        if not models:
            return ''
        lines = ['\n## Top Entities\n']
        for mm in models:
            meta = mm.entity_metadata or {}
            name = mm.name
            category = meta.get('category', '')
            obs_count = meta.get('observation_count', len(mm.observations or []))
            description = meta.get('description', '')
            last_seen = mm.last_refreshed

            label_parts = []
            if category:
                label_parts.append(category)
            label_parts.append(f'{obs_count} obs')
            label = ', '.join(label_parts)

            if include_trends:
                obs = _sort_observations(mm.observations or [])
                trend_parts = []
                for o in obs[:2]:
                    trend = o.get('trend', 'stable')
                    arrow = _TREND_ARROWS.get(trend, '')
                    title = o.get('title', '')
                    if title:
                        trend_parts.append(f'{arrow} {title}')
                trend_str = ' | '.join(trend_parts)

                line = f'- **{name}** ({label})'
                if description:
                    line += f' — {description}'
                if trend_str:
                    line += f'\n  {trend_str}'
                if last_seen and hasattr(last_seen, 'strftime'):
                    line += f'\n  Last seen: {last_seen.strftime("%Y-%m-%d")}'
                lines.append(line)
            else:
                line = f'- {name} ({label})'
                if description:
                    line += f' — {description}'
                lines.append(line)

        return '\n'.join(lines) + '\n'

    def _build_vaults_section(self, vaults: list[Any], active_vault_id: UUID) -> str:
        """Build the available vaults section."""
        if not vaults:
            return ''
        lines = ['## Available Vaults\n']
        for v in vaults:
            vault = v.get('vault', v) if isinstance(v, dict) else v
            name = getattr(vault, 'name', str(vault))
            desc = getattr(vault, 'description', None) or ''
            if len(desc) > 60:
                desc = desc[:57] + '...'
            note_count = (
                v.get('note_count', 0) if isinstance(v, dict) else getattr(vault, 'note_count', 0)
            )
            active = ' **(active)**' if getattr(vault, 'id', None) == active_vault_id else ''
            if desc:
                lines.append(f'- **{name}** — {desc} ({note_count} notes){active}')
            else:
                lines.append(f'- **{name}** ({note_count} notes){active}')
        return '\n'.join(lines) + '\n'

    def _build_vault_binding(self, vault_id: UUID, project_id: str | None) -> str:
        """Build the vault binding footer."""
        parts = [f'\n---\n*Vault: {vault_id}*']
        if project_id:
            parts.append(f' | *Project: {project_id}*')
        return ''.join(parts) + '\n'

    def _assemble(self, sections: list[tuple[str, str]]) -> str:
        """Assemble all sections into a single string."""
        return ''.join(content for _, content in sections if content)

    def _apply_overflow(
        self,
        sections: list[tuple[str, str]],
        budget: int,
        summary: Any,
        mental_models: list[MentalModel],
        kv_entries: list[Any],
    ) -> str:
        """Apply overflow degradation to fit within budget."""
        # Snapshot procedure rows BEFORE Step 4 mutates kv_entries — keeps Step 5
        # decoupled from any future change to the Step-4 drop-prefix list.
        # `_ts_floor` is tz-aware to match `updated_at` from `KVEntry` (the ORM
        # column is `TIMESTAMP WITH TIME ZONE`, so sorted() never mixes tz-aware
        # and naive datetimes). Rows with `updated_at is None` fall back to
        # `_ts_floor`, sorting them oldest — they'll be the first dropped by
        # Step 5's oldest-first trim. This is the intended degradation order.
        _ts_floor = datetime.min.replace(tzinfo=timezone.utc)
        procs_initial = sorted(
            [e for e in kv_entries if is_procedure_key(e.key)],
            key=lambda e: getattr(e, 'updated_at', None) or _ts_floor,
        )
        # Steps 1/1b only apply at budget>=2000 where initial build used 10 models + trends.
        # At budget<2000, _build_sections already used 5 models without trends.
        if budget >= 2000:
            # Step 1: Trim model count 10 -> 7 -> 5
            for model_limit in (7, 5):
                sections = self._replace_section(
                    sections,
                    'mental_models',
                    self._build_mental_models(mental_models[:model_limit], include_trends=True),
                )
                if _estimate_tokens(self._assemble(sections)) <= budget:
                    return self._assemble(sections)

            # Step 1b: Drop observation titles (trends) from models
            sections = self._replace_section(
                sections,
                'mental_models',
                self._build_mental_models(mental_models[:5], include_trends=False),
            )
            if _estimate_tokens(self._assemble(sections)) <= budget:
                return self._assemble(sections)

        # Step 2: Compact vault overview (drop theme descriptions)
        sections = self._replace_section(
            sections,
            'vault_overview',
            self._build_vault_overview(summary, compact=True),
        )
        if _estimate_tokens(self._assemble(sections)) <= budget:
            return self._assemble(sections)

        # Step 3: Drop vault overview entirely
        sections = self._replace_section(sections, 'vault_overview', '')
        if _estimate_tokens(self._assemble(sections)) <= budget:
            return self._assemble(sections)

        # Step 4: Drop KV namespaces (app -> user -> project).
        # procedure: rows are tracked separately via `procs_initial` (snapshotted
        # at the top of this method). Strip them from `kv_entries` once up-front
        # so any subsequent consumer sees a procedure-free list; Step 5 below
        # trims procedures from the snapshot, not from `kv_entries`.
        kv_entries = [e for e in kv_entries if not is_procedure_key(e.key)]
        for drop_prefix in ('app:', 'user:', 'project:'):
            kv_entries = [e for e in kv_entries if not e.key.startswith(drop_prefix)]
            sections = self._replace_section(
                sections,
                'kv',
                self._build_kv_section(kv_entries),
            )
            if _estimate_tokens(self._assemble(sections)) <= budget:
                return self._assemble(sections)

        # Step 5: Trim procedures (high-signal but droppable when budget is exhausted).
        # Oldest-first: `procs` is sorted ascending by `updated_at`, so
        # `procs[n_dropped:]` retains the most-recently-touched rules and drops
        # the oldest `n_dropped` entries first.
        # If all procedures are exhausted without fitting the budget, fall through
        # to Step 7 (drop vaults).
        procs = list(procs_initial)
        n_dropped = 0
        while n_dropped < len(procs):
            n_dropped += 1
            sections = self._replace_section(
                sections,
                'procedures',
                self._build_procedures_section(procs[n_dropped:]),
            )
            if _estimate_tokens(self._assemble(sections)) <= budget:
                return self._assemble(sections)

        # Step 6: (reserved — intentionally skipped in the numbering)

        # Step 7: Drop vaults section entirely
        sections = self._replace_section(sections, 'vaults', '')
        if _estimate_tokens(self._assemble(sections)) <= budget:
            return self._assemble(sections)

        return self._assemble(sections)  # Best effort

    def _replace_section(
        self,
        sections: list[tuple[str, str]],
        name: str,
        new_content: str,
    ) -> list[tuple[str, str]]:
        """Replace a named section's content."""
        return [(n, new_content if n == name else c) for n, c in sections]
