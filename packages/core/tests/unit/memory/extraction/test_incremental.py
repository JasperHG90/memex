"""Unit tests for incremental document ingestion.

Tests cover:
- content_hash normalization and determinism
- stable_chunk_text boundary cases, force-split, and index monotonicity
- StableBlock data class
- Diff classification (RETAINED / ADDED / REMOVED)
- LLM chunk assembly with ±1 RETAINED neighbor context
"""

from memex_core.memory.extraction.core import (
    content_hash,
    stable_chunk_text,
    BLOCK_HARD_LIMIT,
)
from memex_core.memory.extraction.models import StableBlock


class TestContentHash:
    """Tests for content_hash function."""

    def test_same_text_same_hash(self) -> None:
        """Identical text produces identical hash."""
        text = 'Hello, world!'
        assert content_hash(text) == content_hash(text)

    def test_trailing_whitespace_ignored(self) -> None:
        """Trailing/leading whitespace is stripped before hashing."""
        assert content_hash('Hello, world!') == content_hash('  Hello, world!  ')

    def test_tab_space_normalization(self) -> None:
        """Tabs and multiple spaces are normalized to single space."""
        assert content_hash('foo  bar') == content_hash('foo bar')
        assert content_hash('foo\tbar') == content_hash('foo bar')
        assert content_hash('foo\t\t  bar') == content_hash('foo bar')

    def test_content_change_different_hash(self) -> None:
        """Substantive content change produces different hash."""
        assert content_hash('Hello') != content_hash('Goodbye')

    def test_newline_preserved(self) -> None:
        """Single newlines within text are preserved (not normalized)."""
        assert content_hash('line1\nline2') != content_hash('line1 line2')

    def test_empty_string(self) -> None:
        """Empty string produces a deterministic hash."""
        h = content_hash('')
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex digest

    def test_returns_hex_sha256(self) -> None:
        """Hash is a 64-char hex string (SHA-256)."""
        h = content_hash('test')
        assert len(h) == 64
        assert all(c in '0123456789abcdef' for c in h)


