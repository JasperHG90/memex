"""Edge case tests for MCP tools — covers failure modes, boundary conditions,
and malformed input that happy-path tests miss."""

import datetime as dt

import pytest
from fastmcp.exceptions import ToolError
from uuid import uuid4, UUID

from memex_common.schemas import (
    EntityDTO,
    MemoryUnitDTO,
    NoteDTO,
    NodeDTO,
    FactTypes,
    SupersessionInfo,
)


# ═══════════════════════════════════════════════════════════════════════════════
# memex_get_nodes — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetNodesEdgeCases:
    """Edge cases for the batch node retrieval tool."""

    @pytest.mark.asyncio
    async def test_empty_list_returns_no_nodes_message(self, mock_api, mcp_client):
        """Empty node_ids should return a no-result message, not crash."""
        mock_api.get_nodes.return_value = []

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': []})
        assert 'No nodes found' in result.content[0].text

    @pytest.mark.asyncio
    async def test_duplicate_ids_returned_twice(self, mock_api, mcp_client):
        """Duplicate IDs in the request should be passed through (no dedup)."""
        nid = uuid4()
        node = NodeDTO(
            id=nid,
            note_id=uuid4(),
            vault_id=uuid4(),
            title='Dup Section',
            text='Content.',
            level=1,
            seq=0,
            status='active',
            created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        )
        mock_api.get_nodes.return_value = [node]

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(nid), str(nid)]})
        text = result.content[0].text

        # Node is found, no error for the duplicate
        assert 'Dup Section' in text
        # The duplicate ID should NOT produce a not-found error since it matched
        assert 'not found' not in text

    @pytest.mark.asyncio
    async def test_api_exception_falls_back_to_individual(self, mock_api, mcp_client):
        """If batch call fails, tool falls back to individual get_node calls."""
        nid = uuid4()
        mock_api.get_nodes.side_effect = RuntimeError('batch not supported')
        node = NodeDTO(
            id=nid,
            note_id=uuid4(),
            vault_id=uuid4(),
            title='Fallback Node',
            text='Recovered.',
            level=1,
            seq=0,
            status='active',
            created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        )
        mock_api.get_node.return_value = node

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(nid)]})
        text = result.content[0].text

        assert 'Fallback Node' in text
        mock_api.get_node.assert_called_once()

    @pytest.mark.asyncio
    async def test_both_batch_and_individual_fail(self, mock_api, mcp_client):
        """If both batch and individual lookups fail, report errors."""
        nid = uuid4()
        mock_api.get_nodes.side_effect = RuntimeError('batch down')
        mock_api.get_node.side_effect = RuntimeError('also down')

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(nid)]})
        text = result.content[0].text

        assert 'also down' in text

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_uuids(self, mock_api, mcp_client):
        """Mix of valid and invalid UUIDs: valid ones should be fetched, invalid reported."""
        nid = uuid4()
        node = NodeDTO(
            id=nid,
            note_id=uuid4(),
            vault_id=uuid4(),
            title='Found',
            text='OK.',
            level=1,
            seq=0,
            status='active',
            created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        )
        mock_api.get_nodes.return_value = [node]

        result = await mcp_client.call_tool(
            'memex_get_nodes', {'node_ids': [str(nid), 'garbage-id']}
        )
        text = result.content[0].text

        assert 'Found' in text
        assert 'Invalid UUID: garbage-id' in text

    @pytest.mark.asyncio
    async def test_all_ids_not_found(self, mock_api, mcp_client):
        """When all valid UUIDs are not found, output should show errors for all."""
        nid1 = uuid4()
        nid2 = uuid4()
        mock_api.get_nodes.return_value = []

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(nid1), str(nid2)]})
        text = result.content[0].text

        assert str(nid1) in text
        assert str(nid2) in text
        assert 'not found' in text
        assert 'child node IDs' in text

    @pytest.mark.asyncio
    async def test_partial_success_shows_hint_for_missing(self, mock_api, mcp_client):
        """When some nodes found and some not, show content + helpful hint."""
        found_id = uuid4()
        missing_id = uuid4()
        mock_api.get_nodes.return_value = [
            NodeDTO(
                id=found_id,
                note_id=uuid4(),
                vault_id=uuid4(),
                title='Found Section',
                text='Content here.',
                level=1,
                node_hash='abc123',
                seq=0,
                status='active',
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            ),
        ]

        result = await mcp_client.call_tool(
            'memex_get_nodes', {'node_ids': [str(found_id), str(missing_id)]}
        )
        text = result.content[0].text

        # Should include the found node's content
        assert 'Found Section' in text
        assert 'Content here.' in text
        # Should include helpful hint about missing, not a harsh error
        assert str(missing_id) in text
        assert 'parent sections' in text
        assert 'Errors' not in text  # not in the old error block format

    @pytest.mark.asyncio
    async def test_hash_matched_node_not_reported_missing(self, mock_api, mcp_client):
        """Node found via node_hash (page index hash ID) should not be reported as missing.

        Page index IDs are MD5 content hashes, not the node's primary key UUID.
        When an agent passes a hash ID, UUID(hash_hex) != node.id, but the node
        is found via the node_hash column. The found_hashes tracking must suppress
        the "not found" hint for these IDs.
        """
        # Simulate: agent passes hash "aabbccdd..." from page index.
        # UUID("aabbccdd...") parses fine but is NOT the node's primary key.
        hash_hex = 'aabbccdd11223344aabbccdd11223344'
        real_node_id = uuid4()  # different from UUID(hash_hex)

        mock_api.get_nodes.return_value = [
            NodeDTO(
                id=real_node_id,
                note_id=uuid4(),
                vault_id=uuid4(),
                title='Hash-Matched Section',
                text='Found via hash.',
                level=1,
                node_hash=hash_hex,
                seq=0,
                status='active',
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            ),
        ]

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [hash_hex]})
        text = result.content[0].text

        assert 'Hash-Matched Section' in text
        assert 'Found via hash.' in text
        # Must NOT report the hash ID as "not found"
        assert 'not found' not in text
        assert 'parent sections' not in text

    @pytest.mark.asyncio
    async def test_hash_id_mixed_with_uuid_id(self, mock_api, mcp_client):
        """Mix of hash-matched and UUID-matched nodes: both found, no errors."""
        hash_hex = 'deadbeef12345678deadbeef12345678'
        uuid_node_id = uuid4()

        mock_api.get_nodes.return_value = [
            NodeDTO(
                id=uuid4(),  # different from UUID(hash_hex)
                note_id=uuid4(),
                vault_id=uuid4(),
                title='Via Hash',
                text='Hash content.',
                level=1,
                node_hash=hash_hex,
                seq=0,
                status='active',
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            ),
            NodeDTO(
                id=uuid_node_id,
                note_id=uuid4(),
                vault_id=uuid4(),
                title='Via UUID',
                text='UUID content.',
                level=2,
                node_hash='other_hash',
                seq=1,
                status='active',
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            ),
        ]

        result = await mcp_client.call_tool(
            'memex_get_nodes', {'node_ids': [hash_hex, str(uuid_node_id)]}
        )
        text = result.content[0].text

        assert 'Via Hash' in text
        assert 'Via UUID' in text
        assert 'not found' not in text

    @pytest.mark.asyncio
    async def test_hash_matched_plus_missing_shows_hint_only_for_missing(
        self, mock_api, mcp_client
    ):
        """Hash-matched node OK, truly missing node gets the hint."""
        hash_hex = 'abcdef0123456789abcdef0123456789'
        missing_id = uuid4()

        mock_api.get_nodes.return_value = [
            NodeDTO(
                id=uuid4(),
                note_id=uuid4(),
                vault_id=uuid4(),
                title='Found By Hash',
                text='Content.',
                level=1,
                node_hash=hash_hex,
                seq=0,
                status='active',
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            ),
        ]

        result = await mcp_client.call_tool(
            'memex_get_nodes', {'node_ids': [hash_hex, str(missing_id)]}
        )
        text = result.content[0].text

        assert 'Found By Hash' in text
        # Only the truly missing ID should appear in the hint
        assert str(missing_id) in text
        assert (
            hash_hex not in text.split('not found')[0].split('**Note:**')[-1]
            if '**Note:**' in text
            else True
        )
        assert 'parent sections' in text

    @pytest.mark.asyncio
    async def test_more_than_five_missing_truncated(self, mock_api, mcp_client):
        """When >5 IDs are not found, output truncates with '...'."""
        missing_ids = [uuid4() for _ in range(7)]
        mock_api.get_nodes.return_value = []

        result = await mcp_client.call_tool(
            'memex_get_nodes', {'node_ids': [str(m) for m in missing_ids]}
        )
        text = result.content[0].text

        assert '7 node ID(s) not found' in text
        assert '...' in text
        # Only first 5 IDs should be listed
        for mid in missing_ids[:5]:
            assert str(mid) in text
        for mid in missing_ids[5:]:
            assert str(mid) not in text

    @pytest.mark.asyncio
    async def test_not_found_hint_not_in_errors_block(self, mock_api, mcp_client):
        """Not-found hint should be a **Note:** block, not an ### Errors block."""
        mock_api.get_nodes.return_value = []

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(uuid4())]})
        text = result.content[0].text

        assert '**Note:**' in text
        assert 'memex_get_page_indices' in text
        assert '### Errors' not in text

    @pytest.mark.asyncio
    async def test_node_with_empty_text(self, mock_api, mcp_client):
        """Node with text=None or text='' should show placeholder."""
        nid = uuid4()
        node = NodeDTO(
            id=nid,
            note_id=uuid4(),
            vault_id=uuid4(),
            title='Empty Section',
            text='',
            level=1,
            seq=0,
            status='active',
            created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        )
        mock_api.get_nodes.return_value = [node]

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(nid)]})
        text = result.content[0].text

        assert 'No text content' in text


