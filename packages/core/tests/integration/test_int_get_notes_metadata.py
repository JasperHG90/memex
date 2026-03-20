"""Integration tests for NoteService.get_notes_metadata batch query.

Verifies that get_notes_metadata fetches all notes in a single query
instead of N+1 individual queries.
"""

from uuid import uuid4

import pytest

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import Note


@pytest.mark.integration
class TestGetNotesMetadataBatch:
    """Test get_notes_metadata returns correct metadata in a single query."""

    @pytest.mark.asyncio
    async def test_returns_metadata_for_multiple_notes(self, api, metastore):
        """Multiple notes with page_index metadata should all be returned."""
        note_ids = []
        for i in range(5):
            note_id = uuid4()
            note_ids.append(note_id)
            async with metastore.session() as session:
                note = Note(
                    id=note_id,
                    vault_id=GLOBAL_VAULT_ID,
                    content_hash=f'hash-{uuid4().hex}',
                    original_text=f'Note {i} content {uuid4()}',
                    page_index={
                        'metadata': {
                            'title': f'Note {i}',
                            'publish_date': f'2024-0{i + 1}-15',
                            'tags': ['test'],
                        },
                        'toc': [],
                    },
                )
                session.add(note)
                await session.commit()

        results = await api.get_notes_metadata(note_ids)

        assert len(results) == 5
        titles = {r['title'] for r in results}
        for i in range(5):
            assert f'Note {i}' in titles
        # Verify note_id is set on each result
        result_note_ids = {r['note_id'] for r in results}
        for nid in note_ids:
            assert str(nid) in result_note_ids

    @pytest.mark.asyncio
    async def test_skips_notes_without_page_index(self, api, metastore):
        """Notes without page_index should be skipped in results."""
        note_with_pi = uuid4()
        note_without_pi = uuid4()

        async with metastore.session() as session:
            session.add(
                Note(
                    id=note_with_pi,
                    vault_id=GLOBAL_VAULT_ID,
                    content_hash=f'hash-{uuid4().hex}',
                    original_text=f'Content {uuid4()}',
                    page_index={
                        'metadata': {'title': 'Has Index', 'tags': []},
                        'toc': [],
                    },
                )
            )
            session.add(
                Note(
                    id=note_without_pi,
                    vault_id=GLOBAL_VAULT_ID,
                    content_hash=f'hash-{uuid4().hex}',
                    original_text=f'Content {uuid4()}',
                    page_index=None,
                )
            )
            await session.commit()

        results = await api.get_notes_metadata([note_with_pi, note_without_pi])

        assert len(results) == 1
        assert results[0]['title'] == 'Has Index'
        assert results[0]['note_id'] == str(note_with_pi)

    @pytest.mark.asyncio
    async def test_skips_notes_without_metadata_key(self, api, metastore):
        """Notes with page_index but no 'metadata' key should be skipped."""
        note_id = uuid4()
        async with metastore.session() as session:
            session.add(
                Note(
                    id=note_id,
                    vault_id=GLOBAL_VAULT_ID,
                    content_hash=f'hash-{uuid4().hex}',
                    original_text=f'Content {uuid4()}',
                    page_index={'toc': []},  # no 'metadata' key
                )
            )
            await session.commit()

        results = await api.get_notes_metadata([note_id])
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_handles_nonexistent_note_ids(self, api, metastore):
        """Non-existent note IDs should be silently skipped."""
        real_note_id = uuid4()
        async with metastore.session() as session:
            session.add(
                Note(
                    id=real_note_id,
                    vault_id=GLOBAL_VAULT_ID,
                    content_hash=f'hash-{uuid4().hex}',
                    original_text=f'Content {uuid4()}',
                    page_index={
                        'metadata': {'title': 'Real Note'},
                        'toc': [],
                    },
                )
            )
            await session.commit()

        fake_id = uuid4()
        results = await api.get_notes_metadata([real_note_id, fake_id])

        assert len(results) == 1
        assert results[0]['title'] == 'Real Note'

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self, api):
        """An empty list of note IDs should return an empty list."""
        results = await api.get_notes_metadata([])
        assert results == []

    @pytest.mark.asyncio
    async def test_has_assets_flag(self, api, metastore):
        """Metadata should include has_assets based on note assets."""
        note_with_assets = uuid4()
        note_without_assets = uuid4()

        async with metastore.session() as session:
            session.add(
                Note(
                    id=note_with_assets,
                    vault_id=GLOBAL_VAULT_ID,
                    content_hash=f'hash-{uuid4().hex}',
                    original_text=f'Content {uuid4()}',
                    assets=['assets/global/img.png'],
                    page_index={
                        'metadata': {'title': 'With Assets'},
                        'toc': [],
                    },
                )
            )
            session.add(
                Note(
                    id=note_without_assets,
                    vault_id=GLOBAL_VAULT_ID,
                    content_hash=f'hash-{uuid4().hex}',
                    original_text=f'Content {uuid4()}',
                    assets=[],
                    page_index={
                        'metadata': {'title': 'No Assets'},
                        'toc': [],
                    },
                )
            )
            await session.commit()

        results = await api.get_notes_metadata([note_with_assets, note_without_assets])

        by_title = {r['title']: r for r in results}
        assert by_title['With Assets']['has_assets'] is True
        assert by_title['No Assets']['has_assets'] is False

    @pytest.mark.asyncio
    async def test_vault_info_included(self, api, metastore):
        """Metadata should include vault_id and vault_name."""
        note_id = uuid4()
        async with metastore.session() as session:
            session.add(
                Note(
                    id=note_id,
                    vault_id=GLOBAL_VAULT_ID,
                    content_hash=f'hash-{uuid4().hex}',
                    original_text=f'Content {uuid4()}',
                    page_index={
                        'metadata': {'title': 'Vault Test'},
                        'toc': [],
                    },
                )
            )
            await session.commit()

        results = await api.get_notes_metadata([note_id])

        assert len(results) == 1
        assert results[0]['vault_id'] == str(GLOBAL_VAULT_ID)
        assert 'vault_name' in results[0]

    @pytest.mark.asyncio
    async def test_consistency_with_single_get(self, api, metastore):
        """Batch results should match individual get_note_metadata results."""
        note_id = uuid4()
        async with metastore.session() as session:
            session.add(
                Note(
                    id=note_id,
                    vault_id=GLOBAL_VAULT_ID,
                    content_hash=f'hash-{uuid4().hex}',
                    original_text=f'Content {uuid4()}',
                    assets=['assets/global/img.png'],
                    page_index={
                        'metadata': {
                            'title': 'Consistency Test',
                            'tags': ['a', 'b'],
                            'publish_date': '2024-06-01',
                        },
                        'toc': [],
                    },
                )
            )
            await session.commit()

        single = await api.get_note_metadata(note_id)
        batch = await api.get_notes_metadata([note_id])

        assert len(batch) == 1
        batch_result = batch[0]
        # The batch version adds note_id; single doesn't
        assert batch_result['note_id'] == str(note_id)
        # Core metadata fields should match
        assert batch_result['title'] == single['title']
        assert batch_result['has_assets'] == single['has_assets']
        assert batch_result['vault_id'] == single['vault_id']
