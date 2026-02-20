from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class ExtractedContent(BaseModel):
    """
    Unified model for extracted content from any source (web, file, etc.).
    """

    content: str = Field(..., description='The main text content, ideally in Markdown format.')
    source: str = Field(..., description='The original source identifier (URL or file path).')
    content_type: str = Field(..., description='The MIME type or category of the source.')
    metadata: dict[str, Any] = Field(
        default_factory=dict, description='Flexible metadata extracted from the source.'
    )
    images: dict[str, bytes] = Field(
        default_factory=dict, description='Extracted images: filename -> bytes.'
    )
    document_date: datetime | None = Field(
        default=None,
        description='Best-available source date for the document (e.g. publication date, file mtime).',
    )
