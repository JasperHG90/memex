"""Session briefing service — generates a token-budgeted briefing for LLM agents.

Composes data from VaultSummaryService, EntityService, and KVService into a
structured markdown briefing that fits within a specified token budget.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, TypeVar
from uuid import UUID

from memex_core.services.entities import EntityService, EntityWithMetadata
from memex_core.services.kv import KVService
from memex_core.services.vault_summary import VaultSummaryService

logger = logging.getLogger('memex.core.services.session_briefing')

T = TypeVar('T')

_TREND_PRIORITY: dict[str, int] = {
    'new': 0,
    'strengthening': 1,
    'weakening': 2,
    'stable': 3,
    'stale': 4,
}

_TREND_ARROWS: dict[str, str] = {
    'new': '\u2605',  # ★
    'strengthening': '\u2191',  # ↑
    'weakening': '\u2193',  # ↓
    'stable': '\u2192',  # →
    'stale': '\u26a0',  # ⚠
}


def _estimate_tokens(text: str) -> int:
    """Rough chars-to-tokens estimate (÷4)."""
    return len(text) // 4


async def _collect_async_gen(gen: AsyncGenerator[T, None]) -> list[T]:
    """Drain an async generator into a list."""
    return [item async for item in gen]


def _build_kv_namespaces(project_id: str | None) -> list[str]:
    """Build the list of KV namespaces to include in the briefing."""
    ns = ['global', 'user', 'app:claude-code']
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
        entity_service: EntityService,
        kv_service: KVService,
    ) -> None:
        self._vault_summary = vault_summary_service
        self._entities = entity_service
        self._kv = kv_service

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
        summary, entities, kv_entries = await self._fetch_all(vault_id, project_id)

        sections = self._build_sections(summary, entities, kv_entries, vault_id, project_id, budget)

        assembled = self._assemble(sections)
        tokens = _estimate_tokens(assembled)

        if tokens > budget:
            assembled = self._apply_overflow(
                sections,
                budget,
                summary,
                entities,
                kv_entries,
                vault_id,
                project_id,
            )

        return assembled

    async def _fetch_all(
        self,
        vault_id: UUID,
        project_id: str | None,
    ) -> tuple[Any, list[EntityWithMetadata], list[Any]]:
        """Fetch all data sources in parallel."""
        summary_coro = self._vault_summary.get_summary(vault_id)
        entities_coro = _collect_async_gen(
            self._entities.list_entities_ranked(limit=10, vault_ids=[vault_id])
        )
        kv_coro = self._kv.list_entries(namespaces=_build_kv_namespaces(project_id))

        summary, entities, kv_entries = await asyncio.gather(summary_coro, entities_coro, kv_coro)
        return summary, entities, kv_entries

    def _build_sections(
        self,
        summary: Any,
        entities: list[EntityWithMetadata],
        kv_entries: list[Any],
        vault_id: UUID,
        project_id: str | None,
        budget: int,
    ) -> list[tuple[str, str]]:
        """Build all sections in priority order."""
        sections: list[tuple[str, str]] = []

        # 1. Header (always included)
        sections.append(('header', self._build_header(summary, len(entities))))

        # 2. KV facts (priority 1)
        sections.append(('kv', self._build_kv_section(kv_entries)))

        # 3. Vault summary (priority 2)
        if budget >= 2000 and summary:
            sections.append(('vault_prose', self._build_vault_prose(summary)))
        else:
            sections.append(('vault_prose', ''))

        sections.append(('topics', self._build_topics(summary, compact=(budget < 2000))))

        # 4. Entities (priority 3)
        entity_limit = 10 if budget >= 2000 else 5
        include_trends = budget >= 2000
        sections.append(('entities', self._build_entities(entities[:entity_limit], include_trends)))

        # 5. Vault binding (always included)
        sections.append(('binding', self._build_vault_binding(vault_id, project_id)))

        return sections

    def _build_header(self, summary: Any, entity_count: int) -> str:
        """Build the header section with inline stats."""
        lines = ['# Session Briefing']
        stat_parts: list[str] = []
        if summary and summary.stats:
            total_notes = summary.stats.get('total_notes', 0)
            stat_parts.append(f'{total_notes} notes')
        if entity_count:
            stat_parts.append(f'{entity_count} entities')
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
        """Build the KV facts section."""
        if not kv_entries:
            return ''
        lines = ['\n## Key-Value Facts\n']
        for entry in kv_entries:
            lines.append(f'- `{entry.key}`: {entry.value}')
        return '\n'.join(lines) + '\n'

    def _build_vault_prose(self, summary: Any) -> str:
        """Build the vault summary prose section (2000-budget only)."""
        if not summary or not summary.summary:
            return ''
        return f'\n## Vault Summary\n\n{summary.summary}\n'

    def _build_topics(self, summary: Any, compact: bool = False) -> str:
        """Build the topics section."""
        if not summary or not summary.topics:
            return ''
        lines = ['\n## Topics\n']
        for topic in summary.topics:
            name = topic.get('name', '')
            count = topic.get('note_count', 0)
            desc = topic.get('description', '')
            if compact or not desc:
                lines.append(f'- {name} ({count})')
            else:
                lines.append(f'- **{name}** ({count}): {desc}')
        return '\n'.join(lines) + '\n'

    def _build_entities(
        self,
        entities: list[EntityWithMetadata],
        include_trends: bool,
    ) -> str:
        """Build the entities section with optional trend indicators."""
        if not entities:
            return ''
        lines = ['\n## Top Entities\n']
        for ewm in entities:
            e = ewm.entity
            name = e.canonical_name
            etype = getattr(e, 'entity_type', '')
            mentions = getattr(e, 'mention_count', 0)

            if include_trends:
                obs = _sort_observations(ewm.observations)
                trend_parts = []
                for o in obs[:2]:
                    trend = o.get('trend', 'stable')
                    arrow = _TREND_ARROWS.get(trend, '')
                    title = o.get('title', '')
                    if title:
                        trend_parts.append(f'{arrow} {title}')
                trend_str = ' | '.join(trend_parts)
                if trend_str:
                    lines.append(f'- **{name}** ({etype}, {mentions}m) — {trend_str}')
                else:
                    lines.append(f'- **{name}** ({etype}, {mentions}m)')
            else:
                lines.append(f'- {name} ({etype}, {mentions}m)')

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
        entities: list[EntityWithMetadata],
        kv_entries: list[Any],
        vault_id: UUID,
        project_id: str | None,
    ) -> str:
        """Apply overflow degradation to fit within budget."""
        # Steps 1/1b only apply at budget>=2000 where initial build used 10 entities + trends.
        # At budget<2000, _build_sections already used 5 entities without trends.
        if budget >= 2000:
            # Step 1: Trim entity count 10 -> 7 -> 5
            for entity_limit in (7, 5):
                sections = self._replace_section(
                    sections,
                    'entities',
                    self._build_entities(entities[:entity_limit], include_trends=True),
                )
                if _estimate_tokens(self._assemble(sections)) <= budget:
                    return self._assemble(sections)

            # Step 1b: Drop observation titles (trends) from entities
            sections = self._replace_section(
                sections,
                'entities',
                self._build_entities(entities[:5], include_trends=False),
            )
            if _estimate_tokens(self._assemble(sections)) <= budget:
                return self._assemble(sections)

        # Step 2: Drop topic descriptions (compact mode)
        sections = self._replace_section(
            sections,
            'topics',
            self._build_topics(summary, compact=True),
        )
        if _estimate_tokens(self._assemble(sections)) <= budget:
            return self._assemble(sections)

        # Step 3: Trim vault prose sentence by sentence
        sections = self._trim_prose(sections, budget)
        if _estimate_tokens(self._assemble(sections)) <= budget:
            return self._assemble(sections)

        # Step 4: Drop KV namespaces (app -> user -> project)
        for drop_prefix in ('app:', 'user:', 'project:'):
            filtered = [e for e in kv_entries if not e.key.startswith(drop_prefix)]
            sections = self._replace_section(
                sections,
                'kv',
                self._build_kv_section(filtered),
            )
            kv_entries = filtered
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

    def _trim_prose(
        self,
        sections: list[tuple[str, str]],
        budget: int,
    ) -> list[tuple[str, str]]:
        """Trim vault prose sentence by sentence from the end."""
        prose_content = ''
        for name, content in sections:
            if name == 'vault_prose':
                prose_content = content
                break

        if not prose_content:
            return sections

        # Extract the prose text (strip the header)
        header = '\n## Vault Summary\n\n'
        if prose_content.startswith(header):
            text = prose_content[len(header) :].rstrip('\n')
        else:
            text = prose_content

        sentences = text.split('. ')
        while sentences:
            sentences.pop()
            trimmed_text = '. '.join(sentences)
            if trimmed_text and not trimmed_text.endswith('.'):
                trimmed_text += '.'
            if trimmed_text:
                new_prose = f'{header}{trimmed_text}\n'
            else:
                new_prose = ''
            new_sections = self._replace_section(sections, 'vault_prose', new_prose)
            if _estimate_tokens(self._assemble(new_sections)) <= budget:
                return new_sections

        return self._replace_section(sections, 'vault_prose', '')
