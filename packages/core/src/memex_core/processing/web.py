"""
Web processing module using trafilatura and cloudscraper.
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import Any

import cloudscraper  # type: ignore
import requests  # type: ignore[import-untyped]
import trafilatura
from dateutil import parser as dateutil_parser

from memex_core.processing.models import ExtractedContent

logger = logging.getLogger('memex.core.processing.web')


class WebContentProcessor:
    """
    Fetches and extracts content from URLs using CloudScraper and Trafilatura.
    """

    @staticmethod
    async def fetch_and_extract(url: str) -> ExtractedContent:
        """
        Fetch URL and extract main content and metadata.

        Returns:
            ExtractedContent containing the text and metadata.
        """
        # Run synchronous code in a thread
        data = await asyncio.to_thread(WebContentProcessor._sync_process, url)

        # Parse document date from trafilatura metadata
        document_date = _parse_document_date(data.get('date'))

        # Create the ExtractedContent object
        return ExtractedContent(
            content=data.pop('text'),
            source=url,
            content_type='web',
            metadata=data,
            images={},  # Web extraction doesn't download images yet, just references
            document_date=document_date,
        )

    @staticmethod
    def _sync_process(url: str) -> dict[str, Any]:
        """Synchronous fetch and extract logic."""
        scraper = cloudscraper.create_scraper()
        try:
            response = scraper.get(url)
            response.raise_for_status()
            downloaded = response.text
        except requests.RequestException as e:
            raise ValueError(f'Failed to fetch content from {url}: {e}')

        # Extract main text
        result = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            include_images=True,
            include_formatting=True,
            no_fallback=False,
        )

        if not result:
            raise ValueError(f'Could not extract meaningful content from {url}')

        # Extract metadata
        metadata = trafilatura.bare_extraction(downloaded)
        if not metadata:
            # Fallback if bare_extraction fails but extract worked
            return {
                'text': result,
                'title': None,
                'date': None,
                'author': None,
                'url': url,
                'hostname': None,
            }

        # trafilatura >= 2.0.0 returns a Document object, not a dict
        def get_val(obj: Any, attr: str, default: Any = None) -> Any:
            if isinstance(obj, dict):
                return obj.get(attr, default)
            return getattr(obj, attr, default)

        return {
            'text': result,  # Use extracted text with formatting/images
            'title': get_val(metadata, 'title') or None,
            'date': get_val(metadata, 'date'),
            'author': get_val(metadata, 'author'),
            'url': get_val(metadata, 'url') or url,
            'hostname': get_val(metadata, 'hostname'),
        }


def _parse_document_date(date_str: str | None) -> datetime | None:
    """Parse a date string from trafilatura metadata into a timezone-aware datetime."""
    if not date_str:
        return None
    try:
        parsed = dateutil_parser.parse(date_str)
        # Ensure timezone-aware (assume UTC if naive)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, OverflowError):
        logger.warning(f'Could not parse document date: {date_str!r}')
        return None