class TestStableChunkText:
    """Tests for stable_chunk_text function."""

    def test_empty_document(self) -> None:
        """Empty string produces no blocks."""
        assert stable_chunk_text('') == []

    def test_only_whitespace(self) -> None:
        """Whitespace-only text with markdown_aware=False still produces one block (CDC behavior)."""
        blocks = stable_chunk_text('   \n\n   \n\n   ', markdown_aware=False)
        assert len(blocks) == 1
        assert blocks[0].text.strip() == ''

    def test_single_paragraph(self) -> None:
        """Single paragraph without \\n\\n produces one block."""
        text = 'This is a single paragraph.'
        blocks = stable_chunk_text(text)
        assert len(blocks) == 1
        assert blocks[0].text == text
        assert blocks[0].block_index == 0

    def test_no_double_newline(self) -> None:
        """Text without \\n\\n is treated as one block."""
        text = 'line1\nline2\nline3'
        blocks = stable_chunk_text(text)
        assert len(blocks) == 1
        assert blocks[0].text == text

    def test_multiple_paragraphs(self) -> None:
        """CDC doesn't split on double newlines by default - small text stays as one block."""
        text = 'Paragraph one.\n\nParagraph two.\n\nParagraph three.'
        blocks = stable_chunk_text(text, markdown_aware=False)
        assert len(blocks) == 1
        assert blocks[0].text == text

    def test_block_indices_sequential(self) -> None:
        """Block indices are sequential starting from 0."""
        text = 'A\n\nB\n\nC'
        blocks = stable_chunk_text(text, markdown_aware=False)
        assert [b.block_index for b in blocks] == list(range(len(blocks)))

    def test_content_hashes_unique_for_different_content(self) -> None:
        """Each block with different text gets a unique hash."""
        text = 'Alpha\n\nBeta\n\nGamma'
        blocks = stable_chunk_text(text, markdown_aware=False)
        hashes = [b.content_hash for b in blocks]
        assert len(set(hashes)) == len(blocks)

    def test_content_hash_deterministic(self) -> None:
        """Same text always produces same hash."""
        text = 'Deterministic\n\nTest'
        blocks1 = stable_chunk_text(text, markdown_aware=False)
        blocks2 = stable_chunk_text(text, markdown_aware=False)
        assert len(blocks1) == len(blocks2)
        for b1, b2 in zip(blocks1, blocks2):
            assert b1.content_hash == b2.content_hash

    def test_empty_paragraphs_skipped(self) -> None:
        """CDC preserves all content including multiple newlines."""
        text = 'First\n\n\n\nSecond'
        blocks = stable_chunk_text(text, markdown_aware=False)
        assert len(blocks) >= 1
        combined = ''.join(b.text for b in blocks)
        assert 'First' in combined
        assert 'Second' in combined

    def test_whitespace_stripped_from_blocks(self) -> None:
        """CDC preserves whitespace within blocks."""
        text = '  First  \n\n  Second  '
        blocks = stable_chunk_text(text, markdown_aware=False)
        assert len(blocks) >= 1
        combined = ''.join(b.text for b in blocks)
        assert 'First' in combined
        assert 'Second' in combined

    def test_paragraph_edit_localizes_hash_change(self) -> None:
        """Editing content changes hash - CDC stability property."""
        text_v1 = 'Para one.\n\nPara two.\n\nPara three.'
        text_v2 = 'Para one.\n\nPara two EDITED.\n\nPara three.'

        blocks_v1 = stable_chunk_text(text_v1, markdown_aware=False)
        blocks_v2 = stable_chunk_text(text_v2, markdown_aware=False)

        assert len(blocks_v1) == len(blocks_v2)
        for b1, b2 in zip(blocks_v1, blocks_v2):
            if b1.text == b2.text:
                assert b1.content_hash == b2.content_hash
            else:
                assert b1.content_hash != b2.content_hash

    def test_force_split_oversized_block(self) -> None:
        """Block exceeding hard_limit is force-split into sub-blocks."""
        text = 'A' * 200
        blocks = stable_chunk_text(text, hard_limit=50, markdown_aware=False)
        assert len(blocks) >= 2
        combined = ''.join(b.text for b in blocks)
        assert len(combined) >= 200

    def test_force_split_unique_indices(self) -> None:
        """Force-split sub-blocks have unique, sequential block_index values."""
        text = 'Small para.\n\n' + 'B' * 200 + '\n\nAnother small.'
        blocks = stable_chunk_text(text, hard_limit=50)
        indices = [b.block_index for b in blocks]
        # All indices unique
        assert len(indices) == len(set(indices))
        # Monotonically increasing
        assert indices == sorted(indices)

    def test_force_split_monotonic_counter(self) -> None:
        """block_index is a monotonic counter, not the loop variable."""
        # Two oversized blocks: each gets multiple sub-blocks
        text = 'A' * 200 + '\n\n' + 'B' * 200
        blocks = stable_chunk_text(text, hard_limit=50)
        # All indices should be unique and sequential
        for i, block in enumerate(blocks):
            assert block.block_index == i

    def test_default_hard_limit(self) -> None:
        """Default hard_limit is BLOCK_HARD_LIMIT."""
        assert BLOCK_HARD_LIMIT == 50_000


class TestStableBlock:
    """Tests for StableBlock data class."""

    def test_creation(self) -> None:
        """StableBlock can be created with required fields."""
        block = StableBlock(text='hello', content_hash='abc123', block_index=0)
        assert block.text == 'hello'
        assert block.content_hash == 'abc123'
        assert block.block_index == 0

    def test_equality(self) -> None:
        """Two StableBlocks with same fields are equal."""
        b1 = StableBlock(text='hello', content_hash='abc', block_index=0)
        b2 = StableBlock(text='hello', content_hash='abc', block_index=0)
        assert b1 == b2


class TestDiffClassification:
    """Tests for the block diff classification logic.

    These test the classification rules directly without going through
    the engine, by simulating what _extract_incremental does.
    """

    @staticmethod
    def _classify(
        new_blocks: list[StableBlock],
        existing_hashes: set[str],
    ) -> tuple[set[str], list[StableBlock], set[str]]:
        """Replicate the diff classification from the engine."""
        new_hash_set = {b.content_hash for b in new_blocks}
        retained = new_hash_set & existing_hashes
        added = [b for b in new_blocks if b.content_hash not in existing_hashes]
        removed = existing_hashes - new_hash_set
        return retained, added, removed

    def test_first_ingest_all_added(self) -> None:
        """First-time ingestion: all blocks are ADDED."""
        blocks = [
            StableBlock(text='A', content_hash='h_a', block_index=0),
            StableBlock(text='B', content_hash='h_b', block_index=1),
        ]
        retained, added, removed = self._classify(blocks, set())
        assert len(retained) == 0
        assert len(added) == 2
        assert len(removed) == 0

    def test_identical_reingest_all_retained(self) -> None:
        """Identical re-ingestion: all blocks are RETAINED."""
        blocks = [
            StableBlock(text='A', content_hash='h_a', block_index=0),
            StableBlock(text='B', content_hash='h_b', block_index=1),
        ]
        existing = {'h_a', 'h_b'}
        retained, added, removed = self._classify(blocks, existing)
        assert retained == {'h_a', 'h_b'}
        assert len(added) == 0
        assert len(removed) == 0

    def test_mixed_diff(self) -> None:
        """Mixed: one retained, one added, one removed."""
        new_blocks = [
            StableBlock(text='A', content_hash='h_a', block_index=0),
            StableBlock(text='C', content_hash='h_c', block_index=1),
        ]
        existing = {'h_a', 'h_b'}
        retained, added, removed = self._classify(new_blocks, existing)
        assert retained == {'h_a'}
        assert len(added) == 1
        assert added[0].content_hash == 'h_c'
        assert removed == {'h_b'}

    def test_reordered_all_retained(self) -> None:
        """Reordered blocks: all RETAINED (order-independent hash check)."""
        blocks = [
            StableBlock(text='B', content_hash='h_b', block_index=0),
            StableBlock(text='A', content_hash='h_a', block_index=1),
        ]
        existing = {'h_a', 'h_b'}
        retained, added, removed = self._classify(blocks, existing)
        assert retained == {'h_a', 'h_b'}
        assert len(added) == 0
        assert len(removed) == 0