# ═══════════════════════════════════════════════════════════════════════════════
# memex_get_notes_metadata — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetNotesMetadataEdgeCases:
    """Edge cases for the batch metadata retrieval tool."""

    @pytest.mark.asyncio
    async def test_empty_list(self, mock_api, mcp_client):
        """Empty note_ids list should return a graceful message."""
        result = await mcp_client.call_tool('memex_get_notes_metadata', {'note_ids': []})
        assert 'No metadata found' in result.content[0].text

    @pytest.mark.asyncio
    async def test_all_lookups_fail(self, mock_api, mcp_client):
        """When both batch and individual lookups fail, output should report all errors."""
        nid1 = uuid4()
        nid2 = uuid4()
        mock_api.get_notes_metadata.side_effect = RuntimeError('batch down')
        mock_api.get_note_metadata.side_effect = RuntimeError('DB down')

        result = await mcp_client.call_tool(
            'memex_get_notes_metadata', {'note_ids': [str(nid1), str(nid2)]}
        )
        text = result.content[0].text

        assert 'Errors' in text
        assert str(nid1) in text
        assert str(nid2) in text

    @pytest.mark.asyncio
    async def test_metadata_returns_none(self, mock_api, mcp_client):
        """When batch returns empty for a note, report it."""
        nid = uuid4()
        # Batch returns empty — note not found in batch results
        mock_api.get_notes_metadata.return_value = []

        result = await mcp_client.call_tool('memex_get_notes_metadata', {'note_ids': [str(nid)]})
        text = result.content[0].text

        assert 'no metadata available' in text

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_uuids(self, mock_api, mcp_client):
        """Valid UUIDs proceed; invalid ones are reported in errors."""
        nid = uuid4()
        mock_api.get_notes_metadata.return_value = [
            {'note_id': str(nid), 'title': 'OK Note', 'total_tokens': 100, 'tags': []},
        ]

        result = await mcp_client.call_tool(
            'memex_get_notes_metadata', {'note_ids': [str(nid), 'not-a-uuid']}
        )
        text = result.content[0].text

        assert 'OK Note' in text
        assert 'Invalid UUID: not-a-uuid' in text

    @pytest.mark.asyncio
    async def test_metadata_missing_title_field(self, mock_api, mcp_client):
        """Metadata dict without title or name should show 'Untitled'."""
        nid = uuid4()
        mock_api.get_notes_metadata.return_value = [
            {'note_id': str(nid), 'total_tokens': 300, 'tags': ['misc']},
        ]

        result = await mcp_client.call_tool('memex_get_notes_metadata', {'note_ids': [str(nid)]})
        text = result.content[0].text

        assert 'Untitled' in text
        assert '300' in text

    @pytest.mark.asyncio
    async def test_duplicate_ids_fetched_individually(self, mock_api, mcp_client):
        """Duplicate IDs: batch returns one, second is reported as missing."""
        nid = uuid4()
        mock_api.get_notes_metadata.return_value = [
            {'note_id': str(nid), 'title': 'Same Note', 'total_tokens': 50, 'tags': []},
        ]

        result = await mcp_client.call_tool(
            'memex_get_notes_metadata', {'note_ids': [str(nid), str(nid)]}
        )
        text = result.content[0].text

        # Batch returns one result, the duplicate ID matches so no "not found"
        assert 'Same Note' in text

    @pytest.mark.asyncio
    async def test_batch_fails_falls_back_to_individual(self, mock_api, mcp_client):
        """When batch API fails, tool falls back to individual lookups."""
        nid = uuid4()
        mock_api.get_notes_metadata.side_effect = RuntimeError('batch unavailable')
        mock_api.get_note_metadata.return_value = {
            'title': 'Fallback Note',
            'total_tokens': 100,
            'tags': [],
        }

        result = await mcp_client.call_tool('memex_get_notes_metadata', {'note_ids': [str(nid)]})
        text = result.content[0].text

        assert 'Fallback Note' in text
        mock_api.get_note_metadata.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# memex_get_entities — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetEntitiesEdgeCases:
    """Edge cases for the batch entity retrieval tool."""

    @pytest.mark.asyncio
    async def test_empty_list(self, mock_api, mcp_client):
        """Empty entity_ids should return no-entities message."""
        mock_api.get_entities.return_value = []

        result = await mcp_client.call_tool('memex_get_entities', {'entity_ids': []})
        assert 'No entities found' in result.content[0].text

    @pytest.mark.asyncio
    async def test_batch_fails_fallback_to_individual(self, mock_api, mcp_client):
        """When batch API fails, tool falls back to individual lookups."""
        eid = uuid4()
        mock_api.get_entities.side_effect = RuntimeError('batch not supported')
        mock_api.get_entity.return_value = EntityDTO(
            id=eid, name='Fallback Entity', mention_count=5
        )

        result = await mcp_client.call_tool('memex_get_entities', {'entity_ids': [str(eid)]})
        text = result.content[0].text

        assert 'Fallback Entity' in text
        mock_api.get_entity.assert_called_once_with(eid)

    @pytest.mark.asyncio
    async def test_batch_fails_fallback_also_fails(self, mock_api, mcp_client):
        """When both batch and individual lookups fail, report all errors."""
        eid1 = uuid4()
        eid2 = uuid4()
        mock_api.get_entities.side_effect = RuntimeError('batch error')
        mock_api.get_entity.side_effect = RuntimeError('also broken')

        result = await mcp_client.call_tool(
            'memex_get_entities', {'entity_ids': [str(eid1), str(eid2)]}
        )
        text = result.content[0].text

        assert 'Errors' in text
        assert str(eid1) in text
        assert str(eid2) in text

    @pytest.mark.asyncio
    async def test_no_double_reporting_of_not_found(self, mock_api, mcp_client):
        """Entity not found in batch should NOT be double-reported."""
        eid1 = uuid4()
        eid2 = uuid4()
        e1 = EntityDTO(id=eid1, name='Found', mention_count=10)
        # Batch returns only eid1
        mock_api.get_entities.return_value = [e1]

        result = await mcp_client.call_tool(
            'memex_get_entities', {'entity_ids': [str(eid1), str(eid2)]}
        )
        text = result.content[0].text

        # eid2 should appear in errors exactly once
        assert text.count(f'{eid2}: entity not found') == 1

    @pytest.mark.asyncio
    async def test_fallback_not_found_no_double_report(self, mock_api, mcp_client):
        """When fallback path reports not-found, the post-loop check should not duplicate."""
        eid = uuid4()
        mock_api.get_entities.side_effect = RuntimeError('batch error')
        mock_api.get_entity.return_value = None  # not found

        result = await mcp_client.call_tool('memex_get_entities', {'entity_ids': [str(eid)]})
        text = result.content[0].text

        # "entity not found" for this eid should appear exactly once
        assert text.count('entity not found') == 1

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_uuids(self, mock_api, mcp_client):
        eid = uuid4()
        mock_api.get_entities.return_value = [EntityDTO(id=eid, name='Good', mention_count=1)]

        result = await mcp_client.call_tool('memex_get_entities', {'entity_ids': [str(eid), 'xyz']})
        text = result.content[0].text

        assert 'Good' in text
        assert 'Invalid UUID: xyz' in text

    @pytest.mark.asyncio
    async def test_entity_without_type_or_vault(self, mock_api, mcp_client):
        """Entities without type or vault should render cleanly."""
        eid = uuid4()
        mock_api.get_entities.return_value = [
            EntityDTO(id=eid, name='TypelessEntity', mention_count=3)
        ]

        result = await mcp_client.call_tool('memex_get_entities', {'entity_ids': [str(eid)]})
        text = result.content[0].text

        assert 'TypelessEntity' in text
        assert '**Type:**' not in text
        assert '**Vault:**' not in text


