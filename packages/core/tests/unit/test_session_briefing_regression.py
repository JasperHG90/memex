"""Regression tests for session briefing with structured vault summary.

Covers:
1. _build_vault_overview renders narrative + themes correctly
2. Header uses inventory stats
3. Overflow degradation works with new section names
4. No duplicate entity rendering (key_entities not in briefing)
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4


from memex_core.services.session_briefing import SessionBriefingService


def _make_summary(**overrides):
    """Create a mock vault summary with structured fields."""
    defaults = {
        'narrative': 'This vault tracks AI research and agent architecture.',
        'themes': [
            {
                'name': 'LLM Agents',
                'description': 'Design patterns for autonomous AI agents',
                'note_count': 12,
                'trend': 'growing',
                'last_addition': '2026-04-06',
                'representative_titles': ['ReAct paper', 'Toolformer'],
            },
            {
                'name': 'Knowledge Graphs',
                'description': 'Graph-based knowledge representation',
                'note_count': 8,
                'trend': 'stable',
                'last_addition': '2026-03-20',
                'representative_titles': ['Neo4j patterns'],
            },
            {
                'name': 'Legacy Systems',
                'description': 'Migrating from monolith to microservices',
                'note_count': 3,
                'trend': 'dormant',
                'last_addition': '2025-11-01',
                'representative_titles': ['Migration guide'],
            },
        ],
        'inventory': {
            'total_notes': 42,
            'total_entities': 15,
            'date_range': {'earliest': '2024-01-15', 'latest': '2026-04-06'},
            'by_template': {'article': 30, 'quick-note': 10, 'bookmark': 2},
            'by_source_domain': {'arxiv.org': 15, 'github.com': 8},
            'top_tags': {'ai': 20, 'agents': 12, 'graphs': 8},
            'recent_activity': {'7d': 5, '30d': 12},
        },
        'key_entities': [
            {'name': 'Claude', 'type': 'product', 'mention_count': 25},
            {'name': 'RAG', 'type': 'concept', 'mention_count': 18},
        ],
        'version': 7,
        'notes_incorporated': 42,
        'updated_at': datetime(2026, 4, 7, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_briefing_service():
    """Create a SessionBriefingService with mock dependencies."""
    vault_summary_svc = MagicMock()
    metastore = MagicMock()
    kv_svc = MagicMock()
    kv_svc.list_entries = AsyncMock(return_value=[])
    vault_svc = MagicMock()
    vault_svc.list_vaults_with_counts = AsyncMock(return_value=[])
    return SessionBriefingService(
        vault_summary_service=vault_summary_svc,
        metastore=metastore,
        kv_service=kv_svc,
        vault_service=vault_svc,
    )


class TestBuildVaultOverview:
    """Tests for the unified vault overview section."""

    def test_contains_narrative(self):
        svc = _make_briefing_service()
        summary = _make_summary()
        result = svc._build_vault_overview(summary)
        assert '## Vault Overview' in result
        assert 'This vault tracks AI research' in result

    def test_contains_themes_with_trend_arrows(self):
        svc = _make_briefing_service()
        summary = _make_summary()
        result = svc._build_vault_overview(summary)
        # Growing theme gets ↑
        assert '\u2191' in result  # ↑
        assert 'LLM Agents' in result
        assert '(12)' in result
        # Stable theme gets →
        assert '\u2192' in result  # →
        assert 'Knowledge Graphs' in result
        # Dormant theme gets ⚠
        assert '\u26a0' in result  # ⚠
        assert 'Legacy Systems' in result

    def test_compact_mode_drops_descriptions(self):
        svc = _make_briefing_service()
        summary = _make_summary()
        full = svc._build_vault_overview(summary, compact=False)
        compact = svc._build_vault_overview(summary, compact=True)
        assert 'Design patterns for autonomous AI agents' in full
        assert 'Design patterns for autonomous AI agents' not in compact
        # But theme names and counts still present
        assert 'LLM Agents' in compact
        assert '(12)' in compact

    def test_no_summary_returns_empty(self):
        svc = _make_briefing_service()
        result = svc._build_vault_overview(None)
        assert result == ''

    def test_empty_themes_still_shows_narrative(self):
        svc = _make_briefing_service()
        summary = _make_summary(themes=[])
        result = svc._build_vault_overview(summary)
        assert '## Vault Overview' in result
        assert 'This vault tracks AI research' in result

    def test_empty_narrative_still_shows_themes(self):
        svc = _make_briefing_service()
        summary = _make_summary(narrative='')
        result = svc._build_vault_overview(summary)
        assert '## Vault Overview' in result
        assert 'LLM Agents' in result


class TestHeaderUsesInventory:
    """Header stats come from inventory, not the old stats field."""

    def test_header_shows_note_count_from_inventory(self):
        svc = _make_briefing_service()
        summary = _make_summary()
        result = svc._build_header(summary, model_count=3)
        assert '42 notes' in result
        assert '15 entities' in result

    def test_header_shows_recent_activity(self):
        svc = _make_briefing_service()
        summary = _make_summary()
        result = svc._build_header(summary, model_count=0)
        assert '5 added this week' in result

    def test_header_handles_empty_inventory(self):
        svc = _make_briefing_service()
        summary = _make_summary(inventory={})
        result = svc._build_header(summary, model_count=0)
        assert '# Session Briefing' in result


class TestKeyEntitiesNotInBriefing:
    """key_entities should NOT appear in the session briefing.
    Mental models (vault-scoped) are richer and cover the same ground."""

    def test_no_key_entities_section_in_briefing(self):
        svc = _make_briefing_service()
        summary = _make_summary()
        sections = svc._build_sections(
            summary=summary,
            mental_models=[],
            kv_entries=[],
            vaults=[],
            vault_id=uuid4(),
            project_id=None,
            budget=2000,
        )
        section_names = [name for name, _ in sections]
        assert 'key_entities' not in section_names


class TestOverflowDegradation:
    """Overflow steps use the new section names."""

    def test_compact_vault_overview_drops_descriptions(self):
        svc = _make_briefing_service()
        summary = _make_summary()

        full_overview = svc._build_vault_overview(summary, compact=False)
        compact_overview = svc._build_vault_overview(summary, compact=True)

        # Full includes descriptions, compact does not
        assert 'Design patterns for autonomous AI agents' in full_overview
        assert 'Design patterns for autonomous AI agents' not in compact_overview

        # Compact is strictly smaller
        assert len(compact_overview) < len(full_overview)

    def test_overflow_drops_vault_overview_at_tiny_budget(self):
        svc = _make_briefing_service()
        summary = _make_summary()

        sections = [
            ('header', '# Session Briefing\n'),
            ('kv', ''),
            ('procedures', ''),
            ('vault_overview', svc._build_vault_overview(summary, compact=False)),
            ('mental_models', ''),
            ('vaults', ''),
            ('binding', ''),
        ]

        # Budget=5 is impossibly small — overflow should drop everything it can
        result = svc._apply_overflow(
            sections=sections,
            budget=5,
            summary=summary,
            mental_models=[],
            kv_entries=[],
        )

        # Best-effort: vault_overview should have been dropped in step 3
        assert '## Vault Overview' not in result


class TestProceduresRendering:
    """V4 structural regression pins for the ## Procedures section."""

    @staticmethod
    def _kv(key: str, value: str) -> MagicMock:
        entry = MagicMock()
        entry.key = key
        entry.value = value
        entry.updated_at = datetime(2026, 5, 20, tzinfo=timezone.utc)
        return entry

    def test_procedures_section_is_rendered_when_rows_present(self):
        import json

        svc = _make_briefing_service()
        entries = [
            self._kv(
                'global:procedure:answer:briefing',
                json.dumps({'v': 1, 'value': 'cite briefing first', 'tags': {}, 'history': []}),
            )
        ]
        rendered = svc._build_procedures_section(entries)
        assert rendered.startswith('\n## Procedures\n')
        # Scope is always shown — global included — so the rule is never ambiguous.
        # Brackets are markdown-defanged by _defang_procedure_name.
        assert r'**\[global\] answer:briefing**' in rendered
        assert 'cite briefing first' in rendered

    def test_procedures_section_suppressed_when_no_rows(self):
        svc = _make_briefing_service()
        assert svc._build_procedures_section([]) == ''

    def test_operational_app_project_kv_excluded_from_facts(self):
        # app:<app>:project:*:vault routing config is plumbing, not a fact, and
        # must not pollute the Key-Value Facts section.
        from memex_core.services.session_briefing import _is_operational_kv

        assert _is_operational_kv('app:claude-code:project:github.com/x/y:vault')
        assert _is_operational_kv('app:hermes:project:foo:vault')
        # Real facts / preferences / procedures are NOT operational.
        assert not _is_operational_kv('user:name')
        assert not _is_operational_kv('app:claude-code:theme')
        assert not _is_operational_kv('global:procedure:commit:pr-first')
        assert not _is_operational_kv('project:memex:lang:python')

    def test_procedures_section_suppressed_when_all_degenerate(self):
        import json

        svc = _make_briefing_service()
        entries = [
            self._kv(
                'procedure:',
                json.dumps({'v': 1, 'value': 'orphan', 'tags': {}, 'history': []}),
            ),
            self._kv(
                'procedure:empty',
                json.dumps({'v': 1, 'value': '', 'tags': {}, 'history': []}),
            ),
        ]
        assert svc._build_procedures_section(entries) == ''

    def test_sanitize_indents_embedded_newlines(self):
        svc = _make_briefing_service()
        sanitized = svc._sanitize_procedure_value('intro\n## fake')
        assert '\n## fake' not in sanitized
        assert '\n  ## fake' in sanitized

    def test_sanitize_truncates_at_max_chars(self):
        from memex_core.services.session_briefing import _PROCEDURE_VALUE_MAX_CHARS

        svc = _make_briefing_service()
        long_value = 'x' * (_PROCEDURE_VALUE_MAX_CHARS + 50)
        sanitized = svc._sanitize_procedure_value(long_value)
        assert sanitized.endswith('…')
        # The named cap is the rendered ceiling — content + ellipsis together
        # must not exceed _PROCEDURE_VALUE_MAX_CHARS.
        assert len(sanitized) <= _PROCEDURE_VALUE_MAX_CHARS

    def test_sanitize_falls_back_when_no_value_field(self):
        import json

        svc = _make_briefing_service()
        assert svc._sanitize_procedure_value(json.dumps([1, 2, 3])) == '[1, 2, 3]'

    def test_sanitize_handles_none(self):
        svc = _make_briefing_service()
        assert svc._sanitize_procedure_value(None) == ''

    def test_sanitize_handles_json_null_literal(self):
        """`json.loads("null")` returns None — render '' rather than the literal "null"."""
        svc = _make_briefing_service()
        assert svc._sanitize_procedure_value('null') == ''

    def test_defang_procedure_name_escapes_markdown_metachars(self):
        """A key containing `*`, backtick, or `[`/`]` must not corrupt the
        `**name**` bold span or open a link inside it."""
        from memex_core.services.session_briefing import SessionBriefingService

        assert SessionBriefingService._defang_procedure_name('foo*bar') == 'foo\\*bar'
        assert SessionBriefingService._defang_procedure_name('foo`bar') == 'foo\\`bar'
        assert SessionBriefingService._defang_procedure_name('foo[bar') == 'foo\\[bar'
        assert SessionBriefingService._defang_procedure_name('foo]bar') == 'foo\\]bar'
        # Backslash must be escaped FIRST so we don't double-escape downstream chars.
        assert SessionBriefingService._defang_procedure_name('foo\\bar') == 'foo\\\\bar'
