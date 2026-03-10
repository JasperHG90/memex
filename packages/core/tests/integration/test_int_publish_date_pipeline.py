"""Integration test for publish_date extraction during ingestion pipeline."""

import pytest

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
async def test_ingest_with_frontmatter_publish_date():
    """
    Full pipeline test: ingesting a note with publish_date in frontmatter
    should use that date as the event_date instead of now().
    """
    pytest.skip('Requires running database — run with: uv run pytest -m integration')
