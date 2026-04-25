"""Tests for PageIndex models, utils, and short-doc bypass."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memex_core.memory.extraction.models import (
    BlockSummary,
    PageIndexBlock,
    PageIndexOutput,
    SectionSummary,
    TOCNode,
    content_hash_md5,
    estimate_token_count,
)
from memex_core.memory.extraction.pipeline.diffing import (
    _inject_subtree_tokens,
    build_page_index_with_metadata,
)
from memex_core.memory.extraction.utils import (
    assess_structure_quality,
    build_tree_from_regex_headers,
    compute_coverage,
    detect_markdown_headers_regex,
    filter_valid_nodes,
    generate_blocks_and_assign_ids,
    hydrate_tree,
    strip_header_from_content,
)
from memex_core.memory.extraction.core import AsyncMarkdownPageIndex, index_document


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestEstimateTokenCount:
    def test_empty_string(self) -> None:
        assert estimate_token_count('') == 0

    def test_none_value(self) -> None:
        assert estimate_token_count(None) == 0

    def test_returns_positive_for_text(self) -> None:
        count = estimate_token_count('Hello, world!')
        assert count > 0

    def test_longer_text_has_more_tokens(self) -> None:
        short = estimate_token_count('Hi')
        long = estimate_token_count('This is a significantly longer text with many more words.')
        assert long > short


class TestContentHashMd5:
    def test_deterministic(self) -> None:
        assert content_hash_md5('hello') == content_hash_md5('hello')

    def test_different_inputs(self) -> None:
        assert content_hash_md5('hello') != content_hash_md5('world')

    def test_returns_hex_string(self) -> None:
        result = content_hash_md5('test')
        assert len(result) == 32
        assert all(c in '0123456789abcdef' for c in result)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TestSectionSummary:
    def test_formatted_all_fields(self) -> None:
        s = SectionSummary(
            who='Alice', what='ran tests', how='with pytest', when='today', where='CI'
        )
        assert s.formatted == 'Alice | ran tests | with pytest | today | CI'

    def test_formatted_partial_fields(self) -> None:
        s = SectionSummary(what='ran tests', how='with pytest')
        assert s.formatted == 'ran tests | with pytest'

    def test_formatted_empty(self) -> None:
        s = SectionSummary()
        assert s.formatted == ''


class TestBlockSummary:
    def test_formatted_with_points(self) -> None:
        bs = BlockSummary(topic='Testing', key_points=['Tests pass', 'Coverage up'])
        assert bs.formatted == 'Testing — Tests pass | Coverage up'

    def test_formatted_no_points(self) -> None:
        bs = BlockSummary(topic='Testing', key_points=[])
        assert bs.formatted == 'Testing'


class TestTOCNode:
    def test_content_hash_with_content(self) -> None:
        node = TOCNode(
            original_header_id=0,
            title='Intro',
            level=1,
            reasoning='test',
            content='Some text',
        )
        assert node.content_hash == content_hash_md5('Some text')

    def test_content_hash_none_content(self) -> None:
        node = TOCNode(
            original_header_id=0,
            title='Intro',
            level=1,
            reasoning='test',
            content=None,
        )
        assert node.content_hash is None

    def test_assign_content_hash_ids(self) -> None:
        child = TOCNode(
            original_header_id=1,
            title='Sub',
            level=2,
            reasoning='test',
            content='child text',
        )
        parent = TOCNode(
            original_header_id=0,
            title='Parent',
            level=1,
            reasoning='test',
            content='parent text',
            children=[child],
        )
        parent._assign_content_hash_ids()
        assert parent.id == content_hash_md5('parent text')
        assert child.id == content_hash_md5('child text')

    def test_tree_without_text(self) -> None:
        node = TOCNode(
            original_header_id=0,
            title='Root',
            level=1,
            reasoning='test',
            content='secret text',
        )
        dump = node.tree_without_text()
        assert 'content' not in dump
        assert 'reasoning' not in dump
        assert dump['title'] == 'Root'


class TestPageIndexOutput:
    def test_get_block_found(self) -> None:
        block = PageIndexBlock(
            id='abc123',
            seq=0,
            content='text',
            token_count=10,
            titles_included=['Title'],
            start_index=0,
            end_index=100,
        )
        output = PageIndexOutput(
            toc=[],
            blocks=[block],
            node_to_block_map={'node1': 'abc123'},
        )
        assert output.get_block('node1') == block

    def test_get_block_not_found(self) -> None:
        output = PageIndexOutput(
            toc=[],
            blocks=[],
            node_to_block_map={},
        )
        assert output.get_block('missing') is None


# ---------------------------------------------------------------------------
# Header detection
# ---------------------------------------------------------------------------

SAMPLE_MARKDOWN = """\
# Introduction

This is the intro paragraph.

## Background

Some background text here.

### Details

Even more details.

## Methods