# ═══════════════════════════════════════════════════════════════════════════════
# memex_get_memory_units — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetMemoryUnitsEdgeCases:
    """Edge cases for the batch memory unit retrieval tool."""

    @pytest.mark.asyncio
    async def test_empty_list(self, mock_api, mcp_client):
        result = await mcp_client.call_tool('memex_get_memory_units', {'unit_ids': []})
        assert 'No units found' in result.content[0].text

    @pytest.mark.asyncio
    async def test_all_lookups_fail(self, mock_api, mcp_client):
        """All units fail — should report errors for all, not crash."""
        uid1 = uuid4()
        uid2 = uuid4()
        mock_api.get_memory_unit.side_effect = RuntimeError('storage offline')

        result = await mcp_client.call_tool(
            'memex_get_memory_units', {'unit_ids': [str(uid1), str(uid2)]}
        )
        text = result.content[0].text

        assert 'Error' in text
        assert 'storage offline' in text

    @pytest.mark.asyncio
    async def test_all_units_not_found(self, mock_api, mcp_client):
        """All unit IDs valid but not found — should report each."""
        uid1 = uuid4()
        uid2 = uuid4()
        mock_api.get_memory_unit.return_value = None

        result = await mcp_client.call_tool(
            'memex_get_memory_units', {'unit_ids': [str(uid1), str(uid2)]}
        )
        text = result.content[0].text

        assert 'not found' in text
        assert str(uid1) in text
        assert str(uid2) in text

    @pytest.mark.asyncio
    async def test_all_invalid_uuids(self, mock_api, mcp_client):
        """All UUIDs malformed — should list all as invalid."""
        result = await mcp_client.call_tool('memex_get_memory_units', {'unit_ids': ['aaa', 'bbb']})
        text = result.content[0].text

        assert 'Invalid UUID: aaa' in text
        assert 'Invalid UUID: bbb' in text

    @pytest.mark.asyncio
    async def test_unit_with_supersession_metadata(self, mock_api, mcp_client):
        """Unit with superseded_by metadata should render the chain."""
        uid = uuid4()
        nid = uuid4()
        unit = MemoryUnitDTO(
            id=uid,
            text='Old fact.',
            fact_type=FactTypes.WORLD,
            status='superseded',
            note_id=nid,
            vault_id=uuid4(),
            superseded_by=[
                SupersessionInfo(
                    unit_id=uuid4(),
                    unit_text='Updated fact that replaces the old one.',
                    relation='correction',
                    note_title='Corrections Doc',
                )
            ],
        )
        mock_api.get_memory_unit.return_value = unit

        result = await mcp_client.call_tool('memex_get_memory_units', {'unit_ids': [str(uid)]})
        text = result.content[0].text

        assert 'correction' in text
        assert 'Updated fact' in text
        assert 'Corrections Doc' in text


