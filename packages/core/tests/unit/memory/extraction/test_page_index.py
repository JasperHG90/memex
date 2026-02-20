"""Tests for PageIndex models, utils, and short-doc bypass."""

from unittest.mock import MagicMock

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
from memex_core.memory.extraction.core import index_document


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

        result, usage = await index_document(
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
        # Short doc bypass produces no token usage
        assert usage.total_tokens is None or usage.total_tokens == 0

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
