"""Unit tests for _synthesize_tags merging user tags with block tags."""

from memex_core.memory.extraction.engine import _synthesize_tags
from memex_core.memory.extraction.models import (
    BlockSummary,
    PageIndexBlock,
    PageIndexOutput,
)


def _make_page_index(block_tags: list[list[str]]) -> PageIndexOutput:
    """Helper: build a PageIndexOutput with blocks carrying the given tag lists."""
    blocks = []
    for i, tags in enumerate(block_tags):
        blocks.append(
            PageIndexBlock(
                id=f'block-{i}',
                seq=i,
                token_count=100,
                start_index=0,
                end_index=100,
                titles_included=[f'Section {i}'],
                content=f'block {i} content',
                summary=BlockSummary(
                    topic=f'topic {i}',
                    key_points=[],
                    tags=tags,
                ),
            )
        )
    return PageIndexOutput(
        toc=[],
        blocks=blocks,
        node_to_block_map={},
    )


class TestSynthesizeTags:
    def test_user_tags_merged_with_block_tags(self):
        """User tags and block tags should both appear in the output."""
        page_index = _make_page_index([['python', 'testing']])
        result = _synthesize_tags(page_index, user_tags=['obsidian'])
        assert 'obsidian' in result
        assert 'python' in result
        assert 'testing' in result

    def test_block_tags_only_when_no_user_tags(self):
        """Without user tags, only block tags are returned."""
        page_index = _make_page_index([['alpha', 'beta']])
        result = _synthesize_tags(page_index, user_tags=[])
        assert result == ['alpha', 'beta']

    def test_dedup_case_insensitive(self):
        """Duplicate tags (case-insensitive) are removed."""
        page_index = _make_page_index([['Python', 'TESTING']])
        result = _synthesize_tags(page_index, user_tags=['python', 'testing'])
        # User tags preserved as-is, block dupes excluded
        assert len(result) == 2
        assert result[0] == 'python'
        assert result[1] == 'testing'

    def test_fifteen_tag_cap(self):
        """Output must never exceed 15 tags; user tags are prioritized (appear first)."""
        many_tags = [f'tag-{i}' for i in range(10)]
        page_index = _make_page_index([many_tags])
        user_tags = [f'user-{i}' for i in range(10)]
        result = _synthesize_tags(page_index, user_tags=user_tags)
        assert len(result) == 15
        # User tags must appear first and not be truncated
        assert result[:10] == user_tags

    def test_multiple_blocks_merged(self):
        """Tags from multiple blocks are all included."""
        page_index = _make_page_index([['a', 'b'], ['c', 'd']])
        result = _synthesize_tags(page_index, user_tags=[])
        assert set(result) == {'a', 'b', 'c', 'd'}

    def test_empty_block_tags_with_user_tags(self):
        """User tags returned when blocks have no summaries/tags."""
        page_index = PageIndexOutput(
            toc=[],
            blocks=[
                PageIndexBlock(
                    id='block-0',
                    seq=0,
                    token_count=100,
                    start_index=0,
                    end_index=100,
                    titles_included=[],
                    content='content',
                    summary=None,
                )
            ],
            node_to_block_map={},
        )
        result = _synthesize_tags(page_index, user_tags=['myTag'])
        assert result == ['myTag']

    def test_empty_tags_skipped(self):
        """Whitespace-only tags from blocks are not included."""
        page_index = _make_page_index([['valid', '', '  ']])
        result = _synthesize_tags(page_index, user_tags=[])
        assert result == ['valid']