# ═══════════════════════════════════════════════════════════════════════════════
# memex_memory_search — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestMemorySearchEdgeCases:
    """Edge cases for the search tool."""

    @pytest.mark.asyncio
    async def test_note_title_fetch_fails_gracefully(self, mock_api, mcp_client):
        """If metadata batch call fails entirely, search still returns results."""
        nid = uuid4()
        uid = uuid4()
        mock_api.search.return_value = [
            MemoryUnitDTO(
                id=uid,
                text='Some fact.',
                fact_type=FactTypes.WORLD,
                note_id=nid,
                vault_id=uuid4(),
            )
        ]
        mock_api.get_notes_metadata.side_effect = RuntimeError('metadata service down')

        result = await mcp_client.call_tool(
            'memex_memory_search', {'query': 'test', 'vault_ids': ['test-vault']}
        )
        text = result.content[0].text

        # Results should still appear, just without enriched title
        assert 'Some fact.' in text
        assert str(uid) in text

    @pytest.mark.asyncio
    async def test_result_with_none_note_id(self, mock_api, mcp_client):
        """Results with note_id=None should not render 'Note: None'."""
        uid = uuid4()
        mock_api.search.return_value = [
            MemoryUnitDTO(
                id=uid,
                text='Orphan fact.',
                fact_type=FactTypes.WORLD,
                note_id=None,
                vault_id=uuid4(),
            )
        ]
        mock_api.get_notes_metadata.return_value = []

        result = await mcp_client.call_tool(
            'memex_memory_search', {'query': 'orphan', 'vault_ids': ['test-vault']}
        )
        text = result.content[0].text

        assert 'Note: None' not in text
        assert 'Note: unknown' in text

    @pytest.mark.asyncio
    async def test_malformed_date_raises_tool_error(self, mock_api, mcp_client):
        """Malformed date strings should produce a ToolError, not crash."""
        with pytest.raises(ToolError, match='Search failed'):
            await mcp_client.call_tool(
                'memex_memory_search',
                {'query': 'test', 'after': 'not-a-date', 'vault_ids': ['test-vault']},
            )

    @pytest.mark.asyncio
    async def test_partial_note_titles(self, mock_api, mcp_client):
        """When only some notes have titles, those without should still render."""
        nid1 = uuid4()
        nid2 = uuid4()
        uid1 = uuid4()
        uid2 = uuid4()
        mock_api.search.return_value = [
            MemoryUnitDTO(
                id=uid1,
                text='Fact A.',
                fact_type=FactTypes.WORLD,
                note_id=nid1,
                vault_id=uuid4(),
                score=0.9,
            ),
            MemoryUnitDTO(
                id=uid2,
                text='Fact B.',
                fact_type=FactTypes.WORLD,
                note_id=nid2,
                vault_id=uuid4(),
                score=0.8,
            ),
        ]
        # Only return metadata for nid1
        mock_api.get_notes_metadata.return_value = [{'note_id': str(nid1), 'title': 'Titled Note'}]

        result = await mcp_client.call_tool(
            'memex_memory_search', {'query': 'facts', 'vault_ids': ['test-vault']}
        )
        text = result.content[0].text

        assert 'Titled Note' in text
        assert f'Note: {nid2}' in text  # nid2 falls back to plain ID

    @pytest.mark.asyncio
    async def test_very_long_text_truncated(self, mock_api, mcp_client):
        """Search results with long text should be truncated to 300 chars."""
        long_text = 'x' * 500
        mock_api.search.return_value = [
            MemoryUnitDTO(
                id=uuid4(),
                text=long_text,
                fact_type=FactTypes.WORLD,
                note_id=uuid4(),
                vault_id=uuid4(),
                score=0.9,
            )
        ]
        mock_api.get_notes_metadata.return_value = []

        result = await mcp_client.call_tool(
            'memex_memory_search', {'query': 'test', 'vault_ids': ['test-vault']}
        )
        text = result.content[0].text

        # The snippet should be 300 chars + "..."
        assert '...' in text
        assert 'x' * 301 not in text

    @pytest.mark.asyncio
    async def test_empty_results(self, mock_api, mcp_client):
        mock_api.search.return_value = []

        result = await mcp_client.call_tool(
            'memex_memory_search', {'query': 'nothing', 'vault_ids': ['test-vault']}
        )
        assert 'No results' in result.content[0].text


