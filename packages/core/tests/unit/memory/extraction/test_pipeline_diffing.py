"""Unit tests for extraction pipeline diffing module."""

from __future__ import annotations

import uuid


from memex_core.memory.extraction.models import (
    PageIndexBlock,
    PageIndexOutput,
    StableBlock,
    TOCNode,
)
from memex_core.memory.extraction.models import content_hash_md5
from memex_core.memory.extraction.pipeline.diffing import (
    BlockDiffResult,
    PageIndexDiffResult,
    _walk_nodes,
    assemble_llm_chunks,
    build_thin_tree,
    collect_toc_hashes,
    diff_blocks,
    diff_page_index_blocks,
    find_node_hash,
    flatten_toc_to_node_rows,
    replace_tree_ids,
)
from memex_core.memory.sql_models import ContentStatus


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


# ===========================================================================
# flatten_toc_to_node_rows tests
# ===========================================================================


class TestFlattenTocToNodeRows:
    """Tests for flatten_toc_to_node_rows."""

    def test_basic_flatten(self) -> None:
        """Flatten a simple TOC tree into node rows."""
        node = _make_toc_node('n1', content='Hello world')
        node.token_estimate = 100
        pio = PageIndexOutput(
            toc=[node],
            blocks=[_make_page_index_block('block1', 0)],
            node_to_block_map={'n1': 'block1'},
        )
        vault_id = uuid.uuid4()
        note_id = uuid.uuid4()

        rows = flatten_toc_to_node_rows([node], pio, vault_id, note_id)

        assert len(rows) == 1
        row = rows[0]
        assert row['vault_id'] == vault_id
        assert row['note_id'] == note_id
        assert row['title'] == 'Test'
        assert row['text'] == 'Hello world'
        assert row['level'] == 1
        assert row['seq'] == 0
        assert row['token_estimate'] == 100
        assert row['status'] == ContentStatus.ACTIVE

    def test_deduplicates_by_node_hash(self) -> None:
        """Duplicate node hashes are deduplicated."""
        # Two nodes with same content => same hash
        n1 = _make_toc_node('a', content='same content')
        n1.token_estimate = 50
        n2 = _make_toc_node('b', content='same content')
        n2.token_estimate = 50
        pio = PageIndexOutput(
            toc=[n1, n2],
            blocks=[_make_page_index_block('block1', 0)],
            node_to_block_map={'a': 'block1', 'b': 'block1'},
        )
        vault_id = uuid.uuid4()
        note_id = uuid.uuid4()

        rows = flatten_toc_to_node_rows([n1, n2], pio, vault_id, note_id)

        assert len(rows) == 1

    def test_min_node_tokens_skips_small_nodes(self) -> None:
        """Nodes below min_node_tokens are skipped."""
        big = _make_toc_node('big', content='big content')
        big.token_estimate = 100
        small = _make_toc_node('small', content='x')
        small.token_estimate = 5
        pio = PageIndexOutput(
            toc=[big, small],
            blocks=[_make_page_index_block('block1', 0)],
            node_to_block_map={'big': 'block1', 'small': 'block1'},
        )
        vault_id = uuid.uuid4()
        note_id = uuid.uuid4()

        rows = flatten_toc_to_node_rows([big, small], pio, vault_id, note_id, min_node_tokens=10)

        assert len(rows) == 1
        assert rows[0]['text'] == 'big content'

    def test_min_node_tokens_promotes_children(self) -> None:
        """Small parent nodes promote their children when skipped."""
        child = _make_toc_node('child', content='child content')
        child.token_estimate = 100
        parent = _make_toc_node('parent', content='p', children=[child])
        parent.token_estimate = 5  # Below threshold
        pio = PageIndexOutput(
            toc=[parent],
            blocks=[_make_page_index_block('block1', 0)],
            node_to_block_map={'parent': 'block1', 'child': 'block1'},
        )
        vault_id = uuid.uuid4()
        note_id = uuid.uuid4()

        rows = flatten_toc_to_node_rows([parent], pio, vault_id, note_id, min_node_tokens=10)

        assert len(rows) == 1
        assert rows[0]['text'] == 'child content'

    def test_nested_children_flattened(self) -> None:
        """Children are recursively flattened."""
        child = _make_toc_node('c', content='child')
        child.token_estimate = 50
        parent = _make_toc_node('p', content='parent', children=[child])
        parent.token_estimate = 100
        pio = PageIndexOutput(
            toc=[parent],
            blocks=[_make_page_index_block('block1', 0)],
            node_to_block_map={'p': 'block1', 'c': 'block1'},
        )
        vault_id = uuid.uuid4()
        note_id = uuid.uuid4()

        rows = flatten_toc_to_node_rows([parent], pio, vault_id, note_id)

        assert len(rows) == 2
        assert rows[0]['text'] == 'parent'
        assert rows[0]['seq'] == 0
        assert rows[1]['text'] == 'child'
        assert rows[1]['seq'] == 1

    def test_node_hash_uses_content_hash_property(self) -> None:
        """Nodes with content use content_hash property."""
        node = _make_toc_node('n', content='some text')
        node.token_estimate = 50
        pio = PageIndexOutput(
            toc=[node],
            blocks=[_make_page_index_block('block1', 0)],
            node_to_block_map={'n': 'block1'},
        )

        rows = flatten_toc_to_node_rows([node], pio, uuid.uuid4(), uuid.uuid4())

        assert rows[0]['node_hash'] == content_hash_md5('some text')

    def test_node_without_content_uses_title(self) -> None:
        """Nodes without content compute hash from title."""
        node = _make_toc_node('n', content=None)
        node.token_estimate = 50
        pio = PageIndexOutput(
            toc=[node],
            blocks=[_make_page_index_block('block1', 0)],
            node_to_block_map={'n': 'block1'},
        )

        rows = flatten_toc_to_node_rows([node], pio, uuid.uuid4(), uuid.uuid4())

        expected_hash = content_hash_md5(node.title)
        assert rows[0]['node_hash'] == expected_hash

    def test_empty_toc(self) -> None:
        """Empty TOC returns empty list."""
        pio = PageIndexOutput(
            toc=[],
            blocks=[],
            node_to_block_map={},
        )

        rows = flatten_toc_to_node_rows([], pio, uuid.uuid4(), uuid.uuid4())

        assert rows == []


