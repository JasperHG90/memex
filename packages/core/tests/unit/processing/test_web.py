import pytest
import requests  # type: ignore[import-untyped]
from unittest.mock import patch, MagicMock
from memex_core.processing.web import WebContentProcessor, _parse_document_date


@pytest.mark.asyncio
async def test_fetch_and_extract_success():
    url = 'https://example.com/article'

    # Mock cloudscraper
    with patch('memex_core.processing.web.cloudscraper.create_scraper') as mock_create_scraper:
        mock_scraper = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '<html><body>Content</body></html>'
        mock_response.raise_for_status = MagicMock()
        mock_scraper.get.return_value = mock_response
        mock_create_scraper.return_value = mock_scraper

        with (
            patch('memex_core.processing.web.trafilatura.extract') as mock_extract,
            patch('memex_core.processing.web.trafilatura.bare_extraction') as mock_bare,
        ):
            mock_extract.return_value = 'Main Text Content with **Formatting**'

            # bare_extraction returns metadata
            # We mock it returning a dict
            mock_bare.return_value = {
                'text': 'Plain text content',
                'title': 'Test Article',
                'date': '2023-01-01',
                'author': 'John Doe',
                'url': url,
                'hostname': 'example.com',
            }

            result = await WebContentProcessor.fetch_and_extract(url)

            # Assert content comes from extract() not bare_extraction()
            assert result.content == 'Main Text Content with **Formatting**'
            assert result.metadata['title'] == 'Test Article'
            assert result.metadata['author'] == 'John Doe'
            assert result.metadata['hostname'] == 'example.com'
            assert result.source == url
            assert result.content_type == 'web'
            # document_date should be parsed from trafilatura date
            assert result.document_date is not None
            assert result.document_date.year == 2023
            assert result.document_date.month == 1
            assert result.document_date.day == 1
            assert result.document_date.tzinfo is not None

            # Verify extract calls
            mock_extract.assert_called_once()
            args, kwargs = mock_extract.call_args
            assert args[0] == '<html><body>Content</body></html>'
            assert kwargs['include_images'] is True
            assert kwargs['include_formatting'] is True


@pytest.mark.asyncio
async def test_fetch_and_extract_failure_download():
    url = 'https://example.com/fail'

    with patch('memex_core.processing.web.cloudscraper.create_scraper') as mock_create_scraper:
        mock_scraper = MagicMock()
        # Mock get to raise exception
        mock_scraper.get.side_effect = requests.RequestException('Download error')
        mock_create_scraper.return_value = mock_scraper

        with pytest.raises(ValueError, match='Failed to fetch'):
            await WebContentProcessor.fetch_and_extract(url)


@pytest.mark.asyncio
async def test_fetch_and_extract_failure_extract():
    url = 'https://example.com/empty'

    with patch('memex_core.processing.web.cloudscraper.create_scraper') as mock_create_scraper:
        mock_scraper = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '<html></html>'
        mock_scraper.get.return_value = mock_response
        mock_create_scraper.return_value = mock_scraper

        with patch('memex_core.processing.web.trafilatura.extract') as mock_extract:
            mock_extract.return_value = None

            with pytest.raises(ValueError, match='Could not extract'):
                await WebContentProcessor.fetch_and_extract(url)


@pytest.mark.asyncio
async def test_fetch_and_extract_no_date():
    """When trafilatura returns no date, document_date should be None."""
    url = 'https://example.com/no-date'

    with patch('memex_core.processing.web.cloudscraper.create_scraper') as mock_create_scraper:
        mock_scraper = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '<html><body>Content</body></html>'
        mock_response.raise_for_status = MagicMock()
        mock_scraper.get.return_value = mock_response
        mock_create_scraper.return_value = mock_scraper

        with (
            patch('memex_core.processing.web.trafilatura.extract') as mock_extract,
            patch('memex_core.processing.web.trafilatura.bare_extraction') as mock_bare,
        ):
            mock_extract.return_value = 'Some content'
            mock_bare.return_value = {
                'text': 'Some content',
                'title': 'No Date Article',
                'date': None,
                'author': None,
                'url': url,
                'hostname': 'example.com',
            }

            result = await WebContentProcessor.fetch_and_extract(url)
            assert result.document_date is None