# ═══════════════════════════════════════════════════════════════════════════════
# memex_get_entity_mentions — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetEntityMentionsEdgeCases:
    """Edge cases for entity mentions tool."""

    @pytest.mark.asyncio
    async def test_mention_with_none_unit(self, mock_api, mcp_client):
        """Mention dict with unit=None should not crash."""
        eid = uuid4()
        nid = uuid4()
        doc = type('Doc', (), {'id': str(nid), 'title': 'SomeNote', 'name': None})()
        mock_api.get_entity_mentions.return_value = [{'unit': None, 'note': doc}]

        result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
        text = result.content[0].text

        assert 'Found 1 mention(s)' in text
        assert 'N/A' in text  # unit_id should be N/A

    @pytest.mark.asyncio
    async def test_mention_with_none_note_and_none_unit(self, mock_api, mcp_client):
        """Completely empty mention dict should render gracefully."""
        eid = uuid4()
        mock_api.get_entity_mentions.return_value = [{'unit': None}]

        result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
        text = result.content[0].text

        assert 'Found 1 mention(s)' in text
        assert 'N/A' in text

    @pytest.mark.asyncio
    async def test_mention_note_has_name_but_not_title(self, mock_api, mcp_client):
        """Note with .name but not .title should still show in output."""
        eid = uuid4()
        uid = uuid4()
        nid = uuid4()
        unit = type(
            'Unit',
            (),
            {
                'id': str(uid),
                'text': 'Fact here.',
                'fact_type': 'world',
            },
        )()
        doc = type(
            'Doc',
            (),
            {
                'id': str(nid),
                'title': None,
                'name': 'Name-Based Title',
            },
        )()
        mock_api.get_entity_mentions.return_value = [{'unit': unit, 'note': doc}]

        result = await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})
        text = result.content[0].text

        assert 'Name-Based Title' in text

    @pytest.mark.asyncio
    async def test_api_error_becomes_tool_error(self, mock_api, mcp_client):
        eid = uuid4()
        mock_api.get_entity_mentions.side_effect = RuntimeError('timeout')

        with pytest.raises(ToolError, match='timeout'):
            await mcp_client.call_tool('memex_get_entity_mentions', {'entity_id': str(eid)})