# ===========================================================================
# find_node_hash tests
# ===========================================================================


class TestFindNodeHash:
    """Tests for find_node_hash."""

    def test_finds_top_level_node(self) -> None:
        """Find a node at the top level."""
        node = _make_toc_node('target', content='content')

        result = find_node_hash([node], 'target')

        assert result == content_hash_md5('content')

    def test_finds_nested_node(self) -> None:
        """Find a deeply nested node."""
        grandchild = _make_toc_node('gc', content='gc text')
        child = _make_toc_node('c', content='c text', children=[grandchild])
        parent = _make_toc_node('p', content='p text', children=[child])

        result = find_node_hash([parent], 'gc')

        assert result == content_hash_md5('gc text')

    def test_returns_none_for_missing_node(self) -> None:
        """Return None when node ID is not found."""
        node = _make_toc_node('other', content='text')

        result = find_node_hash([node], 'nonexistent')

        assert result is None

    def test_computes_hash_from_content(self) -> None:
        """Content hash is computed from content via property."""
        node = _make_toc_node('n', content='some text')

        result = find_node_hash([node], 'n')

        expected = content_hash_md5('some text')
        assert result == expected

    def test_uses_title_when_no_content(self) -> None:
        """Fall back to title when content is None."""
        node = _make_toc_node('n', content=None)

        result = find_node_hash([node], 'n')

        expected = content_hash_md5(node.title)
        assert result == expected

    def test_empty_toc(self) -> None:
        """Empty TOC returns None."""
        assert find_node_hash([], 'any') is None


# ===========================================================================
# assemble_llm_chunks tests
# ===========================================================================


class TestAssembleLlmChunks:
    """Tests for assemble_llm_chunks."""

    def test_basic_assembly(self) -> None:
        """Added blocks are assembled into LLM chunks."""
        b0 = _make_stable_block('block zero', 0)
        b1 = _make_stable_block('block one', 1)
        all_blocks = [b0, b1]
        added = [b0, b1]

        result = assemble_llm_chunks(all_blocks, added, set())

        assert len(result) == 2
        assert result[0]['text'] == 'block zero'
        assert result[1]['text'] == 'block one'

    def test_retained_neighbor_context(self) -> None:
        """Retained neighbor blocks are included as context."""
        b0 = _make_stable_block('retained before', 0)
        b1 = _make_stable_block('added block', 1)
        b2 = _make_stable_block('retained after', 2)
        all_blocks = [b0, b1, b2]
        added = [b1]
        retained = {b0.content_hash, b2.content_hash}

        result = assemble_llm_chunks(all_blocks, added, retained)

        assert len(result) == 1
        assert result[0]['text'] == 'added block'
        assert 'retained before' in result[0]['context']
        assert 'retained after' in result[0]['context']

    def test_no_context_when_neighbors_not_retained(self) -> None:
        """Non-retained neighbors are not included as context."""
        b0 = _make_stable_block('also added', 0)
        b1 = _make_stable_block('main added', 1)
        all_blocks = [b0, b1]
        added = [b1]

        result = assemble_llm_chunks(all_blocks, added, set())

        assert result[0]['context'] == ''

    def test_empty_added_blocks(self) -> None:
        """Empty added blocks produces empty result."""
        b0 = _make_stable_block('retained', 0)

        result = assemble_llm_chunks([b0], [], {b0.content_hash})

        assert result == []

    def test_content_hash_in_result(self) -> None:
        """Each result dict includes the content_hash."""
        b0 = _make_stable_block('test', 0)

        result = assemble_llm_chunks([b0], [b0], set())

        assert result[0]['content_hash'] == b0.content_hash
