"""Unit tests for SessionBriefingService."""

from __future__ import annotations

import json

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from memex_core.memory.sql_models import MentalModel
from memex_core.services.session_briefing import (
    SessionBriefingService,
    _compute_importance,
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
        'narrative': 'This vault tracks research into memory systems and knowledge graphs.',
        'themes': [
            {
                'name': 'Memory systems',
                'description': 'Research on memory architectures',
                'note_count': 12,
                'trend': 'growing',
                'last_addition': '2026-04-06',
                'representative_titles': ['Memory Architecture Design'],
            },
            {
                'name': 'Entity extraction',
                'description': 'NER pipelines and methods',
                'note_count': 8,
                'trend': 'stable',
                'last_addition': '2026-04-01',
                'representative_titles': ['NER Pipeline Overview'],
            },
            {
                'name': 'Retrieval',
                'description': 'Search and retrieval strategies',
                'note_count': 5,
                'trend': 'growing',
                'last_addition': '2026-04-05',
                'representative_titles': ['TEMPR Strategy'],
            },
        ],
        'inventory': {
            'total_notes': 42,
            'total_entities': 15,
            'date_range': {'earliest': '2024-01-01', 'latest': '2026-04-06'},
            'recent_activity': {'7d': 3, '30d': 10},
        },
        'key_entities': [
            {'name': 'Memory Systems', 'type': 'concept', 'mention_count': 12},
            {'name': 'DSPy', 'type': 'technology', 'mention_count': 8},
        ],
        'version': 5,
        'notes_incorporated': 42,
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _make_mental_model(
    name: str,
    category: str = 'Technology',
    observation_count: int = 5,
    description: str = '',
    observations: list[dict] | None = None,
    last_refreshed: datetime | None = None,
    vault_id=None,
) -> MentalModel:
    """Create a mock MentalModel."""
    mm = MagicMock(spec=MentalModel)
    mm.name = name
    mm.vault_id = vault_id or uuid4()
    mm.entity_metadata = {
        'description': description or f'{name} description',
        'category': category,
        'observation_count': observation_count,
    }
    mm.observations = observations or []
    mm.last_refreshed = last_refreshed or datetime(2026, 4, 1, tzinfo=timezone.utc)
    mm.version = 1
    return mm


def _make_kv_entry(key: str, value: str) -> MagicMock:
    """Create a mock KV entry."""
    entry = MagicMock()
    entry.key = key
    entry.value = value
    entry.updated_at = datetime.now(timezone.utc)
    return entry


def _make_mental_models(count: int, with_observations: bool = False) -> list[MentalModel]:
    """Create a list of mock mental models."""
    models = []
    for i in range(count):
        obs = []
        if with_observations:
            obs = [
                {'title': f'Observation {i}a', 'trend': 'new', 'content': f'Content {i}a'},
                {'title': f'Observation {i}b', 'trend': 'stable', 'content': f'Content {i}b'},
            ]
        models.append(
            _make_mental_model(
                f'Entity{i}',
                observation_count=100 - i,
                observations=obs,
            )
        )
    return models


def _mock_vault(name: str, description: str = '', note_count: int = 0, vault_id=None):
    """Create a mock vault dict matching VaultService.list_vaults_with_counts() output."""
    vault = MagicMock()
    vault.name = name
    vault.description = description
    vault.id = vault_id or uuid4()
    vault.note_count = note_count
    return {'vault': vault, 'note_count': note_count}


def _mock_metastore(mental_models: list[MentalModel] | None = None):
    """Create a mock metastore that returns mental models from session.exec()."""
    metastore = MagicMock()
    session = AsyncMock()

    result_mock = MagicMock()
    result_mock.all.return_value = mental_models or []
    session.exec = AsyncMock(return_value=result_mock)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore.session.return_value = ctx

    return metastore


def _make_service(
    summary=None,
    mental_models=None,
    kv_entries=None,
    vaults=None,
) -> SessionBriefingService:
    """Create a SessionBriefingService with mocked dependencies."""
    vault_summary_svc = AsyncMock()
    vault_summary_svc.get_summary = AsyncMock(return_value=summary)

    metastore = _mock_metastore(mental_models or [])

    kv_svc = AsyncMock()
    kv_svc.list_entries = AsyncMock(return_value=kv_entries or [])

    vault_svc = AsyncMock()
    vault_svc.list_vaults_with_counts = AsyncMock(return_value=vaults or [])

    return SessionBriefingService(
        vault_summary_service=vault_summary_svc,
        metastore=metastore,
        kv_service=kv_svc,
        vault_service=vault_svc,
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
# Importance scoring
# ---------------------------------------------------------------------------


class TestImportanceScoring:
    def test_new_observations_score_highest(self):
        """Mental models with 'new' observations rank higher."""
        mm_new = _make_mental_model(
            'NewEntity',
            observations=[{'trend': 'new', 'title': 'x'}],
        )
        mm_stable = _make_mental_model(
            'StableEntity',
            observations=[{'trend': 'stable', 'title': 'x'}],
        )
        assert _compute_importance(mm_new) > _compute_importance(mm_stable)

    def test_stale_observations_score_zero(self):
        """Stale observations contribute nothing to importance."""
        mm = _make_mental_model(
            'StaleEntity',
            observations=[{'trend': 'stale', 'title': 'x'}],
        )
        assert _compute_importance(mm) == 0.0

    def test_mixed_trends(self):
        """Weighted sum of mixed trends."""
        mm = _make_mental_model(
            'MixedEntity',
            observations=[
                {'trend': 'new', 'title': 'a'},  # 3.0
                {'trend': 'stable', 'title': 'b'},  # 0.5
                {'trend': 'stale', 'title': 'c'},  # 0.0
            ],
        )
        assert _compute_importance(mm) == 3.5

    def test_empty_observations(self):
        mm = _make_mental_model('Empty', observations=[])
        assert _compute_importance(mm) == 0.0


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
        assert ns == ['global', 'user', 'app:claude-code', 'procedure']

    def test_with_project(self):
        ns = _build_kv_namespaces('my-project')
        assert ns == [
            'global',
            'user',
            'app:claude-code',
            'procedure',
            'project:my-project',
        ]


# ---------------------------------------------------------------------------
# SessionBriefingService.generate
# ---------------------------------------------------------------------------


class TestSessionBriefingGenerate:
    @pytest.mark.asyncio
    async def test_budget_2000_under_limit(self):
        """Output tokens <= 2000 for standard budget."""
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=_make_mental_models(10, with_observations=True),
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
            mental_models=_make_mental_models(10, with_observations=True),
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
        """Sections appear in order: header, KV, procedures, vault overview, entities, binding."""
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=_make_mental_models(3, with_observations=True),
            kv_entries=[
                _make_kv_entry('global:test', 'value'),
                _make_kv_entry(
                    'procedure:answer:test',
                    json.dumps({'v': 1, 'value': 'sample procedure', 'tags': {}, 'history': []}),
                ),
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)

        header_pos = result.find('# Session Briefing')
        kv_pos = result.find('## Key-Value Facts')
        proc_pos = result.find('## Procedures')
        vault_pos = result.find('## Vault Overview')
        entity_pos = result.find('## Top Entities')
        binding_pos = result.find('*Vault:')

        assert header_pos < kv_pos < proc_pos < vault_pos < entity_pos < binding_pos

    @pytest.mark.asyncio
    async def test_2000_includes_narrative_and_trends(self):
        """At 2000 budget: narrative + theme descriptions + trend indicators present."""
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=_make_mental_models(3, with_observations=True),
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)

        # Vault overview section present with narrative
        assert '## Vault Overview' in result
        assert 'memory systems and knowledge graphs' in result

        # Theme descriptions present (non-compact mode)
        assert 'Research on memory architectures' in result

        # Theme trend arrows present (↑ for growing)
        assert '\u2191' in result

        # Mental model trend arrows present (★ for new observations)
        assert '\u2605' in result

    @pytest.mark.asyncio
    async def test_1000_compact_vault_overview_no_entity_trends(self):
        """At 1000 budget: compact vault overview (no theme descriptions), no entity trends."""
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=_make_mental_models(3, with_observations=True),
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=1000)

        # Vault overview present with compact themes (no descriptions)
        assert '## Vault Overview' in result
        assert 'Memory systems (12)' in result
        assert 'Research on memory architectures' not in result

        # No entity trend arrows (★ for new observations)
        assert '\u2605' not in result  # ★

    @pytest.mark.asyncio
    async def test_1000_limits_models_to_5(self):
        """At 1000 budget: max 5 mental models."""
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=_make_mental_models(10),
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=1000)

        # Count entity lines (models named Entity0..Entity9)
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
        svc = _make_service(summary=None, mental_models=[], kv_entries=[])
        result = await svc.generate(uuid4(), budget=2000)

        assert '# Session Briefing' in result
        assert '*Vault:' in result
        # Should not have entity/vault overview/kv sections
        assert '## Top Entities' not in result
        assert '## Vault Overview' not in result
        assert '## Key-Value Facts' not in result

    @pytest.mark.asyncio
    async def test_parallel_fetch(self):
        """All services are called during generate."""
        vault_summary_svc = AsyncMock()
        vault_summary_svc.get_summary = AsyncMock(return_value=None)

        metastore = _mock_metastore([])

        kv_svc = AsyncMock()
        kv_svc.list_entries = AsyncMock(return_value=[])

        svc = SessionBriefingService(
            vault_summary_service=vault_summary_svc,
            metastore=metastore,
            kv_service=kv_svc,
        )
        vault_id = uuid4()
        await svc.generate(vault_id, budget=2000)

        vault_summary_svc.get_summary.assert_called_once_with(vault_id)
        kv_svc.list_entries.assert_called_once()

    @pytest.mark.asyncio
    async def test_project_id_in_binding(self):
        """Project ID appears in binding when provided."""
        svc = _make_service(summary=None, mental_models=[], kv_entries=[])
        result = await svc.generate(uuid4(), budget=2000, project_id='my-project')
        assert '*Project: my-project*' in result

    @pytest.mark.asyncio
    async def test_trend_sort_in_output(self):
        """Mental models show trend-sorted observations (new first)."""
        models = [
            _make_mental_model(
                'TestEntity',
                observations=[
                    {'title': 'Old thing', 'trend': 'stale', 'content': 'old'},
                    {'title': 'New thing', 'trend': 'new', 'content': 'new'},
                ],
            )
        ]
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=models,
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)

        # ★ (new) should appear before ⚠ (stale)
        new_pos = result.find('\u2605 New thing')
        stale_pos = result.find('\u26a0 Old thing')
        assert new_pos < stale_pos

    @pytest.mark.asyncio
    async def test_description_in_output(self):
        """Mental model description appears in output."""
        models = [_make_mental_model('DSPy', description='ML framework for LLM pipelines')]
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=models,
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert 'ML framework for LLM pipelines' in result

    @pytest.mark.asyncio
    async def test_last_seen_in_output(self):
        """Mental model last_refreshed appears in output at budget >= 2000."""
        models = [
            _make_mental_model(
                'DSPy',
                last_refreshed=datetime(2026, 4, 5, tzinfo=timezone.utc),
                observations=[{'title': 'Active', 'trend': 'new', 'content': 'x'}],
            )
        ]
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=models,
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert 'Last seen: 2026-04-05' in result

    @pytest.mark.asyncio
    async def test_category_and_obs_count_in_output(self):
        """Category and observation count appear in output."""
        models = [_make_mental_model('DSPy', category='Technology', observation_count=7)]
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=models,
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert 'Technology' in result
        assert '7 obs' in result