# ═══════════════════════════════════════════════════════════════════════════════
# memex_get_entity_cooccurrences — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetEntityCooccurrencesEdgeCases:
    """Edge cases for co-occurrences tool."""

    @pytest.mark.asyncio
    async def test_limit_one(self, mock_api, mcp_client):
        """Limit=1 should pass through and return at most 1 result."""
        eid = uuid4()
        mock_api.get_entity_cooccurrences.return_value = []

        await mcp_client.call_tool(
            'memex_get_entity_cooccurrences', {'entity_id': str(eid), 'limit': 1}
        )
        mock_api.get_entity_cooccurrences.assert_called_once_with(UUID(str(eid)), limit=1)

    @pytest.mark.asyncio
    async def test_singular_co_occurrence_label(self, mock_api, mcp_client):
        """Count=1 should say 'co-occurrence' (singular), not 'co-occurrences'."""
        eid = uuid4()
        other_id = uuid4()
        mock_api.get_entity_cooccurrences.return_value = [
            {
                'entity_id_1': str(eid),
                'entity_id_2': str(other_id),
                'entity_1_name': 'A',
                'entity_2_name': 'B',
                'entity_2_type': '',
                'cooccurrence_count': 1,
            }
        ]

        result = await mcp_client.call_tool(
            'memex_get_entity_cooccurrences', {'entity_id': str(eid)}
        )
        text = result.content[0].text

        assert '1 co-occurrence' in text
        assert '1 co-occurrences' not in text

    @pytest.mark.asyncio
    async def test_name_present_but_type_empty(self, mock_api, mcp_client):
        """When name exists but type is empty, output should NOT have '(, ID: ...)'."""
        eid = uuid4()
        other_id = uuid4()
        mock_api.get_entity_cooccurrences.return_value = [
            {
                'entity_id_1': str(eid),
                'entity_id_2': str(other_id),
                'entity_1_name': 'Source',
                'entity_2_name': 'Target',
                'entity_2_type': '',
                'cooccurrence_count': 3,
            }
        ]

        result = await mcp_client.call_tool(
            'memex_get_entity_cooccurrences', {'entity_id': str(eid)}
        )
        text = result.content[0].text

        assert '**Target**' in text
        # Must not have "(, ID:" — the comma before ID without a type
        assert '(, ID:' not in text
        assert f'(ID: {other_id})' in text

    @pytest.mark.asyncio
    async def test_api_error_becomes_tool_error(self, mock_api, mcp_client):
        eid = uuid4()
        mock_api.get_entity_cooccurrences.side_effect = RuntimeError('broke')

        with pytest.raises(ToolError, match='broke'):
            await mcp_client.call_tool('memex_get_entity_cooccurrences', {'entity_id': str(eid)})

    @pytest.mark.asyncio
    async def test_entity_id_not_in_either_position(self, mock_api, mcp_client):
        """Cooccurrence where queried ID is not in either position falls through correctly."""
        eid = uuid4()
        other1 = uuid4()
        other2 = uuid4()
        mock_api.get_entity_cooccurrences.return_value = [
            {
                'entity_id_1': str(other1),
                'entity_id_2': str(other2),
                'entity_1_name': 'Alpha',
                'entity_2_name': 'Beta',
                'entity_1_type': 'Type1',
                'entity_2_type': 'Type2',
                'cooccurrence_count': 5,
            }
        ]

        result = await mcp_client.call_tool(
            'memex_get_entity_cooccurrences', {'entity_id': str(eid)}
        )
        text = result.content[0].text

        # Falls to else branch — shows entity_1 info
        assert 'Alpha' in text


# ═══════════════════════════════════════════════════════════════════════════════
# memex_recent_notes — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecentNotesEdgeCases:
    """Edge cases for recent notes tool."""

    @pytest.mark.asyncio
    async def test_vault_resolution_failure(self, mock_api, mcp_client):
        """If vault resolution fails, the tool should raise ToolError."""
        mock_api.resolve_vault_identifier = AsyncMock(side_effect=RuntimeError('no such vault'))

        with pytest.raises(ToolError, match='Vault not found'):
            await mcp_client.call_tool('memex_recent_notes', {'vault_ids': ['nonexistent-vault']})

    @pytest.mark.asyncio
    async def test_note_without_title(self, mock_api, mcp_client):
        """Note with title=None should show 'Untitled'."""
        n = NoteDTO(
            id=uuid4(),
            title=None,
            vault_id=uuid4(),
            created_at=dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc),
        )
        mock_api.get_recent_notes.return_value = [n]

        result = await mcp_client.call_tool('memex_recent_notes', {})
        text = result.content[0].text

        assert '**Untitled**' in text


# ═══════════════════════════════════════════════════════════════════════════════
# memex_read_note — boundary conditions
# ═══════════════════════════════════════════════════════════════════════════════


