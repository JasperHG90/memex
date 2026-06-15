"""Session briefing service — generates a token-budgeted briefing for LLM agents.

Composes data from VaultSummaryService, MentalModel, and KVService into a
structured markdown briefing that fits within a specified token budget.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlmodel import col, select

from memex_core.memory.sql_models import MentalModel
from memex_core.services.kv import KVService
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


def _estimate_tokens(text: str) -> int:
    """Rough chars-to-tokens estimate (÷4)."""
    return len(text) // 4


def _compute_importance(mm: MentalModel) -> float:
    """Compute importance score from trend-weighted observations."""
    return sum(_TREND_WEIGHTS.get(o.get('trend', 'stable'), 0.5) for o in (mm.observations or []))


def _build_kv_namespaces(project_id: str | None) -> list[str]:
    """Build the list of KV namespaces to include in the briefing.

    Procedures are NOT in KV — they live in the procedural plane and
    render as procedural cards. KV here is only the ``global:``/``user:``/
    ``project:<id>:``/``app:<id>:`` namespaced facts.
    """
    ns = ['global', 'user', 'app:claude-code']
    if project_id:
        ns.append(f'project:{project_id}')
    return ns


def _build_pin_contexts(project_id: str | None, app: str | None) -> list[str]:
    """Build the procedural pin-context chain (design §19.8).

    The layered chain is ``global → project:<id> → app:<consumer>``,
    most-specific last. Distinct from the KV namespaces above: pins
    have NO ``user`` context (per-user curation rides app/project
    contexts — JG decision 2026-06-10), and the app segment is the
    *caller's* identity (``claude-code``, ``hermes:<agent_identity>``)
    rather than a hardcoded consumer.
    """
    contexts = ['global']
    if project_id:
        contexts.append(f'project:{project_id}')
    if app:
        contexts.append(f'app:{app}')
    return contexts


def _is_operational_kv(key: str) -> bool:
    """Per-app, per-project routing config — plumbing, not a briefing fact.

    The Claude Code plugin records each project's default vault under
    ``app:<app>:project:<project-id>:vault``. These are operational mappings
    the agent never acts on directly; surfacing them as "Key-Value Facts"
    just clutters the briefing with one row per project the user has touched.
    """
    return key.startswith('app:') and ':project:' in key


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
        procedural_search: Any | None = None,
    ) -> None:
        """Configure the briefing service.

        ``procedural_search`` is the optional plane handle
        (:class:`memex_core.services.procedural_search_service.ProceduralSearchService`).
        When provided, the briefing includes a "Procedural Cards" section
        surfacing pin-chain entries for the active contexts. When None
        (the default for tests and installations without the plane),
        the section is silently omitted — the briefing still works.
        """
        self._vault_summary = vault_summary_service
        self._metastore = metastore
        self._kv = kv_service
        self._vaults = vault_service
        self._procedural_search = procedural_search

    async def generate(
        self,
        vault_id: UUID,
        budget: int = 2000,
        project_id: str | None = None,
        app: str | None = None,
    ) -> str:
        """Generate a session briefing within the given token budget.

        Args:
            vault_id: The vault to generate a briefing for.
            budget: Token budget (1000 for compact, 2000 for standard).
            project_id: Optional project ID for scoping KV entries and
                the procedural pin chain.
            app: Optional consumer identity for the pin chain's app
                context (``claude-code``, ``hermes:<agent_identity>`` —
                design §19.8). When None, the chain is global+project
                only.

        Returns:
            A markdown-formatted briefing string.
        """
        summary, mental_models, kv_entries, vaults, procedural_cards = await self._fetch_all(
            vault_id, project_id, app
        )

        sections = self._build_sections(
            summary,
            mental_models,
            kv_entries,
            vaults,
            vault_id,
            project_id,
            budget,
            procedural_cards=procedural_cards,
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
                procedural_cards=procedural_cards,
            )

        return assembled

    async def _fetch_all(
        self,
        vault_id: UUID,
        project_id: str | None,
        app: str | None = None,
    ) -> tuple[Any, list[MentalModel], list[Any], list[Any], Any]:
        """Fetch all data sources in parallel.

        The 5th element of the returned tuple is the procedural-cards
        response (``memex_common.procedural_schemas.ProceduralBriefingCards``)
        or ``None`` when the plane is not wired into this briefing
        service. A None is treated as an empty-cards state in the
        downstream section builder — the briefing still renders.
        """
        summary_coro = self._vault_summary.get_summary(vault_id)
        models_coro = self._fetch_mental_models(vault_id)
        kv_coro = self._kv.list_entries(namespaces=_build_kv_namespaces(project_id))

        if self._vaults:
            # Available-vaults enumeration is a discovery surface — content only.
            # The active vault still gets its full briefing even if it is a
            # system vault (addressability), via summary_coro / models_coro above.
            vaults_coro = self._vaults.list_vaults_with_counts(include_system=False)
        else:

            async def _empty() -> list[Any]:
                return []

            vaults_coro = _empty()

        if self._procedural_search is not None:
            # Pin-context chain per §19.8: global → project:<id> →
            # app:<consumer>. NOT the KV namespace list — pins have no
            # `user` context and the app segment is the caller's
            # identity, not a hardcoded consumer.
            card_contexts = _build_pin_contexts(project_id, app)
            cards_coro = self._procedural_search.briefing_cards(
                card_contexts, scope=None, limit_per_context=3
            )
        else:

            async def _empty_cards() -> Any:
                return None

            cards_coro = _empty_cards()

        summary, mental_models, kv_entries, vaults, procedural_cards = await asyncio.gather(
            summary_coro, models_coro, kv_coro, vaults_coro, cards_coro
        )
        # Defensive: if the procedural call failed, drop to None so
        # downstream treats the briefing as having no cards rather than
        # propagating a half-populated object.
        if self._procedural_search is not None and procedural_cards is None:
            procedural_cards = None
        return summary, mental_models, kv_entries, vaults, procedural_cards

    async def _fetch_mental_models(self, vault_id: UUID) -> list[MentalModel]:
        """Fetch active mental models for the vault, sorted by importance score.

        Archived models (``archived_at IS NOT NULL`` — set by the
        archive_mental_model proposal action) are excluded so the briefing
        only surfaces the entities reflection still maintains.
        """
        async with self._metastore.session() as session:
            stmt = (
                select(MentalModel)
                .where(col(MentalModel.vault_id) == vault_id)
                .where(col(MentalModel.archived_at).is_(None))
            )
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
        procedural_cards: Any = None,
    ) -> list[tuple[str, str]]:
        """Build all sections in priority order."""
        sections: list[tuple[str, str]] = []

        # 1. Header (always included)
        sections.append(('header', self._build_header(summary, len(mental_models))))

        # 2. KV facts (priority 1). Operational routing config (the plugin's
        # per-project default-vault mappings, ``app:<app>:project:*``) is
        # excluded — it is plumbing the agent doesn't act on, not a fact.
        # NB: procedures are NOT in KV — they live in the procedural plane
        # and render as procedural cards (2b) below.
        non_proc = [e for e in kv_entries if not _is_operational_kv(e.key)]
        sections.append(('kv', self._build_kv_section(non_proc)))

        # 2b. Procedural cards (priority 1b plane pin-chain entries).
        # Sits between KV facts and vault overview so the agent sees the
        # most actionable rules first. When the plane is not wired
        # (procedural_cards is None) the section is empty and contributes
        # nothing to the briefing.
        sections.append(
            ('procedural_cards', self._build_procedural_cards_section(procedural_cards))
        )

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

        Operational routing config is excluded by the caller. Procedures
        are NOT in KV — they live in the plane and render as procedural
        cards.
        """
        if not kv_entries:
            return ''
        lines = ['\n## Key-Value Facts\n']
        for entry in kv_entries:
            lines.append(f'- `{entry.key}`: {entry.value}')
        return '\n'.join(lines) + '\n'

    def _build_procedural_cards_section(self, cards: Any) -> str:
        """Build the procedural-cards section from the plane.

        Render shape: one bullet per card with the entry's kind + title,
        a 1-line summary, and the pin's context_key. The card list is
        already in (context_key, position) order from the search service,
        so the section naturally follows the pin chain priority.

        Returns ``''`` (no section) when the plane is not wired or has
        no cards — callers treat empty sections as "no content", not as
        "section omitted".
        """
        if cards is None or not getattr(cards, 'cards', None):
            return ''
        lines = ['\n## Procedural Cards\n']
        for card in cards.cards:
            entry = getattr(card, 'entry', None)
            if entry is None:
                continue
            kind = getattr(entry, 'kind', '')
            title = getattr(entry, 'title', '') or ''
            summary = getattr(entry, 'summary', '') or ''
            ctx = getattr(card, 'context_key', '') or ''
            # Cap the summary at 240 chars to keep one card ≈ one
            # briefing line (the briefing's per-card budget is roughly
            # 50 tokens; 240 chars ≈ 60 tokens).
            if len(summary) > 240:
                summary = summary[:239].rstrip() + '…'
            label = f'**{kind}/{title}**' if title else f'**{kind}**'
            if summary:
                line = f'- {label} — {summary}'
            else:
                line = f'- {label}'
            if ctx:
                line += f' _(pinned: {ctx})_'
            lines.append(line)
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
        # Leading blank line so the heading separates from the preceding
        # section (every other section starts with '\n' too); without it the
        # heading glues onto the prior section's last list item.
        lines = ['\n## Available Vaults\n']
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
        procedural_cards: Any = None,
    ) -> str:
        """Apply overflow degradation to fit within budget."""
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
        for drop_prefix in ('app:', 'user:', 'project:'):
            kv_entries = [e for e in kv_entries if not e.key.startswith(drop_prefix)]
            sections = self._replace_section(
                sections,
                'kv',
                self._build_kv_section(kv_entries),
            )
            if _estimate_tokens(self._assemble(sections)) <= budget:
                return self._assemble(sections)

        # Step 5: Drop the procedural-cards section entirely. The
        # cards are a discovery surface — the agent can re-fetch them
        # via the briefing-cards HTTP route when it needs more — so they
        # are the first thing to go under a tight budget.
        if procedural_cards is not None:
            sections = self._replace_section(sections, 'procedural_cards', '')
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