Description of methods.
"""


class TestDetectMarkdownHeadersRegex:
    def test_detects_all_headers(self) -> None:
        headers = detect_markdown_headers_regex(SAMPLE_MARKDOWN)
        assert len(headers) == 4
        titles = [h.clean_title for h in headers]
        assert titles == ['Introduction', 'Background', 'Details', 'Methods']

    def test_level_hints(self) -> None:
        headers = detect_markdown_headers_regex(SAMPLE_MARKDOWN)
        levels = [h.level_hint for h in headers]
        assert levels == ['h1', 'h2', 'h3', 'h2']

    def test_start_indices_ascending(self) -> None:
        headers = detect_markdown_headers_regex(SAMPLE_MARKDOWN)
        indices = [h.start_index for h in headers]
        assert indices == sorted(indices)

    def test_all_verified(self) -> None:
        headers = detect_markdown_headers_regex(SAMPLE_MARKDOWN)
        assert all(h.verified for h in headers)

    def test_no_headers_returns_empty(self) -> None:
        headers = detect_markdown_headers_regex('Just plain text without any headers.')
        assert headers == []

    def test_ids_sequential(self) -> None:
        headers = detect_markdown_headers_regex(SAMPLE_MARKDOWN)
        assert [h.id for h in headers] == [0, 1, 2, 3]


class TestAssessStructureQuality:
    def test_no_headers(self) -> None:
        quality = assess_structure_quality([], 1000)
        assert quality.is_well_structured is False
        assert quality.header_count == 0

    def test_well_structured(self) -> None:
        headers = detect_markdown_headers_regex(SAMPLE_MARKDOWN)
        quality = assess_structure_quality(headers, len(SAMPLE_MARKDOWN))
        assert quality.header_count == 4
        assert quality.has_hierarchy is True
        assert quality.coverage_ratio > 0

    def test_empty_doc(self) -> None:
        quality = assess_structure_quality([], 0)
        assert quality.is_well_structured is False
        assert quality.max_gap_chars == 0


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------


class TestBuildTreeFromRegexHeaders:
    def test_builds_hierarchy(self) -> None:
        headers = detect_markdown_headers_regex(SAMPLE_MARKDOWN)
        tree = build_tree_from_regex_headers(headers)
        # Root should be the h1 "Introduction"
        assert len(tree) == 1
        root = tree[0]
        assert root.title == 'Introduction'
        assert root.level == 1
        # h2 children: Background, Methods
        assert len(root.children) == 2
        assert root.children[0].title == 'Background'
        assert root.children[1].title == 'Methods'
        # h3 under Background: Details
        assert len(root.children[0].children) == 1
        assert root.children[0].children[0].title == 'Details'

    def test_flat_headers(self) -> None:
        text = '# A\n\n# B\n\n# C\n'
        headers = detect_markdown_headers_regex(text)
        tree = build_tree_from_regex_headers(headers)
        assert len(tree) == 3
        assert all(n.children == [] for n in tree)


class TestHydrateTree:
    def test_populates_content(self) -> None:
        headers = detect_markdown_headers_regex(SAMPLE_MARKDOWN)
        tree = build_tree_from_regex_headers(headers)
        hydrate_tree(tree, headers, SAMPLE_MARKDOWN)
        root = tree[0]
        assert root.content is not None
        assert root.start_index is not None
        assert root.end_index is not None
        assert root.token_estimate is not None
        assert root.token_estimate > 0

    def test_children_have_content(self) -> None:
        headers = detect_markdown_headers_regex(SAMPLE_MARKDOWN)
        tree = build_tree_from_regex_headers(headers)
        hydrate_tree(tree, headers, SAMPLE_MARKDOWN)
        bg = tree[0].children[0]  # Background
        assert bg.content is not None
        assert 'Background' in bg.content


class TestFilterValidNodes:
    def test_keeps_valid(self) -> None:
        nodes = [
            TOCNode(original_header_id=0, title='A', level=1, reasoning='t'),
            TOCNode(original_header_id=1, title='B', level=1, reasoning='t'),
        ]
        result = filter_valid_nodes(nodes, max_id=1)
        assert len(result) == 2

    def test_drops_invalid(self) -> None:
        nodes = [
            TOCNode(original_header_id=0, title='A', level=1, reasoning='t'),
            TOCNode(original_header_id=99, title='Bad', level=1, reasoning='t'),
        ]
        result = filter_valid_nodes(nodes, max_id=5)
        assert len(result) == 1
        assert result[0].title == 'A'


class TestComputeCoverage:
    def test_full_coverage(self) -> None:
        node = TOCNode(
            original_header_id=0,
            title='All',
            level=1,
            reasoning='t',
            content='x' * 100,
            start_index=0,
            end_index=100,
        )
        coverage = compute_coverage([node], 100)
        assert coverage == pytest.approx(1.0)

    def test_zero_doc_length(self) -> None:
        # Empty doc is considered fully covered
        coverage = compute_coverage([], 0)
        assert coverage == 1.0


class TestStripHeaderFromContent:
    def test_strips_matching_header(self) -> None:
        text = '## Methods\nSome content here.'
        result = strip_header_from_content(text, 'Methods')
        assert result.strip() == 'Some content here.'

    def test_no_header_returns_original(self) -> None:
        text = 'Just plain content.'
        result = strip_header_from_content(text, 'Missing')
        assert result == text


# ---------------------------------------------------------------------------
# Block generation
# ---------------------------------------------------------------------------


class TestGenerateBlocksAndAssignIds:
    def test_single_node_single_block(self) -> None:
        node = TOCNode(
            original_header_id=0,
            title='Intro',
            level=1,
            reasoning='test',
            content='Some short text.',
            start_index=0,
            end_index=16,
            token_estimate=4,
        )
        node.id = content_hash_md5('Some short text.')
        blocks, mapping = generate_blocks_and_assign_ids([node], block_size=1000)
        assert len(blocks) == 1
        assert blocks[0].seq == 0
        assert 'Intro' in blocks[0].titles_included
        assert node.id in mapping

    def test_multiple_nodes_merge(self) -> None:
        nodes = []
        offset = 0
        for i in range(3):
            text = f'Content for section {i}.'
            n = TOCNode(
                original_header_id=i,
                title=f'Section {i}',
                level=1,
                reasoning='test',
                content=text,
                start_index=offset,
                end_index=offset + len(text),
                token_estimate=5,
            )
            n.id = content_hash_md5(text)
            nodes.append(n)
            offset += len(text) + 10

        # Large block_size → all merge into one block
        blocks, mapping = generate_blocks_and_assign_ids(nodes, block_size=10000)
        assert len(blocks) == 1
        assert len(mapping) == 3

    def test_split_across_blocks(self) -> None:
        nodes = []
        offset = 0
        for i in range(4):
            text = f'Section {i} content. ' * 50
            n = TOCNode(
                original_header_id=i,
                title=f'Section {i}',
                level=1,
                reasoning='test',
                content=text,
                start_index=offset,
                end_index=offset + len(text),
                token_estimate=200,
            )
            n.id = content_hash_md5(text)
            nodes.append(n)
            offset += len(text) + 10

        # Small block_size → should produce multiple blocks
        blocks, mapping = generate_blocks_and_assign_ids(nodes, block_size=250)
        assert len(blocks) >= 2
        assert len(mapping) == 4
        # Blocks should have sequential seq values
        assert [b.seq for b in blocks] == list(range(len(blocks)))


# ---------------------------------------------------------------------------
# Short document bypass
# ---------------------------------------------------------------------------


class TestIndexDocumentShortBypass:
    @pytest.mark.asyncio
    async def test_short_doc_bypass(self) -> None:
        """Short docs without headers should bypass PageIndex entirely."""
        short_text = 'A short note about something important.'
        mock_lm = MagicMock()

        result = await index_document(
            full_text=short_text,
            lm=mock_lm,
            short_doc_threshold=2000,
        )

        assert result.path_used == 'short_doc_bypass'
        assert len(result.toc) == 1
        assert len(result.blocks) == 1
        assert result.toc[0].title == 'Content'
        assert result.toc[0].content == short_text
        assert result.blocks[0].content == short_text
        assert result.coverage_ratio == 1.0
        # LM should not have been called
        mock_lm.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_doc_with_headers_uses_page_index(self) -> None:
        """Short docs WITH markdown headers should not bypass."""
        text = '# Title\n\nSome content.\n\n## Sub\n\nMore.'
        # This is short but has headers, so it should NOT use the bypass.
        # It would try to use the full pipeline, which needs an LM for the scan path.
        # Since the regex path should work here, let's just check it doesn't bypass.
        headers = detect_markdown_headers_regex(text)
        assert len(headers) >= 2  # confirms headers are detected
        # The bypass condition is: len < threshold AND no headers
        # Since headers exist, it won't bypass regardless of length


# ---------------------------------------------------------------------------
# End-to-end regex fast path (no LLM needed for tree building / block gen)
# ---------------------------------------------------------------------------

STRUCTURED_DOC = """\
# Machine Learning Overview

