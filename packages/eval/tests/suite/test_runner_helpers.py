"""Targeted tests for runner helpers fixed by adversarial review."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from memex_eval.suite.runner import _extract_judge_revision, _wait_extraction_per_note


class TestJudgeRevisionExtractor:
    def test_returns_response_model_when_present(self) -> None:
        lm = SimpleNamespace(history=[{'response': {'model': 'gemini-2.5-pro-001'}}])
        assert _extract_judge_revision(lm) == 'gemini-2.5-pro-001'

    def test_returns_top_level_model_when_no_response_block(self) -> None:
        lm = SimpleNamespace(history=[{'model': 'gemini-2.5-flash'}])
        assert _extract_judge_revision(lm) == 'gemini-2.5-flash'

    def test_returns_none_on_empty_history(self) -> None:
        lm = SimpleNamespace(history=[])
        assert _extract_judge_revision(lm) is None

    def test_returns_none_on_malformed_entries(self) -> None:
        lm = SimpleNamespace(history=['not a dict'])
        assert _extract_judge_revision(lm) is None

    def test_returns_none_when_no_history_attr(self) -> None:
        lm = SimpleNamespace()
        assert _extract_judge_revision(lm) is None


class TestWaitExtractionPerNote:
    """P3: per-note vault must be honoured.

    Pre-fix the helper polled `default_vault_id` for every note; notes
    routed to a non-default vault timed out spuriously (visible in the
    project-delta / project-gamma vault-isolation scenarios).
    """

    @pytest.mark.asyncio
    async def test_polls_per_note_vault_not_default(self) -> None:
        # Two notes: one in vault A (the default), one in vault B.
        vault_a = uuid4()
        vault_b = uuid4()
        note_in_a = 'note-a-uuid'
        note_in_b = 'note-b-uuid'

        async def fake_list(note_id: str, vault_id: UUID):
            # Returns units only when probed under the matching vault.
            if note_id == note_in_a and vault_id == vault_a:
                return [SimpleNamespace(id='unit-a-1')]
            if note_id == note_in_b and vault_id == vault_b:
                return [SimpleNamespace(id='unit-b-1')]
            return []

        api = MagicMock()
        api.list_memory_units_by_note = AsyncMock(side_effect=fake_list)

        result = await _wait_extraction_per_note(
            api,
            note_id_by_key={'note-a': note_in_a, 'note-b': note_in_b},
            note_key_to_vault_id={'note-a': vault_a, 'note-b': vault_b},
            per_note_timeout_s=2.0,
            poll_interval_s=0.05,
        )
        assert result == {'note-a': ['unit-a-1'], 'note-b': ['unit-b-1']}

    @pytest.mark.asyncio
    async def test_rejects_duplicate_note_key_across_vaults(self, tmp_path) -> None:
        """P3 + round-2 M1: ingest must fail-fast on duplicate note_key
        across vaults. Silent collapse would break TemporalOrdering /
        NoteAttribution outcomes."""
        from memex_eval.suite.runner import _ingest_sources
        from memex_eval.suite.base import Suite, SuiteMetadata, SuiteSources
        from memex_eval.suite.sources import SourceNote

        meta = SuiteMetadata(
            name='dup_kt', schema_version='1', suite_version='1.0.0', description='d'
        )
        # SourceNote requires a path; tmp_path provides one. Note bodies need
        # not match the file content here — _ingest_sources walks the model objects,
        # not the disk.
        path_a = tmp_path / 'a.md'
        path_a.write_text('alpha')
        path_b = tmp_path / 'b.md'
        path_b.write_text('beta')
        notes = [
            SourceNote(note_key='shared', path=path_a, content='alpha', vault_name='vault-a'),
            SourceNote(note_key='shared', path=path_b, content='beta', vault_name='vault-b'),
        ]
        suite = Suite(
            metadata=meta,
            sources=SuiteSources(notes=notes),
            scenarios=[],
        )
        api = MagicMock()
        api.ingest = AsyncMock()
        with pytest.raises(ValueError, match='Duplicate note_key'):
            await _ingest_sources(api, uuid4(), {None: uuid4()}, suite)
        api.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_fix_regression_default_vault_misses_note_in_b(self) -> None:
        # Without the per-note vault threading, both notes would be polled
        # under vault A — the note in vault B would time out. Verify the
        # new helper does NOT regress to that behavior.
        vault_a = uuid4()
        vault_b = uuid4()

        async def fake_list(note_id: str, vault_id: UUID):
            if vault_id == vault_a:
                return [SimpleNamespace(id='only-a')] if note_id == 'a' else []
            if vault_id == vault_b:
                return [SimpleNamespace(id='only-b')] if note_id == 'b' else []
            return []

        api = MagicMock()
        api.list_memory_units_by_note = AsyncMock(side_effect=fake_list)
        result = await _wait_extraction_per_note(
            api,
            note_id_by_key={'a': 'a', 'b': 'b'},
            note_key_to_vault_id={'a': vault_a, 'b': vault_b},
            per_note_timeout_s=2.0,
            poll_interval_s=0.05,
        )
        assert result['a'] == ['only-a']
        assert result['b'] == ['only-b']  # would be [] under the old helper
