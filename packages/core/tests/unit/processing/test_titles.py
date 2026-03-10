"""Tests for document title extraction and resolution."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memex_core.processing.titles import (
    _build_summary_text_from_page_index,
    _is_meaningful_name,
    extract_title_from_markdown,
    extract_title_from_page_index_toc,
    extract_title_via_llm,
    resolve_document_title,
    resolve_title_from_page_index,
)


# ---------------------------------------------------------------------------
# _is_meaningful_name
# ---------------------------------------------------------------------------


class TestIsMeaningfulName:
    @pytest.mark.parametrize(
        'name,expected',
        [
            (None, False),
            ('', False),
            ('   ', False),
            ('untitled', False),
            ('Untitled', False),
            ('UNTITLED', False),
            ('note', False),
            ('document', False),
            ('file', False),
            ('page', False),
            ('text', False),
            # UUID-format strings
            ('3f2504e0-4f89-11d3-9a0c-0305e82c3301', False),
            ('3F2504E0-4F89-11D3-9A0C-0305E82C3301', False),
            # Generic stems (new additions)
            ('content', False),
            ('readme', False),
            ('index', False),
            ('main', False),
            ('draft', False),
            # Filenames with generic stems + recognized extensions
            ('content.md', False),
            ('README.md', False),
            ('index.html', False),
            ('untitled.txt', False),
            ('draft.yaml', False),
            ('main.json', False),
            # Temp file names
            ('tmpj0elmicy', False),
            ('tmp12345678', False),
            ('tmp_abc_1234', False),
            # Real words starting with "tmp" are meaningful
            ('template', True),
            # Filenames with meaningful stems (not generic)
            ('my-meeting-notes.md', True),
            ('006: Detailing the Sys Layer architecture', True),
            ('report.pdf', True),  # "report" not in generic names
            # Meaningful names
            ('My Research Notes', True),
            ('Q3 Financial Report', True),
            ('meeting-notes-2024', True),
            ('Report Phase 0', True),
            ('a', True),  # single non-generic character
        ],
    )
    def test_various_inputs(self, name: str | None, expected: bool):
        assert _is_meaningful_name(name) is expected


# ---------------------------------------------------------------------------
# extract_title_from_markdown
# ---------------------------------------------------------------------------


class TestExtractTitleFromMarkdown:
    def test_h1_header_returned(self):
        text = '# My Great Article\n\nSome body text here.'
        assert extract_title_from_markdown(text) == 'My Great Article'

    def test_first_h1_returned_when_multiple(self):
        text = '# First Title\n\n## Sub-section\n\n# Second Title'
        assert extract_title_from_markdown(text) == 'First Title'

    def test_no_h1_returns_none(self):
        text = '## Only a level-2 header\n\nSome content.'
        assert extract_title_from_markdown(text) is None

    def test_empty_string_returns_none(self):
        assert extract_title_from_markdown('') is None

    def test_plain_text_returns_none(self):
        assert extract_title_from_markdown('Just plain text without any headers.') is None

    def test_h1_with_extra_whitespace_stripped(self):
        text = '#   Spaced Title   \n\nContent.'
        assert extract_title_from_markdown(text) == 'Spaced Title'


# ---------------------------------------------------------------------------
# extract_title_from_page_index_toc
# ---------------------------------------------------------------------------


class TestExtractTitleFromPageIndexToc:
    def test_returns_first_level1_node_title(self):
        toc: list[dict[str, Any]] = [
            {'title': 'Introduction', 'level': 1, 'children': []},
            {'title': 'Background', 'level': 2, 'children': []},
        ]
        assert extract_title_from_page_index_toc(toc) == 'Introduction'

    def test_skips_non_level1_nodes(self):
        toc: list[dict[str, Any]] = [
            {'title': 'Section A', 'level': 2, 'children': []},
            {'title': 'Main Title', 'level': 1, 'children': []},
        ]
        assert extract_title_from_page_index_toc(toc) == 'Main Title'

    def test_empty_toc_returns_none(self):
        assert extract_title_from_page_index_toc([]) is None

    def test_no_level1_nodes_returns_none(self):
        toc: list[dict[str, Any]] = [
            {'title': 'Sub', 'level': 2, 'children': []},
            {'title': 'Sub2', 'level': 3, 'children': []},
        ]
        assert extract_title_from_page_index_toc(toc) is None

    def test_empty_title_skipped(self):
        toc: list[dict[str, Any]] = [
            {'title': '', 'level': 1, 'children': []},
            {'title': 'Real Title', 'level': 1, 'children': []},
        ]
        assert extract_title_from_page_index_toc(toc) == 'Real Title'

    def test_whitespace_title_skipped(self):
        toc: list[dict[str, Any]] = [
            {'title': '   ', 'level': 1, 'children': []},
            {'title': 'Actual Title', 'level': 1, 'children': []},
        ]
        assert extract_title_from_page_index_toc(toc) == 'Actual Title'


# ---------------------------------------------------------------------------
# _build_summary_text_from_page_index
# ---------------------------------------------------------------------------


class TestBuildSummaryTextFromPageIndex:
    def test_includes_topic_and_key_points(self):
        toc: list[dict[str, Any]] = [
            {
                'title': 'Intro',
                'level': 1,
                'summary': {
                    'topic': 'Overview of the system',
                    'key_points': ['Point A', 'Point B'],
                },
                'children': [],
            }
        ]
        text = _build_summary_text_from_page_index(toc)
        assert 'Overview of the system' in text
        assert 'Point A' in text

    def test_falls_back_to_section_summary_what_field(self):
        toc: list[dict[str, Any]] = [
            {
                'title': 'Background',
                'level': 1,
                'summary': {'who': None, 'what': 'Historical context of the problem', 'how': None},
                'children': [],
            }
        ]
        text = _build_summary_text_from_page_index(toc)
        assert 'Background' in text
        assert 'Historical context' in text

    def test_uses_title_only_when_no_summary(self):
        toc: list[dict[str, Any]] = [
            {'title': 'Results', 'level': 1, 'summary': None, 'children': []},
        ]
        text = _build_summary_text_from_page_index(toc)
        assert 'Results' in text

    def test_walks_children(self):
        toc: list[dict[str, Any]] = [
            {
                'title': 'Parent',
                'level': 1,
                'summary': None,
                'children': [
                    {
                        'title': 'Child',
                        'level': 2,
                        'summary': {'topic': 'Child topic', 'key_points': []},
                        'children': [],
                    }
                ],
            }
        ]
        text = _build_summary_text_from_page_index(toc)
        assert 'Child topic' in text

    def test_empty_toc_returns_empty_string(self):
        assert _build_summary_text_from_page_index([]) == ''


# ---------------------------------------------------------------------------
# extract_title_via_llm
# ---------------------------------------------------------------------------


class TestExtractTitleViaLlm:
    @pytest.mark.asyncio
    async def test_returns_title_on_success(self):
        mock_prediction = MagicMock()
        mock_prediction.title = '  The Document Title  '
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=(mock_prediction, MagicMock()),
        ):
            result = await extract_title_via_llm('Some content about a topic.', mock_lm)

        assert result == 'The Document Title'

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_title(self):
        mock_prediction = MagicMock()
        mock_prediction.title = ''
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=(mock_prediction, MagicMock()),
        ):
            result = await extract_title_via_llm('Some content.', mock_lm)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_whitespace_title(self):
        mock_prediction = MagicMock()
        mock_prediction.title = '   '
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=(mock_prediction, MagicMock()),
        ):
            result = await extract_title_via_llm('Some content.', mock_lm)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self):
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.run_dspy_operation',
            new_callable=AsyncMock,
            side_effect=RuntimeError('LLM unreachable'),
        ):
            result = await extract_title_via_llm('Some content.', mock_lm)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_text(self):
        mock_lm = MagicMock()
        result = await extract_title_via_llm('', mock_lm)
        assert result is None

    @pytest.mark.asyncio
    async def test_strips_surrounding_quotes_from_title(self):
        mock_prediction = MagicMock()
        mock_prediction.title = '"Quoted Title"'
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=(mock_prediction, MagicMock()),
        ):
            result = await extract_title_via_llm('Content.', mock_lm)

        assert result == 'Quoted Title'


# ---------------------------------------------------------------------------
# resolve_document_title  (simple path)
# ---------------------------------------------------------------------------


class TestResolveDocumentTitle:
    @pytest.mark.asyncio
    async def test_meaningful_name_used_directly(self):
        """A meaningful provided_name skips all extraction."""
        mock_lm = MagicMock()

        with (
            patch('memex_core.processing.titles.extract_title_from_markdown') as mock_md,
            patch(
                'memex_core.processing.titles.extract_title_via_llm',
                new_callable=AsyncMock,
            ) as mock_llm,
        ):
            result = await resolve_document_title(
                '# Some H1\nContent', 'My Meaningful Name', mock_lm
            )

        assert result == 'My Meaningful Name'
        mock_md.assert_not_called()
        mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_uuid_name_triggers_markdown_extraction(self):
        """UUID-format provided_name is not meaningful; H1 header should be used."""
        mock_lm = MagicMock()
        uuid_name = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'

        result = await resolve_document_title('# My Real Title\nContent here.', uuid_name, mock_lm)

        assert result == 'My Real Title'

    @pytest.mark.asyncio
    async def test_none_name_triggers_markdown_extraction(self):
        """None provided_name falls through to H1 header extraction."""
        mock_lm = MagicMock()

        result = await resolve_document_title('# Article Title\nBody.', None, mock_lm)

        assert result == 'Article Title'

    @pytest.mark.asyncio
    async def test_no_h1_triggers_llm(self):
        """When no H1 header exists, the LLM is called."""
        mock_lm = MagicMock()
        content = 'Plain text without any markdown headers.'

        with patch(
            'memex_core.processing.titles.extract_title_via_llm',
            new_callable=AsyncMock,
            return_value='LLM Extracted Title',
        ):
            result = await resolve_document_title(content, None, mock_lm)

        assert result == 'LLM Extracted Title'

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_provided_name(self):
        """When LLM returns None, use provided_name (even if not meaningful)."""
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.extract_title_via_llm',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await resolve_document_title('Plain text.', 'note', mock_lm)

        assert result == 'note'

    @pytest.mark.asyncio
    async def test_llm_failure_no_name_falls_back_to_untitled(self):
        """When LLM returns None and no name is given, fall back to 'Untitled'."""
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.extract_title_via_llm',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await resolve_document_title('Plain text.', None, mock_lm)

        assert result == 'Untitled'

    @pytest.mark.asyncio
    async def test_generic_name_and_no_h1_calls_llm(self):
        """Generic placeholder names trigger extraction even without an H1."""
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.extract_title_via_llm',
            new_callable=AsyncMock,
            return_value='Synthesized Title',
        ) as mock_llm:
            result = await resolve_document_title('Just some content.', 'untitled', mock_lm)

        assert result == 'Synthesized Title'
        mock_llm.assert_awaited_once()


# ---------------------------------------------------------------------------
# resolve_title_from_page_index  (page_index path)
# ---------------------------------------------------------------------------


class TestResolveTitleFromPageIndex:
    def _make_toc(self, title: str, level: int = 1) -> list[dict[str, Any]]:
        return [{'title': title, 'level': level, 'summary': None, 'children': []}]

    @pytest.mark.asyncio
    async def test_meaningful_name_wins_over_toc(self):
        """A meaningful provided_name is returned immediately without inspecting the TOC."""
        mock_lm = MagicMock()
        toc = self._make_toc('TOC Title')

        with patch('memex_core.processing.titles.extract_title_from_page_index_toc') as mock_toc:
            result = await resolve_title_from_page_index(toc, 'My Report Q4', mock_lm)

        assert result == 'My Report Q4'
        mock_toc.assert_not_called()

    @pytest.mark.asyncio
    async def test_level1_toc_node_used_when_name_generic(self):
        """When provided_name is generic, the first level-1 TOC title is used."""
        mock_lm = MagicMock()
        toc = self._make_toc('Architecture Overview', level=1)

        result = await resolve_title_from_page_index(toc, 'untitled', mock_lm)

        assert result == 'Architecture Overview'

    @pytest.mark.asyncio
    async def test_level1_toc_node_used_when_name_is_none(self):
        mock_lm = MagicMock()
        toc = self._make_toc('Deep Dive into Postgres', level=1)

        result = await resolve_title_from_page_index(toc, None, mock_lm)

        assert result == 'Deep Dive into Postgres'

    @pytest.mark.asyncio
    async def test_llm_called_when_no_level1_node(self):
        """No level-1 node → fall through to LLM with summaries."""
        mock_lm = MagicMock()
        toc: list[dict[str, Any]] = [
            {
                'title': 'Section',
                'level': 2,
                'summary': {'topic': 'Database internals', 'key_points': ['WAL', 'MVCC']},
                'children': [],
            }
        ]

        with patch(
            'memex_core.processing.titles.extract_title_via_llm',
            new_callable=AsyncMock,
            return_value='Database Internals Guide',
        ) as mock_llm:
            result = await resolve_title_from_page_index(toc, None, mock_lm)

        assert result == 'Database Internals Guide'
        mock_llm.assert_awaited_once()
        # The summary text is passed, not raw content
        call_args = mock_llm.call_args[0][0]
        assert 'Database internals' in call_args

    @pytest.mark.asyncio
    async def test_fallback_to_provided_name_when_llm_fails(self):
        mock_lm = MagicMock()
        toc: list[dict[str, Any]] = [{'title': 'Sub', 'level': 2, 'summary': None, 'children': []}]

        with patch(
            'memex_core.processing.titles.extract_title_via_llm',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await resolve_title_from_page_index(toc, 'note', mock_lm)

        assert result == 'note'

    @pytest.mark.asyncio
    async def test_fallback_to_untitled_when_everything_fails(self):
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.extract_title_via_llm',
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await resolve_title_from_page_index([], None, mock_lm)

        assert result == 'Untitled'

    @pytest.mark.asyncio
    async def test_llm_not_called_when_toc_empty_and_no_summaries(self):
        """Empty TOC produces empty summary text — LLM is still attempted but won't be called
        if the text is blank (extract_title_via_llm guards on empty input)."""
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.titles.extract_title_via_llm',
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_llm:
            result = await resolve_title_from_page_index([], None, mock_lm)

        assert result == 'Untitled'
        # LLM is not called because summary_text is empty
        mock_llm.assert_not_awaited()
