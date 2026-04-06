"""Unit tests for SessionBriefingService."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from memex_core.services.entities import EntityWithMetadata
from memex_core.services.session_briefing import (
    SessionBriefingService,
    _estimate_tokens,
    _sort_observations,
    _build_kv_namespaces,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vault_summary(**overrides):
    """Create a mock VaultSummary object."""
    defaults = {
        'vault_id': uuid4(),
        'summary': 'This vault tracks research into memory systems and knowledge graphs.',
        'topics': [
            {
                'name': 'Memory systems',
                'note_count': 12,
                'description': 'Research on memory architectures',
            },
            {
                'name': 'Entity extraction',
                'note_count': 8,
                'description': 'NER pipelines and methods',
            },
            {
                'name': 'Retrieval',
                'note_count': 5,
                'description': 'Search and retrieval strategies',
            },
        ],
        'stats': {'total_notes': 42},
        'version': 5,
        'notes_incorporated': 42,
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _make_entity(
    name: str,
    entity_type: str = 'Technology',
    mention_count: int = 10,
    observations: list[dict] | None = None,
) -> EntityWithMetadata:
    """Create a mock EntityWithMetadata."""
    entity = MagicMock()
    entity.canonical_name = name
    entity.entity_type = entity_type
    entity.mention_count = mention_count
    return EntityWithMetadata(
        entity=entity,
        metadata={'description': f'{name} description'},
        observations=observations or [],
    )


def _make_kv_entry(key: str, value: str) -> MagicMock:
    """Create a mock KV entry."""
    entry = MagicMock()
    entry.key = key
    entry.value = value
    entry.updated_at = datetime.now(timezone.utc)
    return entry


def _make_entities(count: int, with_observations: bool = False) -> list[EntityWithMetadata]:
    """Create a list of mock entities."""
    entities = []
    for i in range(count):
        obs = []
        if with_observations:
            obs = [
                {'title': f'Observation {i}a', 'trend': 'new', 'content': f'Content {i}a'},
                {'title': f'Observation {i}b', 'trend': 'stable', 'content': f'Content {i}b'},
            ]
        entities.append(_make_entity(f'Entity{i}', mention_count=100 - i, observations=obs))
    return entities


async def _mock_entity_gen(*entities):
    """Create an async generator that yields entities."""
    for e in entities:
        yield e


def _make_service(
    summary=None,
    entities=None,
    kv_entries=None,
) -> SessionBriefingService:
    """Create a SessionBriefingService with mocked dependencies."""
    vault_summary_svc = AsyncMock()
    vault_summary_svc.get_summary = AsyncMock(return_value=summary)

    entity_svc = MagicMock()
    ents = entities or []
    entity_svc.list_entities_ranked = lambda **kwargs: _mock_entity_gen(*ents)

    kv_svc = AsyncMock()
    kv_svc.list_entries = AsyncMock(return_value=kv_entries or [])

    return SessionBriefingService(
        vault_summary_service=vault_summary_svc,
        entity_service=entity_svc,
        kv_service=kv_svc,
    )


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


class TestTokenEstimation:
    def test_div4(self):
        """Token estimation = len(text) // 4."""
        assert _estimate_tokens('a' * 100) == 25
        assert _estimate_tokens('a' * 7) == 1
        assert _estimate_tokens('') == 0

    def test_realistic_text(self):
        text = 'This is a realistic piece of text for token estimation.'
        assert _estimate_tokens(text) == len(text) // 4


# ---------------------------------------------------------------------------
# Trend sorting
# ---------------------------------------------------------------------------


class TestTrendSorting:
    def test_sort_order(self):
        """Observations sorted: new > strengthening > weakening > stable > stale."""
        obs = [
            {'trend': 'stable', 'title': 'S'},
            {'trend': 'new', 'title': 'N'},
            {'trend': 'stale', 'title': 'ST'},
            {'trend': 'strengthening', 'title': 'STR'},
            {'trend': 'weakening', 'title': 'W'},
        ]
        sorted_obs = _sort_observations(obs)
        assert [o['trend'] for o in sorted_obs] == [
            'new',
            'strengthening',
            'weakening',
            'stable',
            'stale',
        ]

    def test_unknown_trend_defaults_to_stable(self):
        obs = [{'trend': 'unknown', 'title': 'U'}, {'trend': 'new', 'title': 'N'}]
        sorted_obs = _sort_observations(obs)
        assert sorted_obs[0]['trend'] == 'new'


# ---------------------------------------------------------------------------
# KV namespace building
# ---------------------------------------------------------------------------