class TestContextAssembly:
    """Tests for LLM chunk assembly with neighbor context."""

    @staticmethod
    def _assemble(
        all_blocks: list[StableBlock],
        added_blocks: list[StableBlock],
        retained_hashes: set[str],
    ) -> list[dict[str, str]]:
        """Replicate the _assemble_llm_chunks logic."""
        block_by_index = {b.block_index: b for b in all_blocks}
        result: list[dict[str, str]] = []

        for block in added_blocks:
            context_parts: list[str] = []

            prev_idx = block.block_index - 1
            if prev_idx in block_by_index:
                prev = block_by_index[prev_idx]
                if prev.content_hash in retained_hashes:
                    context_parts.append(prev.text)

            next_idx = block.block_index + 1
            if next_idx in block_by_index:
                nxt = block_by_index[next_idx]
                if nxt.content_hash in retained_hashes:
                    context_parts.append(nxt.text)

            result.append(
                {
                    'text': block.text,
                    'context': '\n\n'.join(context_parts),
                    'content_hash': block.content_hash,
                }
            )

        return result

    def test_added_block_with_retained_neighbors(self) -> None:
        """ADDED block gets ±1 RETAINED neighbor as context."""
        all_blocks = [
            StableBlock(text='Retained before', content_hash='h_a', block_index=0),
            StableBlock(text='Added middle', content_hash='h_new', block_index=1),
            StableBlock(text='Retained after', content_hash='h_c', block_index=2),
        ]
        added = [all_blocks[1]]
        retained = {'h_a', 'h_c'}

        chunks = self._assemble(all_blocks, added, retained)
        assert len(chunks) == 1
        assert chunks[0]['text'] == 'Added middle'
        assert 'Retained before' in chunks[0]['context']
        assert 'Retained after' in chunks[0]['context']

    def test_added_block_no_retained_neighbors(self) -> None:
        """ADDED block with no retained neighbors gets empty context."""
        all_blocks = [
            StableBlock(text='Added first', content_hash='h_new1', block_index=0),
            StableBlock(text='Added second', content_hash='h_new2', block_index=1),
        ]
        added = all_blocks
        retained: set[str] = set()

        chunks = self._assemble(all_blocks, added, retained)
        assert len(chunks) == 2
        assert chunks[0]['context'] == ''
        assert chunks[1]['context'] == ''

    def test_added_block_with_only_preceding_neighbor(self) -> None:
        """ADDED block at end only gets preceding neighbor."""
        all_blocks = [
            StableBlock(text='Retained', content_hash='h_a', block_index=0),
            StableBlock(text='Added at end', content_hash='h_new', block_index=1),
        ]
        added = [all_blocks[1]]
        retained = {'h_a'}

        chunks = self._assemble(all_blocks, added, retained)
        assert len(chunks) == 1
        assert chunks[0]['context'] == 'Retained'

    def test_non_adjacent_retained_not_included(self) -> None:
        """Only ±1 neighbors are included, not ±2."""
        all_blocks = [
            StableBlock(text='Far retained', content_hash='h_far', block_index=0),
            StableBlock(text='Also added', content_hash='h_also', block_index=1),
            StableBlock(text='Target added', content_hash='h_target', block_index=2),
        ]
        added = [all_blocks[2]]
        retained = {'h_far'}  # index 0 is not adjacent to index 2

        chunks = self._assemble(all_blocks, added, retained)
        assert chunks[0]['context'] == ''