# ---------------------------------------------------------------------------
# Overflow degradation
# ---------------------------------------------------------------------------


class TestOverflowDegradation:
    @pytest.mark.asyncio
    async def test_overflow_trims_models(self):
        """Large model set triggers model count reduction."""
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
        models = [_make_mental_model(f'Entity{i}', observations=long_obs) for i in range(10)]

        svc = _make_service(
            summary=_make_vault_summary(
                narrative='A very detailed summary. ' * 50,
                themes=[
                    {
                        'name': f'Theme{i}',
                        'description': 'desc ' * 20,
                        'note_count': i,
                        'trend': 'growing',
                        'last_addition': '2026-04-06',
                        'representative_titles': [f'Title {i}'],
                    }
                    for i in range(15)
                ],
            ),
            mental_models=models,
            kv_entries=[_make_kv_entry(f'global:key{i}', 'value ' * 10) for i in range(10)],
        )
        result = await svc.generate(uuid4(), budget=2000)
        tokens = _estimate_tokens(result)

        # Should have applied overflow to stay close to budget
        assert tokens <= 2000 * 1.05

    @pytest.mark.asyncio
    async def test_never_drops_header_or_binding(self):
        """Header and vault binding are never removed during overflow."""
        svc = _make_service(
            summary=_make_vault_summary(narrative='x ' * 5000),
            mental_models=_make_mental_models(10, with_observations=True),
            kv_entries=[_make_kv_entry(f'global:k{i}', 'v' * 200) for i in range(20)],
        )
        result = await svc.generate(uuid4(), budget=1000)

        assert '# Session Briefing' in result
        assert '*Vault:' in result

    @pytest.mark.asyncio
    async def test_never_drops_global_kv(self):
        """Global KV entries survive all overflow steps."""
        svc = _make_service(
            summary=_make_vault_summary(narrative='x ' * 3000),
            mental_models=_make_mental_models(10, with_observations=True),
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
        svc = _make_service(
            summary=_make_vault_summary(narrative='x ' * 2000),
            mental_models=_make_mental_models(10, with_observations=True),
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
            summary=_make_vault_summary(narrative='Summary text. ' * 100),
            mental_models=_make_mental_models(10, with_observations=True),
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
    async def test_header_includes_model_count(self):
        """Header shows mental model count."""
        svc = _make_service(
            summary=_make_vault_summary(),
            mental_models=_make_mental_models(7),
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '7 mental models' in result

    @pytest.mark.asyncio
    async def test_header_includes_note_count(self):
        """Header shows note count from vault summary inventory."""
        svc = _make_service(
            summary=_make_vault_summary(
                inventory={
                    'total_notes': 42,
                    'total_entities': 15,
                    'date_range': {'earliest': '2024-01-01', 'latest': '2026-04-06'},
                    'recent_activity': {'7d': 3, '30d': 10},
                }
            ),
            mental_models=[],
            kv_entries=[],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '42 notes' in result

    @pytest.mark.asyncio
    async def test_header_includes_updated_at(self):
        """Header shows updated_at date when available."""
        summary = _make_vault_summary()
        summary.updated_at = datetime(2026, 4, 4, tzinfo=timezone.utc)
        svc = _make_service(summary=summary, mental_models=[], kv_entries=[])
        result = await svc.generate(uuid4(), budget=2000)
        assert 'Updated 2026-04-04' in result

    @pytest.mark.asyncio
    async def test_header_includes_version(self):
        """Header shows summary version."""
        svc = _make_service(
            summary=_make_vault_summary(version=5),
            mental_models=[],
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
    async def test_step1_model_trim_activates(self):
        """Overflow step 1: model count is reduced when data exceeds budget.

        Calibrated: >8000 chars (~2500+ tokens) to reliably exceed 2000-token budget.
        Trimming models reduces count until it fits.
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
        models = [_make_mental_model(f'Entity{i}', observations=long_obs) for i in range(10)]

        svc = _make_service(
            summary=_make_vault_summary(
                narrative='A moderately long vault summary sentence that adds tokens. ' * 80,
                themes=[
                    {
                        'name': f'Theme{i}',
                        'description': 'A detailed description of this theme area ' * 4,
                        'note_count': i * 3,
                        'trend': 'growing',
                        'last_addition': '2026-04-06',
                        'representative_titles': [f'Title {i}'],
                    }
                    for i in range(10)
                ],
            ),
            mental_models=models,
            kv_entries=[
                _make_kv_entry(
                    f'global:k{i}', f'value-data-for-key-{i}-with-extra-padding-text-here'
                )
                for i in range(12)
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)

        # Overflow should have activated: fewer than 10 models in output
        entity_section = (
            result.split('## Top Entities')[-1].split('---')[0]
            if '## Top Entities' in result
            else ''
        )
        entity_lines = [ln for ln in entity_section.split('\n') if ln.strip().startswith('- ')]
        assert len(entity_lines) < 10, f'Expected fewer than 10 models, got {len(entity_lines)}'
        assert _estimate_tokens(result) <= 2000 * 1.05

    @pytest.mark.asyncio
    async def test_step1b_drops_observation_titles(self):
        """Overflow step 1b: observation titles dropped when trimming isn't enough."""
        long_obs = [
            {'title': 'Long observation title ' * 5, 'trend': 'new', 'content': 'c'},
            {'title': 'Another long title ' * 5, 'trend': 'strengthening', 'content': 'c'},
        ]
        models = [_make_mental_model(f'E{i}', observations=long_obs) for i in range(10)]

        svc = _make_service(
            summary=_make_vault_summary(
                narrative='Detailed. ' * 150,
                themes=[
                    {
                        'name': f'T{i}',
                        'description': 'detailed desc ' * 10,
                        'note_count': i,
                        'trend': 'growing',
                        'last_addition': '2026-04-06',
                        'representative_titles': [f'Title {i}'],
                    }
                    for i in range(10)
                ],
            ),
            mental_models=models,
            kv_entries=[_make_kv_entry(f'global:k{i}', f'value{i}') for i in range(8)],
        )
        result = await svc.generate(uuid4(), budget=2000)

        assert _estimate_tokens(result) <= 2000 * 1.05

    @pytest.mark.asyncio
    async def test_step2_drops_theme_descriptions(self):
        """Overflow step 2: theme descriptions removed, leaving just name (count)."""
        models = _make_mental_models(10, with_observations=True)

        svc = _make_service(
            summary=_make_vault_summary(
                narrative='Very long summary. ' * 200,
                themes=[
                    {
                        'name': f'Theme{i}',
                        'description': 'very long description ' * 15,
                        'note_count': i,
                        'trend': 'growing',
                        'last_addition': '2026-04-06',
                        'representative_titles': [f'Title {i}'],
                    }
                    for i in range(12)
                ],
            ),
            mental_models=models,
            kv_entries=[_make_kv_entry(f'global:k{i}', f'v{i}' * 20) for i in range(8)],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert _estimate_tokens(result) <= 2000 * 1.05

    @pytest.mark.asyncio
    async def test_step3_drops_vault_overview(self):
        """Overflow step 3: vault overview dropped entirely."""
        svc = _make_service(
            summary=_make_vault_summary(
                narrative='First sentence. Second sentence. Third sentence. ' * 50,
                themes=[],
            ),
            mental_models=_make_mental_models(5),
            kv_entries=[_make_kv_entry(f'global:k{i}', f'v{i}' * 30) for i in range(10)],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert _estimate_tokens(result) <= 2000 * 1.05

    @pytest.mark.asyncio
    async def test_step4_drops_app_kv_before_user(self):
        """Overflow step 4: app: KV dropped before user: KV."""
        svc = _make_service(
            summary=_make_vault_summary(narrative='x ' * 3000),
            mental_models=_make_mental_models(10, with_observations=True),
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
        assert _estimate_tokens(result) <= 1000 * 1.05

    @pytest.mark.asyncio
    async def test_overflow_at_1000_does_not_inflate_models(self):
        """At budget=1000, overflow must not re-add trends or inflate model count beyond 5."""
        long_obs = [
            {'title': 'Long observation ' * 5, 'trend': 'new', 'content': 'c'},
            {'title': 'Another observation ' * 5, 'trend': 'stable', 'content': 'c'},
        ]
        models = [_make_mental_model(f'Entity{i}', observations=long_obs) for i in range(10)]

        svc = _make_service(
            summary=_make_vault_summary(
                narrative='Summary. ' * 200,
                themes=[
                    {
                        'name': f'Theme{i}',
                        'description': 'desc ' * 20,
                        'note_count': i,
                        'trend': 'growing',
                        'last_addition': '2026-04-06',
                        'representative_titles': [f'Title {i}'],
                    }
                    for i in range(10)
                ],
            ),
            mental_models=models,
            kv_entries=[_make_kv_entry(f'global:k{i}', f'v{i}' * 20) for i in range(8)],
        )
        result = await svc.generate(uuid4(), budget=1000)

        # Must stay within budget
        assert _estimate_tokens(result) <= 1000 * 1.05

        # Model count must be <= 5 (the 1000-budget initial limit)
        entity_section = (
            result.split('## Top Entities')[-1].split('---')[0]
            if '## Top Entities' in result
            else ''
        )
        entity_lines = [ln for ln in entity_section.split('\n') if ln.strip().startswith('- ')]
        assert len(entity_lines) <= 5, f'Expected <= 5 models, got {len(entity_lines)}'

        # No entity observation trend arrows should appear (★ for new, ↓ for weakening)
        # Note: ↑ is now used for theme trends (growing) in vault overview, so we check
        # entity-specific indicators instead
        assert '\u2605' not in result  # ★ (new observation)
        # Verify no observation trend lines in entity section (indented trend text)
        if '## Top Entities' in result:
            entity_section = result.split('## Top Entities')[-1].split('---')[0]
            # Trend lines are indented with 2 spaces and contain trend arrows
            trend_lines = [
                ln for ln in entity_section.split('\n') if ln.startswith('  ') and ln.strip()
            ]
            assert len(trend_lines) == 0, f'Expected no trend lines, got {trend_lines}'


# ---------------------------------------------------------------------------
# Vaults section
# ---------------------------------------------------------------------------

VAULT_ID = uuid4()


class TestVaultsSection:
    @pytest.mark.asyncio
    async def test_vaults_section_appears(self):
        vaults = [
            _mock_vault('global', 'Default vault', 42, vault_id=VAULT_ID),
            _mock_vault('memex', 'Memex project', 156),
        ]
        svc = _make_service(
            summary=_make_vault_summary(),
            vaults=vaults,
        )
        result = await svc.generate(vault_id=VAULT_ID, budget=2000)
        assert '## Available Vaults' in result
        assert '**global**' in result
        assert '**memex**' in result

    @pytest.mark.asyncio
    async def test_vaults_shows_note_count(self):
        vaults = [_mock_vault('research', 'Research vault', 99)]
        svc = _make_service(summary=_make_vault_summary(), vaults=vaults)
        result = await svc.generate(vault_id=VAULT_ID, budget=2000)
        assert '99 notes' in result

    @pytest.mark.asyncio
    async def test_vaults_marks_active(self):
        vaults = [
            _mock_vault('global', 'Default', 10, vault_id=VAULT_ID),
            _mock_vault('other', 'Other vault', 5),
        ]
        svc = _make_service(summary=_make_vault_summary(), vaults=vaults)
        result = await svc.generate(vault_id=VAULT_ID, budget=2000)
        assert '**(active)**' in result

    @pytest.mark.asyncio
    async def test_empty_vaults_no_section(self):
        svc = _make_service(summary=_make_vault_summary(), vaults=[])
        result = await svc.generate(vault_id=VAULT_ID, budget=2000)
        assert '## Available Vaults' not in result

    @pytest.mark.asyncio
    async def test_vaults_description_truncated(self):
        long_desc = 'A' * 100
        vaults = [_mock_vault('test', long_desc, 5)]
        svc = _make_service(summary=_make_vault_summary(), vaults=vaults)
        result = await svc.generate(vault_id=VAULT_ID, budget=2000)
        assert '...' in result
        assert long_desc not in result


class TestProcedures:
    """V4: procedure KV rows render in their own ## Procedures section."""

    @staticmethod
    def _wrap(value: str) -> str:
        return json.dumps({'v': 1, 'value': value, 'tags': {}, 'history': []})

    @pytest.mark.asyncio
    async def test_section_present_when_procedure_rows_exist(self):
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[
                _make_kv_entry('procedure:answer:foo', self._wrap('answer like X')),
                _make_kv_entry('procedure:format:bar', self._wrap('format like Y')),
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedures' in result
        assert '**answer:foo**' in result
        assert 'answer like X' in result
        assert '**format:bar**' in result
        assert 'format like Y' in result

    @pytest.mark.asyncio
    async def test_section_absent_when_no_procedure_rows(self):
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[_make_kv_entry('global:k', 'v')],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedures' not in result

    @pytest.mark.asyncio
    async def test_project_scoped_procedure_renders_under_procedures_section(self):
        """A `project:<id>:procedure:<verb>:<context>` key must render in the
        ## Procedures section alongside global procedures, prefixed with the
        project tag so the scope is visible to the agent."""
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[
                _make_kv_entry('procedure:commit:lint-first', self._wrap('global rule')),
                _make_kv_entry(
                    'project:memex:procedure:commit:pr-workflow',
                    self._wrap('project rule'),
                ),
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedures' in result
        # Global procedure: bare verb:context label.
        assert '**commit:lint-first**' in result
        assert 'global rule' in result
        # Project procedure: bracketed scope prefix.
        assert '**\\[project:memex\\] commit:pr-workflow**' in result
        assert 'project rule' in result

    @pytest.mark.asyncio
    async def test_project_procedure_envelope_unwrap(self):
        """Project procedures use the same JSON envelope as global ones —
        the active value must be extracted via `parsed['value']`."""
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[
                _make_kv_entry(
                    'project:memex:procedure:deploy:gitops',
                    self._wrap('use ArgoCD'),
                ),
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert 'use ArgoCD' in result

    @pytest.mark.asyncio
    async def test_envelope_unwrap_extracts_value_field(self):
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[_make_kv_entry('procedure:do:x', self._wrap('hello'))],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert 'hello' in result
        assert '"v":' not in result.split('## Procedures')[1].split('---')[0]

    @pytest.mark.asyncio
    async def test_envelope_fallback_on_malformed_json(self):
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[_make_kv_entry('procedure:do:x', 'raw text not json')],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert 'raw text not json' in result

    @pytest.mark.asyncio
    async def test_envelope_fallback_on_non_dict_json(self):
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[_make_kv_entry('procedure:do:x', json.dumps([1, 2, 3]))],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '[1, 2, 3]' in result

    @pytest.mark.asyncio
    async def test_kv_section_excludes_procedure_entries(self):
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[
                _make_kv_entry('global:foo', 'bar'),
                _make_kv_entry('procedure:do:x', self._wrap('proc text')),
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)
        kv_section = result.split('## Key-Value Facts')[1].split('##')[0]
        assert 'procedure:do:x' not in kv_section
        assert 'proc text' not in kv_section
        assert 'global:foo' in kv_section

    @pytest.mark.asyncio
    async def test_user_namespace_included_in_briefing(self):
        """Guard: `user:*` rows must appear in KV section (regression pin)."""
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[_make_kv_entry('user:editor', 'neovim')],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Key-Value Facts' in result
        assert 'user:editor' in result
        assert 'neovim' in result

    @pytest.mark.asyncio
    async def test_value_truncation_at_max_chars(self):
        from memex_core.services.session_briefing import _PROCEDURE_VALUE_MAX_CHARS

        long_text = 'x' * (_PROCEDURE_VALUE_MAX_CHARS + 500)
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[_make_kv_entry('procedure:do:x', self._wrap(long_text))],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '…' in result
        # Rendered text should be ≤ max + ellipsis; not the full long_text.
        assert ('x' * (_PROCEDURE_VALUE_MAX_CHARS + 500)) not in result

    @pytest.mark.asyncio
    async def test_newline_defang_indents_embedded_headings(self):
        """A `\\n##` in the stored value must NOT create a fake briefing heading."""
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[_make_kv_entry('procedure:do:x', self._wrap('intro\n## fake heading'))],
        )
        result = await svc.generate(uuid4(), budget=2000)
        # The injected `\n## fake heading` becomes `\n  ## fake heading` (indented),
        # so `\n## fake heading` (line-start H2) must not appear.
        assert '\n## fake heading' not in result
        assert '  ## fake heading' in result

    @pytest.mark.asyncio
    async def test_procedures_render_in_every_vaults_briefing(self):
        """KV has no vault_id; the same procedure row surfaces in any vault's briefing."""
        entries = [_make_kv_entry('procedure:do:shared', self._wrap('global rule'))]
        svc_a = _make_service(summary=_make_vault_summary(), kv_entries=entries)
        svc_b = _make_service(summary=_make_vault_summary(), kv_entries=entries)
        result_a = await svc_a.generate(uuid4(), budget=2000)
        result_b = await svc_b.generate(uuid4(), budget=2000)
        assert 'global rule' in result_a
        assert 'global rule' in result_b

    @pytest.mark.asyncio
    async def test_procedure_answer_session_briefing_round_trip(self):
        """Bug-motivation pin: the exact KV row that triggered V4."""
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[
                _make_kv_entry(
                    'procedure:answer:session-briefing',
                    self._wrap(
                        'When the SessionStart briefing already contains the data, answer from it.'
                    ),
                )
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '**answer:session-briefing**' in result
        assert 'answer from it' in result

    @pytest.mark.asyncio
    async def test_orphan_header_suppressed_when_all_entries_degenerate(self):
        """`procedure:` (empty body) or empty value → no entry rendered → no orphan heading."""
        svc = _make_service(
            summary=_make_vault_summary(),
            kv_entries=[
                _make_kv_entry('procedure:', self._wrap('orphan body')),
                _make_kv_entry('procedure:empty', self._wrap('')),
            ],
        )
        result = await svc.generate(uuid4(), budget=2000)
        assert '## Procedures' not in result

    @pytest.mark.asyncio
    async def test_overflow_step5_trims_procedures_under_budget(self):
        """Step 5 should drop procedures when budget is exhausted by other content."""
        large_summary = _make_vault_summary()
        # Pad the summary narrative to push overflow into Step 5.
        large_summary.narrative = 'x ' * 600

        # Many procedure rows, each near the per-row cap, to ensure they're the
        # largest droppable section after vault overview + KV trim.
        entries = [_make_kv_entry(f'procedure:do:p{i}', self._wrap('y ' * 200)) for i in range(15)]
        svc = _make_service(summary=large_summary, kv_entries=entries)
        result = await svc.generate(uuid4(), budget=1000)
        # After overflow, the procedure section MUST be slimmer than the input —
        # not every seeded row should still be present. Two-sided: a regression
        # that drops EVERY procedure under partial-content budget should also fail.
        # Expected range (budget=1000, 15 × ~200-char procedures + padded narrative):
        # typically 2-8 survive after Step 5 trims oldest-first. If this test flips
        # to `present == 0` after touching _build_sections / header overhead, audit
        # the budget-arithmetic before relaxing this assertion.
        present = sum(1 for i in range(15) if f'**do:p{i}**' in result)
        assert 0 < present < 15, f'expected partial trim, got present={present}'