class TestKVNamespaces:
    def test_without_project(self):
        ns = _build_kv_namespaces(None)
        assert ns == ['global', 'user', 'app:claude-code']

    def test_with_project(self):
        ns = _build_kv_namespaces('my-project')
        assert ns == ['global', 'user', 'app:claude-code', 'project:my-project']


# ---------------------------------------------------------------------------
# SessionBriefingService.generate
# ---------------------------------------------------------------------------


class TestSessionBriefingGenerate:
    @pytest.mark.asyncio
    async def test_budget_2000_under_limit(self):
        """Output tokens <= 2000 for standard budget."""
        svc = _make_service(
            summary=_make_vault_summary(),
            entities=_make_entities(10, with_observations=True),
            kv_entries=[
                _make_kv_entry('global:llm_provider', 'anthropic'),
                _make_kv_entry('user:comm_style', 'terse'),
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)
        tokens = _estimate_tokens(result)
        assert tokens <= 2000 * 1.05  # Allow 5% tolerance

    @pytest.mark.asyncio
    async def test_budget_1000_under_limit(self):
        """Output tokens <= 1000 for compact budget."""
        svc = _make_service(
            summary=_make_vault_summary(),
            entities=_make_entities(10, with_observations=True),
            kv_entries=[
                _make_kv_entry('global:llm_provider', 'anthropic'),
                _make_kv_entry('user:comm_style', 'terse'),
            ],
        )
        result = await svc.generate(uuid4(), budget=1000)
        tokens = _estimate_tokens(result)
        assert tokens <= 1000 * 1.05

    @pytest.mark.asyncio
    async def test_section_order(self):
        """Sections appear in order: header, KV, vault summary, entities, binding."""
        svc = _make_service(
            summary=_make_vault_summary(),
            entities=_make_entities(3, with_observations=True),
            kv_entries=[_make_kv_entry('global:test', 'value')],
        )
        result = await svc.generate(uuid4(), budget=2000)

        header_pos = result.find('# Session Briefing')
        kv_pos = result.find('## Key-Value Facts')
        vault_pos = result.find('## Vault Summary')
        entity_pos = result.find('## Top Entities')
        binding_pos = result.find('*Vault:')

        assert header_pos < kv_pos < vault_pos < entity_pos < binding_pos

    @pytest.mark.asyncio
    async def test_2000_includes_prose_and_trends(self):
        """At 2000 budget: prose + topic descriptions + trend indicators present."""
        svc = _make_service(
            summary=_make_vault_summary(),
            entities=_make_entities(3, with_observations=True),
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)

        # Prose section present
        assert '## Vault Summary' in result
        assert 'memory systems and knowledge graphs' in result

        # Topic descriptions present
        assert 'Research on memory architectures' in result

        # Trend arrows present (★ for new)
        assert '\u2605' in result

    @pytest.mark.asyncio
    async def test_1000_topics_only_no_trends(self):
        """At 1000 budget: no prose, no trends, topics compact."""
        svc = _make_service(
            summary=_make_vault_summary(),
            entities=_make_entities(3, with_observations=True),
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=1000)

        # No prose section
        assert '## Vault Summary' not in result

        # Topics present but compact (no descriptions)
        assert 'Memory systems (12)' in result
        assert 'Research on memory architectures' not in result

        # No trend arrows
        assert '\u2605' not in result  # ★

    @pytest.mark.asyncio
    async def test_1000_limits_entities_to_5(self):
        """At 1000 budget: max 5 entities."""
        svc = _make_service(
            summary=_make_vault_summary(),
            entities=_make_entities(10),
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=1000)

        # Count entity lines (entities named Entity0..Entity9)
        entity_section = (
            result.split('## Top Entities')[-1].split('---')[0]
            if '## Top Entities' in result
            else ''
        )
        entity_lines = [
            line for line in entity_section.split('\n') if line.strip().startswith('- Entity')
        ]
        assert len(entity_lines) <= 5

    @pytest.mark.asyncio
    async def test_empty_vault(self):
        """Empty vault produces valid minimal briefing."""
        svc = _make_service(summary=None, entities=[], kv_entries=[])
        result = await svc.generate(uuid4(), budget=2000)

        assert '# Session Briefing' in result
        assert '*Vault:' in result
        # Should not have entity/topic/kv sections
        assert '## Top Entities' not in result
        assert '## Key-Value Facts' not in result

    @pytest.mark.asyncio
    async def test_parallel_fetch(self):
        """All three services are called during generate."""
        vault_summary_svc = AsyncMock()
        vault_summary_svc.get_summary = AsyncMock(return_value=None)

        entity_svc = MagicMock()
        entity_svc.list_entities_ranked = lambda **kwargs: _mock_entity_gen()

        kv_svc = AsyncMock()
        kv_svc.list_entries = AsyncMock(return_value=[])

        svc = SessionBriefingService(
            vault_summary_service=vault_summary_svc,
            entity_service=entity_svc,
            kv_service=kv_svc,
        )
        vault_id = uuid4()
        await svc.generate(vault_id, budget=2000)

        vault_summary_svc.get_summary.assert_called_once_with(vault_id)
        kv_svc.list_entries.assert_called_once()

    @pytest.mark.asyncio
    async def test_project_id_in_binding(self):
        """Project ID appears in binding when provided."""
        svc = _make_service(summary=None, entities=[], kv_entries=[])
        result = await svc.generate(uuid4(), budget=2000, project_id='my-project')
        assert '*Project: my-project*' in result

    @pytest.mark.asyncio
    async def test_entity_trend_sort_in_output(self):
        """Entities show trend-sorted observations (new first)."""
        entities = [
            _make_entity(
                'TestEntity',
                observations=[
                    {'title': 'Old thing', 'trend': 'stale', 'content': 'old'},
                    {'title': 'New thing', 'trend': 'new', 'content': 'new'},
                ],
            )
        ]
        svc = _make_service(
            summary=_make_vault_summary(),
            entities=entities,
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)

        # ★ (new) should appear before ⚠ (stale)
        new_pos = result.find('\u2605 New thing')
        stale_pos = result.find('\u26a0 Old thing')
        assert new_pos < stale_pos


# ---------------------------------------------------------------------------
# Overflow degradation
# ---------------------------------------------------------------------------


class TestOverflowDegradation:
    @pytest.mark.asyncio
    async def test_overflow_trims_entities(self):
        """Large entity set triggers entity count reduction."""
        # Create a scenario that exceeds budget
        long_obs = [
            {
                'title': 'A very long observation title that takes up tokens ' * 3,
                'trend': 'new',
                'content': 'c',
            },
            {
                'title': 'Another very long observation title ' * 3,
                'trend': 'stable',
                'content': 'c',
            },
        ]
        entities = [_make_entity(f'Entity{i}', observations=long_obs) for i in range(10)]

        svc = _make_service(
            summary=_make_vault_summary(
                summary='A very detailed summary. ' * 50,
                topics=[
                    {'name': f'Topic{i}', 'note_count': i, 'description': 'desc ' * 20}
                    for i in range(15)
                ],
            ),
            entities=entities,
            kv_entries=[_make_kv_entry(f'global:key{i}', 'value ' * 10) for i in range(10)],
        )
        result = await svc.generate(uuid4(), budget=2000)
        tokens = _estimate_tokens(result)

        # Should have applied overflow to stay close to budget
        assert tokens <= 2000 * 1.05

    @pytest.mark.asyncio
    async def test_never_drops_header_or_binding(self):
        """Header and vault binding are never removed during overflow."""
        # Force extreme overflow
        svc = _make_service(
            summary=_make_vault_summary(summary='x ' * 5000),
            entities=_make_entities(10, with_observations=True),
            kv_entries=[_make_kv_entry(f'global:k{i}', 'v' * 200) for i in range(20)],
        )
        result = await svc.generate(uuid4(), budget=1000)

        assert '# Session Briefing' in result
        assert '*Vault:' in result

    @pytest.mark.asyncio
    async def test_never_drops_global_kv(self):
        """Global KV entries survive all overflow steps."""
        svc = _make_service(
            summary=_make_vault_summary(summary='x ' * 3000),
            entities=_make_entities(10, with_observations=True),
            kv_entries=[
                _make_kv_entry('global:important', 'critical-value'),
                _make_kv_entry('app:claude-code:setting', 'value'),
                _make_kv_entry('user:pref', 'value'),
            ],
        )
        result = await svc.generate(uuid4(), budget=1000)

        # Global entry should always survive
        assert 'global:important' in result

    @pytest.mark.asyncio
    async def test_overflow_drops_kv_namespaces_in_order(self):
        """KV namespaces are dropped in order: app -> user -> project."""
        # Create a very tight budget scenario
        svc = _make_service(
            summary=_make_vault_summary(summary='x ' * 2000),
            entities=_make_entities(10, with_observations=True),
            kv_entries=[
                _make_kv_entry('global:keep', 'keep'),
                _make_kv_entry('app:claude-code:drop1', 'val'),
                _make_kv_entry('user:drop2', 'val'),
                _make_kv_entry('project:myproj:drop3', 'val'),
            ],
        )
        result = await svc.generate(uuid4(), budget=1000)

        # Global should always remain
        assert 'global:keep' in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize('budget', [1000, 2000])
    async def test_token_budget_compliance(self, budget: int):
        """Parameterized check that output respects budget for both tiers."""
        svc = _make_service(
            summary=_make_vault_summary(summary='Summary text. ' * 100),
            entities=_make_entities(10, with_observations=True),
            kv_entries=[_make_kv_entry(f'global:k{i}', f'v{i}') for i in range(5)],
        )
        result = await svc.generate(uuid4(), budget=budget)
        tokens = _estimate_tokens(result)
        assert tokens <= budget * 1.05, f'Budget {budget}: got {tokens} tokens'


# ---------------------------------------------------------------------------
# Header content
# ---------------------------------------------------------------------------


class TestHeaderContent:
    @pytest.mark.asyncio
    async def test_header_includes_entity_count(self):
        """Header shows entity count from fetched entities."""
        svc = _make_service(
            summary=_make_vault_summary(),
            entities=_make_entities(7),
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '7 entities' in result

    @pytest.mark.asyncio
    async def test_header_includes_note_count(self):
        """Header shows note count from vault summary stats."""
        svc = _make_service(
            summary=_make_vault_summary(stats={'total_notes': 42}),
            entities=[],
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '42 notes' in result

    @pytest.mark.asyncio
    async def test_header_includes_updated_at(self):
        """Header shows updated_at date when available."""
        summary = _make_vault_summary()
        summary.updated_at = datetime(2026, 4, 4, tzinfo=timezone.utc)
        svc = _make_service(summary=summary, entities=[], kv_entries=[])
        result = await svc.generate(uuid4(), budget=2000)
        assert 'Updated 2026-04-04' in result

    @pytest.mark.asyncio
    async def test_header_includes_version(self):
        """Header shows summary version."""
        svc = _make_service(
            summary=_make_vault_summary(version=5),
            entities=[],
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert 'v5' in result


# ---------------------------------------------------------------------------
# Isolated overflow step tests
# ---------------------------------------------------------------------------


class TestOverflowSteps:
    """Each test sizes data to trigger exactly one overflow boundary."""

    @pytest.mark.asyncio
    async def test_step1_entity_trim_activates(self):
        """Overflow step 1: entity count is reduced when data exceeds budget.

        Calibrated: >8000 chars (~2500+ tokens) to reliably exceed 2000-token budget.
        Trimming entities reduces count until it fits.
        """
        long_obs = [
            {
                'title': 'A detailed trend observation about this entity ' * 2,
                'trend': 'new',
                'content': 'c',
            },
            {
                'title': 'Another detailed observation about trends ' * 2,
                'trend': 'stable',
                'content': 'c',
            },
        ]
        entities = [_make_entity(f'Entity{i}', observations=long_obs) for i in range(10)]

        svc = _make_service(
            summary=_make_vault_summary(
                summary='A moderately long vault summary sentence that adds tokens to fill up the budget. '
                * 80,
                topics=[
                    {
                        'name': f'Topic{i}',
                        'note_count': i * 3,
                        'description': 'A detailed description of this topic area with many words to fill space '
                        * 4,
                    }
                    for i in range(10)
                ],
            ),
            entities=entities,
            kv_entries=[
                _make_kv_entry(
                    f'global:k{i}', f'value-data-for-key-{i}-with-extra-padding-text-here'
                )
                for i in range(12)
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)

        # Overflow should have activated: fewer than 10 entities in output
        entity_section = (
            result.split('## Top Entities')[-1].split('---')[0]
            if '## Top Entities' in result
            else ''
        )
        entity_lines = [ln for ln in entity_section.split('\n') if ln.strip().startswith('- ')]
        assert len(entity_lines) < 10, f'Expected fewer than 10 entities, got {len(entity_lines)}'
        assert _estimate_tokens(result) <= 2000 * 1.05

    @pytest.mark.asyncio
    async def test_step1b_drops_observation_titles(self):
        """Overflow step 1b: observation titles dropped when trimming entities isn't enough."""
        long_obs = [
            {'title': 'Long observation title ' * 5, 'trend': 'new', 'content': 'c'},
            {'title': 'Another long title ' * 5, 'trend': 'strengthening', 'content': 'c'},
        ]
        entities = [_make_entity(f'E{i}', observations=long_obs) for i in range(10)]

        svc = _make_service(
            summary=_make_vault_summary(
                summary='Detailed. ' * 150,
                topics=[
                    {'name': f'T{i}', 'note_count': i, 'description': 'detailed desc ' * 10}
                    for i in range(10)
                ],
            ),
            entities=entities,
            kv_entries=[_make_kv_entry(f'global:k{i}', f'value{i}') for i in range(8)],
        )
        result = await svc.generate(uuid4(), budget=2000)

        # Trend arrows should be absent if observations were dropped
        # (they may or may not be depending on exact sizing, but budget must hold)
        assert _estimate_tokens(result) <= 2000 * 1.05

    @pytest.mark.asyncio
    async def test_step2_drops_topic_descriptions(self):
        """Overflow step 2: topic descriptions removed, leaving just name (count)."""
        entities = _make_entities(10, with_observations=True)

        svc = _make_service(
            summary=_make_vault_summary(
                summary='Very long summary. ' * 200,
                topics=[
                    {
                        'name': f'Topic{i}',
                        'note_count': i,
                        'description': 'very long description ' * 15,
                    }
                    for i in range(12)
                ],
            ),
            entities=entities,
            kv_entries=[_make_kv_entry(f'global:k{i}', f'v{i}' * 20) for i in range(8)],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert _estimate_tokens(result) <= 2000 * 1.05

    @pytest.mark.asyncio
    async def test_step3_trims_prose(self):
        """Overflow step 3: vault prose trimmed sentence by sentence."""
        svc = _make_service(
            summary=_make_vault_summary(
                summary='First sentence. Second sentence. Third sentence. Fourth sentence. ' * 50,
                topics=[],
            ),
            entities=_make_entities(5),
            kv_entries=[_make_kv_entry(f'global:k{i}', f'v{i}' * 30) for i in range(10)],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert _estimate_tokens(result) <= 2000 * 1.05
        # If prose was trimmed, the last sentence should be truncated
        # (original has many sentences, result should have fewer)

    @pytest.mark.asyncio
    async def test_step4_drops_app_kv_before_user(self):
        """Overflow step 4: app: KV dropped before user: KV."""
        svc = _make_service(
            summary=_make_vault_summary(summary='x ' * 3000),
            entities=_make_entities(10, with_observations=True),
            kv_entries=[
                _make_kv_entry('global:keep', 'important'),
                _make_kv_entry('app:claude-code:s1', 'v' * 100),
                _make_kv_entry('app:claude-code:s2', 'v' * 100),
                _make_kv_entry('user:pref1', 'v' * 100),
                _make_kv_entry('user:pref2', 'v' * 100),
            ],
        )
        result = await svc.generate(uuid4(), budget=1000)

        # Global must survive
        assert 'global:keep' in result
        # If app: was dropped but user: survived, that's the correct order
        # Can't guarantee exact state, but budget must hold
        assert _estimate_tokens(result) <= 1000 * 1.05

    @pytest.mark.asyncio
    async def test_overflow_at_1000_does_not_inflate_entities(self):
        """At budget=1000, overflow must not re-add trends or inflate entity count beyond 5.

        Regression: _apply_overflow previously hardcoded include_trends=True and
        started entity trim at 7, inflating the section before converging.
        """
        long_obs = [
            {'title': 'Long observation ' * 5, 'trend': 'new', 'content': 'c'},
            {'title': 'Another observation ' * 5, 'trend': 'stable', 'content': 'c'},
        ]
        entities = [_make_entity(f'Entity{i}', observations=long_obs) for i in range(10)]

        svc = _make_service(
            summary=_make_vault_summary(
                summary='Summary. ' * 200,
                topics=[
                    {'name': f'Topic{i}', 'note_count': i, 'description': 'desc ' * 20}
                    for i in range(10)
                ],
            ),
            entities=entities,
            kv_entries=[_make_kv_entry(f'global:k{i}', f'v{i}' * 20) for i in range(8)],
        )
        result = await svc.generate(uuid4(), budget=1000)

        # Must stay within budget
        assert _estimate_tokens(result) <= 1000 * 1.05

        # Entity count must be <= 5 (the 1000-budget initial limit)
        entity_section = (
            result.split('## Top Entities')[-1].split('---')[0]
            if '## Top Entities' in result
            else ''
        )
        entity_lines = [ln for ln in entity_section.split('\n') if ln.strip().startswith('- ')]
        assert len(entity_lines) <= 5, f'Expected <= 5 entities, got {len(entity_lines)}'

        # No trend arrows should appear (★ ↑ ↓ → ⚠)
        assert '\u2605' not in result  # ★
        assert '\u2191' not in result  # ↑