class TestReadNoteBoundary:
    """Token limit boundary tests for read_note."""

    @pytest.mark.asyncio
    async def test_exactly_499_tokens_allowed(self, mock_api, mcp_client):
        """499 tokens should be allowed (< 500)."""
        doc_id = uuid4()
        mock_api.get_note_metadata.return_value = {'title': 'Edge', 'total_tokens': 499}
        mock_api.get_note.return_value = NoteDTO(
            id=doc_id,
            doc_metadata={'name': 'Edge'},
            original_text='Content.',
            vault_id=uuid4(),
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        )

        result = await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})
        assert 'Content.' in result.content[0].text

    @pytest.mark.asyncio
    async def test_exactly_500_tokens_blocked(self, mock_api, mcp_client):
        """500 tokens should be blocked (>= 500)."""
        doc_id = uuid4()
        mock_api.get_note_metadata.return_value = {'title': 'Big', 'total_tokens': 500}

        with pytest.raises(ToolError, match='500 tokens'):
            await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})

    @pytest.mark.asyncio
    async def test_metadata_missing_total_tokens_allows_read(self, mock_api, mcp_client):
        """When metadata exists but has no total_tokens, fall through to read."""
        doc_id = uuid4()
        mock_api.get_note_metadata.return_value = {'title': 'NoTokens'}
        mock_api.get_note.return_value = NoteDTO(
            id=doc_id,
            doc_metadata={'name': 'NoTokens'},
            original_text='Should work.',
            vault_id=uuid4(),
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        )

        result = await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})
        assert 'Should work.' in result.content[0].text


# ═══════════════════════════════════════════════════════════════════════════════
# memex_list_entities — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestListEntitiesEdgeCases:
    """Edge cases for list entities tool."""

    @pytest.mark.asyncio
    async def test_ranked_generator_error_mid_iteration(self, mock_api, mcp_client):
        """If the async generator raises mid-iteration, the tool should surface it."""

        async def _broken_generator(limit=100, vault_ids=None, entity_type=None):
            yield EntityDTO(id=uuid4(), name='Good', mention_count=5)
            raise RuntimeError('generator broke')

        mock_api.list_entities_ranked = _broken_generator

        with pytest.raises(ToolError, match='generator broke'):
            await mcp_client.call_tool('memex_list_entities', {'vault_id': 'test-vault'})

    @pytest.mark.asyncio
    async def test_entity_with_special_characters_in_name(self, mock_api, mcp_client):
        """Entity names with markdown-special chars should render without breaking."""
        e = EntityDTO(id=uuid4(), name='C++ & "Rust" <lang>', mention_count=2)

        async def _gen(limit=100, vault_ids=None, entity_type=None):
            yield e

        mock_api.list_entities_ranked = _gen

        result = await mcp_client.call_tool('memex_list_entities', {'vault_id': 'test-vault'})
        text = result.content[0].text

        assert 'C++ & "Rust" <lang>' in text

    @pytest.mark.asyncio
    async def test_entity_type_case_insensitive(self, mock_api, mcp_client):
        """entity_type should be normalised to title-case (e.g. 'person' → 'Person')."""
        e = EntityDTO(id=uuid4(), name='Alice', mention_count=3)
        mock_api.search_entities = AsyncMock(return_value=[e])

        result = await mcp_client.call_tool(
            'memex_list_entities',
            {'query': 'alice', 'entity_type': 'person', 'vault_id': 'test-vault'},
        )
        text = result.content[0].text

        assert 'Alice' in text
        mock_api.search_entities.assert_called_once()
        call_kwargs = mock_api.search_entities.call_args
        assert call_kwargs[0][0] == 'alice'
        assert call_kwargs[1]['limit'] == 20
        assert call_kwargs[1]['entity_type'] == 'Person'


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-cutting: UUID format variations
# ═══════════════════════════════════════════════════════════════════════════════


class TestUUIDFormats:
    """Test that different UUID formats are handled correctly."""

    @pytest.mark.asyncio
    async def test_uppercase_uuid_accepted(self, mock_api, mcp_client):
        """UUID in uppercase should be accepted."""
        nid = uuid4()
        mock_api.get_nodes.return_value = []

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(nid).upper()]})
        text = result.content[0].text
        # Should not say "Invalid UUID"
        assert 'Invalid UUID' not in text

    @pytest.mark.asyncio
    async def test_uuid_without_dashes_accepted(self, mock_api, mcp_client):
        """UUID without dashes (hex format) should be accepted by Python's UUID()."""
        nid = uuid4()
        hex_str = nid.hex  # no dashes
        mock_api.get_nodes.return_value = []

        result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [hex_str]})
        text = result.content[0].text
        assert 'Invalid UUID' not in text

    @pytest.mark.asyncio
    async def test_empty_string_uuid_rejected(self, mock_api, mcp_client):
        """Empty string should be caught as invalid UUID."""
        with pytest.raises(ToolError, match='Invalid UUID'):
            await mcp_client.call_tool('memex_get_nodes', {'node_ids': ['']})


# Need AsyncMock / MagicMock for vault resolution + resource tests
from unittest.mock import AsyncMock, MagicMock


# ═══════════════════════════════════════════════════════════════════════════════
# SVG resource handling
# ═══════════════════════════════════════════════════════════════════════════════


