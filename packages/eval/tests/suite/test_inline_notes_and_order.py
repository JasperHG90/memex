"""Tests for inline notes, scenario execution order, and per-note asset subdirs."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memex_eval.suite import (
    GoldUnitIds,
    InlineNote,
    KeywordsPresent,
    Scenario,
    Suite,
    SuiteMetadata,
    SuiteSources,
    load_suite,
)


def _scenario(sid: str, **kwargs) -> Scenario:
    return Scenario(
        id=sid,
        description='d',
        query='q',
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        **kwargs,
    )


class TestBuiltInSuitesLoad:
    """Every shipped suite must load — catches regressions like a stricter
    filename regex that breaks digit-leading sources (the temporal suite)."""

    def test_acme_corp_suite_loads(self) -> None:
        suite = load_suite('acme_corp')
        assert suite.name == 'acme_corp'

    def test_all_built_in_suites_load(self) -> None:
        from memex_eval.suite import discover_suite_names

        names = discover_suite_names()
        assert names, 'discover_suite_names returned nothing'
        for name in names:
            # Will raise (and the test will fail) if any suite fails to load,
            # making the regression class loud rather than swallowed.
            load_suite(name)


class TestInlineNoteModel:
    def test_inline_note_validates_note_key(self) -> None:
        with pytest.raises(ValueError, match='must match'):
            InlineNote(note_key='Invalid Key', content='body')

    def test_inline_note_accepts_underscore_key(self) -> None:
        n = InlineNote(note_key='contradicts_alpha', content='body')
        assert n.note_key == 'contradicts_alpha'

    def test_inline_note_accepts_hyphenated_key_matching_filename_stem(self) -> None:
        # Source notes use hyphenated filenames (project-alpha-kickoff.md).
        # InlineNote.note_key must accept the same shape so users don't have
        # to remember a separate naming rule. (H-4 fix.)
        n = InlineNote(note_key='alpha-lead-succession', content='body')
        assert n.note_key == 'alpha-lead-succession'

    def test_scenario_carries_inline_notes(self) -> None:
        sc = Scenario(
            id='contradiction_check',
            description='d',
            query='q',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
            inline_notes=[
                InlineNote(note_key='contradicts_alpha', content='Sarah is no longer lead.'),
            ],
        )
        assert len(sc.inline_notes) == 1
        assert sc.inline_notes[0].note_key == 'contradicts_alpha'


class TestSuiteValidatorAcceptsInlineKeys:
    def test_gold_unit_ids_can_reference_inline_note(self) -> None:
        # The validator must accept an outcome's note_key reference when
        # the key matches an inline_note on the same scenario, even if it's
        # not in the suite-level sources.
        suite = Suite(
            metadata=SuiteMetadata(
                name='inline_ref',
                schema_version='1',
                suite_version='1.0.0',
                description='d',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[
                Scenario(
                    id='uses_inline',
                    description='d',
                    query='q',
                    expected=GoldUnitIds(
                        type='gold_unit_ids',
                        note_keys=['my_inline'],  # short form
                    ),
                    inline_notes=[InlineNote(note_key='my_inline', content='body')],
                ),
            ],
        )
        assert suite.scenarios[0].inline_notes[0].note_key == 'my_inline'

    def test_gold_unit_ids_can_reference_prefixed_inline_form(self) -> None:
        suite = Suite(
            metadata=SuiteMetadata(
                name='inline_ref_prefixed',
                schema_version='1',
                suite_version='1.0.0',
                description='d',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[
                Scenario(
                    id='uses_inline2',
                    description='d',
                    query='q',
                    expected=GoldUnitIds(
                        type='gold_unit_ids',
                        note_keys=['inline-uses_inline2-my_inline'],
                    ),
                    inline_notes=[InlineNote(note_key='my_inline', content='body')],
                ),
            ],
        )
        assert suite.scenarios[0].id == 'uses_inline2'

    def test_unresolved_reference_still_fails(self) -> None:
        with pytest.raises(ValueError, match='note_keys'):
            Suite(
                metadata=SuiteMetadata(
                    name='bad_ref',
                    schema_version='1',
                    suite_version='1.0.0',
                    description='d',
                ),
                sources=SuiteSources(notes=[]),
                scenarios=[
                    Scenario(
                        id='bad',
                        description='d',
                        query='q',
                        expected=GoldUnitIds(type='gold_unit_ids', note_keys=['ghost-key']),
                    ),
                ],
            )


class TestScenarioExecutionOrder:
    """The runner contractually iterates suite.scenarios in list order."""

    def test_order_preserved_through_pydantic(self) -> None:
        order = ['z_first', 'a_middle', 'm_last']
        suite = Suite(
            metadata=SuiteMetadata(
                name='order_test',
                schema_version='1',
                suite_version='1.0.0',
                description='d',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[_scenario(sid) for sid in order],
        )
        assert [s.id for s in suite.scenarios] == order

    def test_order_preserved_after_round_trip(self) -> None:
        """JSON dump/load must preserve scenario order — this is the contract."""
        order = ['c_one', 'a_two', 'b_three']
        original = Suite(
            metadata=SuiteMetadata(
                name='order_roundtrip',
                schema_version='1',
                suite_version='1.0.0',
                description='d',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[_scenario(sid) for sid in order],
        )
        rebuilt = Suite.model_validate_json(original.model_dump_json())
        assert [s.id for s in rebuilt.scenarios] == order

    @pytest.mark.asyncio
    async def test_runner_iterates_in_definition_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: feed scenarios in [B, C, A] order and assert outcomes
        come back in the same order, regardless of alphabetical sort."""
        from memex_eval.recorders.mlflow_recorder import NullRecorder
        from memex_eval.suite.runner import run_suite

        scenario_ids = ['scenario_b', 'scenario_c', 'scenario_a']
        suite = Suite(
            metadata=SuiteMetadata(
                name='runner_order_test',
                schema_version='1',
                suite_version='1.0.0',
                description='d',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[_scenario(sid) for sid in scenario_ids],
        )

        from types import SimpleNamespace

        api = SimpleNamespace()
        api.list_vaults = AsyncMock(return_value=[])
        api.create_vault = AsyncMock(return_value=SimpleNamespace(id=uuid4(), name='v-tmp'))
        api.delete_vault = AsyncMock(return_value=None)
        api.truncate_vault = AsyncMock(return_value=None)
        api.get_system_config = AsyncMock(return_value={})
        api.list_memory_units_by_note = AsyncMock(return_value=[])
        api.search = AsyncMock(return_value=[SimpleNamespace(id='u1', text='x irrelevant')])

        class _FakeAsyncClient:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                pass

        monkeypatch.setattr('memex_eval.suite.runner.httpx.AsyncClient', _FakeAsyncClient)
        monkeypatch.setattr('memex_eval.suite.runner.RemoteMemexAPI', lambda _c: api)

        result = await run_suite(
            suite,
            server_url='http://fake/api/v1/',
            recorder=NullRecorder(),
            use_llm_judge=False,
            seed=1,
        )
        assert [o.scenario_id for o in result.scenario_outcomes] == scenario_ids


class TestInlineNotesAcrossScenarios:
    """Two scenarios reusing the same short note_key must NOT collide."""

    @pytest.mark.asyncio
    async def test_short_key_does_not_leak_between_scenarios(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-1 regression: each scenario's inline note must be ingested even
        when a sibling already published the same short key."""
        from types import SimpleNamespace

        from memex_eval.suite.runner import _ingest_inline_notes

        sc_a = Scenario(
            id='scenario_a',
            description='d',
            query='q',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
            inline_notes=[InlineNote(note_key='shared_short', content='from A')],
        )
        sc_b = Scenario(
            id='scenario_b',
            description='d',
            query='q',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
            inline_notes=[InlineNote(note_key='shared_short', content='from B')],
        )

        suite = Suite(
            metadata=SuiteMetadata(
                name='collision_suite',
                schema_version='1',
                suite_version='1.0.0',
                description='d',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[sc_a, sc_b],
        )

        ingest_calls: list[str] = []

        async def fake_ingest(dto: Any) -> Any:
            ingest_calls.append(dto.note_key)
            return SimpleNamespace(note_id=f'note-{len(ingest_calls)}')

        api = SimpleNamespace()
        api.ingest = fake_ingest

        # Each list_memory_units_by_note call returns a fresh unit so we can
        # tell the two scenarios' inline-note units apart.
        async def fake_list(note_id: str, _vault_id: Any) -> list[Any]:
            return [SimpleNamespace(id=f'unit-for-{note_id}')]

        api.list_memory_units_by_note = fake_list

        ctx: dict[str, list[str]] = {}
        await _ingest_inline_notes(api, uuid4(), suite, sc_a, ctx)
        await _ingest_inline_notes(api, uuid4(), suite, sc_b, ctx)

        # Both ingest calls fired; B's was NOT short-circuited by A's cache.
        assert len(ingest_calls) == 2
        assert ctx['inline-scenario_a-shared_short'] != ctx['inline-scenario_b-shared_short']
        # The short form points at the LATEST scenario's units (Scenario B's,
        # since B ran second and re-published the short alias).
        assert ctx['shared_short'] == ctx['inline-scenario_b-shared_short']


class TestAssetSubdirConvention:
    def test_per_note_subdir_attaches_only_to_matching_note(self, tmp_path: Path) -> None:
        sources = tmp_path / 'sources'
        sources.mkdir()
        (sources / 'note-a.md').write_text('# A\nbody')
        (sources / 'note-b.md').write_text('# B\nbody')
        # Per-note subdir: only attaches to note-a
        per_a = sources / 'assets' / 'note-a'
        per_a.mkdir(parents=True)
        (per_a / 'diagram.png').write_bytes(b'\x89PNG\x0d\x0a\x1a\x0a' + b'\x00' * 8)

        loaded = SuiteSources.from_directory(sources)
        a = next(n for n in loaded.notes if n.note_key == 'note-a')
        b = next(n for n in loaded.notes if n.note_key == 'note-b')
        assert 'diagram.png' in a.assets
        assert b.assets == {}

    def test_bare_files_in_assets_dir_not_attached(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bare files at sources/assets/* are NOT auto-attached; loader warns."""
        import logging

        sources = tmp_path / 'sources'
        sources.mkdir()
        (sources / 'note.md').write_text('# Note\nbody')
        assets_dir = sources / 'assets'
        assets_dir.mkdir()
        (assets_dir / 'orphan.png').write_bytes(b'\x89PNG' + b'\x00' * 8)

        with caplog.at_level(logging.WARNING, logger='memex_eval.suite.sources'):
            loaded = SuiteSources.from_directory(sources)

        note = loaded.notes[0]
        assert note.assets == {}, 'bare files must not auto-attach'
        assert any('Unattached' in rec.message for rec in caplog.records), (
            'loader should warn about bare files in sources/assets/'
        )

    def test_digit_leading_filename_loads(self, tmp_path: Path) -> None:
        """C-R2-1 regression: ``2023-historical.md`` style stems are valid."""
        sources = tmp_path / 'sources'
        sources.mkdir()
        (sources / '2023-historical.md').write_text('# 2023\nbody')
        (sources / '2025-current.md').write_text('# 2025\nbody')

        loaded = SuiteSources.from_directory(sources)
        keys = {n.note_key for n in loaded.notes}
        assert keys == {'2023-historical', '2025-current'}

    def test_capitalized_filename_skipped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """M-R2-1 regression: README.md and other non-conforming stems are
        skipped (with warning), not raised — so a stray README in sources/
        doesn't take down the whole suite."""
        import logging

        sources = tmp_path / 'sources'
        sources.mkdir()
        (sources / 'README.md').write_text('# Internal\nnotes')
        (sources / 'good-note.md').write_text('# Good\nbody')

        with caplog.at_level(logging.WARNING, logger='memex_eval.suite.sources'):
            loaded = SuiteSources.from_directory(sources)

        assert {n.note_key for n in loaded.notes} == {'good-note'}
        assert any('README.md' in rec.message for rec in caplog.records)

    def test_underscore_prefixed_filename_silently_skipped(self, tmp_path: Path) -> None:
        """``_shared.md`` and similar partials are intentional — log at DEBUG only."""
        sources = tmp_path / 'sources'
        sources.mkdir()
        (sources / '_shared.md').write_text('# Internal\nbody')
        (sources / 'real-note.md').write_text('# Real\nbody')

        loaded = SuiteSources.from_directory(sources)
        assert {n.note_key for n in loaded.notes} == {'real-note'}

    def test_explicit_frontmatter_assets_still_wins(self, tmp_path: Path) -> None:
        sources = tmp_path / 'sources'
        sources.mkdir()
        (sources / 'note-a.md').write_text(
            textwrap.dedent("""\
            ---
            assets:
              picture.jpg: assets/note-a/diagram.png
            ---
            # Note A
            body
        """)
        )
        per_a = sources / 'assets' / 'note-a'
        per_a.mkdir(parents=True)
        (per_a / 'diagram.png').write_bytes(b'\x89PNG' + b'\x00' * 8)

        loaded = SuiteSources.from_directory(sources)
        a = loaded.notes[0]
        # Frontmatter explicit binding wins; note-a sees only the renamed key.
        assert 'picture.jpg' in a.assets
        # Per-note auto-attach is skipped when frontmatter is explicit.
        assert 'diagram.png' not in a.assets
