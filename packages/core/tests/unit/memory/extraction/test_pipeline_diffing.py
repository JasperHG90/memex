"""Unit tests for extraction pipeline diffing module."""

from __future__ import annotations

import uuid


from memex_core.memory.extraction.models import (
    PageIndexBlock,
    PageIndexOutput,
    StableBlock,
    TOCNode,
)
from memex_core.memory.extraction.pipeline.diffing import (
    BlockDiffResult,
    PageIndexDiffResult,
    build_thin_tree,
    collect_toc_hashes,
    diff_blocks,
    diff_page_index_blocks,
    replace_tree_ids,
    _walk_nodes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stable_block(text: str, index: int) -> StableBlock:
    """Create a StableBlock with deterministic content hash."""
    import hashlib

    normalized = ' '.join(text.split())
    content_hash = hashlib.sha256(normalized.encode()).hexdigest()
    return StableBlock(text=text, content_hash=content_hash, block_index=index)


def _make_existing_block(content_hash: str, block_id: str | None = None) -> dict[str, object]:
    """Simulate a row from storage.get_note_blocks."""
    return {
        'id': block_id or str(uuid.uuid4()),
        'content_hash': content_hash,
        'block_index': 0,
    }


def _make_page_index_block(
    block_id: str, seq: int, content: str = 'block content'
) -> PageIndexBlock:
    return PageIndexBlock(
        id=block_id,
        seq=seq,
        token_count=10,
        start_index=0,
        end_index=len(content),
        titles_included=['Title'],
        content=content,
    )


def _make_toc_node(
    node_id: str,
    content: str | None = None,
    children: list[TOCNode] | None = None,
) -> TOCNode:
    return TOCNode(
        id=node_id,
        reasoning='test',
        original_header_id=1,
        title='Test',
        level=1,
        content=content,
        children=children or [],
    )


# ===========================================================================
# diff_blocks tests
# ===========================================================================


class TestDiffBlocks:
    """Tests for simple hash-based block diffing."""

    def test_all_new_blocks(self) -> None:
        """When no existing blocks, all new blocks are added."""
        blocks = [_make_stable_block('hello world', 0), _make_stable_block('foo bar', 1)]
        result = diff_blocks(blocks, [])

        assert len(result.added_blocks) == 2
        assert len(result.retained_hashes) == 0
        assert len(result.removed_hashes) == 0

    def test_all_retained_blocks(self) -> None:
        """When new and existing hashes match, all are retained."""
        blocks = [_make_stable_block('hello world', 0), _make_stable_block('foo bar', 1)]
        existing = [_make_existing_block(b.content_hash) for b in blocks]

        result = diff_blocks(blocks, existing)

        assert len(result.retained_hashes) == 2
        assert len(result.added_blocks) == 0
        assert len(result.removed_hashes) == 0

    def test_all_removed_blocks(self) -> None:
        """When new blocks are empty, all existing are removed."""
        existing = [_make_existing_block('hash_a'), _make_existing_block('hash_b')]

        result = diff_blocks([], existing)

        assert len(result.removed_hashes) == 2
        assert len(result.added_blocks) == 0
        assert len(result.retained_hashes) == 0

    def test_mixed_diff(self) -> None:
        """Mixed scenario: some retained, some added, some removed."""
        b1 = _make_stable_block('retained content', 0)
        b2 = _make_stable_block('new content', 1)
        existing = [
            _make_existing_block(b1.content_hash),
            _make_existing_block('will_be_removed'),
        ]

        result = diff_blocks([b1, b2], existing)

        assert result.retained_hashes == {b1.content_hash}
        assert len(result.added_blocks) == 1
        assert result.added_blocks[0].content_hash == b2.content_hash
        assert result.removed_hashes == {'will_be_removed'}

    def test_empty_inputs(self) -> None:
        """Both inputs empty yields empty result."""
        result = diff_blocks([], [])

        assert len(result.retained_hashes) == 0
        assert len(result.added_blocks) == 0
        assert len(result.removed_hashes) == 0

    def test_duplicate_existing_hashes(self) -> None:
        """Duplicate hashes in existing blocks don't cause issues."""
        b1 = _make_stable_block('content', 0)
        existing = [
            _make_existing_block(b1.content_hash, 'id1'),
            _make_existing_block(b1.content_hash, 'id2'),
        ]

        result = diff_blocks([b1], existing)

        assert result.retained_hashes == {b1.content_hash}
        assert len(result.added_blocks) == 0
        assert len(result.removed_hashes) == 0

    def test_result_type(self) -> None:
        """Verify the return type is BlockDiffResult."""
        result = diff_blocks([], [])
        assert isinstance(result, BlockDiffResult)


# ===========================================================================
# _walk_nodes tests
# ===========================================================================


class TestWalkNodes:
    """Tests for the recursive TOC tree walker."""

    def test_flat_nodes(self) -> None:
        """Walk flat list of nodes, collecting content hashes."""
        node_a = _make_toc_node('node_a', content='content A')
        node_b = _make_toc_node('node_b', content='content B')

        node_to_block_map = {
            node_a.id: 'block_1',
            node_b.id: 'block_1',
        }
        block_node_hashes: dict[str, set[str]] = {}
        from collections import defaultdict

        block_node_hashes = defaultdict(set)

        _walk_nodes([node_a, node_b], node_to_block_map, block_node_hashes)

        assert node_a.content_hash in block_node_hashes['block_1']
        assert node_b.content_hash in block_node_hashes['block_1']

    def test_nested_nodes(self) -> None:
        """Walk nested tree structure."""
        child = _make_toc_node('child', content='child content')
        parent = _make_toc_node('parent', content='parent content', children=[child])

        node_to_block_map = {
            parent.id: 'block_1',
            child.id: 'block_2',
        }
        from collections import defaultdict

        block_node_hashes: dict[str, set[str]] = defaultdict(set)

        _walk_nodes([parent], node_to_block_map, block_node_hashes)

        assert parent.content_hash in block_node_hashes['block_1']
        assert child.content_hash in block_node_hashes['block_2']

    def test_node_without_content(self) -> None:
        """Nodes without content (content_hash is None) are skipped."""
        node = _make_toc_node('node_a', content=None)
        from collections import defaultdict

        block_node_hashes: dict[str, set[str]] = defaultdict(set)
        _walk_nodes([node], {'node_a': 'block_1'}, block_node_hashes)

        assert len(block_node_hashes) == 0

    def test_node_not_in_block_map(self) -> None:
        """Nodes not in the node_to_block_map are skipped."""
        node = _make_toc_node('node_a', content='content')
        from collections import defaultdict

        block_node_hashes: dict[str, set[str]] = defaultdict(set)
        _walk_nodes([node], {}, block_node_hashes)

        assert len(block_node_hashes) == 0


# ===========================================================================
# diff_page_index_blocks tests
# ===========================================================================


class TestDiffPageIndexBlocks:
    """Tests for page-index block diffing with node-level classification."""

    def _make_page_index_output(
        self,
        blocks: list[PageIndexBlock],
        toc: list[TOCNode],
        node_to_block_map: dict[str, str],
    ) -> PageIndexOutput:
        return PageIndexOutput(
            toc=toc,
            blocks=blocks,
            node_to_block_map=node_to_block_map,
            coverage_ratio=1.0,
            path_used='test',
        )

    def test_all_new_content_changed(self) -> None:
        """With no existing blocks and no prev nodes, all blocks are content_changed."""
        block = _make_page_index_block('hash_a', seq=0)
        node = _make_toc_node('node_1', content='some content')
        pio = self._make_page_index_output(
            blocks=[block],
            toc=[node],
            node_to_block_map={node.id: 'hash_a'},
        )

        result = diff_page_index_blocks(pio, [], set())

        assert result.content_changed_hashes == {'hash_a'}
        assert len(result.retained_hashes) == 0
        assert len(result.boundary_shift_hashes) == 0
        assert len(result.removed_hashes) == 0

    def test_retained_blocks(self) -> None:
        """Blocks with matching existing hashes are retained."""
        block = _make_page_index_block('hash_a', seq=0)
        existing = [_make_existing_block('hash_a')]
        pio = self._make_page_index_output(
            blocks=[block],
            toc=[],
            node_to_block_map={},
        )

        result = diff_page_index_blocks(pio, existing, set())

        assert result.retained_hashes == {'hash_a'}
        assert len(result.content_changed_hashes) == 0
        assert len(result.boundary_shift_hashes) == 0

    def test_removed_blocks(self) -> None:
        """Existing blocks not in new output are removed."""
        block = _make_page_index_block('hash_a', seq=0)
        existing = [_make_existing_block('hash_a'), _make_existing_block('hash_old')]
        pio = self._make_page_index_output(
            blocks=[block],
            toc=[],
            node_to_block_map={},
        )

        result = diff_page_index_blocks(pio, existing, set())

        assert result.removed_hashes == {'hash_old'}

    def test_boundary_shift_detection(self) -> None:
        """New block hash where all constituent nodes existed before is boundary_shift."""
        node = _make_toc_node('node_1', content='existing content')
        block = _make_page_index_block('hash_new', seq=0)
        pio = self._make_page_index_output(
            blocks=[block],
            toc=[node],
            node_to_block_map={node.id: 'hash_new'},
        )
        # The node's content hash existed in the previous version
        prev_node_hashes = {node.content_hash}  # type: ignore[arg-type]

        result = diff_page_index_blocks(pio, [], prev_node_hashes)

        assert result.boundary_shift_hashes == {'hash_new'}
        assert len(result.content_changed_hashes) == 0

    def test_content_changed_detection(self) -> None:
        """New block with at least one new node hash is content_changed."""
        node = _make_toc_node('node_1', content='brand new content')
        block = _make_page_index_block('hash_new', seq=0)
        pio = self._make_page_index_output(
            blocks=[block],
            toc=[node],
            node_to_block_map={node.id: 'hash_new'},
        )
        # Previous nodes had different hashes
        prev_node_hashes = {'some_other_hash'}

        result = diff_page_index_blocks(pio, [], prev_node_hashes)

        assert result.content_changed_hashes == {'hash_new'}
        assert len(result.boundary_shift_hashes) == 0

    def test_mixed_classification(self) -> None:
        """Test a scenario with retained, boundary_shift, content_changed, and removed."""
        # Retained block
        retained_block = _make_page_index_block('hash_retained', seq=0)

        # Boundary shift block: new hash but nodes existed
        bs_node = _make_toc_node('bs_node', content='existing node content')
        bs_block = _make_page_index_block('hash_bs', seq=1)

        # Content changed block: new hash with new nodes
        cc_node = _make_toc_node('cc_node', content='totally new content')
        cc_block = _make_page_index_block('hash_cc', seq=2)

        pio = self._make_page_index_output(
            blocks=[retained_block, bs_block, cc_block],
            toc=[bs_node, cc_node],
            node_to_block_map={
                bs_node.id: 'hash_bs',
                cc_node.id: 'hash_cc',
            },
        )

        existing = [
            _make_existing_block('hash_retained'),
            _make_existing_block('hash_removed'),
        ]
        prev_node_hashes = {bs_node.content_hash}  # type: ignore[arg-type]

        result = diff_page_index_blocks(pio, existing, prev_node_hashes)

        assert result.retained_hashes == {'hash_retained'}
        assert result.boundary_shift_hashes == {'hash_bs'}
        assert result.content_changed_hashes == {'hash_cc'}
        assert result.removed_hashes == {'hash_removed'}

    def test_block_node_hashes_populated(self) -> None:
        """Verify block_node_hashes is correctly populated."""
        node_a = _make_toc_node('node_a', content='content A')
        node_b = _make_toc_node('node_b', content='content B')
        block = _make_page_index_block('hash_1', seq=0)

        pio = self._make_page_index_output(
            blocks=[block],
            toc=[node_a, node_b],
            node_to_block_map={
                node_a.id: 'hash_1',
                node_b.id: 'hash_1',
            },
        )

        result = diff_page_index_blocks(pio, [], set())

        assert node_a.content_hash in result.block_node_hashes['hash_1']
        assert node_b.content_hash in result.block_node_hashes['hash_1']

    def test_result_type(self) -> None:
        """Verify the return type is PageIndexDiffResult."""
        pio = self._make_page_index_output(blocks=[], toc=[], node_to_block_map={})
        result = diff_page_index_blocks(pio, [], set())
        assert isinstance(result, PageIndexDiffResult)

    def test_empty_node_hashes_treated_as_content_changed(self) -> None:
        """A block with no constituent nodes is classified as content_changed."""
        block = _make_page_index_block('hash_new', seq=0)
        pio = self._make_page_index_output(
            blocks=[block],
            toc=[],
            node_to_block_map={},
        )

        result = diff_page_index_blocks(pio, [], {'some_hash'})

        # Empty node_hashes means the condition `node_hashes and ...` is False
        assert result.content_changed_hashes == {'hash_new'}
        assert len(result.boundary_shift_hashes) == 0

    def test_nested_toc_nodes(self) -> None:
        """Deeply nested TOC nodes are correctly walked."""
        grandchild = _make_toc_node('gc', content='deep content')
        child = _make_toc_node('child', content='mid content', children=[grandchild])
        parent = _make_toc_node('parent', content='top content', children=[child])

        block = _make_page_index_block('hash_1', seq=0)
        pio = self._make_page_index_output(
            blocks=[block],
            toc=[parent],
            node_to_block_map={
                parent.id: 'hash_1',
                child.id: 'hash_1',
                grandchild.id: 'hash_1',
            },
        )

        result = diff_page_index_blocks(pio, [], set())

        # All three node hashes should be in the block's node_hashes
        assert len(result.block_node_hashes['hash_1']) == 3


# ===========================================================================
# Thin-tree builder tests
# ===========================================================================


class TestCollectTocHashes:
    """Tests for collect_toc_hashes."""

    def test_flat_nodes(self) -> None:
        """Collect hashes from flat list of nodes."""
        node_a = _make_toc_node('a', content='content A')
        node_b = _make_toc_node('b', content='content B')

        result = collect_toc_hashes([node_a, node_b])

        assert node_a.id in result
        assert node_b.id in result
        assert result[node_a.id] == node_a.content_hash
        assert result[node_b.id] == node_b.content_hash

    def test_nested_nodes(self) -> None:
        """Collect hashes from nested tree structure."""
        child = _make_toc_node('child', content='child content')
        parent = _make_toc_node('parent', content='parent content', children=[child])

        result = collect_toc_hashes([parent])

        assert parent.id in result
        assert child.id in result

    def test_node_without_content_uses_title(self) -> None:
        """Nodes without content use title for hash."""
        node = _make_toc_node('x', content=None)

        result = collect_toc_hashes([node])

        from memex_core.memory.extraction.models import content_hash_md5

        assert result[node.id] == content_hash_md5(node.title)

    def test_empty_toc(self) -> None:
        """Empty TOC returns empty dict."""
        assert collect_toc_hashes([]) == {}


class TestReplaceTreeIds:
    """Tests for replace_tree_ids."""

    def test_replaces_top_level_id(self) -> None:
        """Replace ID at the top level."""
        tree = {'id': 'old_id', 'children': []}
        id_map = {'old_id': 'new_hash'}

        result = replace_tree_ids(tree, id_map)

        assert result['id'] == 'new_hash'

    def test_replaces_nested_ids(self) -> None:
        """Replace IDs recursively in nested children."""
        tree = {
            'id': 'parent',
            'children': [
                {'id': 'child', 'children': []},
            ],
        }
        id_map = {'parent': 'hash_parent', 'child': 'hash_child'}

        result = replace_tree_ids(tree, id_map)

        assert result['id'] == 'hash_parent'
        assert result['children'][0]['id'] == 'hash_child'

    def test_unmapped_id_kept(self) -> None:
        """IDs not in the map are kept as-is."""
        tree = {'id': 'unknown', 'children': []}

        result = replace_tree_ids(tree, {})

        assert result['id'] == 'unknown'


class TestBuildThinTree:
    """Tests for build_thin_tree."""

    def test_basic_thin_tree(self) -> None:
        """Build thin tree from TOC nodes."""
        node = _make_toc_node('n1', content='some content')
        node.token_estimate = 100

        result = build_thin_tree([node])

        assert len(result) == 1
        assert result[0]['id'] == node.content_hash

    def test_min_node_tokens_filter(self) -> None:
        """Nodes below min_node_tokens are excluded."""
        big_node = _make_toc_node('big', content='big content')
        big_node.token_estimate = 100
        small_node = _make_toc_node('small', content='x')
        small_node.token_estimate = 5

        result = build_thin_tree([big_node, small_node], min_node_tokens=10)

        assert len(result) == 1

    def test_empty_toc(self) -> None:
        """Empty TOC produces empty thin tree."""
        assert build_thin_tree([]) == []

    def test_ids_are_content_hashes(self) -> None:
        """Verify all IDs in the thin tree are content hashes, not UUIDs."""
        child = _make_toc_node('c', content='child text')
        child.token_estimate = 50
        parent = _make_toc_node('p', content='parent text', children=[child])
        parent.token_estimate = 100

        result = build_thin_tree([parent])

        assert result[0]['id'] == parent.content_hash
        # Children that pass the filter should also have hash IDs
        child_entries = result[0].get('children', [])
        if child_entries:
            assert child_entries[0]['id'] == child.content_hash
