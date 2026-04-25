"""
File processing module using markitdown and pymupdf4llm.
"""

import logging
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import fitz  # type: ignore
import pymupdf4llm  # type: ignore
from markitdown import MarkItDown
from memex_core.processing.models import ExtractedContent

logger = logging.getLogger('memex.core.processing.files')


class FileContentProcessor:
    """
    Extracts content from various file formats using Microsoft MarkItDown and PyMuPDF4LLM.
    Supports PDF, DOCX, XLSX, PPTX, Images, CSV, JSON, XML, etc.
    """

    def __init__(self) -> None:
        self._md = MarkItDown()

    async def extract(self, file_path: Path | str) -> ExtractedContent:
        """
        Extract content and metadata from a file.

        Args:
            file_path: Path to the file.

        Returns:
            ExtractedContent containing the markdown text and metadata.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f'File not found: {path}')

        if path.suffix.lower() == '.pdf':
            # exempt: request-bounded sync PDF extract, no model load.
            return await asyncio.to_thread(self._sync_extract_pdf, path)
        # exempt: request-bounded sync markitdown extract, no model load.
        return await asyncio.to_thread(self._sync_extract_markitdown, path)

    def _sync_extract_pdf(self, path: Path) -> ExtractedContent:
        """Synchronous PDF extraction logic using pymupdf4llm."""
        try:
            with TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                # pymupdf4llm returns markdown string
                md_text = pymupdf4llm.to_markdown(
                    str(path),
                    force_text=True,
                    write_images=True,
                    image_path=str(temp_path),
                )

                images: dict[str, bytes] = {}
                for image_file in temp_path.glob('*'):
                    if image_file.is_file():
                        images[image_file.name] = image_file.read_bytes()

                metadata: dict[str, Any] = {}
                try:
                    with fitz.open(str(path)) as doc:
                        pdf_meta = doc.metadata or {}
                    if pdf_meta.get('title'):
                        metadata['title'] = pdf_meta['title']
                    if pdf_meta.get('author'):
                        metadata['author'] = pdf_meta['author']
                    creation = _parse_pdf_date(pdf_meta.get('creationDate'))
                    if creation:
                        metadata['creation_date'] = creation
                except Exception:
                    logger.debug('Could not read PDF metadata for %s', path)

                file_mtime = _file_mtime_utc(path)
                if file_mtime:
                    metadata['file_mtime'] = file_mtime

                return ExtractedContent(
                    content=md_text,
                    source=str(path),
                    content_type='pdf',
                    metadata=metadata,
                    images=images,
                    document_date=None,
                )
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f'Failed to extract content from PDF {path}: {e}')
            raise ValueError(f'PDF extraction failed for {path}: {e}') from e

    def _sync_extract_markitdown(self, path: Path) -> ExtractedContent:
        """Synchronous extraction logic using MarkItDown."""
        try:
            # markitdown.convert returns a Document object
            result = self._md.convert(str(path))

            # Basic metadata extraction from the result object
            # MarkItDown's Document object has 'text_content'
            # It may have other attributes depending on the version/type
            metadata: dict[str, Any] = {}

            # Attempt to gather common metadata if available in future versions
            # For now, we rely on the conversion result

            return ExtractedContent(
                content=result.text_content,
                source=str(path),
                content_type=path.suffix.lower().lstrip('.'),
                metadata=metadata,
                document_date=_file_mtime_utc(path),
            )
        except (OSError, RuntimeError, ValueError, TypeError) as e:
            logger.error(f'Failed to extract content from {path}: {e}')
            raise ValueError(f'Extraction failed for {path}: {e}') from e


def _parse_pdf_date(raw: str | None) -> datetime | None:
    """Parse a PDF date string (e.g. ``D:20260310064822Z00'00'``) into a UTC datetime.

    PDF date format: ``D:YYYYMMDDHHmmSSOHH'mm'`` where O is +, -, or Z.
    The timezone offset is parsed and applied to convert the result to UTC.
    """
    if not raw:
        return None
    s = raw[2:] if raw.startswith('D:') else raw
    try:
        dt = datetime.strptime(s[:14], '%Y%m%d%H%M%S')
    except (ValueError, IndexError):
        return None

    # Parse timezone offset from the remainder (e.g. "Z00'00'", "+05'30'", "-08'00'")
    tz_part = s[14:]
    if tz_part:
        m = re.match(r"([Z+\-])(\d{2})'(\d{2})'", tz_part)
        if m:
            sign_char, hours_s, mins_s = m.group(1), m.group(2), m.group(3)
            offset_minutes = int(hours_s) * 60 + int(mins_s)
            if sign_char == '-':
                offset_minutes = -offset_minutes
            tz = timezone(timedelta(minutes=offset_minutes))
            dt = dt.replace(tzinfo=tz)
            return dt.astimezone(timezone.utc)

    # No timezone suffix or unrecognised format — assume UTC
    return dt.replace(tzinfo=timezone.utc)


def _file_mtime_utc(path: Path) -> datetime | None:
    """Extract the file modification time as a timezone-aware UTC datetime."""
    try:
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except OSError:
        logger.warning(f'Could not read mtime for {path}')
        return None
