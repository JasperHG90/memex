"""DSPy signatures for PageIndex document indexing.

Separates schema definitions from indexing logic for clarity.
"""

import dspy

from memex_core.memory.extraction.models import (
    BlockSummary,
    DetectedHeader,
    SectionSummary,
    TOCNode,
)


class ScanChunk(dspy.Signature):
    """Scan a text chunk and detect section headers."""

    previous_context: str = dspy.InputField(
        description='The last 200 characters of the previous chunk.'
    )
    chunk_text: str = dspy.InputField(description='The current segment of the document to analyze.')
    detected_headers: list[DetectedHeader] = dspy.OutputField(
        description='A list of all section headers found.'
    )


class OrganizeStructure(dspy.Signature):
    """Organize flat headers into a hierarchical TOC tree."""

    flat_headers_json: str = dspy.InputField(description='JSON list of headers.')
    toc_tree: list[TOCNode] = dspy.OutputField(description='The hierarchical tree structure.')


class SummarizeSection(dspy.Signature):
    """Summarize a leaf section using its title and content."""

    title: str = dspy.InputField(description='The title of the section.')
    content: str = dspy.InputField(description='The text content of the section.')
    summary: SectionSummary = dspy.OutputField(
        description='Structured summary (Who, What, How, When, Where). '
        'Total response size: Max 150 tokens.'
    )


class SummarizeParentSection(dspy.Signature):
    """Summarize a parent section based ONLY on its direct content and its children's titles.

    Do NOT invent details not present in the provided content.
    """

    title: str = dspy.InputField(description='The title of the parent section.')
    content: str = dspy.InputField(
        description='The direct text content of the parent section (may be short).'
    )
    children_titles: str = dspy.InputField(
        description='Comma-separated list of child section titles for context.'
    )
    summary: SectionSummary = dspy.OutputField(
        description='Structured summary (Who, What, How, When, Where). '
        'Total response size: Max 150 tokens.'
    )


class SummarizeBlock(dspy.Signature):
    """Synthesize a block-level summary from individual section summaries.

    Do NOT read or reference the raw content — work only from the provided summaries.
    """

    section_summaries: str = dspy.InputField(
        description='Newline-separated list of section titles and their formatted summaries.'
    )
    block_summary: BlockSummary = dspy.OutputField(
        description='A synthesized summary. key_points must be complete sentences '
        'without trailing periods or semicolons. '
        'Use pipe (|) as separator in formatted output. Max 200 tokens.'
    )