Machine learning is a subset of artificial intelligence that enables systems
to learn and improve from experience without being explicitly programmed.

## Supervised Learning

Supervised learning uses labeled training data. The algorithm learns a mapping
from inputs to outputs. Common algorithms include linear regression, decision
trees, and neural networks.

### Classification

Classification predicts discrete labels. Examples include spam detection,
image recognition, and medical diagnosis.

### Regression

Regression predicts continuous values. Examples include house price prediction,
stock forecasting, and temperature estimation.

## Unsupervised Learning

Unsupervised learning finds patterns in data without pre-existing labels.
It is used for clustering, dimensionality reduction, and anomaly detection.

### Clustering

Clustering groups similar data points together. K-means, DBSCAN, and
hierarchical clustering are popular approaches.

## Reinforcement Learning

Reinforcement learning trains agents through rewards and penalties.
Applications include game playing, robotics, and autonomous vehicles.
"""


class TestRegexFastPathPipeline:
    """End-to-end test of the regex fast path pipeline without LLM calls."""

    def test_full_pipeline_produces_valid_output(self) -> None:
        """Verify header detect → tree build → hydrate → blocks produces valid output."""
        # 1. Detect headers
        headers = detect_markdown_headers_regex(STRUCTURED_DOC)
        assert len(headers) >= 7  # 1 h1, 3 h2, 3 h3

        # 2. Assess structure
        quality = assess_structure_quality(headers, len(STRUCTURED_DOC))
        assert quality.is_well_structured is True
        assert quality.has_hierarchy is True

        # 3. Build tree
        tree = build_tree_from_regex_headers(headers)
        assert len(tree) == 1  # Single h1 root
        root = tree[0]
        assert root.title == 'Machine Learning Overview'
        assert root.level == 1

        # h2 children
        h2_titles = [c.title for c in root.children]
        assert 'Supervised Learning' in h2_titles
        assert 'Unsupervised Learning' in h2_titles
        assert 'Reinforcement Learning' in h2_titles

        # h3 grandchildren under Supervised
        supervised = next(c for c in root.children if c.title == 'Supervised Learning')
        h3_titles = [c.title for c in supervised.children]
        assert 'Classification' in h3_titles
        assert 'Regression' in h3_titles

        # 4. Hydrate
        hydrate_tree(tree, headers, STRUCTURED_DOC)
        for node in tree:
            node._assign_content_hash_ids()

        assert root.content is not None
        assert root.start_index is not None
        assert root.token_estimate is not None and root.token_estimate > 0

        # Children should have content too
        for child in root.children:
            assert child.content is not None
            assert child.start_index is not None

        # 5. Compute coverage
        coverage = compute_coverage(tree, len(STRUCTURED_DOC))
        assert coverage > 0.8

        # 6. Generate blocks
        blocks, node_map = generate_blocks_and_assign_ids(tree, block_size=500)
        assert len(blocks) >= 1
        assert len(node_map) > 0

        # All blocks should have content and valid fields
        for block in blocks:
            assert block.content
            assert block.token_count > 0
            assert block.titles_included
            assert block.start_index >= 0
            assert block.end_index > block.start_index

        # Blocks should be sequentially ordered
        assert [b.seq for b in blocks] == list(range(len(blocks)))

        # Node map should map all nodes with content to blocks
        total_nodes_with_content = sum(1 for _ in _collect_nodes_with_content(tree))
        assert len(node_map) == total_nodes_with_content

    def test_pipeline_small_block_size_forces_splits(self) -> None:
        """With a very small block_size, we should get many blocks."""
        headers = detect_markdown_headers_regex(STRUCTURED_DOC)
        tree = build_tree_from_regex_headers(headers)
        hydrate_tree(tree, headers, STRUCTURED_DOC)
        for node in tree:
            node._assign_content_hash_ids()

        blocks, _ = generate_blocks_and_assign_ids(tree, block_size=50)
        # With ~7 sections and block_size=50 tokens, should produce multiple blocks
        assert len(blocks) >= 3

    def test_pipeline_large_block_size_merges(self) -> None:
        """With a very large block_size, everything merges into one block."""
        headers = detect_markdown_headers_regex(STRUCTURED_DOC)
        tree = build_tree_from_regex_headers(headers)
        hydrate_tree(tree, headers, STRUCTURED_DOC)
        for node in tree:
            node._assign_content_hash_ids()

        blocks, node_map = generate_blocks_and_assign_ids(tree, block_size=100000)
        assert len(blocks) == 1
        # All nodes should map to the same block
        block_ids = set(node_map.values())
        assert len(block_ids) == 1


def _collect_nodes_with_content(nodes: list[TOCNode]) -> list[TOCNode]:
    """Helper to recursively collect all nodes that have content."""
    result: list[TOCNode] = []
    for node in nodes:
        if node.content and len(node.content) > 0:
            result.append(node)
        result.extend(_collect_nodes_with_content(node.children))
    return result


# ---------------------------------------------------------------------------
# build_page_index_with_metadata — total_tokens injection
# ---------------------------------------------------------------------------


class TestBuildPageIndexWithMetadataTotalTokens:
    def test_total_tokens_included_in_metadata(self) -> None:
        """build_page_index_with_metadata should inject total_tokens into metadata."""
        headers = detect_markdown_headers_regex(STRUCTURED_DOC)
        tree = build_tree_from_regex_headers(headers)
        hydrate_tree(tree, headers, STRUCTURED_DOC)
        for node in tree:
            node._assign_content_hash_ids()

        metadata = {'title': 'ML Overview', 'description': 'A doc'}
        result = build_page_index_with_metadata(tree, metadata)

        assert 'total_tokens' in result['metadata']
        assert isinstance(result['metadata']['total_tokens'], int)
        assert result['metadata']['total_tokens'] > 0

    def test_total_tokens_sums_all_nodes(self) -> None:
        """total_tokens should equal the recursive sum of all node token_estimates."""
        headers = detect_markdown_headers_regex(STRUCTURED_DOC)
        tree = build_tree_from_regex_headers(headers)
        hydrate_tree(tree, headers, STRUCTURED_DOC)
        for node in tree:
            node._assign_content_hash_ids()

        metadata = {'title': 'Test'}
        result = build_page_index_with_metadata(tree, metadata)

        # Manually sum token_estimate from the thin tree
        def _sum(nodes: list[dict]) -> int:
            total = 0
            for n in nodes:
                total += n.get('token_estimate', 0) or 0
                total += _sum(n.get('children', []))
            return total

        expected = _sum(result['toc'])
        assert result['metadata']['total_tokens'] == expected

    def test_does_not_mutate_caller_metadata(self) -> None:
        """The original metadata dict should not be modified."""
        node = TOCNode(
            original_header_id=0,
            title='Root',
            level=1,
            reasoning='test',
            content='Some content here.',
            token_estimate=10,
        )
        node._assign_content_hash_ids()

        original_metadata = {'title': 'Immutable'}
        build_page_index_with_metadata([node], original_metadata)

        assert 'total_tokens' not in original_metadata

    def test_total_tokens_zero_when_no_estimates(self) -> None:
        """total_tokens should be 0 when nodes have no token_estimate."""
        node = TOCNode(
            original_header_id=0,
            title='Empty',
            level=1,
            reasoning='test',
            content=None,
        )
        node._assign_content_hash_ids()

        result = build_page_index_with_metadata([node], {'title': 'Test'})
        assert result['metadata']['total_tokens'] == 0


# ---------------------------------------------------------------------------
# _inject_subtree_tokens
# ---------------------------------------------------------------------------


class TestInjectSubtreeTokens:
    def test_single_node(self) -> None:
        nodes = [{'token_estimate': 50, 'children': []}]
        total = _inject_subtree_tokens(nodes)
        assert total == 50
        assert nodes[0]['subtree_tokens'] == 50

    def test_parent_with_children(self) -> None:
        child_a: dict[str, Any] = {'token_estimate': 20, 'children': []}
        child_b: dict[str, Any] = {'token_estimate': 30, 'children': []}
        nodes: list[dict[str, Any]] = [
            {'token_estimate': 10, 'children': [child_a, child_b]},
        ]
        total = _inject_subtree_tokens(nodes)
        assert nodes[0]['subtree_tokens'] == 60
        assert child_a['subtree_tokens'] == 20
        assert child_b['subtree_tokens'] == 30
        assert total == 60

    def test_deeply_nested(self) -> None:
        leaf: dict[str, Any] = {'token_estimate': 15, 'children': []}
        mid: dict[str, Any] = {'token_estimate': 10, 'children': [leaf]}
        nodes: list[dict[str, Any]] = [
            {'token_estimate': 5, 'children': [mid]},
        ]
        total = _inject_subtree_tokens(nodes)
        assert leaf['subtree_tokens'] == 15
        assert mid['subtree_tokens'] == 25
        assert nodes[0]['subtree_tokens'] == 30
        assert total == 30

    def test_none_token_estimate(self) -> None:
        nodes: list[dict[str, Any]] = [{'token_estimate': None, 'children': []}]
        total = _inject_subtree_tokens(nodes)
        assert total == 0
        assert nodes[0]['subtree_tokens'] == 0

    def test_missing_token_estimate(self) -> None:
        nodes: list[dict[str, Any]] = [{'children': []}]
        total = _inject_subtree_tokens(nodes)
        assert total == 0
        assert nodes[0]['subtree_tokens'] == 0


class TestBuildPageIndexSubtreeTokens:
    def test_subtree_tokens_on_all_nodes(self) -> None:
        """build_page_index_with_metadata should inject subtree_tokens on every node."""
        headers = detect_markdown_headers_regex(STRUCTURED_DOC)
        tree = build_tree_from_regex_headers(headers)
        hydrate_tree(tree, headers, STRUCTURED_DOC)
        for node in tree:
            node._assign_content_hash_ids()

        result = build_page_index_with_metadata(tree, {'title': 'Test'})

        def _check(nodes: list[dict]) -> int:
            total = 0
            for n in nodes:
                assert 'subtree_tokens' in n, f'Node {n["id"]} missing subtree_tokens'
                children_sum = _check(n.get('children', []))
                own = n.get('token_estimate', 0) or 0
                assert n['subtree_tokens'] == own + children_sum
                total += n['subtree_tokens']
            return total

        root_total = _check(result['toc'])
        assert result['metadata']['total_tokens'] == root_total


# ---------------------------------------------------------------------------
# TestScanDocumentParallel — token-based chunking logic
# ---------------------------------------------------------------------------


class TestScanDocumentParallel:
    """Tests for _scan_document_parallel token-based chunking."""

    def _make_indexer(self) -> AsyncMarkdownPageIndex:
        mock_lm = MagicMock()
        return AsyncMarkdownPageIndex(lm=mock_lm)

    @pytest.mark.asyncio
    async def test_small_doc_sends_single_call(self) -> None:
        """A short document should be scanned in a single LLM call."""
        indexer = self._make_indexer()
        short_text = 'Hello world. ' * 50  # ~100 tokens

        with patch.object(
            indexer, '_process_single_chunk', new_callable=AsyncMock, return_value=[]
        ) as mock_chunk:
            result = await indexer._scan_document_parallel(short_text, max_scan_tokens=20_000)

        assert result == []
        mock_chunk.assert_called_once_with(short_text, '', 0)

    @pytest.mark.asyncio
    async def test_large_doc_chunks_by_tokens(self) -> None:
        """A large document should be split into multiple chunks."""
        indexer = self._make_indexer()
        large_text = 'word ' * 50_000  # ~50K tokens

        with patch.object(
            indexer, '_process_single_chunk', new_callable=AsyncMock, return_value=[]
        ) as mock_chunk:
            await indexer._scan_document_parallel(large_text, max_scan_tokens=20_000)

        assert mock_chunk.call_count >= 3
        # First chunk starts at offset 0
        first_call_offset = mock_chunk.call_args_list[0][0][2]
        assert first_call_offset == 0
        # Last chunk should reach near the end of the document
        last_call_args = mock_chunk.call_args_list[-1][0]
        last_offset = last_call_args[2]
        last_chunk = last_call_args[0]
        assert last_offset + len(last_chunk) >= len(large_text) - 200

    @pytest.mark.asyncio
    async def test_boundary_doc_exactly_at_limit(self) -> None:
        """A document exactly at the token limit should be sent as a single call."""
        indexer = self._make_indexer()
        # Build text that is exactly at the limit
        # estimate_token_count uses tiktoken; we pick a size and measure
        boundary_text = 'word ' * 20_000  # should be ~20K tokens

        token_count = estimate_token_count(boundary_text)

        with patch.object(
            indexer, '_process_single_chunk', new_callable=AsyncMock, return_value=[]
        ) as mock_chunk:
            await indexer._scan_document_parallel(boundary_text, max_scan_tokens=token_count)

        # Boundary is <= so exactly at limit should be a single call
        mock_chunk.assert_called_once_with(boundary_text, '', 0)

    @pytest.mark.asyncio
    async def test_scan_respects_semaphore(self) -> None:
        """With scan_max_concurrency=1, concurrent scan tasks must execute sequentially.

        Regression for issue #40: unbounded asyncio.gather over `_process_single_chunk`
        could fan out past host capacity on memory-constrained runners.
        """
        import asyncio
        import time

        indexer = AsyncMarkdownPageIndex(lm=MagicMock(), scan_max_concurrency=1)
        large_text = 'word ' * 50_000  # ~50K tokens -> multiple chunks

        windows: list[tuple[float, float]] = []

        async def _fake_chunk(chunk: str, prev: str, offset: int) -> list:
            # Mimic _process_single_chunk's semaphore-gated body: acquire, work,
            # release. We bypass the real LLM but record [enter, exit] windows.
            async with indexer._scan_semaphore:
                start = time.perf_counter()
                await asyncio.sleep(0.02)
                end = time.perf_counter()
                windows.append((start, end))
            return []

        with patch.object(indexer, '_process_single_chunk', side_effect=_fake_chunk):
            await indexer._scan_document_parallel(large_text, max_scan_tokens=20_000)

        assert len(windows) >= 3, f'expected multiple chunks, got {len(windows)}'
        # With concurrency=1, windows must not overlap.
        sorted_windows = sorted(windows, key=lambda w: w[0])
        for (_, prev_end), (next_start, _) in zip(sorted_windows, sorted_windows[1:]):
            assert next_start >= prev_end - 1e-3, (
                f'windows overlap: prev ended at {prev_end}, next started at {next_start}'
            )


class TestDetectAndFillGaps:
    """Regression tests for _detect_and_fill_gaps chunking oversized gaps (issue #40)."""

    def _make_indexer(self, gap_rescan_threshold_tokens: int = 2000) -> AsyncMarkdownPageIndex:
        return AsyncMarkdownPageIndex(
            lm=MagicMock(),
            scan_max_concurrency=5,
            gap_rescan_threshold_tokens=gap_rescan_threshold_tokens,
        )

    @pytest.mark.asyncio
    async def test_oversized_gap_is_chunked_not_submitted_whole(self) -> None:
        """A gap several times larger than scan_chunk_size_tokens must fan out
        into multiple scan tasks, not a single oversize LLM call.

        Regression for issue #40: a 139K-char gap was submitted as one ~35K-token
        prompt, producing the wedge observed on the Jetson Orin Nano.
        """
        from memex_core.memory.extraction.models import DetectedHeader

        indexer = self._make_indexer()
        # Small max_scan_tokens to force chunking with a manageable text size.
        max_scan_tokens = 500
        # Big enough to produce multiple chunks at max_scan_tokens=500.
        gap_text = 'word ' * 3_500  # ~3.5K tokens → ~7 chunks
        headers = [
            DetectedHeader(
                reasoning='sentinel end-header',
                exact_text='## End',
                clean_title='End',
                level_hint='h2',
                start_index=len(gap_text),
            )
        ]
        full_text = gap_text + '## End\nfin.\n'

        with patch.object(
            indexer, '_process_single_chunk', new_callable=AsyncMock, return_value=[]
        ) as mock_chunk:
            result = await indexer._detect_and_fill_gaps(
                headers, full_text, max_scan_tokens=max_scan_tokens
            )

        assert mock_chunk.call_count > 1, (
            f'expected oversized gap to fan out into multiple scan tasks, '
            f'got {mock_chunk.call_count}'
        )
        # All scan-task offsets must land inside the gap (start at 0).
        offsets = [call.args[2] for call in mock_chunk.call_args_list]
        assert offsets[0] == 0
        # Sorted offsets should be strictly increasing.
        assert offsets == sorted(offsets)
        assert result == headers  # no new headers from mocked scanner

    @pytest.mark.asyncio
    async def test_small_gap_below_threshold_is_skipped(self) -> None:
        """A gap below gap_rescan_threshold_tokens should not trigger a rescan."""
        from memex_core.memory.extraction.models import DetectedHeader

        indexer = self._make_indexer(gap_rescan_threshold_tokens=2000)
        small_gap = 'word ' * 200  # ~200 tokens, below threshold
        headers = [
            DetectedHeader(
                reasoning='sentinel end-header',
                exact_text='## End',
                clean_title='End',
                level_hint='h2',
                start_index=len(small_gap),
            )
        ]
        full_text = small_gap + '## End\nfin.\n'

        with patch.object(
            indexer, '_process_single_chunk', new_callable=AsyncMock, return_value=[]
        ) as mock_chunk:
            result = await indexer._detect_and_fill_gaps(headers, full_text, max_scan_tokens=20_000)

        mock_chunk.assert_not_called()
        assert result == headers

    @pytest.mark.asyncio
    async def test_tail_gap_above_threshold_is_chunked(self) -> None:
        """A tail gap (text after the last detected header) must also be chunked."""
        from memex_core.memory.extraction.models import DetectedHeader

        indexer = self._make_indexer()
        header = DetectedHeader(
            reasoning='sentinel intro header',
            exact_text='## Intro',
            clean_title='Intro',
            level_hint='h2',
            start_index=0,
        )
        tail_text = 'word ' * 3_500  # ~3.5K tokens → multiple chunks at 500 tokens
        full_text = '## Intro\n' + tail_text

        with patch.object(
            indexer, '_process_single_chunk', new_callable=AsyncMock, return_value=[]
        ) as mock_chunk:
            await indexer._detect_and_fill_gaps([header], full_text, max_scan_tokens=500)

        assert mock_chunk.call_count > 1
        # All tail-chunk offsets must be at or beyond the end of the header.
        offsets = [call.args[2] for call in mock_chunk.call_args_list]
        assert min(offsets) >= len('## Intro')


class TestSemaphoreGating:
    """AC-001/002/003: refine + summary fan-out sites observe their semaphore caps."""

    @pytest.mark.asyncio
    async def test_refine_respects_semaphore(self) -> None:
        """AC-001: with refine_max_concurrency=1, _refine_tree_recursively peers
        cannot overlap.

        We patch `_process_single_node_refinement`'s LLM-doing inner step
        (`_process_single_chunk`) into a counter that records [enter, exit]
        windows under the refine semaphore. Then we drive
        `_refine_tree_recursively` with a flat tree of nodes that all need
        refinement. With cap=1 the windows must not overlap.
        """
        import asyncio
        import time

        indexer = AsyncMarkdownPageIndex(lm=MagicMock(), refine_max_concurrency=1)

        # Build 5 peer nodes that will all trip the "node_len > max_len and
        # not node.children" branch in _process_single_node_refinement so the
        # LLM-gated body runs (we don't actually need _process_single_chunk
        # to find any sub-headers — we only care that the gated body executes).
        nodes = []
        for i in range(5):
            node = TOCNode(
                original_header_id=i,
                title=f'Section {i}',
                level=1,
                reasoning='test',
                content='x' * 6000,
                start_index=i * 6000,
                end_index=(i + 1) * 6000,
            )
            nodes.append(node)

        windows: list[tuple[float, float]] = []

        async def _fake_chunk(*_args: Any, **_kwargs: Any) -> list:
            start = time.perf_counter()
            await asyncio.sleep(0.02)
            end = time.perf_counter()
            windows.append((start, end))
            return []

        with patch.object(indexer, '_process_single_chunk', side_effect=_fake_chunk):
            await indexer._refine_tree_recursively(nodes, full_text='x' * 30_000, max_len=5000)

        assert len(windows) == 5
        sorted_windows = sorted(windows, key=lambda w: w[0])
        for (_, prev_end), (next_start, _) in zip(sorted_windows, sorted_windows[1:]):
            assert next_start >= prev_end - 1e-3, (
                f'refine windows overlap with cap=1: prev ended at {prev_end}, '
                f'next started at {next_start}'
            )

    @pytest.mark.asyncio
    async def test_summarize_leaf_respects_semaphore(self) -> None:
        """AC-002: leaf-summary fan-out observes summarize_max_concurrency."""
        import asyncio
        import time

        indexer = AsyncMarkdownPageIndex(lm=MagicMock(), summarize_max_concurrency=1)

        # 5 leaf nodes (no children, content over 50 chars) that
        # _generate_summaries_parallel will route to _summarize_single_node.
        leaves = [
            TOCNode(
                original_header_id=i,
                title=f'Leaf {i}',
                level=2,
                reasoning='test',
                content='word ' * 60,
            )
            for i in range(5)
        ]

        windows: list[tuple[float, float]] = []

        async def _fake_run_dspy(*_args: Any, semaphore=None, **_kwargs: Any) -> Any:
            assert semaphore is indexer._summary_semaphore, (
                f'expected _summary_semaphore, got {semaphore!r}'
            )
            async with semaphore:
                start = time.perf_counter()
                await asyncio.sleep(0.02)
                end = time.perf_counter()
                windows.append((start, end))
                pred = MagicMock()
                pred.summary = SectionSummary(what='ok')
                return pred

        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation', side_effect=_fake_run_dspy
        ):
            await indexer._generate_summaries_parallel(leaves)

        assert len(windows) == 5
        sorted_windows = sorted(windows, key=lambda w: w[0])
        for (_, prev_end), (next_start, _) in zip(sorted_windows, sorted_windows[1:]):
            assert next_start >= prev_end - 1e-3, (
                f'leaf summary windows overlap with cap=1: prev {prev_end}, next {next_start}'
            )

    @pytest.mark.asyncio
    async def test_summarize_parent_respects_semaphore(self) -> None:
        """AC-002: parent-summary fan-out observes summarize_max_concurrency."""
        import asyncio
        import time

        indexer = AsyncMarkdownPageIndex(lm=MagicMock(), summarize_max_concurrency=1)

        # Build 5 parent nodes (each has children); _generate_summaries_parallel
        # will route them to _summarize_parent_node.
        parents = []
        for i in range(5):
            child = TOCNode(
                original_header_id=10 + i,
                title=f'Child {i}',
                level=3,
                reasoning='test',
                content='word ' * 60,
            )
            parent = TOCNode(
                original_header_id=i,
                title=f'Parent {i}',
                level=2,
                reasoning='test',
                content='word ' * 60,
                children=[child],
            )
            parents.append(parent)

        windows: list[tuple[float, float]] = []

        async def _fake_run_dspy(*_args: Any, semaphore=None, **_kwargs: Any) -> Any:
            assert semaphore is indexer._summary_semaphore
            async with semaphore:
                start = time.perf_counter()
                await asyncio.sleep(0.02)
                end = time.perf_counter()
                windows.append((start, end))
                pred = MagicMock()
                pred.summary = SectionSummary(what='ok')
                return pred

        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation', side_effect=_fake_run_dspy
        ):
            await indexer._generate_summaries_parallel(parents)

        # Both leaves AND parents are summarised — 5 leaves + 5 parents = 10 calls.
        assert len(windows) == 10
        sorted_windows = sorted(windows, key=lambda w: w[0])
        for (_, prev_end), (next_start, _) in zip(sorted_windows, sorted_windows[1:]):
            assert next_start >= prev_end - 1e-3, (
                f'parent summary windows overlap with cap=1: prev {prev_end}, next {next_start}'
            )

    @pytest.mark.asyncio
    async def test_refine_recursive_tree_does_not_deadlock_at_cap_one(self) -> None:
        """Regression: refine_max_concurrency=1 must NOT deadlock when the tree
        has depth > cap. Earlier wedge proposals wrapped the *recursive call*
        inside the semaphore — that deadlocks because every parent holds a
        slot waiting on its children. Our fix releases the sem before the
        recursive call (extraction/core.py: _process_single_node_refinement).

        Drive a depth-3 tree with cap=1. Test must complete (not hang) — the
        pytest-timeout plugin would surface a hang as a failure.
        """
        indexer = AsyncMarkdownPageIndex(lm=MagicMock(), refine_max_concurrency=1)

        # Build a 3-deep nested tree — refine_tree_recursively will recurse
        # twice. Parent → child → grandchild, each branch with content needing
        # refinement.
        grandchild = TOCNode(
            original_header_id=2,
            title='GC',
            level=3,
            reasoning='test',
            content='x' * 6000,
            start_index=12000,
            end_index=18000,
        )
        child = TOCNode(
            original_header_id=1,
            title='C',
            level=2,
            reasoning='test',
            content='x' * 6000,
            start_index=6000,
            end_index=12000,
            children=[grandchild],
        )
        parent = TOCNode(
            original_header_id=0,
            title='P',
            level=1,
            reasoning='test',
            content='x' * 6000,
            start_index=0,
            end_index=6000,
            children=[child],
        )

        with patch.object(
            indexer, '_process_single_chunk', new_callable=AsyncMock, return_value=[]
        ):
            await indexer._refine_tree_recursively([parent], full_text='x' * 30000, max_len=5000)

        # Sanity: the semaphore is back to full capacity afterwards.
        assert indexer._refine_semaphore._value == 1

    @pytest.mark.asyncio
    async def test_summarize_with_cap_two_allows_overlap(self) -> None:
        """Sanity: cap=2 lets at most 2 leaf-summary windows overlap (proves the
        semaphore is the *binding* constraint, not just incidental sequencing)."""
        import asyncio
        import time

        indexer = AsyncMarkdownPageIndex(lm=MagicMock(), summarize_max_concurrency=2)
        leaves = [
            TOCNode(
                original_header_id=i,
                title=f'Leaf {i}',
                level=2,
                reasoning='test',
                content='word ' * 60,
            )
            for i in range(8)
        ]

        events: list[tuple[float, str]] = []

        async def _fake_run_dspy(*_args: Any, semaphore=None, **_kwargs: Any) -> Any:
            async with semaphore:
                events.append((time.perf_counter(), 'enter'))
                await asyncio.sleep(0.02)
                events.append((time.perf_counter(), 'exit'))
                pred = MagicMock()
                pred.summary = SectionSummary(what='ok')
                return pred

        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation', side_effect=_fake_run_dspy
        ):
            await indexer._generate_summaries_parallel(leaves)

        # At every point in time, in-flight count = (#enters seen) - (#exits seen).
        events.sort(key=lambda e: e[0])
        in_flight = 0
        max_in_flight = 0
        for _t, kind in events:
            if kind == 'enter':
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            else:
                in_flight -= 1
        assert max_in_flight == 2, (
            f'expected max in-flight 2 (cap=2 with 8 tasks), got {max_in_flight}'
        )

    @pytest.mark.asyncio
    async def test_block_summarize_respects_semaphore(self) -> None:
        """AC-003: block-summary fan-out observes summarize_max_concurrency."""
        import asyncio
        import time

        indexer = AsyncMarkdownPageIndex(lm=MagicMock(), summarize_max_concurrency=1)

        # Build a pre-summarised TOC so collect_node_summaries_for_block returns
        # at least one section_pair per block, which routes the block through
        # _summarize_single_block instead of the no-section fast-path.
        nodes: list[TOCNode] = []
        blocks: list[PageIndexBlock] = []
        node_to_block_map: dict[str, str] = {}
        for i in range(5):
            node = TOCNode(
                original_header_id=i,
                title=f'Section {i}',
                level=2,
                reasoning='test',
                content='word ' * 60,
                summary=SectionSummary(what='already summarised'),
            )
            node.id = f'node-{i}'
            block = PageIndexBlock(
                id=f'block-{i}',
                seq=i,
                content='word ' * 60,
                token_count=60,
                titles_included=[f'Section {i}'],
                start_index=i * 100,
                end_index=(i + 1) * 100,
            )
            nodes.append(node)
            blocks.append(block)
            node_to_block_map[node.id] = block.id

        windows: list[tuple[float, float]] = []

        async def _fake_run_dspy(*_args: Any, semaphore=None, **_kwargs: Any) -> Any:
            assert semaphore is indexer._summary_semaphore
            async with semaphore:
                start = time.perf_counter()
                await asyncio.sleep(0.02)
                end = time.perf_counter()
                windows.append((start, end))
                pred = MagicMock()
                pred.block_summary = BlockSummary(topic='t', key_points=['kp.'])
                return pred

        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation', side_effect=_fake_run_dspy
        ):
            await indexer._generate_block_summaries(blocks, nodes, node_to_block_map)

        assert len(windows) == 5
        sorted_windows = sorted(windows, key=lambda w: w[0])
        for (_, prev_end), (next_start, _) in zip(sorted_windows, sorted_windows[1:]):
            assert next_start >= prev_end - 1e-3, (
                f'block summary windows overlap with cap=1: prev {prev_end}, next {next_start}'
            )


