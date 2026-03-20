"""Regression tests to detect schema drift between Python models and TypeScript consumers.

These tests fail when the Python NoteSearchResult schema changes without updating
the downstream TypeScript types (dashboard generated.ts, openclaw types.ts).
"""

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


class TestDashboardSchemaSync:
    """Verify dashboard/src/api/generated.ts stays in sync with Python schemas."""

    generated_ts = PROJECT_ROOT / 'packages/dashboard/src/api/generated.ts'

    @pytest.fixture
    def generated_content(self) -> str:
        assert self.generated_ts.exists(), f'{self.generated_ts} not found'
        return self.generated_ts.read_text()

    def test_note_search_result_has_summaries_field(self, generated_content: str) -> None:
        """generated.ts NoteSearchResult must have 'summaries' (not 'summary')."""
        # Extract the NoteSearchResult z.object block
        match = re.search(
            r'export const NoteSearchResult = z\.object\(\{(.*?)\}\)',
            generated_content,
            re.DOTALL,
        )
        assert match, 'NoteSearchResult z.object not found in generated.ts'
        body = match.group(1)

        assert 'summaries' in body, (
            'generated.ts NoteSearchResult is missing "summaries" field. '
            'Regenerate types from the OpenAPI schema. '
            f'Current fields: {body[:200]}'
        )

    def test_note_search_result_no_old_summary_field(self, generated_content: str) -> None:
        """generated.ts NoteSearchResult must NOT have the old 'summary' singular field."""
        match = re.search(
            r'export const NoteSearchResult = z\.object\(\{(.*?)\}\)',
            generated_content,
            re.DOTALL,
        )
        assert match
        body = match.group(1)

        # "summary:" but not "summaries:" — match the old field
        has_old_summary = bool(re.search(r'\bsummary\s*:', body))
        has_summaries = 'summaries' in body

        if has_old_summary and not has_summaries:
            pytest.fail(
                'generated.ts NoteSearchResult still uses old "summary: SectionSummaryDTO" field. '
                'Regenerate types: the Python schema now uses "summaries: list[BlockSummaryDTO]".'
            )

    def test_block_summary_dto_exists(self, generated_content: str) -> None:
        """generated.ts must define BlockSummaryDTO with topic and key_points."""
        if 'BlockSummaryDTO' not in generated_content:
            pytest.fail(
                'generated.ts is missing BlockSummaryDTO. Regenerate types from the OpenAPI schema.'
            )


class TestOpenClawSchemaSync:
    """Verify openclaw/src/types.ts stays in sync with Python schemas."""

    types_ts = PROJECT_ROOT / 'packages/openclaw/src/types.ts'

    @pytest.fixture
    def types_content(self) -> str:
        assert self.types_ts.exists(), f'{self.types_ts} not found'
        return self.types_ts.read_text()

    def test_note_search_result_has_summaries(self, types_content: str) -> None:
        """openclaw types.ts NoteSearchResult must have 'summaries' (not 'summary')."""
        # Extract the NoteSearchResult interface block
        match = re.search(
            r'export interface NoteSearchResult \{(.*?)\}',
            types_content,
            re.DOTALL,
        )
        assert match, 'NoteSearchResult interface not found in types.ts'
        body = match.group(1)

        assert 'summaries' in body, (
            'openclaw types.ts NoteSearchResult is missing "summaries" field. '
            'Update the interface to match Python NoteSearchResult schema. '
            f'Current fields: {body.strip()}'
        )

    def test_note_search_result_no_old_summary(self, types_content: str) -> None:
        """openclaw types.ts NoteSearchResult must NOT have old 'summary' field."""
        match = re.search(
            r'export interface NoteSearchResult \{(.*?)\}',
            types_content,
            re.DOTALL,
        )
        assert match
        body = match.group(1)

        has_old_summary = bool(re.search(r'\bsummary\??\s*:', body))
        has_summaries = 'summaries' in body

        if has_old_summary and not has_summaries:
            pytest.fail(
                'openclaw types.ts NoteSearchResult still uses old "summary?: SectionSummaryDTO" field. '
                'Update to "summaries: BlockSummaryDTO[]" to match Python schema.'
            )