class TestSVGResourceHandling:
    """SVGs should be returned as File objects, not Image objects."""

    @pytest.mark.asyncio
    async def test_svg_local_path_not_returned_as_file_uri(self, mock_api, mcp_client):
        """SVG with local path should NOT get file:// URI (that's for raster images only)."""
        mock_api.get_resource_path = MagicMock(return_value='/data/images/diagram.svg')
        mock_api.get_resource.return_value = b'<svg>...</svg>'

        result = await mcp_client.call_tool(
            'memex_get_resources', {'paths': ['images/diagram.svg'], 'vault_id': 'test-vault'}
        )

        # Should fall through to get_resource and return as File, not file:// URI
        contents = result.content
        assert len(contents) >= 1
        # Should NOT be a plain text file:// URI
        text_contents = [c for c in contents if hasattr(c, 'text')]
        for tc in text_contents:
            assert 'file://' not in tc.text or 'Error' in tc.text

    @pytest.mark.asyncio
    async def test_svg_remote_returned_as_file_not_image(self, mock_api, mcp_client):
        """SVG without local path should be returned as File (EmbeddedResource), not Image."""
        mock_api.get_resource_path = MagicMock(return_value=None)
        mock_api.get_resource.return_value = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'

        result = await mcp_client.call_tool(
            'memex_get_resources', {'paths': ['assets/chart.svg'], 'vault_id': 'test-vault'}
        )

        contents = result.content
        assert len(contents) >= 1
        # Should be an EmbeddedResource (File), not an Image
        resource_contents = [c for c in contents if c.type == 'resource']
        image_contents = [c for c in contents if c.type == 'image']
        assert len(resource_contents) == 1, 'SVG should be returned as EmbeddedResource'
        assert len(image_contents) == 0, 'SVG should NOT be returned as Image'

    @pytest.mark.asyncio
    async def test_png_still_returned_as_image_uri(self, mock_api, mcp_client):
        """Raster images (PNG) should still get file:// URI treatment."""
        mock_api.get_resource_path = MagicMock(return_value='/data/images/photo.png')

        result = await mcp_client.call_tool(
            'memex_get_resources', {'paths': ['images/photo.png'], 'vault_id': 'test-vault'}
        )

        texts = [c.text for c in result.content if hasattr(c, 'text')]
        combined = ' '.join(texts)
        assert 'file:///data/images/photo.png' in combined


# ═══════════════════════════════════════════════════════════════════════════════
# Note title fallback — list_assets and read_note
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoteTitleFallback:
    """note.title should take precedence over doc_metadata for display name."""

    @pytest.mark.asyncio
    async def test_list_assets_uses_note_title(self, mock_api, mcp_client):
        """memex_list_assets should prefer note.title over doc_metadata."""
        doc_id = uuid4()
        mock_api.get_note.return_value = NoteDTO(
            id=doc_id,
            title='Extracted Title',
            doc_metadata={'name': 'Original Filename.md'},
            assets=['images/photo.png'],
            vault_id=uuid4(),
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        )

        result = await mcp_client.call_tool(
            'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
        )
        text = result.content[0].text

        assert 'Extracted Title' in text
        assert 'Original Filename.md' not in text

    @pytest.mark.asyncio
    async def test_list_assets_falls_back_to_doc_metadata(self, mock_api, mcp_client):
        """When note.title is None, use doc_metadata name."""
        doc_id = uuid4()
        mock_api.get_note.return_value = NoteDTO(
            id=doc_id,
            title=None,
            doc_metadata={'name': 'From Metadata'},
            assets=['images/photo.png'],
            vault_id=uuid4(),
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        )

        result = await mcp_client.call_tool(
            'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
        )
        text = result.content[0].text

        assert 'From Metadata' in text

    @pytest.mark.asyncio
    async def test_list_assets_untitled_when_all_empty(self, mock_api, mcp_client):
        """When both note.title and doc_metadata are empty, show 'Untitled'."""
        doc_id = uuid4()
        mock_api.get_note.return_value = NoteDTO(
            id=doc_id,
            title=None,
            doc_metadata={},
            assets=['images/photo.png'],
            vault_id=uuid4(),
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        )

        result = await mcp_client.call_tool(
            'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
        )
        text = result.content[0].text

        assert 'Untitled' in text

    @pytest.mark.asyncio
    async def test_read_note_uses_note_title(self, mock_api, mcp_client):
        """memex_read_note should prefer note.title over doc_metadata."""
        doc_id = uuid4()
        mock_api.get_note_metadata.return_value = {'total_tokens': 100}
        mock_api.get_note.return_value = NoteDTO(
            id=doc_id,
            title='Page Index Title',
            doc_metadata={'name': 'raw-file.md', 'title': 'Meta Title'},
            original_text='Body text.',
            vault_id=uuid4(),
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        )

        result = await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})
        text = result.content[0].text

        assert '# Page Index Title' in text
        assert 'raw-file.md' not in text

    @pytest.mark.asyncio
    async def test_read_note_falls_back_to_doc_metadata_title(self, mock_api, mcp_client):
        """When note.title is None, use doc_metadata['title']."""
        doc_id = uuid4()
        mock_api.get_note_metadata.return_value = {'total_tokens': 50}
        mock_api.get_note.return_value = NoteDTO(
            id=doc_id,
            title=None,
            doc_metadata={'title': 'Fallback Title'},
            original_text='Content.',
            vault_id=uuid4(),
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        )

        result = await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})
        text = result.content[0].text

        assert '# Fallback Title' in text

    @pytest.mark.asyncio
    async def test_read_note_empty_string_title_falls_through(self, mock_api, mcp_client):
        """Empty string title should be falsy and fall through to doc_metadata."""
        doc_id = uuid4()
        mock_api.get_note_metadata.return_value = {'total_tokens': 50}
        mock_api.get_note.return_value = NoteDTO(
            id=doc_id,
            title='',
            doc_metadata={'name': 'Actual Name'},
            original_text='Content.',
            vault_id=uuid4(),
            created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        )

        result = await mcp_client.call_tool('memex_read_note', {'note_id': str(doc_id)})
        text = result.content[0].text

        assert '# Actual Name' in text
        assert '# Untitled' not in text
