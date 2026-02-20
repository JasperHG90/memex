"""
File processing module using markitdown and pymupdf4llm.
"""

import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

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
            return await asyncio.to_thread(self._sync_extract_pdf, path)
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

                return ExtractedContent(
                    content=md_text,
                    source=str(path),
                    content_type='pdf',
                    metadata={},
                    images=images,
                    document_date=_file_mtime_utc(path),
                )
        except Exception as e:
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
        except Exception as e:
            logger.error(f'Failed to extract content from {path}: {e}')
            raise ValueError(f'Extraction failed for {path}: {e}') from e


def _file_mtime_utc(path: Path) -> datetime | None:
    """Extract the file modification time as a timezone-aware UTC datetime."""
    try:
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except OSError:
        logger.warning(f'Could not read mtime for {path}')
        return None