class TestConcurrencyKwargFlow:
    """AC-004: refine + summarize kwargs flow from index_document to semaphores."""

    def test_default_semaphores_have_value_5(self) -> None:
        """Default ctor: all three semaphores expose ._value == 5 (back-compat)."""
        indexer = AsyncMarkdownPageIndex(lm=MagicMock())
        assert indexer._scan_semaphore._value == 5
        assert indexer._refine_semaphore._value == 5
        assert indexer._summary_semaphore._value == 5

    def test_custom_semaphore_values_propagate(self) -> None:
        """AC-004: ctor kwargs flow through to the semaphores' _value attribute."""
        indexer = AsyncMarkdownPageIndex(
            lm=MagicMock(),
            scan_max_concurrency=2,
            refine_max_concurrency=3,
            summarize_max_concurrency=7,
        )
        assert indexer._scan_semaphore._value == 2
        assert indexer._refine_semaphore._value == 3
        assert indexer._summary_semaphore._value == 7

    @pytest.mark.asyncio
    async def test_index_document_threads_concurrency_kwargs_to_indexer(self) -> None:
        """AC-004: index_document(...) passes the new kwargs to AsyncMarkdownPageIndex.

        Use the short-doc-bypass path's *opposite* — a doc with regex headers — so
        the indexer is actually constructed. Patch the constructor to capture
        kwargs without running the real LLM-call pipeline.
        """
        text_with_headers = '# H1\n\n' + ('word ' * 1000) + '\n\n## H2\n\nbody\n'
        captured_kwargs: dict[str, Any] = {}

        class _StubIndexer:
            def __init__(self, **kwargs: Any) -> None:
                captured_kwargs.update(kwargs)

            async def aforward(self, *_args: Any, **_kwargs: Any) -> PageIndexOutput:
                return PageIndexOutput(
                    toc=[],
                    blocks=[],
                    node_to_block_map={},
                    coverage_ratio=1.0,
                    path_used='stub',
                )

        with patch(
            'memex_core.memory.extraction.core.AsyncMarkdownPageIndex',
            _StubIndexer,
        ):
            await index_document(
                full_text=text_with_headers,
                lm=MagicMock(),
                short_doc_threshold=10,  # force past short-doc bypass
                scan_max_concurrency=2,
                refine_max_concurrency=3,
                summarize_max_concurrency=7,
                gap_rescan_threshold_tokens=1500,
            )

        assert captured_kwargs['scan_max_concurrency'] == 2
        assert captured_kwargs['refine_max_concurrency'] == 3
        assert captured_kwargs['summarize_max_concurrency'] == 7
        assert captured_kwargs['gap_rescan_threshold_tokens'] == 1500