@pytest.mark.asyncio
async def test_no_title_when_bare_extraction_fails():
    """When bare_extraction returns None, title must be None (not 'Unknown Title').

    Regression: previously returned 'Unknown Title' which poisoned Priority 1
    of the title resolution pipeline and prevented H1/LLM fallbacks from running.
    """
    url = 'https://example.com/article'

    with patch('memex_core.processing.web.cloudscraper.create_scraper') as mock_create_scraper:
        mock_scraper = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '<html><body>Content</body></html>'
        mock_response.raise_for_status = MagicMock()
        mock_scraper.get.return_value = mock_response
        mock_create_scraper.return_value = mock_scraper

        with (
            patch('memex_core.processing.web.trafilatura.extract') as mock_extract,
            patch('memex_core.processing.web.trafilatura.bare_extraction') as mock_bare,
        ):
            mock_extract.return_value = 'Some content'
            mock_bare.return_value = None  # bare_extraction fails

            result = await WebContentProcessor.fetch_and_extract(url)
            assert result.metadata.get('title') is None


@pytest.mark.asyncio
async def test_no_title_when_metadata_title_missing():
    """When trafilatura metadata has no title, result title must be None."""
    url = 'https://example.com/no-title'

    with patch('memex_core.processing.web.cloudscraper.create_scraper') as mock_create_scraper:
        mock_scraper = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '<html><body>Content</body></html>'
        mock_response.raise_for_status = MagicMock()
        mock_scraper.get.return_value = mock_response
        mock_create_scraper.return_value = mock_scraper

        with (
            patch('memex_core.processing.web.trafilatura.extract') as mock_extract,
            patch('memex_core.processing.web.trafilatura.bare_extraction') as mock_bare,
        ):
            mock_extract.return_value = 'Some content'
            mock_bare.return_value = {'title': None, 'date': None, 'author': None}

            result = await WebContentProcessor.fetch_and_extract(url)
            assert result.metadata.get('title') is None


@pytest.mark.asyncio
async def test_no_title_when_metadata_title_empty():
    """When trafilatura metadata title is an empty string, result title must be None."""
    url = 'https://example.com/empty-title'

    with patch('memex_core.processing.web.cloudscraper.create_scraper') as mock_create_scraper:
        mock_scraper = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '<html><body>Content</body></html>'
        mock_response.raise_for_status = MagicMock()
        mock_scraper.get.return_value = mock_response
        mock_create_scraper.return_value = mock_scraper

        with (
            patch('memex_core.processing.web.trafilatura.extract') as mock_extract,
            patch('memex_core.processing.web.trafilatura.bare_extraction') as mock_bare,
        ):
            mock_extract.return_value = 'Some content'
            mock_bare.return_value = {'title': '', 'date': None, 'author': None}

            result = await WebContentProcessor.fetch_and_extract(url)
            assert result.metadata.get('title') is None


def test_parse_document_date_valid_iso():
    result = _parse_document_date('2023-06-15')
    assert result is not None
    assert result.year == 2023
    assert result.month == 6
    assert result.day == 15
    assert result.tzinfo is not None


def test_parse_document_date_valid_natural():
    result = _parse_document_date('January 5, 2024')
    assert result is not None
    assert result.year == 2024
    assert result.month == 1
    assert result.day == 5


def test_parse_document_date_none():
    assert _parse_document_date(None) is None


def test_parse_document_date_empty():
    assert _parse_document_date('') is None


def test_parse_document_date_invalid():
    assert _parse_document_date('not a date at all') is None
