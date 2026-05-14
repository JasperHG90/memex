"""Tests for NoteService."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_common.exceptions import ResourceNotFoundError
from memex_core.services.notes import NoteService


# ---------------------------------------------------------------------------
# Fixture: 3-level nested TOC (root → child → grandchild)
# ---------------------------------------------------------------------------


def _make_toc():
    """Build a 3-level TOC tree for _filter_toc tests."""
    return [
        {
            'id': 'root-1',
            'title': 'Root 1',
            'level': 1,
            'token_estimate': 100,
            'children': [
                {
                    'id': 'child-1-1',
                    'title': 'Child 1.1',
                    'level': 2,
                    'token_estimate': 50,
                    'children': [
                        {
                            'id': 'grandchild-1-1-1',
                            'title': 'Grandchild 1.1.1',
                            'level': 3,
                            'token_estimate': 25,
                            'children': [],
                        },
                    ],
                },
            ],
        },
        {
            'id': 'root-2',
            'title': 'Root 2',
            'level': 1,
            'token_estimate': 200,
            'children': [],
        },
    ]


# ---------------------------------------------------------------------------
# _filter_toc tests
# ---------------------------------------------------------------------------


class TestFilterToc:
    """Tests for NoteService._filter_toc."""

    def test_depth_0_returns_roots_and_direct_children(self):
        """depth=0 returns roots + direct children (H1+H2 overview), grandchildren emptied."""
        toc = _make_toc()
        result = NoteService._filter_toc(toc, depth=0)

        assert len(result) == 2
        assert result[0]['id'] == 'root-1'
        # depth=0 now includes direct children (H2 level)
        assert len(result[0]['children']) == 1
        assert result[0]['children'][0]['id'] == 'child-1-1'
        # But grandchildren (H3) are trimmed
        assert result[0]['children'][0]['children'] == []
        assert result[1]['id'] == 'root-2'
        assert result[1]['children'] == []

    def test_depth_1_returns_full_tree(self):
        """depth=1 returns the full tree with no trimming."""
        toc = _make_toc()
        result = NoteService._filter_toc(toc, depth=1)

        assert len(result) == 2
        assert len(result[0]['children']) == 1
        assert result[0]['children'][0]['id'] == 'child-1-1'
        # Full tree: grandchildren are preserved
        assert len(result[0]['children'][0]['children']) == 1
        assert result[0]['children'][0]['children'][0]['id'] == 'grandchild-1-1-1'

    def test_depth_none_returns_full_tree(self):
        """depth=None returns the unmodified tree."""
        toc = _make_toc()
        result = NoteService._filter_toc(toc, depth=None)

        assert len(result) == 2
        assert len(result[0]['children']) == 1
        assert len(result[0]['children'][0]['children']) == 1
        assert result[0]['children'][0]['children'][0]['id'] == 'grandchild-1-1-1'

    def test_parent_node_id_returns_subtree(self):
        """parent_node_id returns children of the matched node."""
        toc = _make_toc()
        result = NoteService._filter_toc(toc, parent_node_id='root-1')

        assert len(result) == 1
        assert result[0]['id'] == 'child-1-1'

    def test_parent_node_id_not_found_returns_empty(self):
        """parent_node_id for nonexistent node returns empty list."""
        toc = _make_toc()
        result = NoteService._filter_toc(toc, parent_node_id='nonexistent')

        assert result == []

    def test_depth_and_parent_node_id_combined(self):
        """depth=0 + parent_node_id: returns subtree children with H1+H2 trimming."""
        toc = _make_toc()
        result = NoteService._filter_toc(toc, depth=0, parent_node_id='root-1')

        # Subtree of root-1 is [child-1-1], depth=0 includes its children
        assert len(result) == 1
        assert result[0]['id'] == 'child-1-1'
        assert len(result[0]['children']) == 1
        assert result[0]['children'][0]['id'] == 'grandchild-1-1-1'
        # grandchild's children would be trimmed (if any existed)
        assert result[0]['children'][0]['children'] == []

    def test_empty_toc_input(self):
        """Empty TOC input returns empty list."""
        result = NoteService._filter_toc([], depth=0)

        assert result == []

    def test_depth_0_with_4_level_tree(self):
        """depth=0 on a 4-level tree returns H1+H2, trimming H3 and H4."""
        toc = [
            {
                'id': 'h1',
                'title': 'H1',
                'level': 1,
                'children': [
                    {
                        'id': 'h2',
                        'title': 'H2',
                        'level': 2,
                        'children': [
                            {
                                'id': 'h3',
                                'title': 'H3',
                                'level': 3,
                                'children': [
                                    {
                                        'id': 'h4',
                                        'title': 'H4',
                                        'level': 4,
                                        'children': [],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
        ]
        result = NoteService._filter_toc(toc, depth=0)

        assert len(result) == 1
        assert result[0]['id'] == 'h1'
        assert len(result[0]['children']) == 1
        assert result[0]['children'][0]['id'] == 'h2'
        assert result[0]['children'][0]['children'] == []

    def test_depth_0_multiple_h2_children(self):
        """depth=0 preserves all H2 children under an H1 root."""
        toc = [
            {
                'id': 'root',
                'title': 'Root',
                'level': 1,
                'children': [
                    {'id': 'sec-a', 'title': 'Section A', 'level': 2, 'children': []},
                    {'id': 'sec-b', 'title': 'Section B', 'level': 2, 'children': []},
                    {'id': 'sec-c', 'title': 'Section C', 'level': 2, 'children': []},
                ],
            },
        ]
        result = NoteService._filter_toc(toc, depth=0)

        assert len(result[0]['children']) == 3
        assert [c['id'] for c in result[0]['children']] == ['sec-a', 'sec-b', 'sec-c']

    def test_depth_1_returns_all_levels(self):
        """depth=1 returns the complete tree regardless of nesting depth."""
        toc = [
            {
                'id': 'h1',
                'title': 'H1',
                'level': 1,
                'children': [
                    {
                        'id': 'h2',
                        'title': 'H2',
                        'level': 2,
                        'children': [
                            {
                                'id': 'h3',
                                'title': 'H3',
                                'level': 3,
                                'children': [
                                    {
                                        'id': 'h4',
                                        'title': 'H4',
                                        'level': 4,
                                        'children': [],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
        ]
        result = NoteService._filter_toc(toc, depth=1)

        # Full tree preserved
        assert result[0]['children'][0]['children'][0]['children'][0]['id'] == 'h4'

    def test_depth_2_returns_full_tree(self):
        """depth >= 1 always returns the full tree."""
        toc = _make_toc()
        result = NoteService._filter_toc(toc, depth=2)

        assert len(result[0]['children']) == 1
        assert len(result[0]['children'][0]['children']) == 1
        assert result[0]['children'][0]['children'][0]['id'] == 'grandchild-1-1-1'

    def test_depth_0_does_not_mutate_original(self):
        """Filtering with depth=0 does not mutate the original TOC."""
        toc = _make_toc()
        original_child_count = len(toc[0]['children'][0]['children'])
        NoteService._filter_toc(toc, depth=0)

        # Original should be untouched
        assert len(toc[0]['children'][0]['children']) == original_child_count

    def test_depth_0_flat_roots_only(self):
        """depth=0 on a tree with only root nodes (no children) returns them as-is."""
        toc = [
            {'id': 'a', 'title': 'A', 'level': 1, 'children': []},
            {'id': 'b', 'title': 'B', 'level': 1, 'children': []},
        ]
        result = NoteService._filter_toc(toc, depth=0)

        assert len(result) == 2
        assert result[0]['children'] == []
        assert result[1]['children'] == []

    def test_depth_1_with_parent_node_id(self):
        """depth=1 + parent_node_id returns full subtree (no trimming)."""
        toc = _make_toc()
        result = NoteService._filter_toc(toc, depth=1, parent_node_id='root-1')

        assert len(result) == 1
        assert result[0]['id'] == 'child-1-1'
        assert len(result[0]['children']) == 1
        assert result[0]['children'][0]['id'] == 'grandchild-1-1-1'

    def test_subtree_tokens_preserved_through_depth_0(self):
        """subtree_tokens should survive depth=0 filtering."""
        toc = [
            {
                'id': 'root',
                'title': 'Root',
                'level': 1,
                'token_estimate': 10,
                'subtree_tokens': 85,
                'children': [
                    {
                        'id': 'child',
                        'title': 'Child',
                        'level': 2,
                        'token_estimate': 30,
                        'subtree_tokens': 75,
                        'children': [
                            {
                                'id': 'gc',
                                'title': 'GC',
                                'level': 3,
                                'token_estimate': 45,
                                'subtree_tokens': 45,
                                'children': [],
                            }
                        ],
                    }
                ],
            }
        ]
        result = NoteService._filter_toc(toc, depth=0)
        assert result[0]['subtree_tokens'] == 85
        assert result[0]['children'][0]['subtree_tokens'] == 75


@pytest.fixture
def note_service():
    """NoteService with mocked dependencies."""
    metastore = MagicMock()
    filestore = MagicMock()
    config = MagicMock()
    vaults = MagicMock()
    return NoteService(metastore=metastore, filestore=filestore, config=config, vaults=vaults)


@pytest.mark.asyncio
async def test_get_note_metadata_returns_metadata(note_service):
    """get_note_metadata returns the metadata dict when page_index has one."""
    note_id = uuid4()
    vault_id = uuid4()
    metadata = {'title': 'Test', 'description': 'Desc', 'tags': ['a']}
    mock_note = MagicMock()
    mock_note.page_index = {'metadata': metadata, 'toc': []}
    mock_note.assets = ['file.png']
    mock_note.vault_id = vault_id

    mock_vault = MagicMock()
    mock_vault.name = 'test-vault'

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(
        side_effect=lambda model, id: mock_note if id == note_id else mock_vault
    )
    note_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    note_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await note_service.get_note_metadata(note_id)
    assert result['title'] == 'Test'
    assert result['description'] == 'Desc'
    assert result['tags'] == ['a']
    assert result['has_assets'] is True
    assert result['vault_id'] == str(vault_id)
    assert result['vault_name'] == 'test-vault'


@pytest.mark.asyncio
async def test_get_note_metadata_returns_none_for_no_page_index(note_service):
    """get_note_metadata returns None when the note has no page_index."""
    note_id = uuid4()
    mock_note = MagicMock()
    mock_note.page_index = None

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_note
    note_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    note_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await note_service.get_note_metadata(note_id)
    assert result is None


@pytest.mark.asyncio
async def test_get_note_metadata_raises_for_missing_note(note_service):
    """get_note_metadata raises ResourceNotFoundError for a nonexistent note."""
    note_id = uuid4()

    mock_session = AsyncMock()
    mock_session.get.return_value = None
    note_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    note_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    with pytest.raises(ResourceNotFoundError):
        await note_service.get_note_metadata(note_id)


@pytest.mark.asyncio
async def test_get_note_metadata_returns_none_when_no_metadata_key(note_service):
    """get_note_metadata returns None when page_index exists but has no 'metadata' key."""
    note_id = uuid4()
    mock_note = MagicMock()
    mock_note.page_index = {'toc': []}  # no 'metadata' key

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_note
    note_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    note_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await note_service.get_note_metadata(note_id)
    assert result is None


@pytest.mark.asyncio
async def test_get_note_metadata_returns_none_for_legacy_list_format(note_service):
    """get_note_metadata returns None when page_index is a list (pre-envelope format)."""
    note_id = uuid4()
    mock_note = MagicMock()
    mock_note.page_index = [{'level': 1, 'title': 'Intro', 'children': []}]

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_note
    note_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    note_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await note_service.get_note_metadata(note_id)
    assert result is None


# ---------------------------------------------------------------------------
# set_note_status — memory unit cascade tests
# ---------------------------------------------------------------------------


class TestSetNoteStatusCascade:
    """Verify that set_note_status cascades status changes to memory units."""

    @pytest.fixture
    def service(self):
        metastore = MagicMock()
        filestore = MagicMock()
        config = MagicMock()
        vaults = MagicMock()
        return NoteService(metastore=metastore, filestore=filestore, config=config, vaults=vaults)

    def _make_mock_unit(self, status: str = 'active', is_deprioritized: bool = False) -> MagicMock:
        unit = MagicMock()
        unit.status = status
        unit.is_deprioritized = is_deprioritized
        return unit

    @pytest.mark.asyncio
    async def test_archived_deprioritizes_units_and_records_archived_at(
        self, service: NoteService
    ) -> None:
        """``set_note_status('archived')`` flips every unit to
        ``is_deprioritized=True`` (FSFM cascade), records
        ``Note.archived_at``, and keeps the note row at
        ``status='active'`` (since ``'archived'`` is no longer in the
        ``ck_notes_status`` enum)."""
        note_id = uuid4()
        mock_note = MagicMock()
        mock_note.status = 'active'
        mock_note.archived_at = None

        units = [self._make_mock_unit('active'), self._make_mock_unit('active')]

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_note)
        mock_exec_result = MagicMock()
        mock_exec_result.first.return_value = mock_note
        mock_exec_result.all.return_value = units
        mock_session.exec = AsyncMock(return_value=mock_exec_result)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.set_note_status(note_id, 'archived')

        assert mock_note.status == 'active'
        assert mock_note.archived_at is not None
        for unit in units:
            assert unit.status == 'active'
            assert unit.is_deprioritized is True

    @pytest.mark.asyncio
    async def test_archive_preserves_pre_existing_non_active_status(
        self, service: NoteService
    ) -> None:
        """Archiving a superseded note must NOT clobber ``status`` —
        ``archived_at`` and ``superseded_by`` are independent signals
        and both must coexist."""
        note_id = uuid4()
        linked_id = uuid4()
        mock_note = MagicMock()
        mock_note.status = 'superseded'
        mock_note.superseded_by = linked_id
        mock_note.archived_at = None

        units = [self._make_mock_unit('stale')]

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_note)
        mock_exec_result = MagicMock()
        mock_exec_result.first.return_value = mock_note
        mock_exec_result.all.return_value = units
        mock_session.exec = AsyncMock(return_value=mock_exec_result)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.set_note_status(note_id, 'archived')

        assert mock_note.status == 'superseded'
        assert mock_note.superseded_by == linked_id
        assert mock_note.archived_at is not None

    @pytest.mark.asyncio
    async def test_active_reactivates_stale_and_deprioritized_units(
        self, service: NoteService
    ) -> None:
        """``set_note_status('active')`` clears ``archived_at`` and
        reactivates every unit: ``status='stale' → 'active'`` (the
        supersession reverse) AND ``is_deprioritized=True → False`` (the
        archive reverse)."""
        note_id = uuid4()
        mock_note = MagicMock()
        mock_note.status = 'active'
        mock_note.archived_at = datetime.now(timezone.utc)

        units = [
            self._make_mock_unit('stale', is_deprioritized=True),
            self._make_mock_unit('active', is_deprioritized=True),
        ]

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_note)
        mock_exec_result = MagicMock()
        mock_exec_result.first.return_value = mock_note
        mock_exec_result.all.return_value = units
        mock_session.exec = AsyncMock(return_value=mock_exec_result)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.set_note_status(note_id, 'active')

        for unit in units:
            assert unit.status == 'active'
            assert unit.is_deprioritized is False
        assert mock_note.superseded_by is None
        assert mock_note.appended_to is None
        assert mock_note.archived_at is None

    @pytest.mark.asyncio
    async def test_active_preserves_per_unit_deprioritize_when_not_archived(
        self, service: NoteService
    ) -> None:
        """``set_note_status('active')`` on a non-archived note (e.g.
        reactivating a superseded one) must NOT clobber per-unit
        ``is_deprioritized=True`` signals — those come from
        ``memex_memory_deprioritize`` and are independent of archive
        state. The gating predicate is ``doc.archived_at is not None``
        pre-transition; when it's None, ``is_deprioritized`` stays
        as-is on each unit."""
        note_id = uuid4()
        mock_note = MagicMock()
        mock_note.status = 'superseded'
        mock_note.archived_at = None

        units = [
            self._make_mock_unit('stale', is_deprioritized=False),
            self._make_mock_unit('stale', is_deprioritized=True),
        ]

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_note)
        mock_exec_result = MagicMock()
        mock_exec_result.first.return_value = mock_note
        mock_exec_result.all.return_value = units
        mock_session.exec = AsyncMock(return_value=mock_exec_result)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.set_note_status(note_id, 'active')

        assert units[0].status == 'active'
        assert units[0].is_deprioritized is False
        assert units[1].status == 'active'
        # Preserved: was True before, still True after — the archive
        # reversal only fires when archived_at was non-null pre-transition.
        assert units[1].is_deprioritized is True

    @pytest.mark.asyncio
    async def test_active_after_archived_round_trip(self, service: NoteService) -> None:
        """Full round-trip: active → archived (deprioritized) → active
        (reprioritized + archived_at cleared)."""
        note_id = uuid4()
        mock_note = MagicMock()
        mock_note.status = 'active'
        mock_note.archived_at = None

        units = [self._make_mock_unit('active')]

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_note)
        mock_exec_result = MagicMock()
        mock_exec_result.first.return_value = mock_note
        mock_exec_result.all.return_value = units
        mock_session.exec = AsyncMock(return_value=mock_exec_result)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.set_note_status(note_id, 'archived')
        assert units[0].is_deprioritized is True
        assert mock_note.archived_at is not None

        await service.set_note_status(note_id, 'active')
        assert units[0].status == 'active'
        assert units[0].is_deprioritized is False
        assert mock_note.archived_at is None

    @pytest.mark.asyncio
    async def test_mw_counters_preserved_across_supersede_reactivate(
        self, service: NoteService
    ) -> None:
        """Supersession flips units to ``status='stale'`` but leaves
        ``success_co_count`` / ``failure_co_count`` untouched (audit
        trail preserved per P6). Reactivation flips status back to
        ``'active'``; the counters re-emerge with their pre-supersession
        values. FSFM short-circuits on ``status='stale'``, so the
        counters are inert while the unit is stale — they only matter
        again on reactivation, and they matter exactly as they were."""
        note_id = uuid4()
        mock_note = MagicMock()
        mock_note.status = 'active'

        unit = MagicMock()
        unit.status = 'active'
        unit.success_co_count = 17
        unit.failure_co_count = 4

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_note)
        mock_exec_result = MagicMock()
        mock_exec_result.first.return_value = mock_note
        mock_exec_result.all.return_value = [unit]
        mock_session.exec = AsyncMock(return_value=mock_exec_result)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.set_note_status(note_id, 'superseded', linked_note_id=uuid4())
        assert unit.status == 'stale'
        assert unit.success_co_count == 17
        assert unit.failure_co_count == 4

        await service.set_note_status(note_id, 'active')
        assert unit.status == 'active'
        assert unit.success_co_count == 17
        assert unit.failure_co_count == 4

    @pytest.mark.asyncio
    async def test_set_note_status_appended_raises_with_pointer_to_append(
        self, service: NoteService
    ) -> None:
        """``'appended'`` is not a settable lifecycle status; the message
        must redirect to the public append verb, not an internal symbol."""
        note_id = uuid4()
        with pytest.raises(ValueError) as exc_info:
            await service.set_note_status(note_id, 'appended')
        msg = str(exc_info.value)
        assert "'appended' is not a settable lifecycle status" in msg
        assert 'memex_append_note' in msg
        assert 'POST /notes/append' in msg
        service.metastore.session.assert_not_called()
